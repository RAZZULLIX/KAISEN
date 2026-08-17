# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Telegram channel: notifications + file sharing (control-plane commands
are GUI-first now)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

from .config import FrameworkConfig, get_config


def _cfg() -> Dict[str, Any]:
    return get_config().telegram


def enabled() -> bool:
    return get_config().telegram_enabled


def _api(method: str) -> str:
    cfg = get_config()
    return f"https://api.telegram.org/bot{cfg.telegram_token}/{method}"


def send_message(message: str, max_len: Optional[int] = None) -> Optional[Dict[str, Any]]:
    cfg = get_config()
    if not cfg.telegram_enabled:
        return None
    t = cfg.telegram
    limit = max_len or int(t.get("max_len", 4000))
    trunc = int(t.get("trunc_len", 3900))
    if len(message) > limit:
        message = "[... truncated ...]\n" + message[-trunc:]
    try:
        resp = requests.get(
            _api("sendMessage"),
            params={"chat_id": cfg.telegram_chat_id, "text": message, "disable_notification": True},
            timeout=15,
        )
        return resp.json()
    except Exception:
        return None


def pin_message(message_id: int) -> None:
    cfg = get_config()
    if not cfg.telegram_enabled:
        return
    try:
        requests.get(
            _api("pinChatMessage"),
            params={"chat_id": cfg.telegram_chat_id, "message_id": message_id},
            timeout=15,
        )
    except Exception:
        pass


def send_file(file_path: str, caption: Optional[str] = None) -> bool:
    cfg = get_config()
    if not cfg.telegram_enabled:
        return False
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                _api("sendDocument"),
                data={"chat_id": cfg.telegram_chat_id, **({"caption": caption} if caption else {})},
                files={"document": (os.path.basename(file_path), f, "application/octet-stream")},
                timeout=60,
            )
        return resp.ok
    except Exception:
        return False
