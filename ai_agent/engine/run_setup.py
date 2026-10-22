"""What one assistant message runs with: provider and model, tools, skills, the system prompt, memory,
history and attachments, wired into a ``Loop``. A mixin of ``runner.Runner``: it uses ``self.msg``,
``self.session``, ``self.run_doc``, ``self.agent``, ``self.settings`` and ``self.user``, and sets
``self.caps``, ``self.provider_name`` and ``self.model``."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from ai_agent.engine import memory as memory_mod
from ai_agent.engine import skills as skills_mod
from ai_agent.engine.context import files as files_mod
from ai_agent.engine.context import history as history_mod
from ai_agent.engine.context import system_prompt
from ai_agent.engine.context.compactor import Compactor, budget_for
from ai_agent.engine.loop import Loop, LoopConfig, summarizer
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.providers.factory import provider_for
from ai_agent.engine.tools import ToolExecutor
from ai_agent.engine.types import TEXT, Block
from ai_agent.runtime.permission_context import PermissionContext

CANCEL_KEY = "ai:chat:cancel:{}"


def run_options(route):
    """`skill:x;thinking:adaptive` on AI Run.route -> dict."""
    out = {}
    for part in (route or "").split(";"):
        key, _, value = part.partition(":")
        if key and value:
            out[key.strip()] = value.strip()
    return out


class RunSetup:
    def cancel_token(self):
        cache = frappe.cache
        key = CANCEL_KEY.format(self.msg.name)
        run_name = self.run_doc.name if self.run_doc else None

        def check():
            # use_local_cache=False: the first miss is otherwise kept in frappe.local.cache for the whole job,
            # and the Stop button's flag (set by the web worker) is never seen.
            if cache.get_value(key, use_local_cache=False):
                return True
            return bool(run_name and frappe.db.get_value("AI Run", run_name, "cancel_requested"))

        return CancelToken(check=check)

    def build(self, resume):
        provider, provider_name, model = provider_for(*(getattr(self, "override", None) or (self.session.provider, self.session.model)))
        caps = provider.caps(model)
        ctx = PermissionContext(self.user, self.agent.name, self.session.company)
        executor = ToolExecutor(ctx, self.agent, self.run_doc.name if self.run_doc else None, settings=self.settings, session=self.session.name)

        granted = skills_mod.granted(ctx)
        implicit, advertised = skills_mod.split(granted, ctx)
        catalog = ""
        if executor.allowed("metrics.query"):
            try:
                from ai_agent.metrics.catalog import catalog_text as metric_catalog_text

                catalog = metric_catalog_text() or ""
            except Exception:
                frappe.log_error(frappe.get_traceback(), "AI chat: metric catalog for the prompt")
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
                frappe.log_error(frappe.get_traceback(), f"AI chat: document context {user_msg.context_doctype} {user_msg.context_name}")
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
            # The model row's Max Output Tokens (AI Provider > Models), else the family default.
            max_tokens=cint(caps.max_output_tokens) or 8192,
            thinking=thinking,
            thinking_budget=cint(self.agent.get("thinking_budget_tokens")),
            effort=(self.agent.reasoning_effort or self.settings.get("default_reasoning_effort") or ""),
        )
        # A long possible reply must not starve the history: reserve at most a quarter of the window for it.
        budget = budget_for(caps.context_window, min(config.max_tokens, (caps.context_window or 0) // 4 or config.max_tokens))
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
                frappe.log_error(frappe.get_traceback(), "AI chat: chatbot system prompt")
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
