"""The job that produces one assistant message.

Wires provider, tools, skills, memory and history into the pure loop, then
consumes its events: blocks are persisted (batched), every event is published
over realtime with its payload, and the message / run / session records are
brought to their final state. Cancellation is checked between events through
a Redis flag set by the API plus the run's cancel_requested column.
"""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import cint, now_datetime

from ai_agent.engine import memory as memory_mod
from ai_agent.engine import skills as skills_mod
from ai_agent.engine.context import files as files_mod
from ai_agent.engine.context import history as history_mod
from ai_agent.engine.context import system_prompt
from ai_agent.engine.context.compactor import Compactor, budget_for
from ai_agent.engine.events import (
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
    SkillLoaded,
    ToolResultEvent,
    UsageEvent,
)
from ai_agent.engine.loop import Loop, LoopConfig, summarizer
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.providers.factory import provider_for
from ai_agent.engine.tools import ToolExecutor
from ai_agent.engine.types import TEXT, Block, Usage
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.run_control import run_timeout_seconds

REALTIME_EVENT = "ai_agent:chat"
JOB_PREFIX = "ai-chat-msg-"
CANCEL_KEY = "ai:chat:cancel:{}"
TAIL_KEY = "ai:chat:tail:{}"
FLUSH_SECONDS = 2.0
DELTA_COALESCE_CHARS = 24
DELTA_COALESCE_SECONDS = 0.06


def run_options(route):
    """`skill:x;thinking:adaptive` on AI Run.route -> dict."""
    out = {}
    for part in (route or "").split(";"):
        key, _, value = part.partition(":")
        if key and value:
            out[key.strip()] = value.strip()
    return out


def job_id(message):
    return f"{JOB_PREFIX}{message}"


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


def fail_orphaned_message(message, error):
    """The job died (worker restart, crash before the runner could report):
    settle the message so the chat does not wait forever."""
    doc = frappe.get_doc("AI Chat Message", message)
    if doc.status not in ("pending", "streaming"):
        return
    doc.db_set({"status": "failed", "error": error, "completed_at": now_datetime()}, update_modified=False)
    frappe.db.set_value("AI Message Block", {"message": doc.name, "status": "streaming"}, "status", "completed", update_modified=False)
    frappe.db.commit()
    pub = Publisher(doc.session, doc.name, doc.user, start_seq=cint(doc.event_seq))
    pub.publish(MessageFailed(error=error, kind="interrupted"))
    doc.db_set("event_seq", pub.seq, update_modified=False)
    frappe.db.commit()


def request_cancel(message):
    frappe.cache().set_value(CANCEL_KEY.format(message), "1", expires_in_sec=3600)


def execute_message(message, user=None, resume=False):
    doc = frappe.get_doc("AI Chat Message", message)
    if not doc.user or doc.user in ("Guest", "Administrator") and doc.user != user:
        pass
    if user and user != doc.user:
        frappe.throw("Message owner mismatch.", frappe.PermissionError)
    frappe.set_user(doc.user)
    Runner(doc).run(resume=resume)


class Publisher:
    """Realtime publisher with delta coalescing and a monotonic event sequence."""

    def __init__(self, session, message, user, start_seq=0):
        self.session, self.message, self.user = session, message, user
        self.seq = start_seq
        self._delta = None      # (block_seq, text, t0)

    def publish(self, event):
        if isinstance(event, BlockDelta):
            if self._delta and self._delta[0] == event.block_seq and len(self._delta[1]) < DELTA_COALESCE_CHARS and time.monotonic() - self._delta[2] < DELTA_COALESCE_SECONDS:
                self._delta = (event.block_seq, self._delta[1] + event.delta, self._delta[2])
                return
            self.flush()
            self._delta = (event.block_seq, event.delta, time.monotonic())
            if len(event.delta) >= DELTA_COALESCE_CHARS:
                self.flush()
            return
        self.flush()
        self._send(event.to_dict())

    def flush(self):
        if self._delta:
            block_seq, text, _ = self._delta
            self._delta = None
            self._send({"type": "block.delta", "block_seq": block_seq, "delta": text})

    def _send(self, payload):
        self.seq += 1
        try:
            frappe.publish_realtime(REALTIME_EVENT, {"session": self.session, "message": self.message, "seq": self.seq, "event": payload}, user=self.user)
        except Exception:
            pass
        # The live sequence rides in the delta tail so a resync (get_message) can
        # tell the client exactly which realtime events are already included.
        try:
            frappe.cache().hset(TAIL_KEY.format(self.message), "_seq", self.seq)
        except Exception:
            pass


