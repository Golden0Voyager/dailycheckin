"""记录魔粒登录态是哪天写进 config.json 的，回答「这次会话活了多久」。

只存时间戳和长度，**绝不存 cookie 值**。
写失败一律吞掉 —— 记录只是观测手段，不能影响签到本身。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

STATE_PATH = Path.home() / ".local" / "state" / "dailycheckin" / "modelscope_cookie.json"


def record_refresh(chars: int) -> None:
    """刷新 cookie 后调一次，记下时刻。"""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "refreshed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "chars": chars,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def age_text() -> str:
    """「已用 13 天（自 2026-10-29 刷新）」；没有记录时返回空串。"""
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        refreshed = datetime.fromisoformat(str(raw["refreshed_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    days = (datetime.now().astimezone() - refreshed).days
    return f"已用 {max(days, 0)} 天（自 {refreshed.date().isoformat()} 刷新）"
