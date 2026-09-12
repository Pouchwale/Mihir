"""A conversation that has started going round in circles.

The same order asked about three times in a row, or the same answer sent to the same question, is
not a customer being served: the status will not change because it was asked again, and a bot that
keeps replying looks broken. So the bot says so once and stops - the next message starts a fresh
conversation, or a person takes over, depending on Settings -> Conversation.

The counter lives on the session, so it survives between messages and is cleared with it.
"""
from __future__ import annotations

from ..config import get_settings

KEY_MAX = 120


def note(session, key: str) -> int:
    """Count this request. Returns how many times in a row it has now been the same one."""
    key = " ".join(str(key or "").split()).casefold()[:KEY_MAX]
    if not key or session is None:
        return 0
    session.repeat_count = (session.repeat_count or 0) + 1 if session.repeat_key == key else 1
    session.repeat_key = key
    return session.repeat_count


def too_many(count: int) -> bool:
    """Has the same request come round too often? 0 in Settings means never stop."""
    limit = get_settings().repeat_limit
    return bool(limit) and count >= limit


def hand_to_person() -> bool:
    """What stopping means: end the conversation, or quieten the bot for your team."""
    return get_settings().repeat_action == "person"


def clear(session) -> None:
    """Forget the count - a different request, or a conversation that has been reset."""
    if session is not None:
        session.repeat_key, session.repeat_count = None, 0
