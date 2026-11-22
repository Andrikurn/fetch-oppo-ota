"""Cross-platform persistent settings storage using platformdirs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from platformdirs import user_config_path

APP_NAME = "fetch-oppo-ota"


@dataclass
class AppSettings:
    """User-configurable download settings persisted across sessions."""

    max_parallel: int = 3
    connections: int = 8
    global_speed_limit_bps: int = 0
    per_file_speed_limit_bps: int = 0
    output_dir: str = "downloads"
    verify_md5: bool = True


def get_config_path() -> Path:
    """Return the OS-standard config file path:

    - Linux:   ``~/.config/fetch-oppo-ota/settings.json``
    - macOS:   ``~/Library/Application Support/fetch-oppo-ota/settings.json``
    - Windows: ``%APPDATA%\\fetch-oppo-ota\\settings.json``
    """
    return user_config_path(APP_NAME, appauthor=False) / "settings.json"


def load_settings() -> AppSettings:
    path = get_config_path()
    if not path.exists():
        return AppSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppSettings(
            max_parallel=max(1, int(data.get("max_parallel", 3))),
            connections=max(1, int(data.get("connections", 8))),
            global_speed_limit_bps=max(0, int(data.get("global_speed_limit_bps", 0))),
            per_file_speed_limit_bps=max(0, int(data.get("per_file_speed_limit_bps", 0))),
            output_dir=str(data.get("output_dir") or "downloads"),
            verify_md5=bool(data.get("verify_md5", True)),
        )
    except Exception:
        return AppSettings()


def save_settings(settings: AppSettings) -> Path:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
