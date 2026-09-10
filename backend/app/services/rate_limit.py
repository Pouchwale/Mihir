"""Rate limits.

Two different jobs:
  check()         - per customer, DB-counted: stops one phone flooding the bot (and our WATI bill).
  allow_webhook() - per caller IP, in memory: stops anyone who learns the webhook token filling the
                    database with junk. Deliberately not DB-backed - it must be cheap enough to run
                    before we write anything at all.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MessageLog, utcnow


async def check(db: AsyncSession, phone: str) -> tuple[bool, bool]:
    """Returns (allowed, just_exceeded). just_exceeded is True only on the first blocked message."""
    s = get_settings()
    since = utcnow() - timedelta(minutes=s.rate_limit_window_min)
    n = await db.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.phone_e164 == phone, MessageLog.direction == "in", MessageLog.created_at >= since
        )
    )
    n = n or 0
    if n <= s.rate_limit_msgs:
        return True, False
    return False, n == s.rate_limit_msgs + 1


# ---------------- webhook flood protection ----------------
# Only callers that FAIL authentication are counted. WATI always presents a valid token, and
# throttling it would make it retry, fail again, and eventually disable our webhook - so a caller
# that proves it is WATI is never throttled, however fast it sends.
_bad_auth: dict[str, deque[float]] = defaultdict(deque)
_trusted: dict[str, float] = {}
_MAX_KEYS = 5000  # bound the memory: forget the quietest callers rather than grow forever
_TRUST_FOR = 3600.0  # remember a good caller for an hour


def note_webhook_authenticated(client_ip: str) -> None:
    """This caller proved it holds the webhook token, so stop counting it as a suspect."""
    if client_ip:
        _trusted[client_ip] = time.monotonic()
        _bad_auth.pop(client_ip, None)
        if len(_trusted) > _MAX_KEYS:
            cutoff = time.monotonic() - _TRUST_FOR
            for k in [k for k, seen in _trusted.items() if seen < cutoff][:1000]:
                _trusted.pop(k, None)


def note_webhook_auth_failure(client_ip: str) -> None:
    """A caller that could not present the right token. These are the ones worth throttling."""
    if not client_ip:
        return
    _trusted.pop(client_ip, None)
    q = _bad_auth[client_ip]
    q.append(time.monotonic())
    if len(_bad_auth) > _MAX_KEYS:
        for key in [k for k, v in _bad_auth.items() if not v][:1000]:
            _bad_auth.pop(key, None)


def allow_webhook(client_ip: str, limit: int | None = None, window_s: int = 10) -> bool:
    """True if this caller may post again. A known-good caller always may."""
    if not client_ip:
        return True
    if time.monotonic() - _trusted.get(client_ip, -_TRUST_FOR) < _TRUST_FOR:
        return True
    limit = limit if limit is not None else get_settings().webhook_max_per_10s
    now = time.monotonic()
    q = _bad_auth[client_ip]
    cutoff = now - window_s
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) < limit


def reset_webhook_limit() -> None:
    """Tests only."""
    _bad_auth.clear()
    _trusted.clear()
