"""Exercise both Root modes and the source distribution's normal entry points."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import hcca
from hcca.dsh_task import run_task
from hcca.llm import LLMResponse
from hcca.runner import GenericRun

ROOT = Path(__file__).resolve().parents[1]


class FakeLLM:
    model = "offline-model"
    reasoning_effort = "high"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, *, max_output_tokens=None, **kwargs):
        self.requests.append(deepcopy({"messages": messages, "reasoning_effort": self.reasoning_effort,
                                       "max_output_tokens": max_output_tokens, **kwargs}))
        return next(self.responses)


def reply(text="", tool="", **arguments):
    calls = [{"id": "fixture-call", "name": tool, "arguments": arguments}] if tool else []
    return LLMResponse(text=text, tool_calls=calls, input_tokens=1, output_tokens=1)


def test_standalone_root_plans_reviews_and_reports(tmp_path):
    fake = FakeLLM([reply(text) for text in (
        '{"mode":"team","agents":[{"name":"Audit","prompt":"Read the module"}],"verify":false}',
        '{"decision":"continue","reason":"Check callers","agents":[{"name":"Callers","prompt":"Read callers"}]}',
        '{"decision":"done","reason":"Complete","agents":[]}',
        "Draft report", "Final report",
    )])
    runner = GenericRun(out_dir=tmp_path / "run", llm=fake, goal="Inspect the modules", workspace=tmp_path)
    root = SimpleNamespace(role="root", agent_id="root", task_id="root")
    inbox = SimpleNamespace(messages=[])

    first = runner.step_runner(root, inbox)
    second = runner.step_runner(root, inbox)
    final = runner.step_runner(root, inbox)

    assert first["actions"][0]["type"] == "SPAWN"
    assert any(action["objective"] == "Read callers" for action in second["actions"] if action["type"] == "SPAWN")
    assert final["actions"][0]["type"] == "SUBMIT"
    assert runner._report == "Final report"


@pytest.mark.parametrize("role", ["reader", "executor"])
def test_external_worker_uses_independent_low_context_without_internal_root(tmp_path, role):
    (tmp_path / "readme.txt").write_text("answer=42\n", encoding="utf-8")
    responses = (
        [reply(tool="read_file", path="readme.txt"), reply("answer=42 at readme.txt:1")]
        if role == "reader" else
        [reply(tool="run_command", command="echo official-fixture-ok"),
         reply('{"status":"completed","summary":"Command verified official-fixture-ok"}')]
    )
    fake = FakeLLM(responses)
    output = tmp_path / "run"
    result = run_task({"workspace": str(tmp_path), "out": str(output), "role": role,
                       "mode": "command", "paths": ["readme.txt"], "objective": "Check the local fixture",
                       "max_wall_seconds": 30}, llm=fake)

    assert result["status"] == "completed"
    assert result["external_root"] is True
    assert len(fake.requests) == 2
    assert all(request["reasoning_effort"] == "low" for request in fake.requests)
    assert all(request["max_output_tokens"] is None for request in fake.requests)
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "external-worker"
    assert all(not call["tag"].startswith("root") for call in status["calls"])


def test_unicode_git_workspace_snapshot(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required for workspace snapshots")
    workspace = tmp_path / "中文 [fixture] with spaces'quote"
    workspace.mkdir()
    (workspace / "source.py").write_text("value = 0\n", encoding="utf-8")
    for arguments in (["init", "-q"], ["config", "core.quotePath", "false"], ["add", "source.py"],
                      ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Fixture"]):
        subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True)
    runner = GenericRun(out_dir=tmp_path / "run", llm=FakeLLM([]), goal="Snapshot", workspace=workspace)
    before = runner.capture_workspace_diff()
    (workspace / "source.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "新文件.py").write_text("answer = 42\n", encoding="utf-8")
    after = runner.capture_workspace_diff()

    assert before is not None and before["empty"] is True
    assert after is not None and after["changed_files"] == 2
    assert before["workspace_change_sha"] != after["workspace_change_sha"]


@pytest.mark.parametrize("arguments", [
    ["-m", "hcca.runner", "--help"],
    ["-m", "hcca.dsh_worker", "--help"],
    ["-m", "hcca.dsh_task", "--help"],
    ["benchmarks/fetch_swe_mini.py", "--help"],
    ["benchmarks/swe_mini.py", "--help"],
])
def test_entry_point_help(arguments):
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True,
                            encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_dsh_installer_dry_run_preserves_profile(tmp_path):
    profile = tmp_path / "dsh-home"
    result = subprocess.run([sys.executable, "integrations/dsh-hcca/install.py", "--dry-run",
                             "--dsh-home", str(profile), "--profile", "web"], cwd=ROOT,
                            capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "0.2.1" in result.stdout
    assert not profile.exists()
    assert hcca.__version__ == "0.2.1"
