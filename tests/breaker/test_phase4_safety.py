import tempfile
import unittest
from pathlib import Path

from agentpro.safe_executor import (
    CommandNotAllowedError,
    PathIsolationError,
    SafeExecutionEnvironment,
)


class Phase4BreakerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "sandbox"
        self.environment = SafeExecutionEnvironment(
            self.root,
            timeout_seconds=2.0,
            memory_limit_mb=128,
            cpu_limit_seconds=2,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parent_traversal_cannot_escape_sandbox(self) -> None:
        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("../outside.txt")

    def test_absolute_path_cannot_escape_sandbox(self) -> None:
        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("/tmp/outside.txt")

    def test_symlink_cannot_escape_sandbox(self) -> None:
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        link = self.root / "link"
        self.root.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PathIsolationError):
            self.environment.resolve_path("link/escape.txt")

    def test_shell_executable_allowlist_rejects_command_injection(self) -> None:
        self.environment.write_text_atomic(
            "safe.py",
            "print('safe')\n",
        )

        with self.assertRaises(CommandNotAllowedError):
            self.environment.run_shell(["python", "safe.py", "&&", "rm", "-rf", "/"])

    def test_shell_executable_allowlist_rejects_non_python_commands(self) -> None:
        with self.assertRaises(CommandNotAllowedError):
            self.environment.run_shell(["sh", "-c", "echo unsafe"])

    def test_atomic_write_does_not_leave_partial_target(self) -> None:
        target = self.environment.write_text_atomic(
            "atomic/data.txt",
            "complete-content",
        )

        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "complete-content")
        self.assertFalse((self.root / "atomic" / "data.txt.tmp").exists())

    def test_timeout_kills_long_running_execution(self) -> None:
        self.environment.write_text_atomic(
            "timeout.py",
            "import time\ntime.sleep(30)\n",
        )

        result = self.environment.execute_script("timeout.py")

        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)

    def test_failed_script_can_be_repaired_without_leaving_sandbox(self) -> None:
        self.environment.write_text_atomic(
            "repair.py",
            "raise RuntimeError('broken')\n",
        )

        def repair(_result) -> str:
            return "print('repaired')\n"

        result = self.environment.write_execute_repair(
            "repair.py",
            "raise RuntimeError('broken')\n",
            repair_callback=repair,
            max_repairs=1,
        )

        self.assertTrue(result.success)
        self.assertEqual(
            (self.root / "repair.py").read_text(encoding="utf-8"),
            "print('repaired')\n",
        )


if __name__ == "__main__":
    unittest.main()
