"""Use synthetic temporary inputs to check the portable benchmark adapter."""

import io
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from benchmarks import fetch_swe_mini, swe_mini


def test_downloader_keeps_only_task_inputs(monkeypatch):
    row = {"instance_id": "fixture-1", "repo": "fixture/repo", "base_commit": "a" * 40,
           "version": "1", "problem_statement": "Fix the fixture", "patch": "gold patch",
           "test_patch": "test patch"}
    payload = {"rows": [{"row": row}], "num_rows_total": 1}
    monkeypatch.setattr(fetch_swe_mini, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))

    tasks = fetch_swe_mini.fetch_tasks()

    assert tasks == [{key: row[key] for key in fetch_swe_mini.FIELDS}]
    assert "gold patch" not in json.dumps(tasks)
    assert "test patch" not in json.dumps(tasks)


@pytest.fixture
def workspace(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required for prediction patches")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("answer = 0\n", encoding="utf-8")
    for arguments in (["init", "-q"], ["add", "source.py"],
                      ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Fixture"]):
        swe_mini.git(workspace, *arguments)
    return workspace


def test_prediction_includes_new_files_and_preserves_real_git_index(workspace):
    (workspace / "source.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / "new.py").write_text("value = 1\n", encoding="utf-8")
    output = workspace / "run-output"
    output.mkdir()
    (output / "log.txt").write_text("runtime evidence", encoding="utf-8")
    index_before = swe_mini.git(workspace, "diff", "--cached", "--binary")

    patch = swe_mini.collect_patch(workspace, output)

    assert "+answer = 42" in patch and "+value = 1" in patch
    assert "runtime evidence" not in patch
    assert swe_mini.git(workspace, "diff", "--cached", "--binary") == index_before


def test_adapter_runs_prepared_task_and_writes_prediction(workspace, tmp_path, monkeypatch):
    task = {"instance_id": "fixture-1", "repo": "fixture/repo", "problem_statement": "Fix the answer",
            "base_commit": swe_mini.git(workspace, "rev-parse", "HEAD").decode().strip()}
    monkeypatch.setenv("HCCA_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("HCCA_LLM_API_KEY", "offline-fixture")
    original_run = subprocess.run
    invoked = []

    def run(command, **kwargs):
        if command[1:3] == ["-m", "hcca.runner"]:
            invoked.append(command)
            (workspace / "source.py").write_text("answer = 42\n", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(swe_mini.subprocess, "run", run)
    output = tmp_path / "prediction"
    returncode = swe_mini.run_instance(task, workspace, output, 30)

    assert returncode == 0 and len(invoked) == 1
    assert "--no-web" in invoked[0]
    assert "--max-output-tokens" not in invoked[0]
    assert swe_mini.PREDICTION_FILENAME == "prediction-hcca-v0.2.2.jsonl"
    prediction = json.loads((output / swe_mini.PREDICTION_FILENAME).read_text(encoding="utf-8"))
    assert prediction["instance_id"] == task["instance_id"]
    assert "+answer = 42" in prediction["model_patch"]
    status = json.loads((output / swe_mini.STATUS_FILENAME).read_text(encoding="utf-8"))
    assert status["hcca_version"] == "0.2.2"
