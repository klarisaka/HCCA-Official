"""Small, reusable execution facts without running discovery commands."""

from __future__ import annotations

import configparser
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any


def environment_prompt_path() -> Path:
    agent_home = os.environ.get("HCCA_AGENT_HOME")
    home = Path(agent_home).expanduser() if agent_home else Path.home() / ".hcca" / "agents"
    platform_name = "windows" if sys.platform == "win32" else "linux"
    return home / f"environment.{platform_name}.md"


def execution_environment(workspace: Path | str | None = None) -> dict[str, Any]:
    source = environment_prompt_path()
    if not source.is_file():
        source = Path(__file__).with_name("prompts") / source.name
    shell = "/bin/sh -c"
    if sys.platform == "win32":
        shell = "powershell -NoProfile -NonInteractive -Command"
    elif sys.platform == "linux":
        shell = "/bin/bash -o pipefail -c"
    variables = {
        name: os.environ[name][:256]
        for name in ("VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "CONDA_PREFIX", "PYTHONPATH", "TEMP", "TMP", "TMPDIR")
        if os.environ.get(name)
    }
    facts = {
        "os": sys.platform,
        "shell": shell,
        "python_runtime": sys.executable,
        "python_on_path": shutil.which("python") or shutil.which("python3") or "",
        "variables": variables,
        "notes": source.read_text(encoding="utf-8").strip(),
    }
    if workspace is not None:
        root = Path(workspace).resolve()
        files: dict[str, Path] = {}
        for name in ("tests/runtests.py", "tests/test_sqlite.py", "pytest.ini", "tox.ini", "pyproject.toml"):
            try:
                path = (root / name).resolve()
                if path.is_relative_to(root) and path.is_file():
                    files[name] = path
            except (OSError, RuntimeError):
                continue
        project_tests: dict[str, str] = {}
        if "tests/runtests.py" in files:
            project_tests["runner_script"] = "tests/runtests.py"
            if "tests/test_sqlite.py" in files:
                project_tests["settings_module"] = "test_sqlite"
        else:
            for name in ("pytest.ini", "tox.ini", "pyproject.toml"):
                if name not in files:
                    continue
                try:
                    if name.endswith(".ini"):
                        config = configparser.ConfigParser(interpolation=None)
                        config.read(files[name], encoding="utf-8")
                        pytest_config = config.has_section("pytest")
                    else:
                        document = tomllib.loads(files[name].read_text(encoding="utf-8"))
                        pytest_config = isinstance(document["tool"]["pytest"]["ini_options"], dict)
                except (OSError, UnicodeError, configparser.Error, tomllib.TOMLDecodeError, KeyError, TypeError):
                    continue
                if pytest_config:
                    project_tests["pytest_config"] = name
                    break
        facts["project_tests"] = project_tests
    return facts
