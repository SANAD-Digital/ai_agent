// A non-Anthropic provider sends business data to a third party.
frappe.ui.form.on("AI Provider", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.provider_type && frm.doc.provider_type !== "Anthropic") {
			frm.set_intro(
				__("This provider is outside the managed (Anthropic) setup. It works only in Own Key mode with third-party consent in AI Settings, and data sent to it leaves to {0}.", [frm.doc.provider_type]),
				"orange"
			);
		}
	},
	provider_type(frm) {
		frm.trigger("refresh");
	},
});