class Runner:
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

    # -- setup ---------------------------------------------------------------------

    def cancel_token(self):
        cache = frappe.cache()
        key = CANCEL_KEY.format(self.msg.name)
        run_name = self.run_doc.name if self.run_doc else None

        def check():
            if cache.get_value(key):
                return True
            return bool(run_name and frappe.db.get_value("AI Run", run_name, "cancel_requested"))

        return CancelToken(check=check)

    def build(self, resume):
        provider, provider_name, model = provider_for(self.session.provider, self.session.model)
        caps = provider.caps(model)
        ctx = PermissionContext(self.user, self.agent.name, self.session.company)
        executor = ToolExecutor(ctx, self.agent, self.run_doc.name if self.run_doc else None, settings=self.settings)

        granted = skills_mod.granted(ctx)
        implicit, advertised = skills_mod.split(granted, ctx)
        catalog = ""
        if executor.allowed("metrics.query"):
            try:
                from ai_agent.metrics.catalog import metric_catalog_text

                catalog = metric_catalog_text() or ""
            except Exception:
                catalog = ""
        static = system_prompt.static_prompt(
            self.agent.agent_name or self.agent.name,
            agent_instructions=self.agent.system_instructions or "",
            site_instructions=self.site_instructions(),
            implicit_skills=implicit,
            advertised_skills=advertised,
            metric_catalog=catalog,
            tool_groups=executor.groups_for_prompt(),
        )

        user_msg = frappe.get_doc("AI Chat Message", self.msg.parent_message) if self.msg.parent_message else None
        prompt_text = (user_msg.text if user_msg else "") or ""
        document_context = ""
        if user_msg and user_msg.context_doctype and user_msg.context_name:
            try:
                from ai_agent.runtime.document_context import build as build_doc_context

                document_context = build_doc_context(user_msg.context_doctype, user_msg.context_name, self.user) or ""
            except Exception:
                document_context = ""
        dynamic = system_prompt.dynamic_prompt(
            self.user,
            frappe.db.get_value("User", self.user, "full_name"),
            frappe.utils.nowdate(),
            company=self.session.company,
            memory_text=memory_mod.recall(ctx, self.agent, prompt_text),
            document_context=document_context,
        )

        # History: every finished turn before this one, then the current user turn.
        exclude = {self.msg.name}
        if user_msg:
            exclude.add(user_msg.name)
        messages = history_mod.load(self.session.name, exclude=exclude)
        current_turn = []
        if user_msg:
            user_blocks = [Block(kind=TEXT, text=prompt_text)] if prompt_text else []
            user_blocks += self.attachment_blocks(user_msg.name, caps)
            current_turn.append(("user", user_msg.seq, user_msg.name, user_blocks))
        start_seq = 0
        if resume:
            own = frappe.get_all("AI Message Block", filters={"message": self.msg.name}, fields=["seq", "block_type", "status", "content", "content_json", "tool_call_id", "tool_name", "signature"], order_by="seq asc")
            own_blocks = [history_mod.block_from_row(r) for r in own]
            current_turn.append(("assistant", self.msg.seq, self.msg.name, own_blocks))
            start_seq = (max((b.seq for b in own_blocks), default=-1) + 1)
        messages = messages + history_mod.to_messages(current_turn)

        cancel = self.cancel_token()
        options = run_options(self.run_doc.route if self.run_doc else "")
        thinking = options.get("thinking") or self.agent.get("thinking") or "off"
        if not caps.supports_thinking:
            thinking = "off"
        config = LoopConfig(
            model=model,
            provider_name=provider_name,
            max_steps=cint(self.agent.maximum_steps) or cint(self.settings.maximum_steps) or 20,
            max_tool_calls=cint(self.agent.maximum_tool_calls) or cint(self.settings.maximum_tool_calls) or 40,
            max_tokens=min(caps.max_output_tokens or 8192, 16000),
            thinking=thinking,
            thinking_budget=cint(self.agent.get("thinking_budget_tokens")),
            effort=(self.agent.reasoning_effort or self.settings.get("default_reasoning_effort") or ""),
        )
        budget = budget_for(caps.context_window, config.max_tokens)
        compactor = Compactor(budget, summarize=summarizer(provider, model, cancel))
        loop = Loop(provider, executor, compactor, cancel, config, static, dynamic)
        self.caps = caps
        self.provider_name, self.model = provider_name, model
        return loop, executor, messages, start_seq

    def site_instructions(self):
        """Organisation instructions, plus the support desk's own prompt when
        this conversation comes from the floating support bubble."""
        parts = [self.settings.system_instructions or ""]
        if self.session.get("surface") == "Chatbot":
            try:
                from ai_agent.chatbot import settings as bot_settings

                parts.append(bot_settings.system_prompt())
                parts.append(
                    "You are answering inside a small floating support panel: keep replies short and practical, "
                    "one step at a time, and when the user has a document open (see 'Document the user has open') "
                    "assume their question is about it."
                )
            except Exception:
                pass
        return "\n\n".join(p for p in parts if p)

    def attachment_blocks(self, message_name, caps):
        """Attachment blocks of a user message. Read once (OCR is slow), then kept
        on the message as `document` blocks so later turns replay the same text
        from history instead of losing it. Native image/PDF payloads are not
        stored (size); a note points the model at files__read instead."""
        stored = frappe.get_all(
            "AI Message Block",
            filters={"message": message_name, "block_type": ("in", ("document", "image"))},
            fields=["seq", "block_type", "status", "content", "content_json", "tool_call_id", "tool_name", "signature"],
            order_by="seq asc",
        )
        if stored:
            return [history_mod.block_from_row(r) for r in stored]
        blocks = files_mod.blocks_for(message_name, caps)
        for i, b in enumerate(blocks, start=1):
            native = bool(b.data.get("data"))
            text = b.text if not native else f"[{b.name} was shown to the model as an {'image' if b.kind == 'image' else 'attached PDF'} when it was uploaded; call files__read to read its text again.]"
            doc = frappe.get_doc({"doctype": "AI Message Block", "message": message_name, "seq": i, "block_type": "document", "status": "completed", "tool_name": b.name or None})
            doc.insert(ignore_permissions=True)
            frappe.db.set_value("AI Message Block", doc.name, "content", text, update_modified=False)   # verbatim, no HTML sanitising
        if blocks:
            frappe.db.commit()
        return blocks

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
            for event in loop.run(messages, start_seq=start_seq, resumed=resume):
                self.handle(event)
        except Exception as exc:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"AI chat run {self.msg.name}")
            self.fail(f"Unexpected error: {frappe.utils.cstr(exc)[:300]}")
        finally:
            self.pub.flush()
            try:
                frappe.cache().delete_value(self.tail)
                frappe.cache().delete_value(CANCEL_KEY.format(self.msg.name))
            except Exception:
                pass

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
        elif isinstance(event, (ToolResultEvent, SkillLoaded, MemoryNote, ArtifactCreated)):
            self.insert_block(event.block_seq, event.block, extra={"duration_ms": getattr(event, "duration_ms", 0), "tool_call": getattr(event, "tool_call", "")})
            self.pub.publish(event)
            frappe.db.commit()
        elif isinstance(event, ApprovalRequested):
            self.insert_block(event.block_seq, event.block, extra={"approval": event.approval})
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
        # The compactor folded everything before the recent window; persist the
        # highest seq of a completed message older than the last 4 turns.
        rows = frappe.get_all("AI Chat Message", filters={"session": self.session.name, "status": "completed"}, fields=["seq"], order_by="seq desc", limit=6)
        return rows[-1].seq if len(rows) >= 6 else 0

    # -- block persistence ----------------------------------------------------------------

    def open_block(self, seq, kind, id="", name=""):
        doc = frappe.get_doc({
            "doctype": "AI Message Block",
            "message": self.msg.name,
            "seq": seq,
            "block_type": kind,
            "status": "streaming",
            "tool_call_id": id or None,
            "tool_name": name or None,
            "started_at": now_datetime(),
        })
        doc.insert(ignore_permissions=True)
        self.blocks[seq] = {"name": doc.name, "kind": kind, "text": "", "started": time.monotonic(), "dirty": False}

    def close_block(self, seq, block):
        entry = self.blocks.get(seq)
        if entry is None:
            self.insert_block(seq, block)
            return
        values = {
            "status": "failed" if block.get("is_error") else "completed",
            "content": block.get("text") or entry["text"],
            "completed_at": now_datetime(),
            "duration_ms": int((time.monotonic() - entry["started"]) * 1000),
        }
        if block.get("data"):
            values["content_json"] = json.dumps(block["data"], ensure_ascii=False, default=str)
        if block.get("signature"):
            values["signature"] = block["signature"]
        if block.get("name") and not entry.get("named"):
            values["tool_name"] = block["name"]
        if block.get("id"):
            values["tool_call_id"] = block["id"]
        frappe.db.set_value("AI Message Block", entry["name"], values, update_modified=False)
        entry["dirty"] = False
        entry["closed"] = True
        self.tail_delete(seq)
        frappe.db.commit()

    def insert_block(self, seq, block, extra=None):
        extra = extra or {}
        # Document.insert() would HTML-sanitize the content ("<<<UNTRUSTED_DATA" markers,
        # JSON with "<"), so text lands through db.set_value, which stores it verbatim.
        doc = frappe.get_doc({
            "doctype": "AI Message Block",
            "message": self.msg.name,
            "seq": seq,
            "block_type": block.get("kind"),
            "status": "failed" if block.get("is_error") else "completed",
            "tool_call_id": block.get("id") or None,
            "tool_name": block.get("name") or None,
            "tool_call": extra.get("tool_call") or None,
            "approval": extra.get("approval") or None,
            "artifact": (block.get("data") or {}).get("artifact") if block.get("kind") == "artifact" else None,
            "started_at": now_datetime(),
            "completed_at": now_datetime(),
            "duration_ms": cint(extra.get("duration_ms")),
        })
        doc.insert(ignore_permissions=True)
        frappe.db.set_value("AI Message Block", doc.name, {
            "content": block.get("text") or "",
            "content_json": json.dumps(block["data"], ensure_ascii=False, default=str) if block.get("data") else None,
        }, update_modified=False)
        self.blocks[seq] = {"name": doc.name, "kind": block.get("kind"), "text": block.get("text") or "", "started": time.monotonic(), "dirty": False, "closed": True}

    def maybe_flush(self, force=False):
        if not force and time.monotonic() - self.last_flush < FLUSH_SECONDS:
            return
        self.last_flush = time.monotonic()
        dirty = False
        for entry in self.blocks.values():
            if entry.get("dirty") and not entry.get("closed"):
                frappe.db.set_value("AI Message Block", entry["name"], "content", entry["text"], update_modified=False)
                entry["dirty"] = False
                dirty = True
        if dirty:
            frappe.db.commit()

    def tail_write(self, seq, text):
        try:
            frappe.cache().hset(self.tail, str(seq), text)
        except Exception:
            pass

    def tail_delete(self, seq):
        try:
            frappe.cache().hdel(self.tail, str(seq))
        except Exception:
            pass

    # -- terminal states --------------------------------------------------------------------

    def message_text(self):
        rows = frappe.get_all("AI Message Block", filters={"message": self.msg.name, "block_type": "text"}, fields=["content"], order_by="seq asc")
        return "\n\n".join(r.content for r in rows if r.content)

    def usage_values(self):
        return {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
            "estimated_cost": round(self.caps.cost(self.usage), 6) if hasattr(self, "caps") else 0,
            "context_tokens": self.context_tokens,
        }

    def close_message(self, status, event=None, error=None, run_status="Completed"):
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
                "error_summary": (error or "")[:500] or None,
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
        self.close_message("failed", error=error, run_status="Failed")
        self.pub.publish(MessageFailed(error=error, kind=kind, usage=self.usage.to_dict()))
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
            pass

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
            pass


class _SessionUpdated:
    def __init__(self, session):
        self.session = session

    def to_dict(self):
        return {"type": "session.updated", "session": self.session}
