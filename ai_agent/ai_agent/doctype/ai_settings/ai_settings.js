// Warn whenever business data can leave to a non-Anthropic provider.
frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.provider_mode === "Own Key" && frm.doc.allow_third_party_providers) {
			frm.set_intro(
				__("Own Key mode with third-party providers: questions, tool results and attachments are sent to the selected provider (e.g. DeepSeek, Gemini)."),
				"orange"
			);
		}
	},
	provider_mode(frm) {
		frm.trigger("refresh");
	},
	allow_third_party_providers(frm) {
		frm.trigger("refresh");
	},
});
