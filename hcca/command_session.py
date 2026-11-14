"""Worker-owned persistent command shells with complete output capture."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    elapsed: float
    output: str
    output_path: Path | None = None
    timed_out: bool = False
    session_reset: bool = False
    reset_reason: str | None = None


class _WindowsJob:
    """Own every descendant even when the shell exits before its children."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        try:
            if not self._kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._kernel.AssignProcessToJobObject(self._handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


_POWERSHELL_BOOTSTRAP = r"""
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
[Console]::InputEncoding = $OutputEncoding
$global:__hcca_control_input = [Console]::In
$ProgressPreference = 'SilentlyContinue'
if (Get-Variable PSStyle -ErrorAction SilentlyContinue) { $PSStyle.OutputRendering = 'PlainText' }
"""


_WINDOWS_INPUT_BOOTSTRAP = r"""
$null = [Console]::IsInputRedirected
Add-Type -TypeDefinition '
using System;
using System.Runtime.InteropServices;
public static class HccaCommandInput {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr CreateFileW(
        string path, uint access, uint share, IntPtr security,
        uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool SetStdHandle(int kind, IntPtr handle);
}
' -ErrorAction Stop
$global:__hcca_null_input = [HccaCommandInput]::CreateFileW('NUL', 2147483648, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero)
if ($global:__hcca_null_input -eq [IntPtr]::new(-1)) {
    throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
}
if (-not [HccaCommandInput]::SetHandleInformation($global:__hcca_null_input, 1, 1)) {
    throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
}
if (-not [HccaCommandInput]::SetStdHandle(-10, $global:__hcca_null_input)) {
    throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
}
[Console]::SetIn($global:__hcca_control_input)
"""


_BASH_BOOTSTRAP = r"""
exec 3<&0
exec </dev/null
while IFS= read -r __hcca_request <&3; do
    eval -- "$__hcca_request"
done
"""


class CommandSession:
    """Serialize calls in one shell, retaining its directory and environment.

    Each worker owns its own instance and must close it when the worker ends.
    A timeout or unexpected shell exit discards all shell state. The next call
    starts a fresh shell in ``workspace``. Output is complete and unabridged.
    ``close`` can interrupt a running call from another thread.
    """

    def __init__(self, workspace: Path | str, *, shell: str | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")
        self.shell = shell or ("powershell" if os.name == "nt" else "bash")
        self._powershell = Path(self.shell).stem.lower() in {"powershell", "pwsh"}
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._closed = False
        self._process: subprocess.Popen[bytes] | None = None
        self._job: _WindowsJob | None = None
        self._reader: threading.Thread | None = None
        self._chunks: queue.Queue[bytes | None] = queue.Queue()

    def _start(self) -> None:
        with self._stop_lock:
            self._start_process()

    def _start_process(self) -> None:
        executable = shutil.which(self.shell)
        if executable is None:
            raise FileNotFoundError(f"command shell is unavailable: {self.shell}")
        if self._powershell:
            argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", "-"]
        else:
            argv = [executable, "--noprofile", "--norc", "-o", "pipefail", "-c", _BASH_BOOTSTRAP]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            argv, cwd=self.workspace, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        try:
            job = _WindowsJob(process) if os.name == "nt" else None
        except BaseException:
            process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            raise
        chunks: queue.Queue[bytes | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            try:
                while chunk := process.stdout.read(65536):
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_output, name="hcca-command-output", daemon=True)
        with self._state_lock:
            self._process = process
            self._job = job
            self._chunks = chunks
            self._reader = reader
            closed = self._closed
            reader.start()
        if closed:
            self._stop_process()
            raise RuntimeError("command session is closed")
        if self._powershell:
            workspace_literal = str(self.workspace).replace("'", "''")
            source = f"Set-Location -LiteralPath '{workspace_literal}' -ErrorAction Stop\n" + _POWERSHELL_BOOTSTRAP
            if os.name == "nt":
                # The interpreter retains its reader for control input. Native
                # children inherit an EOF-producing NUL handle until shell exit.
                source += _WINDOWS_INPUT_BOOTSTRAP
            expression = self._powershell_expression(source)
            initialization = (
                f"try {{ {expression} }} catch {{ [Console]::Out.WriteLine($_.Exception.Message); exit 1 }}\n"
            )
            assert process.stdin is not None
            process.stdin.write(initialization.encode("ascii"))
            process.stdin.flush()

    def _stop(self) -> None:
        with self._stop_lock:
            self._stop_process()

    def _stop_process(self) -> None:
        with self._state_lock:
            process, self._process = self._process, None
            job, self._job = self._job, None
        if process is None:
            return
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if self._reader is not None:
            self._reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()

    @staticmethod
    def _powershell_expression(source: str) -> str:
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        return f". ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))))"

    @staticmethod
    def _bash_quote(command: str) -> str:
        escaped = (command.replace("\\", "\\\\").replace("'", "\\'")
                   .replace("\r", "\\r").replace("\n", "\\n"))
        return "$'" + escaped + "'"

    def _request(self, command: str, token: str) -> bytes:
        if self._powershell:
            source = command + "\n$global:__hcca_status = if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }"
            expression = self._powershell_expression(source)
            # Separate top-level input lines complete PowerShell formatting
            # before the end marker. Native bytes bypass its text pipeline.
            request = (
                f"[Console]::Out.Write('__HCCA_START_{token}__'); [Console]::Out.Flush(); "
                "$global:__hcca_status = 0; $global:LASTEXITCODE = 0\n"
                f"try {{ [Console]::SetIn([IO.StringReader]::new('')); {expression} }} "
                "catch { $global:__hcca_status = 1; [Console]::Out.WriteLine($_.Exception.Message) } "
                "finally { [Console]::SetIn($global:__hcca_control_input) }\n"
                f"[Console]::Out.Write('__HCCA_END_{token}__' + $global:__hcca_status + '__HCCA_DONE__'); "
                "[Console]::Out.Flush()\n"
            )
            return request.encode("ascii")
        quoted = self._bash_quote(command)
        request = (
            f"printf '%s' '__HCCA_START_{token}__'; "
            f"eval -- {quoted}; __hcca_status=$?; "
            f"printf '%s%s%s' '__HCCA_END_{token}__' "
            '"$__hcca_status" \'__HCCA_DONE__\'\n'
        )
        return request.encode("utf-8")

    def run(
        self, command: str, timeout: float = 120.0, output_path: Path | str | None = None,
    ) -> CommandResult:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if "\x00" in command:
            raise ValueError("command contains a null character")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        artifact = Path(output_path).resolve() if output_path is not None else None
        with self._lock:
            if self._closed:
                raise RuntimeError("command session is closed")
            started = time.monotonic()
            deadline = started + timeout
            token = uuid.uuid4().hex
            start_marker = f"__HCCA_START_{token}__".encode("ascii")
            end_marker = f"__HCCA_END_{token}__".encode("ascii")
            done_marker = b"__HCCA_DONE__"
            captured = bytearray()
            search_from = 0
            end = -1
            exit_code = None
            reason = None
            timed_out = False
            try:
                if self._process is None:
                    self._start()
                process = self._process
                if process is None:
                    raise RuntimeError("command session is closed")
                assert process.stdin is not None
                try:
                    process.stdin.write(self._request(command, token))
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    reason = "persistent shell exited before accepting the command"
                while reason is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        reason = f"command timed out after {timeout:g} seconds"
                        break
                    try:
                        chunk = self._chunks.get(timeout=min(remaining, 0.05))
                    except queue.Empty:
                        if self._closed:
                            reason = "command session was closed during execution"
                        elif process.poll() is not None:
                            reason = f"persistent shell exited with code {process.returncode}"
                        continue
                    if chunk is None:
                        reason = f"persistent shell exited with code {process.wait(timeout=5)}"
                        break
                    captured.extend(chunk)
                    if end < 0:
                        end = captured.find(end_marker, search_from)
                        search_from = max(0, len(captured) - len(end_marker) + 1)
                    if end >= 0:
                        done = captured.find(done_marker, end + len(end_marker))
                        if done >= 0:
                            exit_code = int(captured[end + len(end_marker):done])
                            del captured[end:]
                            break
                if reason is not None:
                    if self._closed:
                        reason = "command session was closed during execution"
                    self._stop()
                    while True:
                        try:
                            chunk = self._chunks.get_nowait()
                        except queue.Empty:
                            break
                        if chunk is not None:
                            captured.extend(chunk)
                    if not timed_out and not self._closed and start_marker in captured:
                        exit_code = process.returncode
            except BaseException:
                self._stop()
                raise
            start = captured.find(start_marker)
            if start >= 0:
                del captured[:start + len(start_marker)]
            try:
                output = captured.decode("utf-8")
            except UnicodeDecodeError:
                output = captured.decode("oem" if os.name == "nt" else "utf-8", errors="replace")
            if artifact is not None:
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(output, encoding="utf-8", newline="")
            return CommandResult(
                exit_code=exit_code, elapsed=time.monotonic() - started, output=output,
                output_path=artifact, timed_out=timed_out, session_reset=reason is not None,
                reset_reason=reason,
            )

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        self._stop()

    def __enter__(self) -> CommandSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
