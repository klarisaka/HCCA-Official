"""Keep product checks independent from user configuration and real APIs."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def local_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("HCCA_AGENT_HOME", str(tmp_path / "agent-home"))

    def forbidden(*args, **kwargs):
        pytest.fail("An offline check attempted an HTTP request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
