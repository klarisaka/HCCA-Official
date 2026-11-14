"""Exercise the compact executor through the packaged DSH entry point."""
import base64
import json
import os
from pathlib import Path
import shlex
import sys

from hcca.dsh_task import run_task
from hcca.execution_resume import command_exit_code
from test_runtime import FakeLLM, reply


def python_command(source):
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    code = f"exec(__import__('base64').b64decode('{encoded}'))"
    if os.name == "nt":
        executable = sys.executable.replace("'", "''")
        code = code.replace("'", "''")
        return f"& '{executable}' -c '{code}'"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_dsh_executor_uses_local_edit_and_persistent_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("HCCA_TOOL_FIXTURE", raising=False)
    workspace = tmp_path / "workspace [one] quote's"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_bytes(b"# keep context\nvalue = 1\n")
    nested = workspace / "nested"
    nested.mkdir()
    if os.name == "nt":
        path = str(nested).replace("'", "''")
        setup = f"Set-Location -LiteralPath '{path}'; $env:HCCA_TOOL_FIXTURE = 'retained'"
    else:
        setup = f"cd {shlex.quote(str(nested))}; export HCCA_TOOL_FIXTURE=retained"
    verify = python_command(
        "from pathlib import Path; import os; "
        "assert os.environ['HCCA_TOOL_FIXTURE'] == 'retained'; "
        "assert Path('../source.py').read_bytes() == b'# keep context\\nvalue = 42\\n'; "
        "print('verified')"
    )
    fake = FakeLLM([
        reply(tool="edit_file", path=str(source), old="value = 1", new="value = 42"),
        reply(tool="run_command", command=setup), reply(tool="run_command", command=verify),
        reply(tool="submit_report", status="completed", summary="Edited and verified source.py."),
    ])
    result = run_task({"workspace": str(workspace), "out": str(tmp_path / "run"),
                       "role": "executor", "mode": "fix", "paths": ["source.py"],
                       "objective": "Update the fixture and verify it", "max_wall_seconds": 30}, llm=fake)
    assert result["status"] == "completed"
    assert source.read_bytes() == b"# keep context\nvalue = 42\n"
    schemas = fake.requests[0]["tools"]
    assert [tool["function"]["name"] for tool in schemas] == ["run_command", "edit_file", "submit_report"]
    assert all(request["tools"] == schemas for request in fake.requests)
    assert all(request["reasoning_effort"] == "low" for request in fake.requests)
    assert all(request["max_output_tokens"] is None for request in fake.requests)
    transcript = [json.loads(line) for line in Path(result["log_path"]).read_text(encoding="utf-8").splitlines()]
    command_results = [row["result"] for row in transcript if row["event"] == "tool" and row["tool"] == "run_command"]
    assert len(command_results) == 2
    assert all(command_exit_code(text) == 0 for text in command_results)
    assert all("命令：" not in text for text in command_results)
    assert "verified" in command_results[-1]


def test_compact_exit_status_ignores_a_status_in_stdout():
    assert command_exit_code("退出码：7\n输出（尾部）：\n退出码：0") == 7
    assert command_exit_code("output only\n退出码：0") is None
