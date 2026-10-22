"""The job that produces one assistant message.

Wires provider, tools, skills, memory and history into the pure loop (run_setup.RunSetup), then
consumes its events: blocks are persisted (block_store.BlockStore), every event is published
over realtime with its payload (publisher.Publisher), and the message / run / session records are
brought to their final state. Cancellation is checked between events through
a Redis flag set by the API plus the run's cancel_requested column.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import cint, now_datetime

from ai_agent.engine import skills as skills_mod
from ai_agent.engine.block_store import BlockStore, stored_kind  # noqa: F401 (stored_kind: re-exported)
from ai_agent.engine.errors import user_message
from ai_agent.engine.events import (
    ApprovalAutoExecuted,
    ApprovalRequested,
    ArtifactCreated,
    BlockDelta,
    BlockDone,
    BlockStart,
    Compaction,
    MemoryNote,
    MessageCancelled,
    MessageDone,
    MessageFailed,
    MessageStarted,
    ProviderError,
    QuestionAsked,
    SkillLoaded,
    SuggestionsShown,
    ToolResultEvent,
    UsageEvent,
)
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.providers.factory import provider_for
from ai_agent.engine.publisher import TAIL_KEY, Publisher  # noqa: F401 (re-exported: runner.Publisher, runner.TAIL_KEY)
from ai_agent.engine.run_setup import CANCEL_KEY, RunSetup, run_options
from ai_agent.engine.types import TEXT, Block, Usage
from ai_agent.runtime.run_control import run_timeout_seconds

JOB_PREFIX = "ai-chat-msg-"
# Provider failures that say the chosen model cannot be used now; anything else (a bug, a timeout) still fails.
FALLBACK_KINDS = frozenset({"auth", "quota", "model", "not_chat", "no_tools", "rate_limit"})


def job_id(message):
    return f"{JOB_PREFIX}{message}"


def job_is_alive(message):
    """True while the message's queue job can still run (queued or started); True when Redis cannot say."""
    try:
        from frappe.utils.background_jobs import get_job_status

        status = get_job_status(job_id(message))
    except Exception:
        return True   # Redis cannot say: assume alive rather than restart a running reply
    return str(getattr(status, "value", status) or "").lower() in ("queued", "started", "deferred", "scheduled")


def enqueue(message, user, resume=False):
    run = frappe.db.get_value("AI Chat Message", message, "run")
    agent_name = frappe.db.get_value("AI Run", run, "agent") if run else None
    agent = frappe.get_cached_doc("AI Agent", agent_name) if agent_name else None
    timeout = run_timeout_seconds(agent=agent) + 120
    frappe.enqueue(
        "ai_agent.engine.runner.execute_message",
        queue="long",
        timeout=timeout,
        job_id=job_id(message),
        deduplicate=True,
        message=message,
        user=user,
        resume=resume,
    )


def error_detail_for(user, raw):
    return (raw or "")[:600] if "System Manager" in frappe.get_roles(user) else ""


def fail_orphaned_message(message, error):
    """The job died (worker restart, crash before the runner could report):
    settle the message so the chat does not wait forever."""
    doc = frappe.get_doc("AI Chat Message", message)
    if doc.status not in ("pending", "streaming"):
        return
    friendly = user_message("interrupted", error, user=doc.user)
    doc.db_set({"status": "failed", "error": friendly, "completed_at": now_datetime()}, update_modified=False)
    frappe.db.set_value("AI Message Block", {"message": doc.name, "status": "streaming"}, "status", "completed", update_modified=False)
    frappe.db.commit()
    pub = Publisher(doc.session, doc.name, doc.user, start_seq=cint(doc.event_seq))
    pub.publish(MessageFailed(error=friendly, kind="interrupted", detail=error_detail_for(doc.user, error)))
    doc.db_set("event_seq", pub.seq, update_modified=False)
    frappe.db.commit()


def request_cancel(message):
    frappe.cache.set_value(CANCEL_KEY.format(message), "1", expires_in_sec=3600)


def execute_message(message, user=None, resume=False):
    doc = frappe.get_doc("AI Chat Message", message)
    if not doc.user or doc.user in ("Guest", "Administrator") and doc.user != user:
        pass
    if user and user != doc.user:
        frappe.throw("Message owner mismatch.", frappe.PermissionError)
    frappe.set_user(doc.user)
    Runner(doc).run(resume=resume)


