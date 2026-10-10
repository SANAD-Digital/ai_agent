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
const MCP_SETUP = "ai_agent.mcp.setup.";

// MCP setup panel: readiness checks with one-click fixes, per-client connect steps
// with copy/open buttons, and the apps connected to this site.
// Strings go through __() so the page follows the user's language (see translations/ar.csv).
function render_mcp_guide(frm) {
	const field = frm.fields_dict.mcp_guide;
	if (!field || frm.is_new()) return;
	frappe.call(MCP_SETUP + "get_status").then((r) => draw_mcp_panel(frm, field.$wrapper, r.message));
}

function draw_mcp_panel(frm, $w, st) {
	const esc = frappe.utils.escape_html;
	const base = frappe.urllib.get_base_url().replace(/\/$/, "");
	const url = base + MCP_ENDPOINT_PATH;
	const c = st.checks;
	const ready = Object.values(c).every(Boolean);
	const icon = (ok) => `<span class="mcp-dot ${ok ? "ok" : "bad"}">${ok ? "✓" : "!"}</span>`;
	const btn = (action, label, style = "default", extra = "") =>
		`<button class="btn btn-${style} btn-xs" data-act="${action}" ${extra}>${label}</button>`;

	const checks = [
		["mcp_enabled", __("MCP server"), __("Lets external AI tools connect to this site."), __("Enable")],
		["oauth", __("Sign-in for AI tools"), __("Claude, ChatGPT and other tools sign in with each user's own account."), __("Enable")],
		["chatgpt_origin", __("ChatGPT access"), __("Allows ChatGPT to complete the sign-in."), __("Allow")],
		["my_role", __("Your access"), __("Your account has an MCP role, so you can connect."), __("Give me access")],
	]
		.map(([key, title, hint, fix]) => `
			<div class="mcp-row">${icon(c[key])}
				<div class="mcp-row-text"><div class="mcp-row-title">${title}</div><div class="mcp-row-hint">${hint}</div></div>
				${c[key] ? `<span class="mcp-ready">${__("Ready")}</span>` : btn("fix:" + key, fix, "primary")}
			</div>`)
		.join("");

	const clients = {
		claude: {
			name: "Claude",
			steps: [
				__("Click <b>Open Claude settings</b>, then <b>Add custom connector</b>."),
				__("Paste the link, click <b>Add</b>, then <b>Connect</b>."),
				__("Sign in and click <b>Allow</b>. To approve requests later, reply <b>approve</b> in the chat."),
			],
			actions: btn("copy:url", __("Copy link"), "primary") + `<a class="btn btn-default btn-xs" target="_blank" rel="noopener" href="https://claude.ai/settings/connectors">${__("Open Claude settings")} ↗</a>`,
		},
		chatgpt: {
			name: "ChatGPT",
			warn: !c.chatgpt_origin,
			steps: [
				__("In ChatGPT open <b>Settings → Apps → Advanced settings</b> and turn on <b>Developer mode</b>."),
				__("Click <b>Create app</b>, paste the link and choose <b>OAuth</b>."),
				__("Sign in and click <b>Allow</b>."),
			],
			actions: btn("copy:url", __("Copy link"), "primary") + `<a class="btn btn-default btn-xs" target="_blank" rel="noopener" href="https://chatgpt.com">${__("Open ChatGPT")} ↗</a>`,
		},
		code: {
			name: "Claude Code",
			steps: [__("Copy the command and run it in the terminal."), __("In Claude Code type <b>/mcp</b> and choose the server to sign in.")],
			copy: `claude mcp add --transport http sanad-erpnext ${url}`,
		},
		cursor: {
			name: "Cursor",
			steps: [__("Copy the settings into <b>~/.cursor/mcp.json</b>."), __("Restart Cursor; it opens the sign-in page.")],
			copy: JSON.stringify({ mcpServers: { "sanad-erpnext": { url } } }, null, 2),
		},
		vscode: {
			name: "VS Code",
			steps: [__("Copy the settings into <b>.vscode/mcp.json</b>."), __("Click <b>Start</b> above the server name and sign in.")],
			copy: JSON.stringify({ servers: { "sanad-erpnext": { type: "http", url } } }, null, 2),
		},
		opencode: {
			name: "opencode",
			steps: [__("Copy the settings into <b>opencode.json</b>."), __("Restart opencode; it opens the sign-in page.")],
			copy: JSON.stringify({ mcp: { "sanad-erpnext": { type: "remote", url, enabled: true } } }, null, 2),
		},
		other: {
			name: __("Other tools"),
			steps: [
				__("For tools without sign-in: open your user page and click <b>Generate Keys</b> under API access."),
				__("In the tool, paste the link and add the header below with your key and secret."),
			],
			copy: "Authorization: token API_KEY:API_SECRET",
			actions: `<a class="btn btn-default btn-xs" href="/app/user/${encodeURIComponent(frappe.session.user)}">${__("Open my user page")}</a>`,
		},
	};
	const current = $w.data("mcp-client") || "claude";
	const cl = clients[current];
	const clientPanel = `
		${cl.warn ? `<div class="mcp-warn">${__("ChatGPT access is not allowed yet.")} ${btn("fix:chatgpt_origin", __("Allow"), "primary")}</div>` : ""}
		<ol class="mcp-steps">${cl.steps.map((x) => `<li>${x}</li>`).join("")}</ol>
		${cl.copy ? `<pre class="mcp-code" dir="ltr">${esc(cl.copy)}</pre>` : ""}
		<div class="mcp-actions">${cl.copy ? btn("copy:client", __("Copy"), "primary") : ""}${cl.actions || ""}</div>`;

	const apps = st.clients.length
		? st.clients
				.map((a) => `
				<div class="mcp-row">${icon(true)}
					<div class="mcp-row-text"><div class="mcp-row-title">${esc(a.app_name)}</div>
						<div class="mcp-row-hint">${esc(a.users.join("، "))} · ${__("Last used {0}", [frappe.datetime.comment_when(a.last_used)])}</div></div>
					${btn("revoke", __("Disconnect"), "default", `data-client="${esc(a.client)}" data-name="${esc(a.app_name)}"`)}
				</div>`)
				.join("")
		: `<div class="mcp-empty">${__("No app is connected yet.")}</div>`;

	$w.html(`
		<style>
			.mcp-panel { display: grid; gap: 14px; max-width: 980px; }
			.mcp-card { border: 1px solid var(--border-color); border-radius: 12px; padding: 14px 16px; background: var(--card-bg); }
			.mcp-card h5 { margin: 0 0 10px; font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
			.mcp-card h5 .btn { margin-inline-start: auto; }
			.mcp-url { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
			.mcp-url code { flex: 1; min-width: 240px; padding: 7px 10px; border-radius: 8px; background: var(--control-bg); white-space: nowrap; overflow-x: auto; }
			.mcp-test { font-size: 12.5px; margin-top: 8px; }
			.mcp-row { display: flex; align-items: center; gap: 12px; padding: 9px 0; border-top: 1px solid var(--border-color); }
			.mcp-row:first-of-type { border-top: 0; }
			.mcp-row-text { flex: 1; min-width: 0; }
			.mcp-row-title { font-weight: 500; }
			.mcp-row-hint { font-size: 12px; color: var(--text-muted); }
			.mcp-dot { width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: 12px; font-weight: 700; flex: none; }
			.mcp-dot.ok { background: var(--bg-green); color: var(--text-on-green); }
			.mcp-dot.bad { background: var(--bg-orange); color: var(--text-on-orange); }
			.mcp-ready { font-size: 12px; color: var(--text-on-green); }
			.mcp-clients { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
			.mcp-client { border: 1px solid var(--border-color); background: var(--control-bg); border-radius: 999px; padding: 4px 12px; font-size: 13px; }
			.mcp-client.active { background: var(--primary); border-color: var(--primary); color: #fff; }
			.mcp-steps { padding-inline-start: 20px; margin: 0 0 10px; line-height: 1.9; }
			.mcp-code { direction: ltr; text-align: left; background: var(--control-bg); border-radius: 8px; padding: 10px 12px; font-size: 12px; white-space: pre-wrap; word-break: break-all; margin-bottom: 10px; }
			.mcp-actions { display: flex; gap: 8px; flex-wrap: wrap; }
			.mcp-warn { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 8px; background: var(--bg-orange); color: var(--text-on-orange); margin-bottom: 10px; font-size: 13px; }
			.mcp-empty { color: var(--text-muted); font-size: 13px; }
			.mcp-panel code { direction: ltr; unicode-bidi: isolate; }
		</style>
		<div class="mcp-panel">
			<div class="mcp-card">
				<h5>${__("Connection link")}<span class="indicator-pill ${ready ? "green" : "orange"}">${ready ? __("Ready to connect") : __("Setup incomplete")}</span></h5>
				<div class="mcp-url"><code dir="ltr">${esc(url)}</code>${btn("copy:url", __("Copy"))}${btn("test", __("Test connection"))}</div>
				<div class="mcp-test text-muted">${__("Agent: {0}", [`<b>${esc(st.agent)}</b>`])} · ${st.action_requests ? __("Read + approval-gated action requests") : __("Read only")}</div>
			</div>
			<div class="mcp-card">
				<h5>${__("Site readiness")}${ready ? "" : btn("fix:all", __("Fix all"), "primary")}</h5>
				${checks}
				<div class="mcp-row">${icon(st.role_users > 0)}
					<div class="mcp-row-text"><div class="mcp-row-title">${__("Users")}</div>
						<div class="mcp-row-hint">${__("{0} users can connect. Their ERP permissions stay the same.", [st.role_users])}</div></div>
					${btn("grant", __("Add user"))}
				</div>
			</div>
			<div class="mcp-card">
				<h5>${__("Connect an AI tool")}</h5>
				<div class="mcp-clients">${Object.entries(clients).map(([k, v]) => `<button class="mcp-client ${k === current ? "active" : ""}" data-client-tab="${k}">${v.name}</button>`).join("")}</div>
				${clientPanel}
			</div>
			<div class="mcp-card">
				<h5>${__("Connected apps")}</h5>
				${apps}
			</div>
		</div>`);

	const after = (r) => {
		frm.reload_doc();
		draw_mcp_panel(frm, $w, r.message);
		frappe.show_alert({ message: __("Done"), indicator: "green" });
	};
	$w.find("[data-client-tab]").on("click", function () {
		$w.data("mcp-client", $(this).data("client-tab"));
		draw_mcp_panel(frm, $w, st);
	});
	$w.find("[data-act]").on("click", function () {
		const act = $(this).data("act");
		if (act === "copy:url") return frappe.utils.copy_to_clipboard(url);
		if (act === "copy:client") return frappe.utils.copy_to_clipboard(cl.copy);
		if (act.startsWith("fix:")) return frappe.call({ method: MCP_SETUP + "fix", args: { item: act.slice(4) }, freeze: true }).then(after);
		if (act === "test") return test_mcp_connection($w, url);
		if (act === "grant") {
			return frappe.prompt(
				{ fieldname: "user", fieldtype: "Link", options: "User", label: __("User"), reqd: 1, get_query: () => ({ filters: { enabled: 1, user_type: "System User" } }) },
				(v) => frappe.call({ method: MCP_SETUP + "grant_role", args: { user: v.user } }).then(after),
				__("Give MCP access"),
				__("Give access")
			);
		}
		if (act === "revoke") {
			const name = $(this).data("name");
			return frappe.confirm(__("Disconnect {0}? Its users must sign in again.", [`<b>${esc(name)}</b>`]), () =>
				frappe.call({ method: MCP_SETUP + "revoke_client", args: { client: $(this).data("client") } }).then(after)
			);
		}
	});
}

// Checks what an AI tool sees before signing in: the endpoint asks for sign-in (401)
// and the sign-in metadata points back to this endpoint.
async function test_mcp_connection($w, url) {
	const $out = $w.find(".mcp-test");
	$out.html(__("Testing…"));
	try {
		const r = await fetch(url, { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" }, body: '{"jsonrpc":"2.0","id":1,"method":"ping"}' });
		const meta = await fetch("/.well-known/oauth-protected-resource" + MCP_ENDPOINT_PATH, { credentials: "omit" }).then((x) => (x.ok ? x.json() : null));
		const ok = r.status === 401 && meta && meta.resource === url;
		$out.html(ok
			? `<span class="text-success">✓ ${__("The server responds and asks AI tools to sign in. You can connect.")}</span>`
			: `<span class="text-danger">✗ ${__("The server did not respond as expected (status {0}). Check the items above or ask your system administrator.", [r.status])}</span>`);
	} catch (e) {
		$out.html(`<span class="text-danger">✗ ${__("Could not reach the server.")}</span>`);
	}
}
