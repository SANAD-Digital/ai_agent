from frappe.model.document import Document


class FinancialAttachmentSession(Document):
    def validate(self):
        self.page_count = len(self.pages or [])
        self.file_count = len(self.files or [])
        self.selected_page_count = len([row for row in (self.pages or []) if row.selected])
        self.read_page_count = len([row for row in (self.pages or []) if row.status == "Read"])