class Runner(RunSetup, BlockStore):
    def __init__(self, message_doc):
        self.msg = message_doc
        self.session = frappe.get_doc("AI Chat Session", self.msg.session)
        self.run_doc = frappe.get_doc("AI Run", self.msg.run) if self.msg.run else None
        self.agent = frappe.get_cached_doc("AI Agent", self.run_doc.agent if self.run_doc else self.session.agent)
        self.settings = frappe.get_cached_doc("AI Settings")
        self.user = self.msg.user
        self.pub = Publisher(self.session.name, self.msg.name, self.user, start_seq=cint(self.msg.event_seq))
        self.blocks = {}            # seq -> {"name": docname, "kind": str, "text": str, "started": float, "dirty": bool}
        self.usage = Usage()
        self.context_tokens = 0
        self.last_flush = time.monotonic()
        self.started = time.monotonic()
        self.tail = TAIL_KEY.format(self.msg.name)

    # -- run -------------------------------------------------------------------------

    def run(self, resume=False):
        if self.msg.status in ("completed", "failed", "cancelled"):
            return
        try:
            loop, executor, messages, start_seq = self.build(resume)
        except Exception as exc:
            frappe.log_error(frappe.get_traceback(), f"AI chat build {self.msg.name}")
            self.fail(str(exc)[:500], kind="setup")
            return

        self.msg.db_set({"status": "streaming", "started_at": self.msg.started_at or now_datetime(), "provider": self.provider_name, "model": self.model}, update_modified=False)
        if self.run_doc:
            self.run_doc.db_set({"status": "Running", "started_at": self.run_doc.started_at or now_datetime(), "provider": self.provider_name, "model": self.model}, update_modified=False)
        frappe.db.commit()

        # A skill and/or a plugin chosen with / are loaded before the first model call.
        options = run_options(self.run_doc.route if self.run_doc else "")
        if not resume:
            for block in (
                skills_mod.preload(executor, options["skill"]) if options.get("skill") else None,
                skills_mod.preload_plugin(executor, options["plugin"]) if options.get("plugin") else None,
            ):
                if not block:
                    continue
                block.seq = start_seq
                start_seq += 1
                self.handle(SkillLoaded(block_seq=block.seq, block=block.to_dict()))
                messages[-1].blocks.append(block)

        try:
            failed = None
            for event in loop.run(messages, start_seq=start_seq, resumed=resume):
                if self.can_fall_back(event, resume, options):
                    failed = event
                    break
                self.handle(event)
            if failed is not None:
                self.fall_back(failed, resume)
        except Exception as exc:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"AI chat run {self.msg.name}")
            self.fail(f"Unexpected error: {frappe.utils.cstr(exc)[:300]}")
        finally:
            self.pub.flush()
            try:
                frappe.cache.delete_value(self.tail)
                frappe.cache.delete_value(CANCEL_KEY.format(self.msg.name))
            except Exception:
                pass   # cache cleanup only: both keys expire on their own

    # -- model fallback ------------------------------------------------------------------

    def can_fall_back(self, event, resume, options):
        """The chosen model could not answer at all (bad key, no credit, not a chat model, rate limit) before
        anything was shown: answer with the site's default model instead of failing the turn."""
        if not isinstance(event, MessageFailed) or event.kind not in FALLBACK_KINDS:
            return False
        if resume or self.blocks or getattr(self, "override", None) or options.get("skill") or options.get("plugin"):
            return False
        try:
            _provider, name, model = provider_for(None, None)
        except Exception:
            return False   # no usable default model: the original failure is reported as is
        if (name, model) == (self.provider_name, self.model):
            return False
        self.fallback = (name, model)
        return True

    def fall_back(self, failed, resume):
        tried = f"{self.provider_name} / {self.model}"
        self.override = self.fallback
        loop, executor, messages, start_seq = self.build(resume)
        frappe.log_error(f"{tried}: {failed.kind}: {failed.error}"[:2000], f"AI chat model fallback {self.msg.name}")
        self.msg.db_set({"provider": self.provider_name, "model": self.model}, update_modified=False)
        note = {"kind": "status", "text": frappe._("{0} could not answer ({1}); this reply uses {2}.", lang=frappe.db.get_value("User", self.msg.user, "language") or None).format(tried, failed.kind, f"{self.provider_name} / {self.model}")}
        self.insert_block(start_seq, note)
        for event in loop.run(messages, start_seq=start_seq + 1, resumed=resume):
            self.handle(event)

    # -- event handling ----------------------------------------------------------------

    def handle(self, event):
        if isinstance(event, MessageStarted):
            self.pub.publish(event)
        elif isinstance(event, BlockStart):
            self.open_block(event.block_seq, event.kind, id=event.id, name=event.name)
            self.pub.publish(event)
        elif isinstance(event, BlockDelta):
            entry = self.blocks.get(event.block_seq)
            if entry is not None:
                entry["text"] += event.delta
                entry["dirty"] = True
                self.tail_write(event.block_seq, entry["text"])
            self.pub.publish(event)
            self.maybe_flush()
        elif isinstance(event, BlockDone):
            self.close_block(event.block_seq, event.block)
            self.pub.publish(event)
        elif isinstance(event, (ToolResultEvent, SkillLoaded, MemoryNote, ArtifactCreated, QuestionAsked, SuggestionsShown)):
            self.insert_block(event.block_seq, event.block, extra={"duration_ms": getattr(event, "duration_ms", 0), "tool_call": getattr(event, "tool_call", "")})
            self.pub.publish(event)
            frappe.db.commit()
        elif isinstance(event, ApprovalRequested):
            self.insert_block(event.block_seq, event.block, extra={"approval": event.approval})
            self.pub.publish(event)
            frappe.db.commit()
        elif isinstance(event, ApprovalAutoExecuted):
            self.insert_block(event.block_seq, event.block, extra={"approval": (event.block.get("data") or {}).get("approval")})
            self.pub.publish(event)
            frappe.db.commit()
        elif isinstance(event, UsageEvent):
            self.usage.add(Usage(**event.usage))
            self.context_tokens = event.context_tokens or self.context_tokens
            self.pub.publish(event)
        elif isinstance(event, Compaction):
            if event.summary:
                self.session.db_set({"summary": event.summary, "summary_through_seq": self.summary_through_seq(event)}, update_modified=False)
            self.pub.publish(event)
        elif isinstance(event, ProviderError):
            self.pub.publish(event)
        elif isinstance(event, MessageDone):
            self.finish(event)
        elif isinstance(event, MessageFailed):
            self.fail(event.error, kind=event.kind)
        elif isinstance(event, MessageCancelled):
            self.cancelled()

    def summary_through_seq(self, event):
        # The compactor reports the last message it folded into the summary; never move the point back.
        current = cint(frappe.db.get_value("AI Chat Session", self.session.name, "summary_through_seq"))
        return max(current, cint(getattr(event, "summary_through_seq", 0)))

    # -- terminal states --------------------------------------------------------------------

    def usage_values(self):
        return {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
            "estimated_cost": round(self.caps.cost(self.usage), 6) if hasattr(self, "caps") else 0,
            "context_tokens": self.context_tokens,
        }

    def close_message(self, status, event=None, error=None, run_status="Completed", detail=None):
        self.maybe_flush(force=True)
        # Any block still open (cancelled mid-stream) is closed with what it has.
        for seq, entry in list(self.blocks.items()):
            if not entry.get("closed"):
                frappe.db.set_value("AI Message Block", entry["name"], {"status": "completed" if status != "failed" else "failed", "content": entry["text"], "completed_at": now_datetime()}, update_modified=False)
                entry["closed"] = True
                self.tail_delete(seq)
        text = self.message_text()
        now = now_datetime()
        values = {
            "status": status,
            "text": text,
            "completed_at": now if status != "awaiting_approval" else None,
            "duration_ms": int((time.monotonic() - self.started) * 1000) + cint(self.msg.duration_ms),
            "stop_reason": getattr(event, "stop_reason", "") or "",
            "error": error or None,
            "steps": cint(self.msg.steps) + (getattr(event, "steps", 0) or 0),
            "tool_calls": cint(self.msg.tool_calls) + (getattr(event, "tool_calls", 0) or 0),
            "event_seq": self.pub.seq + 1,
        }
        usage = self.usage_values()
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "estimated_cost"):
            values[key] = (self.msg.get(key) or 0) + usage[key]
        values["context_tokens"] = usage["context_tokens"] or self.msg.context_tokens
        self.msg.db_set(values, update_modified=False)
        if self.run_doc:
            self.run_doc.db_set({
                "status": run_status,
                "final_answer": text[:16000] if status == "completed" else self.run_doc.final_answer,
                "error_summary": (detail or error or "")[:500] or None,
                "completed_at": now if run_status in ("Completed", "Failed", "Cancelled") else None,
                "input_tokens": cint(self.run_doc.input_tokens) + usage["input_tokens"],
                "output_tokens": cint(self.run_doc.output_tokens) + usage["output_tokens"],
                "cache_read_input_tokens": cint(self.run_doc.cache_read_input_tokens) + usage["cache_read_input_tokens"],
                "cache_creation_input_tokens": cint(self.run_doc.cache_creation_input_tokens) + usage["cache_creation_input_tokens"],
                "tool_call_count": values["tool_calls"],
                "current_step": values["steps"],
            }, update_modified=False)
        session_values = {"last_message_on": now}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "estimated_cost"):
            session_values[key] = (self.session.get(key) or 0) + usage[key]
        self.session.db_set(session_values, update_modified=False)
        frappe.db.commit()

    def finish(self, event):
        status = event.status
        self.close_message(status, event=event, run_status="Waiting Approval" if status == "awaiting_approval" else "Completed")
        self.pub.publish(event)
        if status == "completed":
            self.maybe_title()
        self.publish_session()

    def fail(self, error, kind="error"):
        # The user sees one sentence in their language; the raw provider text
        # stays on the AI Run (error_summary) and goes live only to System Managers.
        friendly = user_message(kind, error, user=self.msg.user)
        self.close_message("failed", error=friendly, run_status="Failed", detail=error)
        self.pub.publish(MessageFailed(error=friendly, kind=kind, usage=self.usage.to_dict(), detail=error_detail_for(self.msg.user, error)))
        self.publish_session()

    def cancelled(self):
        self.close_message("cancelled", run_status="Cancelled")
        self.pub.publish(MessageCancelled(usage=self.usage.to_dict()))
        self.publish_session()

    def publish_session(self):
        from ai_agent.chat.api import get_session

        try:
            self.pub.publish(_SessionUpdated(get_session(self.session.name)["session"]))
        except Exception:
            pass   # realtime refresh of the sidebar only: the session reloads on next open

    def maybe_title(self):
        """A short title after the first exchange, written by the model."""
        if self.session.get("title_generated") or cint(self.session.message_count) > 2:
            return
        try:
            from ai_agent.engine.events import BlockDone as _Done
            from ai_agent.engine.types import Message, ModelRequest

            provider, _name, model = provider_for(self.session.provider, self.session.model)
            user_text = frappe.db.get_value("AI Chat Message", self.msg.parent_message, "text") or ""
            answer = (self.message_text() or "")[:1500]
            req = ModelRequest(model=model, system="You name conversations.", system_dynamic="",
                               messages=[Message(role="user", blocks=[Block(kind=TEXT, text=f"Write a title of at most 6 words, in the language of the user, no quotes, no trailing punctuation, for this exchange.\n\nUser: {user_text[:800]}\n\nAssistant: {answer}")])],
                               tools=[], max_tokens=40, thinking="off")
            title = ""
            for ev in provider.stream(req, CancelToken()):
                if isinstance(ev, _Done) and ev.block.get("kind") == TEXT:
                    title += ev.block.get("text") or ""
            title = " ".join(title.strip().strip('"\'').split())[:80]
            if title:
                self.session.db_set({"title": title}, update_modified=False)
                frappe.db.commit()
        except Exception:
            pass   # cosmetic: the conversation keeps its default title


class _SessionUpdated:
    def __init__(self, session):
        self.session = session

    def to_dict(self):
        return {"type": "session.updated", "session": self.session}
