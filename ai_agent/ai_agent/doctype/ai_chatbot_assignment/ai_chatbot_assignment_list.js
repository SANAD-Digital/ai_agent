// The site's defaults (AI Chatbot Settings) apply to every user without an assignment.
frappe.listview_settings["AI Chatbot Assignment"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Default Settings"), () => frappe.set_route("Form", "AI Chatbot Settings"));
	},
};
