from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CONFIG_LOCK = threading.RLock()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data: Path
    database: Path
    screenshots: Path
    key_file: Path


def app_paths() -> AppPaths:
    data = PROJECT_ROOT / "data"
    screenshots = data / "screenshots"
    data.mkdir(parents=True, exist_ok=True)
    screenshots.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        root=PROJECT_ROOT,
        data=data,
        database=data / "assistant.db",
        screenshots=screenshots,
        key_file=PROJECT_ROOT / "api_key.txt",
    )


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    config_path = PROJECT_ROOT / "config.json"
    local_path = PROJECT_ROOT / "config.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if local_path.exists():
        local = json.loads(local_path.read_text(encoding="utf-8"))
        config = _merge(config, local)
    return config


def save_local_settings(changes: dict[str, dict[str, Any]]) -> None:
    """Persist several settings atomically while preserving unrelated values."""
    local_path = PROJECT_ROOT / "config.local.json"
    with _LOCAL_CONFIG_LOCK:
        if local_path.exists():
            local = json.loads(local_path.read_text(encoding="utf-8"))
        else:
            local = {}
        for section, values in changes.items():
            section_values = local.setdefault(section, {})
            if not isinstance(section_values, dict):
                section_values = {}
                local[section] = section_values
            section_values.update(values)
        temporary = local_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(local, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(local_path)


def save_local_setting(section: str, key: str, value: Any) -> None:
    """Persist one user-controlled setting without rewriting the base config."""
    save_local_settings({section: {key: value}})
