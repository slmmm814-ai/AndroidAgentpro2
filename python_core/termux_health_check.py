#!/usr/bin/env python3
"""فحص أسباب تجميد Termux وخروج الجلسات على جهاز أندرويد.

يفترض وجود adb (عبر USB/Wi‑Fi) وأن الجهاز مفعّل عليه تصحيح USB.
يعمل فقط للقراءة/الفحص، ولا يُعدّل الجهاز — الخطوات الإصلاحية تُطبع
كأوامر جاهزة ليراجعها المالك وينفذها بنفسه.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

TERMUX_PACKAGE = "com.termux"


@dataclass
class CheckResult:
    name: str
    status: str  # OK | WARN | FAIL | UNKNOWN
    detail: str
    fix: str = ""


def adb(args: list[str]) -> tuple[int, str, str]:
    cmd = ["adb"] + args
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    return proc.returncode, out.strip(), err.strip()


def adb_shell(prop: str) -> str:
    code, out, _ = adb(["shell", "getprop", prop])
    return out.strip() if code == 0 else ""


def adb_shell_setting(namespace: str, key: str) -> str:
    code, out, _ = adb(["shell", "settings", "get", namespace, key])
    return out.strip() if code == 0 else ""


def adb_shell_cmd(cmd: str) -> tuple[int, str]:
    code, out, err = adb(["shell", cmd])
    if code != 0:
        return code, err or out
    return 0, out.strip()


def have_adb() -> bool:
    return shutil.which("adb") is not None


def device_connected() -> bool:
    code, out, _ = adb(["get-state"])
    return code == 0 and out.strip() == "device"


def check_adb() -> list[CheckResult]:
    results: list[CheckResult] = []

    if not have_adb():
        results.append(
            CheckResult(
                "adb متوفر",
                "FAIL",
                "adb غير موجود في PATH. ثبّته: "
                "apt install adb / pkg install adb (Termux).",
                "pkg install adb  # أو apt install adb",
            )
        )
        return results

    if not device_connected():
        results.append(
            CheckResult(
                "اتصال الجهاز",
                "FAIL",
                "لا يوجد جهاز متصل. فعّل خيارات المطوّر + تصحيح USB،"
                " ثم صِل الجهاز واقبل بصمة الحاسوب على شاشة الهاتف.",
                "adb devices  # يجب أن تظهر الحالة device",
            )
        )
        return results

    results.append(CheckResult("adb متوفر", "OK", "adb موجود في PATH"))
    results.append(CheckResult("اتصال الجهاز", "OK", "الجهاز متصل"))

    version = adb_shell("ro.build.version.release")
    results.append(
        CheckResult(
            "إصدار أندرويد",
            "OK" if version else "UNKNOWN",
            f"Android {version}" if version else "تعذّر قراءة الإصدار",
        )
    )
    return results


def check_phantom_processes() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not device_connected():
        return []

    if not any(r.name == "اتصال الجهاز" and r.status == "OK" for r in check_adb()):
        return []

    code, out = adb_shell_cmd(
        "device_config get activity_manager max_phantom_processes"
    )
    max_phantom = out.strip() if code == 0 else ""
    if code != 0 or not max_phantom:
        results.append(
            CheckResult(
                "Phantom Process Killer",
                "UNKNOWN",
                "تعذّر قراءة max_phantom_processes (مطلوب adb shell).",
            )
        )
    elif max_phantom == "2147483647":
        results.append(
            CheckResult(
                "Phantom Process Killer",
                "OK",
                "max_phantom_processes = 2147483647 (القتل معطّل).",
            )
        )
    else:
        results.append(
            CheckResult(
                "Phantom Process Killer",
                "FAIL",
                f"max_phantom_processes = {max_phantom} — "
                "النظام سيقتل عمليات Termux الخلفية الثقيلة بعد دقائق "
                "(السبب الأول للتجميد على Android 12+).",
                "adb shell device_config put activity_manager "
                "max_phantom_processes 2147483647",
            )
        )

    code, out = adb_shell_cmd(
        "settings get global settings_enable_monitor_phantom_procs"
    )
    monitor = out.strip() if code == 0 else ""
    if code == 0 and monitor == "0":
        results.append(
            CheckResult(
                "مراقبة Phantom Processes",
                "OK",
                "settings_enable_monitor_phantom_procs = 0 (معطّلة).",
            )
        )
    elif code == 0 and monitor == "1":
        results.append(
            CheckResult(
                "مراقبة Phantom Processes",
                "FAIL",
                "settings_enable_monitor_phantom_procs = 1 (مفعّلة).",
                "adb shell settings put global "
                "settings_enable_monitor_phantom_procs 0",
            )
        )
    else:
        results.append(
            CheckResult(
                "مراقبة Phantom Processes",
                "UNKNOWN",
                "تعذّر قراءة settings_enable_monitor_phantom_procs.",
            )
        )
    return results


def check_battery_optimization() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not device_connected():
        return []

    code, out = adb_shell_cmd("dumpsys deviceidle whitelist")
    if code != 0:
        results.append(
            CheckResult(
                "إعفاء البطارية",
                "UNKNOWN",
                "تعذّر قراءة deviceidle whitelist.",
            )
        )
        return results

    whitelisted = TERMUX_PACKAGE in out
    if whitelisted:
        results.append(
            CheckResult(
                "إعفاء البطارية",
                "OK",
                f"الحزمة {TERMUX_PACKAGE} موجودة في deviceidle whitelist.",
            )
        )
    else:
        results.append(
            CheckResult(
                "إعفاء البطارية",
                "FAIL",
                f"الحزمة {TERMUX_PACKAGE} ليست مُعفاة من تحسين البطارية —"
                " النظام يُجمّد التطبيق في الخلفية.",
                f"adb shell dumpsys deviceidle whitelist +{TERMUX_PACKAGE}",
            )
        )
    return results


def check_termux_wakelock() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not device_connected():
        return []

    code, out = adb_shell_cmd("dumpsys power | grep -i 'Wake Lock'")
    if code != 0 or not out:
        results.append(
            CheckResult(
                "Termux Wake Lock",
                "UNKNOWN",
                "تعذّر قراءة حالة wake locks.",
            )
        )
        return results

    have_wl = "Termux" in out
    if have_wl:
        results.append(
            CheckResult(
                "Termux Wake Lock",
                "OK",
                "wakelock نشط لـTermux — لن يدخل الجهاز في السكون وينقطع.",
            )
        )
    else:
        results.append(
            CheckResult(
                "Termux Wake Lock",
                "FAIL",
                "لا يوجد wakelock نشط — الجهاز يدخل Doze وينقطع عن الجلسة.",
                "termux-wake-lock",
            )
        )
    return results


def check_memory() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not device_connected():
        return []

    code, out = adb_shell_cmd("cat /proc/meminfo | head -n 3")
    if code != 0 or not out:
        results.append(
            CheckResult("الذاكرة", "UNKNOWN", "تعذّر قراءة /proc/meminfo."))
        return results

    total_kb = avail_kb = 0
    for line in out.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])

    total_mb = total_kb / 1024.0
    avail_mb = avail_kb / 1024.0
    free_pct = (avail_kb / total_kb * 100.0) if total_kb else 0.0

    if free_pct < 10.0:
        status, note = (
            "FAIL",
            "ذاكرة حرجة — OOM killer قد يقتل العمليات فورًا.",
        )
    elif free_pct < 20.0:
        status, note = "WARN", "ذاكرة منخفضة — راقب استخدامك."
    else:
        status, note = "OK", "ذاكرة كافية."

    results.append(
        CheckResult(
            "الذاكرة",
            status,
            f"{avail_mb:.0f} MB متاح من {total_mb:.0f} MB "
            f"({free_pct:.0f}% حر) — {note}",
        )
    )

    # PSI أو oom_score للعمليات الثقيلة
    code, out = adb_shell_cmd("cat /proc/pressure/memory 2>/dev/null")
    if code == 0 and out:
        some_avg = ""
        for line in out.splitlines():
            if line.startswith("some avg10="):
                some_avg = line.split("avg10=")[1].split()[0]
        if some_avg:
            try:
                pressure = float(some_avg) * 100.0
            except ValueError:
                pressure = -1.0
            if pressure >= 50.0:
                results.append(
                    CheckResult(
                        "ضغط الذاكرة (PSI)",
                        "FAIL",
                        f"memory PSI some avg10 = {pressure:.0f}% — "
                        "النظام تحت ضغط ذاكرة شديد ويقتل العمليات.",
                    )
                )
            elif pressure >= 20.0:
                results.append(
                    CheckResult(
                        "ضغط الذاكرة (PSI)",
                        "WARN",
                        f"memory PSI some avg10 = {pressure:.0f}% — "
                        "ضغط متوسط على الذاكرة.",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "ضغط الذاكرة (PSI)",
                        "OK",
                        f"memory PSI some avg10 = {pressure:.0f}% — لا ضغط.",
                    )
                )
    return results


def check_termux_alive() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not device_connected():
        return []

    code, out = adb_shell_cmd(
        f"pidof {TERMUX_PACKAGE} || pidof com.termux:app 2>/dev/null"
    )
    if code == 0 and out:
        results.append(
            CheckResult(
                "عملية Termux",
                "OK",
                f"Termux يعمل الآن (pid={out.split()[0]}).",
            )
        )
    else:
        results.append(
            CheckResult(
                "عملية Termux",
                "WARN",
                "لا توجد عملية Termux نشطة الآن "
                "(طبيعي إن لم يكن مفتوحًا).",
            )
        )

    code, out = adb_shell_cmd(
        "dumpsys accessibility 2>/dev/null | "
        "grep -i 'com.termux' | head -n 1"
    )
    if code == 0 and out:
        results.append(
            CheckResult(
                "خدمة الوصول (Termux:API)",
                "OK",
                "خدمة وصول Termux:API نشطة.",
            )
        )
    else:
        results.append(
            CheckResult(
                "خدمة الوصول (Termux:API)",
                "WARN",
                "خدمة وصول Termux:API غير نشطة — "
                "بعض الأوامر (مثل termux-wake-lock) قد لا تعمل.",
                "فعّل Termux:API في الإعدادات ← الوصول.",
            )
        )
    return results


def render(results: list[CheckResult]) -> int:
    print("=" * 72)
    print("فحص أسباب تجميد Termux وخروج الجلسات")
    print("=" * 72)
    print()

    failures = 0
    warns = 0
    unknowns = 0

    for r in results:
        symbol = {
            "OK": "OK  ",
            "WARN": "WARN",
            "FAIL": "FAIL",
            "UNKNOWN": "????",
        }.get(r.status, "????")

        print(f"[{symbol}] {r.name}")
        print(f"       {r.detail}")
        if r.fix:
            print(f"       الإصلاح: {r.fix}")
        print()

        if r.status == "FAIL":
            failures += 1
        elif r.status == "WARN":
            warns += 1
        elif r.status == "UNKNOWN":
            unknowns += 1

    print("=" * 72)
    print("الملخص")
    print("=" * 72)
    print(f"OK      : {sum(1 for r in results if r.status == 'OK')}")
    print(f"WARN    : {warns}")
    print(f"FAIL    : {failures}")
    print(f"UNKNOWN : {unknowns}")
    print()

    if failures:
        print(f"يوجد {failures} مشكلة تستحق الإصلاح — ابدأ بالخطوات أعلاه.")
    elif warns:
        print(f"يوجد {warns} تنبيه — ليس بالضرورة خطأ.")
    else:
        print("لا توجد مشاكل واضحة في الإعدادات الحالية.")
    print()

    return 1 if failures else 0


def main() -> int:
    checks = [
        check_adb,
        check_phantom_processes,
        check_battery_optimization,
        check_termux_wakelock,
        check_memory,
        check_termux_alive,
    ]

    results: list[CheckResult] = []
    for check in checks:
        try:
            results.extend(check())
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(
                    check.__name__,
                    "UNKNOWN",
                    f"تعذّر تنفيذ الفحص: {type(exc).__name__}: {exc}",
                )
            )

        # إذا فشل adb/الاتصال، لا حاجة لباقي الفحوصات
        if check is check_adb and not any(
            r.name == "اتصال الجهاز" and r.status == "OK" for r in results
        ):
            break

    return render(results)


if __name__ == "__main__":
    raise SystemExit(main())
