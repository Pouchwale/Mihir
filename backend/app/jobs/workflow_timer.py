"""Carries on live workflow conversations whose Wait step is over."""
from __future__ import annotations

from ..services.workflow import runtime


async def run() -> int:
    return await runtime.resume_due()
