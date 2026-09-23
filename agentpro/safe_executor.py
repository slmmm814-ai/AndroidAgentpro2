from __future__ import annotations

import hashlib
import logging
import os
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


LOGGER = logging.getLogger(__name__)


class SafeExecutionError(Exception):
    """Base exception for safe execution failures."""


class PathIsolationError(SafeExecutionError):
    """Raised when a requested path escapes the sandbox."""


class CommandNotAllowedError(SafeExecutionError):
    """Raised when a command is not explicitly allowlisted."""


class ResourceConfigurationError(SafeExecutionError):
    """Raised when execution resource limits cannot be configured."""


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    memory_limit_applied: bool
    cpu_limit_applied: bool
    duration_ms: float
    command: tuple[str, ...]


RepairCallback = Callable[[ExecutionResult], str | None]


class SafeExecutionEnvironment:
    """
    Restricted execution environment for Phase 4.

    Security properties:
    - Every filesystem operation is confined to sandbox_root.
    - Absolute paths are rejected.
    - Parent traversal using '..' is rejected.
    - Resolved paths must remain below sandbox_root.
    - Script writes are atomic.
    - Shell execution is disabled.
    - Only the Python interpreter is allowlisted.
    - Python may execute only a sandbox-confined script.
    - CPU limits are applied to each child process on POSIX systems.
    - Resident-memory usage is monitored and the process group is terminated
      when the configured RSS limit is exceeded.
    - Process groups are terminated on timeout.
    """

    _ALLOWED_EXECUTABLES = frozenset({"python", "python3"})
    _MAX_OUTPUT_BYTES = 1_048_576

    def __init__(
        self,
        sandbox_root: str | os.PathLike[str],
        *,
        timeout_seconds: float = 10.0,
        memory_limit_mb: int = 512,
        cpu_limit_seconds: int = 5,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        if memory_limit_mb <= 0:
            raise ValueError("memory_limit_mb must be greater than zero")

        if cpu_limit_seconds <= 0:
            raise ValueError("cpu_limit_seconds must be greater than zero")

        root = Path(sandbox_root).expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)

        if not root.is_dir():
            raise PathIsolationError(f"Sandbox root is not a directory: {root}")

        self._root = root
        self._timeout_seconds = float(timeout_seconds)
        self._memory_limit_bytes = int(memory_limit_mb) * 1024 * 1024
        self._cpu_limit_seconds = int(cpu_limit_seconds)

        self._operation_lock = threading.Lock()
        self._operation_counter = 0

        LOGGER.info(
            "Safe execution environment initialized root=%s timeout=%.3f "
            "memory_limit_mb=%d cpu_limit_seconds=%d",
            self._root,
            self._timeout_seconds,
            memory_limit_mb,
            self._cpu_limit_seconds,
        )

    @property
    def sandbox_root(self) -> Path:
        return self._root

    def resolve_path(self, relative_path: str | os.PathLike[str]) -> Path:
        """
        Resolve a path while enforcing sandbox containment.

        The caller must provide a relative path. Any absolute path or explicit
        '..' component is rejected before filesystem resolution.
        """
        raw = os.fspath(relative_path)

        if not raw:
            raise PathIsolationError("Empty path is not allowed")

        if "\x00" in raw:
            raise PathIsolationError("NUL byte in path is not allowed")

        candidate = Path(raw)

        if candidate.is_absolute():
            raise PathIsolationError("Absolute paths are not allowed")

        if any(part == ".." for part in candidate.parts):
            raise PathIsolationError("Parent traversal '..' is not allowed")

        resolved = (self._root / candidate).resolve(strict=False)

        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise PathIsolationError(
                f"Resolved path escapes sandbox: {relative_path!r}"
            ) from exc

        return resolved

    def write_text_atomic(
        self,
        relative_path: str | os.PathLike[str],
        content: str,
    ) -> Path:
        """
        Atomically replace a sandbox file.

        The temporary file is created inside the sandbox so os.replace()
        cannot cross filesystems.
        """
        if not isinstance(content, str):
            raise TypeError("content must be a string")

        destination = self.resolve_path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        fd: int | None = None
        temporary_path: Path | None = None

        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=str(destination.parent),
                text=True,
            )
            temporary_path = Path(temporary_name)

            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                fd = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temporary_path, destination)
            temporary_path = None

            LOGGER.info(
                "Atomic sandbox write completed path=%s sha256=%s",
                destination,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )

            return destination

        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    LOGGER.exception("Failed to close temporary file descriptor")

            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception(
                        "Failed to remove temporary file %s",
                        temporary_path,
                    )

            LOGGER.exception("Atomic sandbox write failed path=%s", destination)
            raise

    def run_shell(
        self,
        command: Sequence[str],
    ) -> ExecutionResult:
        """
        Execute one explicitly allowlisted command without a shell.

        Accepted forms:
            python <sandbox-relative-script>
            python3 <sandbox-relative-script>

        The interpreter actually used is the current Python executable. This
        prevents PATH substitution while retaining the explicit command
        allowlist required by Phase 4.
        """
        normalized = tuple(command)

        if not normalized:
            raise CommandNotAllowedError("Empty command is not allowed")

        executable = normalized[0]

        if executable not in self._ALLOWED_EXECUTABLES:
            raise CommandNotAllowedError(
                f"Executable is not allowlisted: {executable!r}"
            )

        if len(normalized) != 2:
            raise CommandNotAllowedError(
                "Only '<python> <sandbox-script>' is allowed"
            )

        script_relative = normalized[1]

        if any(
            marker in script_relative
            for marker in (";", "&&", "||", "|", "&", "$(", "`", "\n", "\r")
        ):
            raise CommandNotAllowedError(
                "Shell metacharacters are not allowed"
            )
        script_path = self.resolve_path(script_relative)

        if not script_path.exists():
            raise SafeExecutionError(
                f"Script does not exist inside sandbox: {script_relative!r}"
            )

        if not script_path.is_file():
            raise SafeExecutionError(
                f"Script path is not a regular file: {script_relative!r}"
            )

        return self._execute_python_script(script_path)

    def execute_script(
        self,
        relative_path: str | os.PathLike[str],
    ) -> ExecutionResult:
        """Execute an already-existing sandbox Python script."""
        script_path = self.resolve_path(relative_path)

        if not script_path.exists() or not script_path.is_file():
            raise SafeExecutionError(
                f"Script does not exist: {relative_path!r}"
            )

        return self._execute_python_script(script_path)

    def write_execute_repair(
        self,
        relative_path: str | os.PathLike[str],
        content: str,
        repair_callback: RepairCallback,
        *,
        max_repairs: int = 1,
    ) -> ExecutionResult:
        """
        Atomically write a script, execute it, and optionally repair it.

        A repair callback receives the failed ExecutionResult and may return
        replacement source. Returning None means that no repair is available.
        """
        if not callable(repair_callback):
            raise TypeError("repair_callback must be callable")

        if max_repairs < 0:
            raise ValueError("max_repairs cannot be negative")

        self.write_text_atomic(relative_path, content)

        result = self.execute_script(relative_path)

        for repair_number in range(1, max_repairs + 1):
            if result.success:
                return result

            repaired_content = repair_callback(result)

            if repaired_content is None:
                return result

            if not isinstance(repaired_content, str):
                raise TypeError("repair_callback must return str or None")

            LOGGER.warning(
                "Applying sandbox repair number=%d path=%s returncode=%s",
                repair_number,
                relative_path,
                result.returncode,
            )

            self.write_text_atomic(relative_path, repaired_content)
            result = self.execute_script(relative_path)

        return result

    def _execute_python_script(self, script_path: Path) -> ExecutionResult:
        operation_id = self._next_operation_id()

        command = (sys.executable, str(script_path))
        started = os.times().elapsed

        memory_limit_applied = True
        cpu_limit_applied = os.name == "posix"
        memory_limit_exceeded = False

        def apply_limits() -> None:
            try:
                resource.setrlimit(
                    resource.RLIMIT_CPU,
                    (
                        self._cpu_limit_seconds,
                        self._cpu_limit_seconds,
                    ),
                )
            except (OSError, ValueError) as exc:
                raise ResourceConfigurationError(
                    f"Unable to apply CPU resource limit: {exc}"
                ) from exc

        process: subprocess.Popen[str] | None = None
        timed_out = False
        memory_monitor_stop = threading.Event()
        memory_monitor: threading.Thread | None = None

        def monitor_memory() -> None:
            nonlocal memory_limit_exceeded

            if process is None:
                return

            status_path = Path(f"/proc/{process.pid}/status")

            while not memory_monitor_stop.wait(0.05):
                if process.poll() is not None:
                    return

                try:
                    status = status_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                except (FileNotFoundError, PermissionError, OSError):
                    continue

                resident_kb: int | None = None

                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        fields = line.split()
                        if len(fields) >= 2:
                            try:
                                resident_kb = int(fields[1])
                            except ValueError:
                                resident_kb = None
                        break

                if resident_kb is None:
                    continue

                resident_bytes = resident_kb * 1024

                if resident_bytes > self._memory_limit_bytes:
                    memory_limit_exceeded = True
                    LOGGER.warning(
                        "Memory limit exceeded operation_id=%d rss_bytes=%d "
                        "limit_bytes=%d",
                        operation_id,
                        resident_bytes,
                        self._memory_limit_bytes,
                    )
                    self._terminate_process_group(process)
                    return

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                cwd=str(self._root),
                start_new_session=True,
                preexec_fn=apply_limits if os.name == "posix" else None,
            )

            memory_monitor = threading.Thread(
                target=monitor_memory,
                name=f"phase4-memory-monitor-{operation_id}",
                daemon=True,
            )
            memory_monitor.start()

            stdout, stderr = process.communicate(
                timeout=self._timeout_seconds
            )

            memory_monitor_stop.set()

            if memory_monitor is not None:
                memory_monitor.join(timeout=1.0)

            returncode = process.returncode

            if len(stdout) > self._MAX_OUTPUT_BYTES:
                stdout = stdout[: self._MAX_OUTPUT_BYTES]

            if len(stderr) > self._MAX_OUTPUT_BYTES:
                stderr = stderr[: self._MAX_OUTPUT_BYTES]

            duration_ms = (os.times().elapsed - started) * 1000.0
            success = returncode == 0 and not memory_limit_exceeded

            if memory_limit_exceeded:
                stderr = (
                    f"{stderr}\n"
                    "Execution terminated because the memory limit was exceeded."
                ).strip()

            LOGGER.info(
                "Safe execution completed operation_id=%d success=%s "
                "returncode=%s duration_ms=%.2f",
                operation_id,
                success,
                returncode,
                duration_ms,
            )

            return ExecutionResult(
                success=success,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                memory_limit_applied=memory_limit_applied,
                cpu_limit_applied=cpu_limit_applied,
                duration_ms=duration_ms,
                command=(sys.executable, str(self._relative_to_root(script_path))),
            )

        except subprocess.TimeoutExpired as exc:
            timed_out = True

            memory_monitor_stop.set()

            if process is not None:
                self._terminate_process_group(process)

            stdout, stderr = process.communicate() if process is not None else ("", "")

            if memory_monitor is not None:
                memory_monitor.join(timeout=1.0)

            stdout = self._limit_output(stdout)
            stderr = self._limit_output(stderr)

            duration_ms = (os.times().elapsed - started) * 1000.0

            LOGGER.warning(
                "Safe execution timed out operation_id=%d duration_ms=%.2f",
                operation_id,
                duration_ms,
            )

            return ExecutionResult(
                success=False,
                returncode=None,
                stdout=stdout,
                stderr=stderr or str(exc),
                timed_out=timed_out,
                memory_limit_applied=memory_limit_applied,
                cpu_limit_applied=cpu_limit_applied,
                duration_ms=duration_ms,
                command=(sys.executable, str(self._relative_to_root(script_path))),
            )

        except ResourceConfigurationError:
            LOGGER.exception(
                "Resource limit configuration failed operation_id=%d",
                operation_id,
            )
            raise

        except Exception:
            LOGGER.exception(
                "Safe execution failed unexpectedly operation_id=%d",
                operation_id,
            )
            raise

    def _terminate_process_group(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        if process.poll() is not None:
            return

        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=1.0)
            else:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
        except ProcessLookupError:
            return

    def _next_operation_id(self) -> int:
        with self._operation_lock:
            self._operation_counter += 1
            return self._operation_counter

    def _relative_to_root(self, path: Path) -> Path:
        try:
            return path.relative_to(self._root)
        except ValueError as exc:
            raise PathIsolationError(
                f"Execution path escaped sandbox: {path}"
            ) from exc

    def _limit_output(self, value: str) -> str:
        if len(value) <= self._MAX_OUTPUT_BYTES:
            return value
        return value[: self._MAX_OUTPUT_BYTES]
