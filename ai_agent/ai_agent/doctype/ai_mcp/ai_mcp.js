// The Connection field lists only the apps (OAuth clients) this user has connected with.
frappe.ui.form.on("AI MCP", {
	setup(frm) {
		frm.set_query("connection", () => ({
			query: "ai_agent.ai_agent.doctype.ai_mcp.ai_mcp.user_connections",
			filters: { user: frm.doc.user },
		}));
		frm.set_query("agent", () => ({ filters: { enabled: 1 } }));
	},
	user(frm) {
		// both belong to the chosen user: Frappe's default company is the admin's own
		frm.set_value("connection", null);
		frm.set_value("company", null);
	},
});
