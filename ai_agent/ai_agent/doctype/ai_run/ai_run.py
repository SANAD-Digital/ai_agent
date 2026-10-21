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
            .where(run.chat_session.isnull() | (run.chat_session == ""))
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
        # A chat run belongs to its conversation (chat_session, counters kept by the chat API). MCP and API runs
        # have none: they no longer get a hidden chat session each (one per MCP tool call before).

def on_doctype_update():
    # The owner permission query runs on every list read; recovery and retention filter by status.
    frappe.db.add_index("AI Run", ["user", "creation"])
    frappe.db.add_index("AI Run", ["status", "modified"])
