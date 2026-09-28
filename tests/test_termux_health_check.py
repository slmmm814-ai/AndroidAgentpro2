from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python_core"))

import termux_health_check as thc  # noqa: E402


class FakeAdb:
    """يسجّل أوامر adb ويعيد ردود مُعدّة مسبقًا."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, tuple[int, str]] = {}
        self.no_device = False

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        if args and args[0] == "get-state":
            if self.no_device:
                return 1, "", "no devices"
            return 0, "device", ""
        key = " ".join(args)
        if key in self.responses:
            code, out = self.responses[key]
            return code, out, "" if code == 0 else "err"
        return 0, "", ""


class Patched:
    """تستبدل دوال adb داخل الوحدة ثم تعيدها."""

    def __init__(self, fake: FakeAdb, device: bool | None = None) -> None:
        self.fake = fake
        self.device = device
        self.orig: dict[str, object] = {}

    def __enter__(self) -> "Patched":
        self.orig = {
            "adb": thc.adb,
            "have_adb": thc.have_adb,
            "device_connected": thc.device_connected,
        }
        thc.adb = self.fake  # type: ignore[assignment]
        thc.have_adb = lambda: True  # type: ignore[assignment]
        if self.device is not None:
            thc.device_connected = lambda: self.device  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        thc.adb = self.orig["adb"]  # type: ignore[assignment]
        thc.have_adb = self.orig["have_adb"]  # type: ignore[assignment]
        thc.device_connected = self.orig["device_connected"]  # type: ignore[assignment]


class CheckAdbTests(unittest.TestCase):
    def test_no_adb_binary(self) -> None:
        orig = thc.have_adb
        thc.have_adb = lambda: False  # type: ignore[assignment]
        try:
            results = thc.check_adb()
        finally:
            thc.have_adb = orig  # type: ignore[assignment]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("adb", results[0].detail)

    def test_no_device(self) -> None:
        fake = FakeAdb()
        fake.no_device = True
        with Patched(fake):
            results = thc.check_adb()
            self.assertTrue(any(r.status == "FAIL" for r in results))
            self.assertEqual(fake.calls[0][:2], ["get-state"])

    def test_connected(self) -> None:
        fake = FakeAdb()
        fake.responses["shell getprop ro.build.version.release"] = (
            0,
            "14",
        )
        with Patched(fake):
            results = thc.check_adb()

        self.assertTrue(all(r.status == "OK" for r in results))
        self.assertTrue(any("Android 14" in r.detail for r in results))


class PhantomProcessesTests(unittest.TestCase):
    def test_phantom_enabled_is_fail(self) -> None:
        fake = FakeAdb()
        fake.responses[
            "shell device_config get activity_manager max_phantom_processes"
        ] = (0, "20")
        with Patched(fake):
            results = thc.check_phantom_processes()

        phantom = [r for r in results if "Phantom" in r.name]
        self.assertTrue(phantom)
        self.assertEqual(phantom[0].status, "FAIL")
        self.assertIn("max_phantom_processes", phantom[0].fix)

    def test_phantom_disabled_is_ok(self) -> None:
        fake = FakeAdb()
        fake.responses[
            "shell device_config get activity_manager max_phantom_processes"
        ] = (0, "2147483647")
        fake.responses[
            "shell settings get global settings_enable_monitor_phantom_procs"
        ] = (0, "0")
        with Patched(fake):
            results = thc.check_phantom_processes()

        self.assertTrue(all(r.status == "OK" for r in results))

    def test_monitor_enabled_is_fail(self) -> None:
        fake = FakeAdb()
        fake.responses[
            "shell device_config get activity_manager max_phantom_processes"
        ] = (0, "2147483647")
        fake.responses[
            "shell settings get global settings_enable_monitor_phantom_procs"
        ] = (0, "1")
        with Patched(fake):
            results = thc.check_phantom_processes()

        monitor = [r for r in results if "مراقبة" in r.name]
        self.assertEqual(monitor[0].status, "FAIL")


class BatteryTests(unittest.TestCase):
    def test_not_whitelisted_is_fail(self) -> None:
        fake = FakeAdb()
        fake.responses["shell dumpsys deviceidle whitelist"] = (
            0,
            "system, com.android.phone",
        )
        with Patched(fake):
            results = thc.check_battery_optimization()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("com.termux", results[0].fix)

    def test_whitelisted_is_ok(self) -> None:
        fake = FakeAdb()
        fake.responses["shell dumpsys deviceidle whitelist"] = (
            0,
            "system, com.termux",
        )
        with Patched(fake):
            results = thc.check_battery_optimization()

        self.assertEqual(results[0].status, "OK")


class MemoryTests(unittest.TestCase):
    def test_low_memory_is_fail(self) -> None:
        fake = FakeAdb()
        fake.responses["shell cat /proc/meminfo | head -n 3"] = (
            0,
            "MemTotal: 2048000 kB\nMemFree: 100000 kB\n"
            "MemAvailable: 100000 kB\n",
        )
        with Patched(fake):
            results = thc.check_memory()

        mem = [r for r in results if r.name == "الذاكرة"]
        self.assertEqual(mem[0].status, "FAIL")

    def test_healthy_memory_is_ok(self) -> None:
        fake = FakeAdb()
        fake.responses["shell cat /proc/meminfo | head -n 3"] = (
            0,
            "MemTotal: 4096000 kB\nMemFree: 2000000 kB\n"
            "MemAvailable: 3000000 kB\n",
        )
        with Patched(fake):
            results = thc.check_memory()

        mem = [r for r in results if r.name == "الذاكرة"]
        self.assertEqual(mem[0].status, "OK")

    def test_psi_pressure_fail(self) -> None:
        fake = FakeAdb()
        fake.responses["shell cat /proc/meminfo | head -n 3"] = (
            0,
            "MemTotal: 4096000 kB\nMemFree: 2000000 kB\n"
            "MemAvailable: 3000000 kB\n",
        )
        fake.responses["shell cat /proc/pressure/memory 2>/dev/null"] = (
            0,
            "some avg10=0.60 avg60=0.40 avg300=0.30 total=0\n",
        )
        with Patched(fake):
            results = thc.check_memory()

        psi = [r for r in results if "PSI" in r.name]
        self.assertTrue(psi)
        self.assertEqual(psi[0].status, "FAIL")


class WakeLockTests(unittest.TestCase):
    def test_no_wakelock_is_fail(self) -> None:
        fake = FakeAdb()
        fake.responses["shell dumpsys power | grep -i 'Wake Lock'"] = (
            0,
            "no wake locks",
        )
        with Patched(fake):
            results = thc.check_termux_wakelock()

        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("termux-wake-lock", results[0].fix)

    def test_wakelock_held_is_ok(self) -> None:
        fake = FakeAdb()
        fake.responses["shell dumpsys power | grep -i 'Wake Lock'"] = (
            0,
            "- PARTIAL_WAKE_LOCK Termux:TermuxWakeLock",
        )
        with Patched(fake):
            results = thc.check_termux_wakelock()

        self.assertEqual(results[0].status, "OK")


class RenderTests(unittest.TestCase):
    def test_render_counts_fail(self) -> None:
        results = [
            thc.CheckResult("a", "OK", "x"),
            thc.CheckResult("b", "WARN", "y"),
            thc.CheckResult("c", "FAIL", "z", "fix"),
            thc.CheckResult("d", "UNKNOWN", "w"),
        ]
        code = thc.render(results)
        self.assertEqual(code, 1)

    def test_render_all_ok(self) -> None:
        code = thc.render([thc.CheckResult("a", "OK", "x")])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
