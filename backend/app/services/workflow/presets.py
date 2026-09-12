"""The owner's own saved questions: a question set up once (its wording, answer type, checks) and
offered again under "What are you asking?" in every workflow."""
from __future__ import annotations

import json

from sqlalchemy import select

from ...db import session_scope
from ...models import QuestionPreset

# The parts of a question step that make it that question - never its position or connections.
SPEC_KEYS = ("input", "validate", "store", "text", "invalid_text", "retry_text", "max_retries", "header", "footer")
LABEL_MAX = 40
MAX_PRESETS = 100


class PresetProblem(ValueError):
    pass


async def list_presets() -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(select(QuestionPreset).order_by(QuestionPreset.id))).scalars().all()
    return [_row(r) for r in rows]


async def save(label: str, spec: dict) -> dict:
    label = (label or "").strip()
    if not label:
        raise PresetProblem("Give your question a short name, like “Order number”.")
    if len(label) > LABEL_MAX:
        raise PresetProblem(f"Keep the name to {LABEL_MAX} characters.")
    kept = {k: v for k, v in (spec or {}).items() if k in SPEC_KEYS}
    if not isinstance(kept.get("input"), dict):
        raise PresetProblem("That is not a question step.")
    async with session_scope() as db:
        count = len((await db.execute(select(QuestionPreset.id))).all())
        if count >= MAX_PRESETS:
            raise PresetProblem(f"You already have {MAX_PRESETS} saved questions. Delete one first.")
        row = QuestionPreset(label=label, spec=json.dumps(kept, ensure_ascii=False))
        db.add(row)
        await db.flush()
        out = _row(row)
    return out


async def remove(preset_id: int) -> bool:
    async with session_scope() as db:
        row = await db.get(QuestionPreset, preset_id)
        if row is None:
            return False
        await db.delete(row)
    return True


def _row(r: QuestionPreset) -> dict:
    try:
        spec = json.loads(r.spec or "{}")
    except ValueError:
        spec = {}
    return {"id": r.id, "label": r.label, "spec": spec}
