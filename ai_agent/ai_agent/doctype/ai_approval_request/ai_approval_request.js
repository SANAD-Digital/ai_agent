// Approve / Reject in ERPNext: the place an AI connector (Claude, ChatGPT) sends the user when its host
// cannot show the approval card, and where Customize requests are always approved. Same path as the chat card.
frappe.ui.form.on("AI Approval Request", {
	refresh(frm) {
		if (frm.doc.status !== "Pending" || frm.is_new()) return;
		const decide = (decision, reason) =>
			frappe
				.call({
					method: "ai_agent.chat.api.decide_approval",
					args: { approval: frm.doc.name, decision, reason },
					freeze: true,
					freeze_message: decision === "approve" ? __("Executing…") : __("Rejecting…"),
				})
				.then((r) => {
					const message = (r.message && r.message.approval && r.message.approval.error) || "";
					if (message) frappe.msgprint({ title: __("The action failed"), message, indicator: "red" });
					else frappe.show_alert({ message: decision === "approve" ? __("Approved and executed") : __("Rejected"), indicator: "green" });
					frm.reload_doc();
				});

		frm.add_custom_button(__("Approve"), () =>
			frappe.confirm(__("Approve and execute this request?"), () => decide("approve"))
		).addClass("btn-primary");
		frm.add_custom_button(__("Reject"), () =>
			frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason") }, (v) => decide("reject", v.reason), __("Reject request"))
		);
	},
});
