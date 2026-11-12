"""Offline workspace tool checks, including real Windows process execution."""
from pathlib import Path
import os
import sys
import time
import json

import pytest

from hcca.agent_tools import AgentToolbox

@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("HCCA web evidence\nsecond line\n", encoding="utf-8")
    (tmp_path / "data.json").write_text(json.dumps({"cache": 0.9}), encoding="utf-8")
    return tmp_path

def test_schemas_expose_expected_tools(workspace: Path) -> None:
    names = [tool["function"]["name"] for tool in AgentToolbox(workspace).schemas()]
    assert names == [
        "web_search",
        "web_fetch",
        "read_file",
        "write_file",
        "list_files",
        "search_files",
        "run_command",
    ]

def test_schemas_respect_disable_flags(workspace: Path) -> None:
    names = [
        tool["function"]["name"]
        for tool in AgentToolbox(workspace, allow_web=False, allow_run=False).schemas()
    ]
    assert "web_search" not in names
    assert "web_fetch" not in names
    assert "run_command" not in names
    assert "read_file" in names

def test_read_file_and_escape_rejected(workspace: Path) -> None:
    toolbox = AgentToolbox(workspace)
    text = toolbox.call("read_file", {"path": "notes/a.md"})
    assert "HCCA web evidence" in text
    denied = toolbox.call("read_file", {"path": "../../outside.txt"})
    assert denied.startswith("ERROR:")
    assert "escapes workspace" in denied

def test_list_files_and_search(workspace: Path) -> None:
    toolbox = AgentToolbox(workspace)
    listing = toolbox.call("list_files", {"path": "notes"})
    assert "notes/a.md" in listing
    hits = toolbox.call("search_files", {"pattern": "web evidence"})
    assert "notes/a.md:1" in hits

def test_write_file_creates_nested_file(workspace: Path) -> None:
    toolbox = AgentToolbox(workspace)
    result = toolbox.call("write_file", {"path": "solutions/hcca_test.md", "content": "# 答案\n42\n"})
    assert "已写入" in result
    target = workspace / "solutions" / "hcca_test.md"
    assert target.read_text(encoding="utf-8") == "# 答案\n42\n"
    denied = toolbox.call("write_file", {"path": "../escape.md", "content": "x"})
    assert denied.startswith("ERROR:")
    assert "escapes workspace" in denied

def test_run_command_in_workspace(workspace: Path) -> None:
    toolbox = AgentToolbox(workspace, timeout=20)
    result = toolbox.call("run_command", {"command": "echo hcca-tool-ok"})
    assert "hcca-tool-ok" in result
    assert "退出码：0" in result

@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell output encoding")
@pytest.mark.parametrize("source", ["powershell", "native", "native_utf8", "native_oem"])
def test_windows_command_preserves_unicode_output_paths_and_exit_codes(tmp_path: Path, monkeypatch, source: str) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    workspace = tmp_path / "中文 [fixture] with spaces'quote"
    workspace.mkdir()
    expected = "café" if source == "native_oem" else "中文稳定性"
    error_text = "résumé" if source == "native_oem" else "错误"
    source_file = workspace / "输入 [one].txt"
    source_file.write_text(expected, encoding="utf-8")
    if source == "powershell":
        source_literal = str(source_file).replace("'", "''")
        command = f"Get-Content -LiteralPath '{source_literal}' -Encoding UTF8; exit 7"
    else:
        if source == "native_oem":
            script = ("import sys\n"
                      f"sys.stdout.buffer.write(({expected!r} + '\\n').encode('oem'))\n"
                      f"sys.stderr.buffer.write(({error_text!r} + '\\n').encode('oem'))\n"
                      "sys.exit(7)\n")
        else:
            script = f"import sys\nprint({expected!r})\nprint({error_text!r}, file=sys.stderr)\nsys.exit(7)\n"
        (workspace / "native output.py").write_text(script, encoding="utf-8")
        prefix = "$env:PYTHONIOENCODING = 'utf-8'; " if source == "native_utf8" else ""
        command = prefix + f"& '{sys.executable}' './native output.py'; exit $LASTEXITCODE"

    result = AgentToolbox(workspace).call("run_command", {"command": command})

    assert "退出码：7" in result
    output = result.split("输出（尾部）：\n", 1)[1]
    assert expected in output
    assert "\ufffd" not in output
    if source != "powershell":
        assert error_text in output
    assert os.environ["PYTHONIOENCODING"] == "cp1252"

@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree semantics")
def test_run_command_timeout_kills_detached_grandchild(tmp_path: Path) -> None:
    """A timed-out command must return even if it spawned a detached child.

    Regression: the old ``subprocess.run(timeout=...)`` killed only PowerShell
    and then blocked forever waiting for the stdout pipe, which the detached
    grandchild still held (a real tool call stayed pending for 24 minutes).
    """
    script = tmp_path / "spawn_sleeper.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('spawned', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    toolbox = AgentToolbox(tmp_path, timeout=60)
    started = time.time()
    result = toolbox.call("run_command", {"command": f'& "{sys.executable}" spawn_sleeper.py', "timeout_seconds": "2"})
    elapsed = time.time() - started
    assert elapsed < 20, f"run_command 超时后仍被 grandchild 拖住：{elapsed:.1f}s"
    assert "命令超时" in result


@pytest.mark.skipif(sys.platform != "linux", reason="Linux Bash invocation")
@pytest.mark.parametrize("producer_exit", [0, 7])
def test_run_command_linux_pipeline_preserves_producer_status(workspace: Path, producer_exit: int) -> None:
    command = (
        f"(printf 'producer stdout\\n'; printf 'producer stderr\\n' >&2; exit {producer_exit}) "
        "| tail -n 1"
    )

    result = AgentToolbox(workspace).call("run_command", {"command": command})

    assert f"退出码：{producer_exit}" in result
    output = result.split("输出（尾部）：\n", 1)[1]
    assert "producer stdout" in output
    assert "producer stderr" in output
