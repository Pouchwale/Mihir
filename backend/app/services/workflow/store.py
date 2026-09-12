"""Saving, publishing and rolling back drawn workflows.

A draft is mutable and is what the editor writes to. Publishing freezes the draft as an immutable
version and points the workflow at it, so the owner can always get back to exactly what customers
saw, and rolling back costs one integer rather than copying a graph around.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime

import structlog
from sqlalchemy import delete, desc, select

from ...db import session_scope
from ...models import Workflow, WorkflowVersion, utcnow
from . import runtime
from .schema import describe_triggers, empty_graph
from .validate import validate_graph

log = structlog.get_logger(__name__)

EXAMPLES = pathlib.Path(__file__).parent / "examples"
_SLUG = re.compile(r"[^a-z0-9]+")


class Conflict(Exception):
    """Someone else saved this workflow since the editor loaded it."""


class NotPublishable(Exception):
    """The graph has problems that would break a real conversation."""

    def __init__(self, issues: list) -> None:
        super().__init__("this workflow is not ready to publish")
        self.issues = issues


def slugify(title: str) -> str:
    base = _SLUG.sub("-", (title or "").strip().casefold()).strip("-")
    return (base or "workflow")[:60]


def examples() -> list[dict]:
    """Ready-made workflows a new install can start from, so the canvas is never a blank page."""
    out = []
    for path in sorted(EXAMPLES.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("workflow_example_unreadable", file=path.name, error=str(e))
            continue
        out.append({"key": path.stem, "title": doc.get("title") or path.stem.replace("-", " ").title(),
                    "description": doc.get("description") or "", "doc": doc})
    return out


def example(key: str) -> dict | None:
    for ex in examples():
        if ex["key"] == key:
            return ex
    return None


# ---------------- reads ----------------
async def list_workflows() -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(select(Workflow).order_by(Workflow.priority, Workflow.id))).scalars().all()
        out = []
        for w in rows:
            doc = await _graph_of(db, w.id, w.draft_version)
            live_doc = await _graph_of(db, w.id, w.published_version) if w.published_version else None
            issues = await _issues(doc, w.key, db) if doc else []
            out.append({**_summary(w),
                        "node_count": len(doc.get("nodes") or []) if doc else 0,
                        # how it starts: the published version is what customers actually meet
                        "starts": describe_triggers(live_doc or doc or {}),
                        "issue_counts": {
                            "fail": sum(1 for i in issues if i.level == "fail"),
                            "warn": sum(1 for i in issues if i.level == "warn")}})
        return out


async def get_workflow(key: str) -> dict | None:
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            return None
        draft = await _graph_of(db, w.id, w.draft_version) or empty_graph()
        published = await _graph_of(db, w.id, w.published_version)
        issues = await _issues(draft, key, db)
        return {**_summary(w), "doc": draft, "published_doc": published,
                "issues": [i.to_dict() for i in issues]}


async def versions(key: str) -> list[dict]:
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            return []
        rows = (await db.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == w.id)
            .order_by(desc(WorkflowVersion.version)))).scalars().all()
        return [{"version": v.version, "status": v.status, "notes": v.notes or "",
                 "created_at": _iso(v.created_at), "published_at": _iso(v.published_at),
                 "node_count": len(json.loads(v.graph).get("nodes") or []),
                 "is_live": v.version == w.published_version} for v in rows]


async def version_doc(key: str, version: int) -> dict | None:
    async with session_scope() as db:
        w = await _by_key(db, key)
        return await _graph_of(db, w.id, version) if w else None


# ---------------- writes ----------------
async def create(title: str, description: str = "", from_example: str = "") -> dict:
    doc = empty_graph()
    if from_example:
        ex = example(from_example)
        if ex:
            doc = ex["doc"]
            description = description or ex["description"]
    return await create_from_doc(title, description, doc)


async def create_from_doc(title: str, description: str, doc: dict) -> dict:
    """A new workflow whose first draft is the given document - a blank one, an example, an import
    or a copy. One path, so all four get the same key and version handling."""
    async with session_scope() as db:
        key = await _free_key(db, slugify(title))
        w = Workflow(key=key, title=(title or "Untitled").strip()[:120], description=description or None,
                     draft_version=1)
        db.add(w)
        await db.flush()
        db.add(WorkflowVersion(workflow_id=w.id, version=1, status="draft",
                               graph=json.dumps(doc, ensure_ascii=False)))
    log.info("workflow_created", key=key)
    return await get_workflow(key)


async def save_draft(key: str, doc: dict, base_version: int | None = None) -> dict:
    """Overwrite the draft. base_version guards against two tabs overwriting each other."""
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        if base_version is not None and w.draft_version is not None and base_version != w.draft_version:
            raise Conflict(f"this workflow moved on to version {w.draft_version} while you were editing")
        version = w.draft_version or 1
        row = await _version_row(db, w.id, version)
        if row is None:
            db.add(WorkflowVersion(workflow_id=w.id, version=version, status="draft",
                                   graph=json.dumps(doc, ensure_ascii=False)))
        elif row.status == "draft":
            row.graph = json.dumps(doc, ensure_ascii=False)
        else:
            # The draft pointer is at a published snapshot (just after a publish). Start a new one
            # rather than mutating history.
            version = await _next_version(db, w.id)
            db.add(WorkflowVersion(workflow_id=w.id, version=version, status="draft",
                                   graph=json.dumps(doc, ensure_ascii=False)))
        w.draft_version = version
        w.updated_at = utcnow()
    issues = await _issues(doc, key)
    return {"ok": True, "version": version, "saved_at": _iso(utcnow()),
            "issues": [i.to_dict() for i in issues]}


async def publish(key: str, version: int | None = None, notes: str = "") -> dict:
    """Make a version live. Refuses anything that would break a real conversation."""
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        target = version or w.draft_version
        row = await _version_row(db, w.id, target) if target else None
        if row is None:
            raise KeyError(f"version {target}")
        doc = json.loads(row.graph)
        issues = await _issues(doc, key, db)
        blocking = [i for i in issues if i.level == "fail"]
        if blocking:
            raise NotPublishable([i.to_dict() for i in issues])

        for other in (await db.execute(
                select(WorkflowVersion).where(WorkflowVersion.workflow_id == w.id,
                                              WorkflowVersion.status == "published"))).scalars():
            other.status = "archived"
        row.status = "published"
        row.published_at = utcnow()
        if notes:
            row.notes = notes[:255]
        w.published_version = row.version
        # w.enabled is left alone: a first publish reaches only the test numbers in Settings until
        # someone switches it on for everyone, and republishing a live workflow keeps it live.
        # The next edit starts a fresh draft, so a published snapshot is never mutated.
        w.draft_version = row.version
        w.updated_at = utcnow()
        published = row.version
    runtime.registry.invalidate()  # customers meet the new version from their next conversation
    log.info("workflow_published", key=key, version=published)
    return {"ok": True, "published_version": published, "issues": [i.to_dict() for i in issues]}


async def unpublish(key: str) -> dict:
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        w.enabled = False
        w.updated_at = utcnow()
    runtime.registry.invalidate()
    log.info("workflow_unpublished", key=key)
    return {"ok": True}


async def set_live(key: str, live: bool) -> dict:
    """Switch a published workflow on for everyone, or back to the test numbers in Settings only,
    without publishing anything new. Anyone inside it who is not a test number is let out at their
    next message."""
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        if live and not w.published_version:
            raise NotPublishable([{"level": "fail", "message": "Publish this workflow before switching it on.",
                                   "node_id": None, "field": ""}])
        w.enabled = bool(live)
        w.updated_at = utcnow()
    runtime.registry.invalidate()
    log.info("workflow_live", key=key, live=live)
    return {"ok": True, "live": bool(live)}


async def rename(key: str, title: str = "", description: str | None = None) -> dict:
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        if title:
            w.title = title.strip()[:120]
        if description is not None:
            w.description = description or None
        w.updated_at = utcnow()
    return {"ok": True}


async def remove(key: str) -> dict:
    async with session_scope() as db:
        w = await _by_key(db, key)
        if w is None:
            raise KeyError(key)
        await db.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id == w.id))
        await db.delete(w)
    runtime.registry.invalidate()
    log.info("workflow_deleted", key=key)
    return {"ok": True}


async def reorder(keys: list[str]) -> dict:
    """Priority decides which workflow answers when two could. Lowest first."""
    async with session_scope() as db:
        for i, key in enumerate(keys):
            w = await _by_key(db, key)
            if w is not None:
                w.priority = (i + 1) * 10
    runtime.registry.invalidate()
    return {"ok": True}


# ---------------- helpers ----------------
async def _issues(doc: dict, key: str, db=None) -> list:
    """The graph's own problems plus how it sits among the live workflows, worst first."""
    issues = validate_graph(doc) + await runtime.check_routing(doc, key, db)
    return sorted(issues, key=lambda i: 0 if i.level == "fail" else 1)


