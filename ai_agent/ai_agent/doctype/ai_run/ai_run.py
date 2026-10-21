import frappe
from frappe.model.document import Document


class AIRun(Document):
    @staticmethod
    def clear_old_logs(days=180):
        """Log Settings retention. Only runs outside a conversation (MCP and API calls: one run per tool call);
        a chat keeps its runs as long as the conversation exists."""
        from frappe.query_builder import Interval
        from frappe.query_builder.functions import Now

        run = frappe.qb.DocType("AI Run")
        old = (
            frappe.qb.from_(run).select(run.name)
            .where(run.creation < (Now() - Interval(days=days)))
            .where((run.session.isnull() | (run.session == "")) & (run.chat_session.isnull() | (run.chat_session == "")))
            .limit(50000)
        ).run(pluck=True)
        for start in range(0, len(old), 1000):
            names = old[start:start + 1000]
            frappe.db.delete("AI Tool Call", {"run": ("in", names)})
            frappe.db.delete("AI Run", {"name": ("in", names)})

    def validate(self):
        if not self.user:
            self.user = frappe.session.user
        if not self.run_id:
            self.run_id = frappe.generate_hash(length=16)

    def before_insert(self):
        # Chat-session identity: follow-up runs inherit the chain's root and
        # session so conversations are queried by indexed fields instead of
        # walking parent_run links.
        if self.parent_run:
            parent = frappe.db.get_value(
                "AI Run", self.parent_run, ["root_run", "chat_session"], as_dict=True
            )
            if parent:
                if not self.root_run:
                    self.root_run = parent.root_run or self.parent_run
                if not self.chat_session:
                    self.chat_session = parent.chat_session

    def after_insert(self):
        if not self.root_run:
            self.db_set("root_run", self.name, update_modified=False)
        if self.session:
            # A run created by the chat engine: the session already exists and
            # its counters are maintained by the chat API, not by the run.
            return
        if self.chat_session:
            self._touch_chat_session()
        else:
            self._create_chat_session()

    def _create_chat_session(self):
        title = " ".join(str(self.prompt or "").split())[:80] or self.name
        # Runs that still come through this legacy path (MCP tool calls,
        # WhatsApp) are not Desk chats:
        # tag them so the chat workspace sidebar never lists them.
        surface = "MCP" if str(self.prompt or "").startswith("MCP tool call:") else "API"
        session = frappe.get_doc({
            "doctype": "AI Chat Session",
            "title": title,
            "surface": surface,
            "user": self.user,
            "agent": self.agent,
            "company": self.company,
            "status": "Active",
            "root_run": self.root_run or self.name,
            "last_run": self.name,
            "last_message_on": frappe.utils.now_datetime(),
            "message_count": 1,
        })
        session.insert(ignore_permissions=True)
        self.db_set("chat_session", session.name, update_modified=False)

    def _touch_chat_session(self):
        frappe.db.set_value(
            "AI Chat Session",
            self.chat_session,
            {
                "last_run": self.name,
                "last_message_on": frappe.utils.now_datetime(),
            },
            update_modified=False,
        )
        frappe.db.sql(
            "update `tabAI Chat Session` set message_count = message_count + 1 where name = %s",
            self.chat_session,
        )
