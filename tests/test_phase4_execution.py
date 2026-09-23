from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentpro.safe_executor import (
    CommandNotAllowedError,
    PathIsolationError,
    SafeExecutionEnvironment,
)


class Phase4PathIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "sandbox"
        self.environment = SafeExecutionEnvironment(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_relative_path_stays_inside_sandbox(self) -> None:
        resolved = self.environment.resolve_path("scripts/test.py")

        self.assertTrue(resolved.is_relative_to(self.root))

    def test_absolute_path_is_rejected(self) -> None:
        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("/tmp/escape.py")

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("../escape.py")

    def test_nested_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("a/b/../../escape.py")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()

        link = self.root / "escape"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("escape/file.txt")

    def test_atomic_write_creates_file_inside_sandbox(self) -> None:
        path = self.environment.write_text_atomic(
            "scripts/example.py",
            "print('phase4')\n",
        )

        self.assertTrue(path.exists())
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "print('phase4')\n")
        self.assertTrue(path.is_relative_to(self.root))


class Phase4AllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "sandbox"
        self.environment = SafeExecutionEnvironment(self.root)
        self.environment.write_text_atomic(
            "scripts/ok.py",
            "print('allowed')\n",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_python_is_allowlisted(self) -> None:
        result = self.environment.run_shell(
            ("python", "scripts/ok.py")
        )

        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "allowed")
        self.assertTrue(result.memory_limit_applied)
        self.assertTrue(result.cpu_limit_applied)

    def test_python3_is_allowlisted(self) -> None:
        result = self.environment.run_shell(
            ("python3", "scripts/ok.py")
        )

        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)

    def test_shell_commands_are_rejected(self) -> None:
        for executable in ("sh", "bash", "zsh", "rm", "chmod", "curl"):
            with self.subTest(executable=executable):
                with self.assertRaises(CommandNotAllowedError):
                    self.environment.run_shell(
                        (executable, "scripts/ok.py")
                    )

    def test_shell_compound_command_is_rejected(self) -> None:
        with self.assertRaises(CommandNotAllowedError):
            self.environment.run_shell(
                ("python", "scripts/ok.py", "&&", "rm", "-rf", "/")
            )

    def test_shell_metacharacters_are_not_interpreted(self) -> None:
        with self.assertRaises(CommandNotAllowedError):
            self.environment.run_shell(
                ("python", "scripts/ok.py; rm -rf /")
            )

    def test_absolute_script_path_is_rejected(self) -> None:
        absolute_script = str(
            (self.root / "scripts" / "ok.py").resolve()
        )

        with self.assertRaises(PathIsolationError):
            self.environment.run_shell(
                ("python", absolute_script)
            )

    def test_script_outside_sandbox_is_rejected(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.py"
        outside.write_text(
            "print('outside')\n",
            encoding="utf-8",
        )

        with self.assertRaises(PathIsolationError):
            self.environment.run_shell(
                ("python", "../outside.py")
            )



class Phase4RepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "sandbox"
        self.environment = SafeExecutionEnvironment(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_syntax_error_is_detected_and_repaired(self) -> None:
        repairs = []

        def repair(result):
            repairs.append(result)
            return "print('syntax repaired')\n"

        result = self.environment.write_execute_repair(
            "repair/syntax.py",
            "print('broken'\n",
            repair,
            max_repairs=1,
        )

        self.assertFalse(repairs[0].success)
        self.assertIsNotNone(repairs[0].returncode)
        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "syntax repaired")

    def test_runtime_failure_is_detected_and_repaired(self) -> None:
        repairs = []

        def repair(result):
            repairs.append(result)
            return "raise SystemExit(0)\n"

        result = self.environment.write_execute_repair(
            "repair/runtime.py",
            "raise RuntimeError('intentional failure')\n",
            repair,
            max_repairs=1,
        )

        self.assertFalse(repairs[0].success)
        self.assertEqual(repairs[0].returncode, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)

    def test_sandbox_file_error_is_detected_and_repaired(self) -> None:
        repairs = []

        self.environment.write_text_atomic(
            "repair/data.txt",
            "phase4-data",
        )

        def repair(result):
            repairs.append(result)
            return (
                "from pathlib import Path\n"
                "value = Path('repair/data.txt').read_text(encoding='utf-8')\n"
                "print(value)\n"
            )

        result = self.environment.write_execute_repair(
            "repair/file.py",
            (
                "from pathlib import Path\n"
                "value = Path('repair/missing.txt').read_text(encoding='utf-8')\n"
                "print(value)\n"
            ),
            repair,
            max_repairs=1,
        )

        self.assertFalse(repairs[0].success)
        self.assertEqual(repairs[0].returncode, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "phase4-data")



class Phase4ResourceLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "sandbox"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_timeout_terminates_long_running_script(self) -> None:
        environment = SafeExecutionEnvironment(
            self.root,
            timeout_seconds=0.5,
            memory_limit_mb=512,
            cpu_limit_seconds=5,
        )

        environment.write_text_atomic(
            "limits/timeout.py",
            (
                "import time\n"
                "time.sleep(30)\n"
            ),
        )

        result = environment.execute_script("limits/timeout.py")

        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertTrue(result.memory_limit_applied)
        self.assertTrue(result.cpu_limit_applied)

    def test_cpu_limit_is_configured_for_each_execution(self) -> None:
        environment = SafeExecutionEnvironment(
            self.root,
            timeout_seconds=3.0,
            memory_limit_mb=512,
            cpu_limit_seconds=1,
        )

        environment.write_text_atomic(
            "limits/cpu.py",
            (
                "while True:\n"
                "    pass\n"
            ),
        )

        result = environment.execute_script("limits/cpu.py")

        self.assertFalse(result.success)
        self.assertTrue(result.memory_limit_applied)
        self.assertTrue(result.cpu_limit_applied)
        self.assertFalse(result.timed_out)

    def test_memory_limit_terminates_excessive_allocation(self) -> None:
        environment = SafeExecutionEnvironment(
            self.root,
            timeout_seconds=5.0,
            memory_limit_mb=64,
            cpu_limit_seconds=5,
        )

        environment.write_text_atomic(
            "limits/memory.py",
            (
                "chunks = []\n"
                "while True:\n"
                "    chunks.append(bytearray(8 * 1024 * 1024))\n"
            ),
        )

        result = environment.execute_script("limits/memory.py")

        self.assertFalse(result.success)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.memory_limit_applied)
        self.assertTrue(result.cpu_limit_applied)
        self.assertIn("memory limit", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
