from pathlib import Path

from nanobot.config import loader
from nanobot.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_legacy_sessions_dir,
    get_workspace_path,
    is_default_workspace,
)
from nanobot.config.schema import Config


def test_default_paths_use_nanoreview_data_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(loader, "_current_config_path", None)

    data_dir = tmp_path / ".nanoreview"

    assert loader.get_config_path() == data_dir / "config.json"
    assert get_workspace_path() == data_dir / "workspace"
    assert get_cli_history_path() == data_dir / "history" / "cli_history"
    assert get_bridge_install_dir() == data_dir / "bridge"
    assert get_legacy_sessions_dir() == data_dir / "sessions"
    assert is_default_workspace(data_dir / "workspace")


def test_default_config_workspace_uses_nanoreview_directory() -> None:
    assert Config().agents.defaults.workspace == "~/.nanoreview/workspace"
