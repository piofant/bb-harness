import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_sync  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Defaults of harness_sync (Claude skills, Codex hooks, main list) must never hit the real home."""
    monkeypatch.setattr(harness_sync, "HOME", tmp_path / "home")
    monkeypatch.setattr(harness_sync, "MAIN", tmp_path / "home" / "main_skills.json")
