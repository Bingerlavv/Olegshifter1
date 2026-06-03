"""Загрузка/сохранение JSON-конфигов клиента и сервера."""

import json
from pathlib import Path
from typing import Dict, Any


CLIENT_CONFIG_PATH = Path.home() / ".olegshifter" / "client.json"
SERVER_CONFIG_PATH = Path.home() / ".olegshifter" / "server.json"


CLIENT_DEFAULTS: Dict[str, Any] = {
    "server_host":     "127.0.0.1",
    "server_port":     8443,
    "num_channels":    3,
    "preshared":       "CHANGE-ME-32-bytes-shared-secret",
    "ws_path":         "/ws",
    "socks5_host":     "127.0.0.1",
    "socks5_port":     1080,
    "use_tls":         False,
    "tls_insecure":    True,
    "tls_ca_path":     "",
    "tls_server_name": "localhost",
}


SERVER_DEFAULTS: Dict[str, Any] = {
    "host":          "0.0.0.0",
    "base_port":     8443,
    "num_channels":  3,
    "preshared":     "CHANGE-ME-32-bytes-shared-secret",
    "ws_path":       "/ws",
    "tls_cert_path": "",
    "tls_key_path":  "",
    "web_host":      "127.0.0.1",
    "web_port":      7070,
    "web_token":     "",
}


def _load(path: Path, defaults: Dict[str, Any]) -> Dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            merged = {**defaults, **data}
            # удаляем устаревшие поля старых конфигов
            for old in ("dh_secret", "salt"):
                merged.pop(old, None)
            return merged
        except (OSError, json.JSONDecodeError):
            pass
    return dict(defaults)


def _save(path: Path, cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_client_config() -> Dict[str, Any]:
    return _load(CLIENT_CONFIG_PATH, CLIENT_DEFAULTS)


def save_client_config(cfg: Dict[str, Any]) -> None:
    _save(CLIENT_CONFIG_PATH, cfg)


def load_server_config() -> Dict[str, Any]:
    return _load(SERVER_CONFIG_PATH, SERVER_DEFAULTS)


def save_server_config(cfg: Dict[str, Any]) -> None:
    _save(SERVER_CONFIG_PATH, cfg)
