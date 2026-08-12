from __future__ import annotations

import os
import re
from pathlib import Path


_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{8,}$")
_KEY_NAMES = ("kimi_key", "mimo_key")


def _load_key_values(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RuntimeError(f"缺少密钥文件：{path.name}。")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or name not in _KEY_NAMES:
            raise RuntimeError(f"{path.name} 第 {line_number} 行格式不正确。")
        if not value:
            continue
        if not _SECRET_PATTERN.fullmatch(value):
            raise RuntimeError(f"{path.name} 第 {line_number} 行格式不正确。")
        values[name] = value
    return values


def load_kimi_key(path: Path) -> str:
    environment_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if environment_key:
        return environment_key
    value = _load_key_values(path).get("kimi_key", "")
    if not value:
        raise RuntimeError("api_key.txt 缺少 kimi_key=...。")
    return value


def load_mimo_key(path: Path) -> str:
    environment_key = os.environ.get("MIMO_API_KEY", "").strip()
    if environment_key:
        return environment_key
    value = _load_key_values(path).get("mimo_key", "")
    if not value:
        raise RuntimeError("api_key.txt 缺少 mimo_key=...。")
    return value


def key_configuration_status(path: Path) -> dict[str, bool]:
    """Report whether each provider is configured without exposing its value."""
    values: dict[str, str] = {}
    try:
        values = _load_key_values(path)
    except RuntimeError:
        pass
    return {
        "kimi_key": bool(os.environ.get("MOONSHOT_API_KEY", "").strip() or values.get("kimi_key")),
        "mimo_key": bool(os.environ.get("MIMO_API_KEY", "").strip() or values.get("mimo_key")),
        "kimi_key_from_environment": bool(os.environ.get("MOONSHOT_API_KEY", "").strip()),
        "mimo_key_from_environment": bool(os.environ.get("MIMO_API_KEY", "").strip()),
    }


def save_api_keys(
    path: Path,
    *,
    kimi_key: str = "",
    mimo_key: str = "",
) -> None:
    """Update non-empty provider keys while preserving the other provider."""
    values: dict[str, str] = {}
    if path.exists():
        try:
            values = _load_key_values(path)
        except RuntimeError:
            values = {}
    updates = {"kimi_key": kimi_key.strip(), "mimo_key": mimo_key.strip()}
    for name, value in updates.items():
        if not value:
            continue
        if not _SECRET_PATTERN.fullmatch(value):
            raise ValueError(f"{name} 格式不正确。")
        values[name] = value
    if not any(values.get(name) for name in _KEY_NAMES):
        raise ValueError("请至少输入一个 API Key。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    lines = [f"{name}={values[name]}" for name in _KEY_NAMES if values.get(name)]
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def redact_secret(text: str) -> str:
    return re.sub(r"sk[-_A-Za-z0-9]{8,}", "sk-[REDACTED]", text)
