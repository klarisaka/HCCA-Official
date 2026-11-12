"""Run one SWE-bench instance in an externally prepared repository checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hcca import __version__

PREDICTION_FILENAME = f"prediction-hcca-v{__version__}.jsonl"
STATUS_FILENAME = f"hcca-v{__version__}-adapter-status.json"


def git(workspace: Path, *arguments: str, env: dict | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(workspace), *arguments],
                            capture_output=True, env=env, check=True)
    return result.stdout


def build_goal(task: dict) -> str:
    return (
        f"Fix this issue in the prepared {task['repo']} repository.\n\n"
        f"{task['problem_statement'].strip()}\n\n"
        "Find the cause, make the necessary source changes, and run the relevant existing tests. "
        "Use only the supplied repository and issue. Keep test files unchanged. "
        "Leave the changes in the workspace and report the checks and results."
    )


def collect_patch(workspace: Path, output: Path) -> str:
    pathspec = ["."]
    if output.is_relative_to(workspace):
        pathspec.append(":(exclude,literal)" + output.relative_to(workspace).as_posix())
    with tempfile.TemporaryDirectory(prefix="hcca-prediction-") as directory:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        git(workspace, "read-tree", "HEAD", env=environment)
        git(workspace, "add", "-A", "--", *pathspec, env=environment)
        patch = git(workspace, "diff", "--cached", "--binary", "HEAD", "--", *pathspec, env=environment)
    return patch.decode("utf-8")


def run_instance(task: dict, workspace: Path, output: Path, wall_seconds: int) -> int:
    expected = task["base_commit"]
    current = git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    if current != expected:
        raise ValueError(f"Workspace HEAD {current} differs from task base {expected}")
    if git(workspace, "status", "--porcelain", "-uall").strip():
        raise ValueError("Prepare a clean task checkout before starting the adapter")
    if not (os.environ.get("HCCA_LLM_BASE_URL") and os.environ.get("HCCA_LLM_API_KEY")):
        raise ValueError("Set HCCA_LLM_BASE_URL and HCCA_LLM_API_KEY for the benchmark model")
    output.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "hcca.runner", "--task-kind", "code",
               "--goal", build_goal(task), "--workspace", str(workspace),
               "--out", str(output / "run"), "--no-web", "--unlimited-executor-rounds",
               "--max-wall-seconds", str(wall_seconds),
               "--thinking-reasoning-effort", "high", "--executor-reasoning-effort", "low",
               "--reader-reasoning-effort", "low"]
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    prediction = {"instance_id": task["instance_id"],
                  "model_name_or_path": f"hcca-v{__version__}-" + os.environ.get("HCCA_LLM_MODEL", "deepseek-chat"),
                  "model_patch": collect_patch(workspace, output)}
    (output / PREDICTION_FILENAME).write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    (output / STATUS_FILENAME).write_text(json.dumps({
        "instance_id": task["instance_id"], "base_commit": expected,
        "hcca_version": __version__,
        "runner_returncode": completed.returncode,
    }, indent=2) + "\n", encoding="utf-8")
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True, help="External task JSON array")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True, help="Prepared clean checkout at base_commit")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-wall-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.max_wall_seconds <= 0:
        parser.error("--max-wall-seconds must be positive")
    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    matches = [task for task in tasks if task["instance_id"] == args.instance_id]
    if len(matches) != 1:
        parser.error("The instance must appear exactly once in --tasks")
    returncode = run_instance(matches[0], args.workspace.resolve(), args.out.resolve(), args.max_wall_seconds)
    print(f"Prediction written to {args.out / PREDICTION_FILENAME}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
