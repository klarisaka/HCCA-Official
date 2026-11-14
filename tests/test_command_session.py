"""Offline shell integration coverage for persistent execution workers."""

from concurrent.futures import ThreadPoolExecutor
import base64
import ctypes
import os
from pathlib import Path
import shlex
import shutil
import sys
import time

import pytest

from hcca.command_session import CommandSession


POWERSHELL = os.name == "nt"


def shell_quote(value: str) -> str:
    if POWERSHELL:
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def python_command(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    wrapped = f"import base64;exec(base64.b64decode('{encoded}'))"
    invocation = f"{shell_quote(sys.executable)} -c {shell_quote(wrapped)}"
    return "& " + invocation if POWERSHELL else invocation


def test_initial_workspace_is_literal_and_relative_reads_use_it(tmp_path: Path) -> None:
    workspace = tmp_path / "中文 [fixture] with spaces'quote"
    workspace.mkdir()
    (workspace / "relative.txt").write_text("初始路径✓", encoding="utf-8")
    with CommandSession(workspace) as session:
        result = session.run(
            "[Console]::Write((Get-Location).Path)" if POWERSHELL else "pwd", timeout=15,
        )
        assert result.exit_code == 0
        assert result.output.strip() == str(workspace)
        native = session.run(python_command(
            "import os, pathlib\nprint(os.getcwd())\n"
            "print(pathlib.Path('relative.txt').read_text(encoding='utf-8'))"
        ), timeout=15)
        assert native.exit_code == 0
        assert str(workspace) in native.output
        assert "初始路径✓" in native.output


def test_cwd_environment_and_variables_persist_and_workers_are_isolated(tmp_path: Path) -> None:
    subdir = tmp_path / "目录 with 'quotes' [1]"
    subdir.mkdir()
    with CommandSession(tmp_path) as first, CommandSession(tmp_path) as second:
        setup = (
            f"Set-Location -LiteralPath {shell_quote(str(subdir))}\n"
            "$env:HCCA_TEST_SESSION='persistent ✓'\n$mySessionValue='retained'"
            if POWERSHELL else
            f"cd {shell_quote(str(subdir))}\n"
            "export HCCA_TEST_SESSION='persistent ✓'\nmySessionValue='retained'"
        )
        assert first.run(setup, timeout=15).exit_code == 0
        result = first.run(python_command(
            "import os; print(os.getcwd()); print(os.environ['HCCA_TEST_SESSION'])"
        ), timeout=15)
        assert result.exit_code == 0
        assert str(subdir) in result.output
        assert "persistent ✓" in result.output
        variable = first.run("Write-Output $mySessionValue" if POWERSHELL else 'printf "%s" "$mySessionValue"')
        assert "retained" in variable.output
        isolated = second.run(python_command(
            "import os; print(os.getcwd()); print(os.getenv('HCCA_TEST_SESSION', 'missing'))"
        ), timeout=15)
        assert isolated.exit_code == 0
        assert str(tmp_path) in isolated.output
        assert "missing" in isolated.output


def test_quotes_multiline_unicode_and_no_newline_output(tmp_path: Path) -> None:
    source = "import sys\ntext = \"双引号 \\\" and 'single' \\\\ $` ✓\\nsecond line\"\nsys.stdout.write(text)"
    expected = "双引号 \" and 'single' \\ $` ✓\nsecond line"
    with CommandSession(tmp_path) as session:
        result = session.run(python_command(source), timeout=15)
        assert result.exit_code == 0
        assert result.output.replace("\r\n", "\n") == expected
        assert not result.session_reset
        assert "__HCCA_" not in result.output


@pytest.mark.parametrize("shell", ["powershell", "pwsh"] if POWERSHELL else ["bash"])
@pytest.mark.parametrize("read_size", [None, 1])
def test_native_stdin_reads_eof_and_preserves_the_control_stream(
    tmp_path: Path, shell: str, read_size: int | None,
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    read = "sys.stdin.read()" if read_size is None else "sys.stdin.read(1)"
    with CommandSession(tmp_path, shell=shell) as session:
        result = session.run(python_command(
            "import sys\nassert sys.stdin is not None\n"
            f"print('INPUT_RESULT=' + repr({read}))"
        ), timeout=15)
        assert result.exit_code == 0
        assert result.output.strip() == "INPUT_RESULT=''"
        assert not result.timed_out
        assert not result.session_reset
        following = session.run("echo control-stream-retained", timeout=15)
        assert following.exit_code == 0
        assert following.output.strip() == "control-stream-retained"
        producer = "Write-Output 'explicit 中文 input'" if POWERSHELL else "printf 'explicit 中文 input\\n'"
        pipeline = session.run(producer + " | " + python_command(
            "import sys\nassert sys.stdin.read().rstrip('\\r\\n') == 'explicit 中文 input'\n"
            "print('PIPE_INPUT_OK')"
        ), timeout=15)
        assert pipeline.exit_code == 0
        assert pipeline.output.strip() == "PIPE_INPUT_OK"


def test_complete_output_artifact_and_merged_order(tmp_path: Path) -> None:
    output_path = tmp_path / "记录 [full] 输出.txt"
    source = (
        "import os\n"
        "os.write(1, b'first\\n')\n"
        "os.write(2, b'second\\n')\n"
        "os.write(1, ('末尾✓' * 30000).encode())"
    )
    with CommandSession(tmp_path) as session:
        result = session.run(python_command(source), timeout=15, output_path=output_path)
        assert result.exit_code == 0
        assert result.output.startswith("first\nsecond\n") or result.output.startswith("first\r\nsecond\r\n")
        assert result.output.endswith("末尾✓" * 30000)
        assert result.output_path == output_path
        assert output_path.read_bytes() == result.output.encode("utf-8")


def test_nonzero_preserves_shell_and_shell_exit_resets(tmp_path: Path) -> None:
    with CommandSession(tmp_path) as session:
        result = session.run(python_command("raise SystemExit(7)"), timeout=15)
        assert result.exit_code == 7
        assert not result.session_reset
        exited = session.run("exit 9", timeout=15)
        assert exited.exit_code == 9
        assert exited.session_reset
        assert "exited" in exited.reset_reason
        assert session.run(python_command("print('recovered')"), timeout=15).output.strip() == "recovered"


def test_idle_shell_exit_does_not_report_an_unexecuted_command_as_success(tmp_path: Path) -> None:
    marker = tmp_path / "must-execute.txt"
    with CommandSession(tmp_path) as session:
        assert session.run("echo ready", timeout=15).exit_code == 0
        process = session._process
        assert process is not None and process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        result = session.run(python_command(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"
        ), timeout=15)
        assert result.exit_code is None
        assert result.session_reset
        assert "before accepting" in result.reset_reason
        assert not marker.exists()
        recovered = session.run("echo recovered", timeout=15)
        assert recovered.exit_code == 0
        assert recovered.output.strip() == "recovered"


def test_explicit_successful_exit_has_status_and_resets(tmp_path: Path) -> None:
    with CommandSession(tmp_path) as session:
        result = session.run("exit 0", timeout=15)
        assert result.exit_code == 0
        assert result.session_reset
        assert not result.timed_out
        assert session.run("echo recovered", timeout=15).exit_code == 0


def test_final_command_status_and_syntax_error_recovery(tmp_path: Path) -> None:
    with CommandSession(tmp_path) as session:
        if POWERSHELL:
            failed = session.run("Write-Error 'fixture failure'", timeout=15)
            success = session.run(python_command("raise SystemExit(7)") + "\nWrite-Output 'last succeeded'", timeout=15)
            malformed = session.run("Write-Output ('unterminated'", timeout=15)
        else:
            failed = session.run("false | cat", timeout=15)
            success = session.run("false\nprintf 'last succeeded'", timeout=15)
            malformed = session.run("printf ('unterminated'", timeout=15)
        assert failed.exit_code != 0
        assert success.exit_code == 0
        assert "last succeeded" in success.output
        assert malformed.exit_code != 0
        assert session.run("echo recovered", timeout=15).output.strip() == "recovered"


def process_is_running(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.read_text().split(") ", 1)[1].startswith("Z"):
            return False
    except (FileNotFoundError, ProcessLookupError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(POWERSHELL, reason="POSIX process-status observation")
@pytest.mark.parametrize("stat_error", [FileNotFoundError, ProcessLookupError])
@pytest.mark.parametrize("running", [False, True])
def test_process_status_handles_disappearing_proc_entry(
    monkeypatch: pytest.MonkeyPatch, stat_error: type[OSError], running: bool,
) -> None:
    def read_stat(path: Path) -> str:
        raise stat_error("process disappeared while reading its status")

    def check_process(pid: int, signal: int) -> None:
        assert (pid, signal) == (12345, 0)
        if not running:
            raise ProcessLookupError("process exited")

    monkeypatch.setattr(Path, "read_text", read_stat)
    monkeypatch.setattr(os, "kill", check_process)
    assert process_is_running(12345) is running


def assert_process_stops(pid: int) -> None:
    deadline = time.monotonic() + 5
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_is_running(pid), f"descendant {pid} survived shell cleanup"


def test_timeout_cleans_descendants_and_resets_state(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    source = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
        "print('started', flush=True)\ntime.sleep(90)"
    )
    with CommandSession(tmp_path) as session:
        setup = "$env:HCCA_TEST_SESSION='will reset'" if POWERSHELL else "export HCCA_TEST_SESSION='will reset'"
        assert session.run(setup, timeout=15).exit_code == 0
        result = session.run(python_command(source), timeout=1)
        assert result.timed_out
        assert result.session_reset
        assert result.exit_code is None
        assert "timed out" in result.reset_reason
        assert "started" in result.output
        assert result.elapsed < 8
        assert_process_stops(int(pid_path.read_text()))
        recovered = session.run(python_command("import os; print(os.getenv('HCCA_TEST_SESSION', 'fresh'))"), timeout=15)
        assert recovered.exit_code == 0
        assert recovered.output.strip() == "fresh"


def test_exiting_shell_kills_background_children(tmp_path: Path) -> None:
    pid_path = tmp_path / "background.pid"
    source = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))"
    )
    with CommandSession(tmp_path) as session:
        result = session.run(python_command(source) + "\nexit 4", timeout=15)
        assert result.exit_code == 4
        assert result.session_reset
        assert_process_stops(int(pid_path.read_text()))


def test_same_session_serializes_concurrent_calls(tmp_path: Path) -> None:
    with CommandSession(tmp_path) as session, ThreadPoolExecutor(max_workers=4) as pool:
        command = python_command("import time; time.sleep(.1); print('one output')")
        futures = [pool.submit(session.run, command, 15) for _ in range(4)]
        results = [future.result(timeout=25) for future in futures]
        assert all(result.exit_code == 0 and result.output.strip() == "one output" for result in results)


def test_close_interrupts_execution_and_rejects_future_calls(tmp_path: Path) -> None:
    pid_path = tmp_path / "active.pid"
    source = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(90)"
    )
    session = CommandSession(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.run, python_command(source), 30)
        deadline = time.monotonic() + 10
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_path.exists()
        session.close()
        result = future.result(timeout=10)
        assert result.session_reset
        assert not result.timed_out
        assert_process_stops(int(pid_path.read_text()))
    session.close()
    with pytest.raises(RuntimeError, match="closed"):
        session.run("echo no")


