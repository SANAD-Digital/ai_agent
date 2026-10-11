// The model is one of the chosen provider's models: the list follows the Provider field.
// Server side, the model is checked against the provider (factory.validate_provider_model).
function paint_provider_models(frm) {
	frm.set_query("provider", () => ({ filters: { enabled: 1 } }));
	if (!frm.doc.provider) {
		frm.set_df_property("model", "options", []);
		return;
	}
	frappe.db.get_doc("AI Provider", frm.doc.provider).then((p) => {
		const models = (p.models || []).filter((m) => !m.model_type || m.model_type === "Chat").map((m) => m.model_name);
		frm.set_df_property("model", "options", models);
		if (!frm.doc.model && p.default_model) frm.get_field("model").$input && frm.get_field("model").$input.attr("placeholder", p.default_model);
	});
}

frappe.ui.form.on("AI Agent", {
	refresh: paint_provider_models,
	provider(frm) {
		frm.set_value("model", "");
		paint_provider_models(frm);
	},
});
