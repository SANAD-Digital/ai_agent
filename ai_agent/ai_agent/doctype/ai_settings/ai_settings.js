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
		render_mcp_guide(frm);
	},
	provider_mode(frm) {
		frm.trigger("refresh");
	},
	allow_third_party_providers(frm) {
		frm.trigger("refresh");
	},
	enable_mcp: render_mcp_guide,
	mcp_agent: render_mcp_guide,
	mcp_enable_action_requests: render_mcp_guide,
});

const MCP_ENDPOINT_PATH = "/api/method/ai_agent.mcp.api.handle";

// Setup guide for every MCP client, built around this site's real endpoint.
// Strings go through __() so the page follows the user's language (see translations/ar.csv).
function render_mcp_guide(frm) {
	const field = frm.fields_dict.mcp_guide;
	if (!field) return;
	const url = frappe.urllib.get_base_url().replace(/\/$/, "") + MCP_ENDPOINT_PATH;
	const esc = frappe.utils.escape_html;
	const agent = frm.doc.mcp_agent || "ERP Analyst";
	const actions = frm.doc.mcp_enable_action_requests;
	const code = (text) => `<pre class="mcp-code" dir="ltr"><code>${esc(text)}</code></pre>`;
	const steps = (items) => `<ol class="mcp-steps">${items.map((i) => `<li>${i}</li>`).join("")}</ol>`;
	const note = (text, tone = "blue") => `<div class="mcp-note mcp-note-${tone}">${text}</div>`;
	const u = `<code dir="ltr">${esc(url)}</code>`;

	const tabs = [
		{
			id: "claude",
			title: "Claude",
			body:
				`<p>${__("Works in <b>claude.ai</b> and the Claude desktop and mobile apps, because connectors belong to your account.")}</p>` +
				steps([
					__("Open <b>Settings → Connectors</b>."),
					__("Click <b>Add custom connector</b>."),
					__("Enter a name such as <b>Sanad ERPNext</b> and this URL: {0}", [u]),
					__("Leave <b>Advanced settings</b> empty; Claude registers itself automatically."),
					__("Click <b>Add</b>, then <b>Connect</b>, sign in to the system and click <b>Allow</b>."),
					__("In a conversation, turn the connector on from the tools menu under the message box."),
				]) +
				note(__("On Team and Enterprise plans the organization owner adds the connector first in the organization settings, then each member clicks <b>Connect</b> with their own account.")) +
				note(__("Claude shows results as text and tables in its reply. To approve a request, reply <b>approve</b> (or <b>موافق</b>) in the chat."), "gray"),
		},
		{
			id: "chatgpt",
			title: "ChatGPT",
			body:
				steps([
					__("Turn on <b>Developer mode</b> in <b>Settings → Apps → Advanced settings</b>."),
					__("In <b>Settings → Apps</b>, click <b>Create app</b>."),
					__("Enter a name, set the URL to {0} and choose <b>OAuth</b> authentication.", [u]),
					__("Save, then sign in to the system and click <b>Allow</b>."),
					__("In a conversation, pick the app from the <b>+</b> button, then <b>Developer mode</b>."),
				]) +
				note(__("<a href=\"/app/oauth-settings\">OAuth Settings</a> must list {0} in <b>Allowed Public Client Origins</b>.", ['<code dir="ltr">https://chatgpt.com</code>']), "orange") +
				note(__("ChatGPT shows tables, charts and approval cards with buttons inside the conversation."), "gray"),
		},
		{
			id: "code",
			title: "Claude Code",
			body:
				`<p>${__("Add the server from the terminal:")}</p>` +
				code(`claude mcp add --transport http sanad-erpnext ${url}`) +
				`<p>${__("Then type {0} in Claude Code and choose the server to sign in.", ['<code dir="ltr">/mcp</code>'])}</p>`,
		},
		{
			id: "editors",
			title: __("Cursor, VS Code and opencode"),
			body:
				`<p>${__("<b>Cursor</b> — in the file {0}:", ['<code dir="ltr">~/.cursor/mcp.json</code>'])}</p>` +
				code(JSON.stringify({ mcpServers: { "sanad-erpnext": { url } } }, null, 2)) +
				`<p>${__("<b>VS Code (Copilot)</b> — in the file {0}:", ['<code dir="ltr">.vscode/mcp.json</code>'])}</p>` +
				code(JSON.stringify({ servers: { "sanad-erpnext": { type: "http", url } } }, null, 2)) +
				`<p>${__("<b>opencode</b> — in the file {0}:", ['<code dir="ltr">opencode.json</code>'])}</p>` +
				code(JSON.stringify({ mcp: { "sanad-erpnext": { type: "remote", url, enabled: true } } }, null, 2)) +
				note(__("These tools open the sign-in page in your browser on first use. If a tool does not support sign-in, use the API key method in the next tab.")),
		},
		{
			id: "token",
			title: __("Other tools (API key)"),
			body:
				`<p>${__("For any tool that supports MCP over HTTP but not sign-in (OAuth), use a per-user API key:")}</p>` +
				steps([
					__("Open the user's page in the system, then <b>Settings → API Access → Generate Keys</b>."),
					__("Copy the <b>API Key</b> and <b>API Secret</b>; the secret is shown only once."),
					__("In the tool, set the URL to {0} and add this header:", [u]),
				]) +
				code("Authorization: token API_KEY:API_SECRET") +
				note(__("The key carries all of its owner's permissions. Do not use an administrator's key, and do not share a key between people. You can revoke it at any time by generating a new one."), "orange"),
		},
		{
			id: "help",
			title: __("Troubleshooting"),
			body: `<table class="table table-bordered mcp-table"><thead><tr><th>${__("Problem")}</th><th>${__("Cause and fix")}</th></tr></thead><tbody>${[
				[__("The sign-in page does not appear"), __("Make sure <a href=\"/app/oauth-settings\">OAuth Settings</a> has Show Auth Server Metadata, Show Protected Resource Metadata and Enable Dynamic Client Registration turned on.")],
				['"The authenticated user is not allowed to use MCP"', __("Give the user one of the roles in <b>Allowed MCP Roles</b> (<b>AI MCP User</b> by default).")],
				['"MCP server is disabled"', __("Turn on <b>Enable MCP Server</b> on this page and save.")],
				['"MCP request rate limit exceeded"', __("Raise <b>MCP Requests per Minute</b>; the limit is per user per minute.")],
				[__("New tools do not appear after a system update"), __("Disconnect the connector in the tool and connect it again so it fetches the tool list again.")],
				[__("It cannot create documents"), __("Turn on <b>Enable MCP Action Requests</b> and choose an agent that allows actions. Every change waits for your approval.")],
			].map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</tbody></table>`,
		},
	];

	const status = [
		[frm.doc.enable_mcp, frm.doc.enable_mcp ? __("Server enabled") : __("Server disabled")],
		[true, __("Agent: {0}", [`<b>${esc(agent)}</b>`])],
		[true, actions ? __("Read + approval-gated action requests") : __("Read only")],
	]
		.map(([ok, text]) => `<span class="mcp-chip ${ok ? "ok" : "off"}">${text}</span>`)
		.join("");

	field.$wrapper.html(`
		<style>
			.mcp-guide { max-width: 900px; text-align: start; }
			.mcp-guide .mcp-url { display: flex; gap: 8px; align-items: center; margin: 4px 0 12px; }
			.mcp-guide .mcp-url code { flex: 1; padding: 8px 10px; border-radius: 8px; background: var(--control-bg); overflow-x: auto; white-space: nowrap; }
			.mcp-guide .mcp-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
			.mcp-guide .mcp-chip { font-size: 12px; padding: 3px 10px; border-radius: 999px; background: var(--bg-green); color: var(--text-on-green); }
			.mcp-guide .mcp-chip.off { background: var(--bg-orange); color: var(--text-on-orange); }
			.mcp-guide .mcp-tabs { display: flex; flex-wrap: wrap; gap: 6px; border-bottom: 1px solid var(--border-color); margin-bottom: 12px; }
			.mcp-guide .mcp-tab { border: 0; background: none; padding: 8px 12px; border-bottom: 2px solid transparent; color: var(--text-muted); font-weight: 500; }
			.mcp-guide .mcp-tab.active { color: var(--text-color); border-bottom-color: var(--primary); }
			.mcp-guide .mcp-panel { display: none; line-height: 1.8; }
			.mcp-guide .mcp-panel.active { display: block; }
			.mcp-guide .mcp-steps { padding-inline-start: 22px; margin-bottom: 10px; }
			.mcp-guide .mcp-steps li { margin-bottom: 4px; }
			.mcp-guide .mcp-code { direction: ltr; text-align: left; background: var(--control-bg); border-radius: 8px; padding: 10px 12px; font-size: 12.5px; white-space: pre-wrap; word-break: break-all; }
			.mcp-guide .mcp-note { padding: 8px 12px; border-radius: 8px; margin: 8px 0; font-size: 13px; }
			.mcp-guide .mcp-note-blue { background: var(--bg-blue); color: var(--text-on-blue); }
			.mcp-guide .mcp-note-orange { background: var(--bg-orange); color: var(--text-on-orange); }
			.mcp-guide .mcp-note-gray { background: var(--control-bg); color: var(--text-muted); }
			.mcp-guide code { direction: ltr; unicode-bidi: isolate; }
			.mcp-guide .mcp-table td, .mcp-guide .mcp-table th { text-align: start; vertical-align: top; }
		</style>
		<div class="mcp-guide">
			<div class="text-muted small">${__("This site's MCP server URL:")}</div>
			<div class="mcp-url"><code dir="ltr">${esc(url)}</code>
				<button class="btn btn-default btn-sm mcp-copy">${__("Copy")}</button></div>
			<div class="mcp-chips">${status}</div>
			<p class="text-muted small">${__("Each user signs in with their own account and sees or changes only what their permissions allow. They must have one of the roles in <b>Allowed MCP Roles</b>.")}</p>
			<div class="mcp-tabs">${tabs.map((t, i) => `<button class="mcp-tab${i ? "" : " active"}" data-tab="${t.id}">${t.title}</button>`).join("")}</div>
			${tabs.map((t, i) => `<div class="mcp-panel${i ? "" : " active"}" data-panel="${t.id}">${t.body}</div>`).join("")}
		</div>`);

	field.$wrapper.find(".mcp-copy").on("click", () => frappe.utils.copy_to_clipboard(url));
	field.$wrapper.find(".mcp-tab").on("click", function () {
		const id = $(this).data("tab");
		field.$wrapper.find(".mcp-tab").removeClass("active").filter(`[data-tab="${id}"]`).addClass("active");
		field.$wrapper.find(".mcp-panel").removeClass("active").filter(`[data-panel="${id}"]`).addClass("active");
	});
}