async def _by_key(db, key: str) -> Workflow | None:
    return (await db.execute(select(Workflow).where(Workflow.key == key))).scalar_one_or_none()


async def _version_row(db, workflow_id: int, version: int | None) -> WorkflowVersion | None:
    if version is None:
        return None
    return (await db.execute(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id,
                                      WorkflowVersion.version == version))).scalar_one_or_none()


async def _graph_of(db, workflow_id: int, version: int | None) -> dict | None:
    row = await _version_row(db, workflow_id, version)
    if row is None:
        return None
    try:
        return json.loads(row.graph)
    except json.JSONDecodeError:
        log.warning("workflow_graph_unreadable", workflow_id=workflow_id, version=version)
        return None


async def _next_version(db, workflow_id: int) -> int:
    top = await db.scalar(select(WorkflowVersion.version).where(WorkflowVersion.workflow_id == workflow_id)
                          .order_by(desc(WorkflowVersion.version)).limit(1))
    return (top or 0) + 1


async def _free_key(db, base: str) -> str:
    key, n = base, 2
    while await _by_key(db, key) is not None:
        key = f"{base[:56]}-{n}"
        n += 1
    return key


def _summary(w: Workflow) -> dict:
    return {"key": w.key, "title": w.title, "description": w.description or "",
            "enabled": bool(w.enabled), "priority": w.priority,
            "live": bool(w.enabled and w.published_version),
            "draft_version": w.draft_version, "published_version": w.published_version,
            "updated_at": _iso(w.updated_at), "created_at": _iso(w.created_at)}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
