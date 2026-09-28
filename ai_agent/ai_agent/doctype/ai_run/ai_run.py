import frappe
from frappe.model.document import Document


class AIRun(Document):
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
        if self.chat_session:
            self._touch_chat_session()
        else:
            self._create_chat_session()

    def _create_chat_session(self):
        title = " ".join(str(self.prompt or "").split())[:80] or self.name
        session = frappe.get_doc({
            "doctype": "AI Chat Session",
            "title": title,
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
