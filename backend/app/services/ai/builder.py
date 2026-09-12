"""The AI assistant: build a workflow from a description, change one, or review one.

Building is a background job the editor polls, because on Groq's free plan a big workflow is several
calls spaced out by a per-minute limit: plan first, then write the steps in batches, then check and
fix. Every stage is reported, including "waiting for Groq's limit", so a long build never looks stuck.
The result is only ever a draft preview - nothing is published, and no customer data is sent.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from ...config import get_settings
from ..workflow import store
from ..workflow.runtime import check_routing
from ..workflow.validate import validate_graph
from . import blueprint as bpm
from . import prompts
from .groq_client import AiUnavailable, Progress, chat_json, configured

log = structlog.get_logger(__name__)

REPAIR_ROUNDS = 2
JOB_TTL_SEC = 3600


# ---------------- jobs the editor polls ----------------
@dataclass
class Job:
    id: str
    status: str = "running"  # running | done | failed
    stage: str = "Starting"
    wait_until: float = 0.0
    result: dict | None = None
    error: str = ""
    created: float = field(default_factory=time.monotonic)

    def view(self) -> dict:
        wait = max(0.0, self.wait_until - time.monotonic()) if self.wait_until else 0.0
        return {"id": self.id, "status": self.status, "stage": self.stage, "wait_seconds": round(wait),
                "result": self.result, "error": self.error}


_jobs: dict[str, Job] = {}
_tasks: set[asyncio.Task] = set()  # held so a running build is never garbage-collected


def start_job(work: Callable[[Progress], Awaitable[dict]]) -> Job:
    now = time.monotonic()
    for old in [j for j in _jobs.values() if now - j.created > JOB_TTL_SEC]:
        _jobs.pop(old.id, None)
    job = Job(id=uuid.uuid4().hex[:12])
    _jobs[job.id] = job

    async def progress(stage: str, wait: float | None = None) -> None:
        job.stage = stage
        job.wait_until = time.monotonic() + wait if wait else 0.0

    async def run() -> None:
        try:
            job.result = await work(progress)
            job.status, job.stage = "done", "Done"
        except AiUnavailable as e:
            job.status, job.error = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - the owner gets a sentence, the log gets the detail
            log.error("ai_job_failed", error=str(e))
            job.status, job.error = "failed", "Something went wrong while building. Try again, or describe a smaller part."
        finally:
            job.wait_until = 0.0

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


def job_view(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    return job.view() if job else None


def status() -> dict:
    s = get_settings()
    return {"configured": configured(), "builder_model": s.ai_builder_model, "live_model": s.ai_live_model,
            "translate_provider": s.translate_provider}


# ---------------- building ----------------
async def build(instruction: str, *, base: dict | None = None, key: str = "", history: list[str] | None = None,
                progress: Progress) -> dict:
    """A workflow document from a description - new, or `base` changed as asked."""
    s = get_settings()
    history = history or []
    system = {"role": "system", "content": prompts.builder_system()}
    tokens = 0
    editing = bpm.has_content(base)

    if editing:
        current = bpm.doc_to_blueprint(base)
        await progress("Working out the changes", None)
        patch, used = await chat_json([system, {"role": "user", "content": prompts.edit_request(instruction, current, history)}],
                                      bpm.PATCH_SCHEMA, name="patch", model=s.ai_builder_model, progress=progress)
        tokens += used
        bp = bpm.apply_patch(current, patch)
        bp["summary"] = patch.get("summary") or bp.get("summary") or ""
    else:
        await progress("Planning the workflow", None)
        outline, used = await chat_json([system, {"role": "user", "content": prompts.outline_request(instruction, history)}],
                                        bpm.OUTLINE_SCHEMA, name="outline", model=s.ai_builder_model, max_tokens=4000,
                                        reasoning="medium", progress=progress)
        tokens += used
        planned = [st for sec in outline.get("sections") or [] for st in sec.get("steps") or [] if isinstance(st, dict)]
        batches = _batches([str(st.get("id") or "") for st in planned if st.get("id")], max(3, s.ai_batch_steps))
        steps: list[dict] = []
        for n, ids in enumerate(batches, 1):
            await progress(f"Writing the steps ({n} of {len(batches)})", None)
            got, used = await chat_json([system, {"role": "user", "content": prompts.steps_request(instruction, outline, ids)}],
                                        bpm.STEPS_SCHEMA, name="steps", model=s.ai_builder_model, progress=progress)
            tokens += used
            steps += [st for st in got.get("steps") or [] if isinstance(st, dict)]
        bp = {"title": outline.get("title") or "", "summary": outline.get("summary") or "",
              "three_languages": outline.get("three_languages") is not False, "triggers": outline.get("triggers"),
              "start": outline.get("start") or (planned[0].get("id") if planned else ""), "steps": steps}

    note = ""
    for round_no in range(REPAIR_ROUNDS + 1):
        doc, notes = bpm.compile_blueprint(bp, base if editing else None)
        issues = validate_graph(doc) + await check_routing(doc, key)
        problems = notes + [i.message for i in issues if i.level == "fail"]
        if not problems or round_no == REPAIR_ROUNDS:
            break
        await progress("Fixing what the checks found", None)
        try:
            patch, used = await chat_json([system, {"role": "user", "content": prompts.repair_request(bp, problems)}],
                                          bpm.PATCH_SCHEMA, name="patch", model=s.ai_builder_model, progress=progress)
        except AiUnavailable as e:  # keep what we have: the owner sees the problems and can fix or retry
            note = str(e)
            break
        tokens += used
        bp = bpm.apply_patch(bp, patch)

    issues.sort(key=lambda i: 0 if i.level == "fail" else 1)
    log.info("ai_built", editing=editing, steps=len(doc["nodes"]), fails=sum(i.level == "fail" for i in issues), tokens=tokens)
    return {"doc": doc, "issues": [i.to_dict() for i in issues], "notes": notes, "title": bp.get("title") or "",
            "summary": bp.get("summary") or "", "tokens": tokens, "mode": "edit" if editing else "new", "note": note}


async def create_workflow(instruction: str, progress: Progress) -> dict:
    """A brand-new workflow from a description, saved as a draft."""
    result = await build(instruction, progress=progress)
    created = await store.create_from_doc((result["title"] or "AI workflow")[:120], result["summary"][:500], result["doc"])
    return {**result, "key": created["key"]}


# ---------------- reviewing ----------------
async def review(doc: dict) -> dict:
    """What a customer would stumble on, in plain words - beyond what the automatic checks say."""
    s = get_settings()
    current = bpm.doc_to_blueprint(doc)
    checks = [i.message for i in validate_graph(doc)]
    got, _ = await chat_json([{"role": "system", "content": prompts.review_system()},
                              {"role": "user", "content": prompts.review_request(current, checks)}],
                             bpm.REVIEW_SCHEMA, name="review", model=s.ai_builder_model, max_tokens=3000)
    ids = {str(n.get("id")) for n in doc.get("nodes") or [] if isinstance(n, dict)}
    findings = []
    for f in got.get("findings") or []:
        if not isinstance(f, dict) or not str(f.get("problem") or "").strip():
            continue
        sid = str(f.get("step_id") or "")
        findings.append({"step_id": sid if sid in ids else None,
                         "severity": f.get("severity") if f.get("severity") in ("problem", "suggestion") else "suggestion",
                         "problem": str(f["problem"]).strip(), "suggestion": str(f.get("suggestion") or "").strip()})
    return {"summary": str(got.get("summary") or ""), "findings": findings[:12]}


def _batches(ids: list[str], size: int) -> list[list[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)] or [[]]