def test_invalid_inputs_and_context_cleanup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workspace"):
        CommandSession(tmp_path / "missing")
    with CommandSession(tmp_path) as session:
        for command in ("", " ", "a\x00b"):
            with pytest.raises(ValueError):
                session.run(command)
        with pytest.raises(ValueError, match="timeout"):
            session.run("echo no", timeout=0)
        assert session.run("echo okay", timeout=15).exit_code == 0
        process = session._process
    assert process is not None
    assert_process_stops(process.pid)


@pytest.mark.skipif(not POWERSHELL or shutil.which("pwsh") is None, reason="PowerShell 7 is unavailable")
def test_powershell_7_offline(tmp_path: Path) -> None:
    with CommandSession(tmp_path, shell="pwsh") as session:
        assert session.run("$env:HCCA_UNICODE='中文✓'", timeout=15).exit_code == 0
        assert session.run("[Console]::Write($env:HCCA_UNICODE)", timeout=15).output == "中文✓"


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell and OEM encoding")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("source", ["native_oem", "native_utf8", "powershell"])
def test_windows_unicode_and_oem_output_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str, source: str,
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    workspace = tmp_path / "中文 [encoding] with spaces'quote"
    workspace.mkdir()
    expected = "café" if source == "native_oem" else "中文稳定性✓"
    error_text = "résumé" if source == "native_oem" else "错误✓"
    output_path = tmp_path / "saved-output.log"
    if source == "powershell":
        source_path = workspace / "输入 [one].txt"
        source_path.write_text(expected, encoding="utf-8")
        command = f"Get-Content -LiteralPath {shell_quote(str(source_path))} -Encoding UTF8"
    else:
        encoding = "oem" if source == "native_oem" else "utf-8"
        (workspace / "native.py").write_text(
            "import sys\n"
            f"sys.stdout.buffer.write(({expected!r} + '\\n').encode({encoding!r}))\n"
            f"sys.stderr.buffer.write(({error_text!r} + '\\n').encode({encoding!r}))\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        command = f"& {shell_quote(sys.executable)} ./native.py"
    with CommandSession(workspace, shell=shell) as session:
        result = session.run(command, timeout=15, output_path=output_path)
        assert result.exit_code == (0 if source == "powershell" else 7)
        assert expected in result.output
        if source != "powershell":
            assert error_text in result.output
        assert "\ufffd" not in result.output
        assert output_path.read_text(encoding="utf-8") == result.output.replace("\r\n", "\n")
        assert session.run("[Console]::Write('next-frame')", timeout=15).output == "next-frame"
    assert os.environ["PYTHONIOENCODING"] == "cp1252"


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell formatting boundaries")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_powershell_object_output_is_flushed_before_the_command_end(tmp_path: Path, shell: str) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    (tmp_path / "object.txt").write_text("fixture", encoding="utf-8")
    with CommandSession(tmp_path, shell=shell) as session:
        table = session.run("[pscustomobject]@{Name='中文对象✓'; Value=42}", timeout=15)
        assert table.exit_code == 0
        assert "中文对象✓" in table.output
        assert "42" in table.output
        assert "__HCCA_" not in table.output
        assert session.run("Write-Output 'second-frame'", timeout=15).output.strip() == "second-frame"
        item = session.run("Get-Item -LiteralPath './object.txt'", timeout=15)
        assert item.exit_code == 0
        assert "object.txt" in item.output
        assert session.run("[Console]::Write('no-newline')", timeout=15).output == "no-newline"


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell managed console input")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_managed_stdin_reads_eof_and_restores_the_control_reader(tmp_path: Path, shell: str) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    with CommandSession(tmp_path, shell=shell) as session:
        result = session.run(
            "$line = [Console]::ReadLine()\n$remaining = [Console]::In.ReadToEnd()\n"
            "[Console]::Write('LINE_IS_NULL=' + ($null -eq $line) + ';ALL_LENGTH=' + $remaining.Length)",
            timeout=15,
        )
        assert result.exit_code == 0
        assert result.output == "LINE_IS_NULL=True;ALL_LENGTH=0"
        assert not result.timed_out
        following = session.run("[Console]::Write('control-reader-restored')", timeout=15)
        assert following.exit_code == 0
        assert following.output == "control-reader-restored"
