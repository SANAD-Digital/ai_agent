// Client Script checker (docs/customization/01 §3.3.3). Reads one JSON object on stdin:
//   {"source", "acorn": "<path to acorn.js>", "view": "Form"|"List", "doctype", "child_doctypes": [...]}
// and writes {"findings": [{code, level, line, column, data}], "methods": [{method, line}], "targets": [...]}.
// Messages are built in Python (translated); this file only reports codes and data.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

function readStdin() {
	return new Promise((resolve, reject) => {
		let data = "";
		process.stdin.setEncoding("utf8");
		process.stdin.on("data", (chunk) => (data += chunk));
		process.stdin.on("end", () => resolve(data));
		process.stdin.on("error", reject);
	});
}

const NETWORK_CALLS = new Set(["fetch", "$.ajax", "$.get", "$.post", "$.getJSON", "jQuery.ajax", "jQuery.get",
	"jQuery.post", "navigator.sendBeacon", "window.fetch"]);
const NETWORK_CONSTRUCTORS = new Set(["WebSocket", "EventSource", "window.WebSocket", "window.EventSource"]);
const STORAGE = new Set(["localStorage", "sessionStorage", "window.localStorage", "window.sessionStorage",
	"document.cookie", "frappe.csrf_token", "indexedDB", "window.indexedDB"]);
const LOCATION = new Set(["location", "window.location", "location.href", "window.location.href",
	"document.location", "document.location.href", "top.location", "window.top.location"]);
const NAVIGATE_CALLS = new Set(["location.assign", "location.replace", "window.location.assign",
	"window.location.replace", "window.open"]);
const CALL_METHODS = new Set(["frappe.call", "frappe.xcall"]);

function dotted(node) {
	const parts = [];
	while (node && node.type === "MemberExpression") {
		if (node.computed) {
			if (node.property.type === "Literal" && typeof node.property.value === "string") parts.push(node.property.value);
			else return null;
		} else parts.push(node.property.name);
		node = node.object;
	}
	if (node && node.type === "Identifier") {
		parts.push(node.name);
		return parts.reverse().join(".");
	}
	if (node && node.type === "ThisExpression") {
		parts.push("this");
		return parts.reverse().join(".");
	}
	return null;
}

function literalString(node) {
	if (!node) return null;
	if (node.type === "Literal" && typeof node.value === "string") return node.value;
	if (node.type === "TemplateLiteral" && node.expressions.length === 0) return node.quasis.map((q) => q.value.cooked).join("");
	return null;
}

function isStringish(node) {
	if (!node) return false;
	if (literalString(node) !== null) return true;
	if (node.type === "TemplateLiteral") return true;
	if (node.type === "BinaryExpression" && node.operator === "+") return isStringish(node.left) || isStringish(node.right);
	return false;
}

// "ok" = relative /api/ URL; "absolute"; "other" = relative non-API path; "unknown" = not a literal.
function classifyUrl(node) {
	let value = literalString(node);
	if (value === null && node && node.type === "TemplateLiteral") value = node.quasis[0].value.cooked || "";
	if (value === null && node && node.type === "BinaryExpression") {
		let left = node;
		while (left.type === "BinaryExpression") left = left.left;
		value = literalString(left);
		if (value === null) return "unknown";
		if (/^\s*([a-z][a-z0-9+.-]*:|\/\/)/i.test(value)) return "absolute";
		return value.startsWith("/api/") ? "ok" : "other";
	}
	if (value === null) return "unknown";
	if (/^\s*([a-z][a-z0-9+.-]*:|\/\/)/i.test(value)) return "absolute";
	if (value.startsWith("/api/")) return "ok";
	return "other";
}

function walk(node, visit, parent) {
	if (!node || typeof node.type !== "string") return;
	visit(node, parent);
	for (const key of Object.keys(node)) {
		if (key === "loc" || key === "start" || key === "end") continue;
		const child = node[key];
		if (Array.isArray(child)) child.forEach((c) => c && typeof c.type === "string" && walk(c, visit, node));
		else if (child && typeof child.type === "string") walk(child, visit, node);
	}
}

function propValue(objectExpression, name) {
	if (!objectExpression || objectExpression.type !== "ObjectExpression") return undefined;
	for (const prop of objectExpression.properties) {
		if (prop.type !== "Property") continue;
		const key = prop.key.type === "Identifier" ? prop.key.name : prop.key.value;
		if (key === name) return prop.value;
	}
	return undefined;
}

async function main() {
	const input = JSON.parse(await readStdin());
	const acorn = require(input.acorn);
	const out = { findings: [], methods: [], targets: [] };
	const add = (code, level, node, data) =>
		out.findings.push({ code, level, line: node && node.loc ? node.loc.start.line : null,
			column: node && node.loc ? node.loc.start.column + 1 : null, data: data || {} });

	let ast;
	const options = { sourceType: "script", allowReturnOutsideFunction: true, locations: true };
	try {
		// acorn 7 (the one Frappe ships) loops forever on ecmaVersion "latest"; 2021 is its newest.
		const major = parseInt(String(acorn.version || "7").split(".")[0], 10);
		ast = acorn.parse(input.source, { ...options, ecmaVersion: major >= 8 ? "latest" : 2021 });
	} catch (e) {
		const loc = e.loc || {};
		out.findings.push({ code: "js.syntax", level: "critical", line: loc.line || null,
			column: loc.column != null ? loc.column + 1 : null, data: { error: String(e.message || e) } });
		process.stdout.write(JSON.stringify(out));
		return;
	}

	const doctype = input.doctype;
	const allowedTargets = new Set([doctype, ...(input.child_doctypes || [])]);
	let listTarget = false;

	walk(ast, (node, parent) => {
		if (node.type === "ImportExpression") add("js.dynamic_code", "critical", node, { what: "import()" });

		if (node.type === "CallExpression" || node.type === "NewExpression") {
			const callee = dotted(node.callee);
			const first = node.arguments[0];
			if (node.type === "CallExpression" && callee === "frappe.ui.form.on") {
				const target = literalString(first);
				out.targets.push(target);
				if (input.view === "Form" && (target === null || !allowedTargets.has(target)))
					add("js.form_target", "critical", node, { target: target === null ? "?" : target });
			}
			if (callee === "eval" || callee === "window.eval" || callee === "Function" || callee === "window.Function")
				add("js.dynamic_code", "critical", node, { what: callee });
			if ((callee === "setTimeout" || callee === "setInterval" || callee === "window.setTimeout" || callee === "window.setInterval") && isStringish(first))
				add("js.dynamic_code", "critical", node, { what: `${callee}(string)` });
			if (node.type === "CallExpression" && NETWORK_CALLS.has(callee)) {
				const urlNode = callee.endsWith("ajax") && first && first.type === "ObjectExpression" ? propValue(first, "url") : first;
				const kind = classifyUrl(urlNode);
				if (kind !== "ok") add("js.network", "critical", node, { call: callee, url: literalString(urlNode) || "", kind });
			}
			if (NETWORK_CONSTRUCTORS.has(callee)) add("js.network", "critical", node, { call: callee, url: literalString(first) || "", kind: "socket" });
			if (callee === "XMLHttpRequest" || callee === "window.XMLHttpRequest") add("js.network", "critical", node, { call: "XMLHttpRequest", url: "", kind: "xhr" });
			if (node.type === "CallExpression" && NAVIGATE_CALLS.has(callee)) {
				const kind = classifyUrl(first);
				if (kind === "absolute" || kind === "unknown") add("js.navigation", "critical", node, { call: callee, url: literalString(first) || "" });
			}
			if (node.type === "CallExpression" && callee && callee.endsWith("insertAdjacentHTML") && node.arguments[1] && literalString(node.arguments[1]) === null)
				add("js.html_injection", "warning", node, { what: "insertAdjacentHTML" });
			if (node.type === "CallExpression" && CALL_METHODS.has(callee)) {
				let method = null;
				if (first && first.type === "ObjectExpression") method = literalString(propValue(first, "method"));
				else method = literalString(first);
				if (method === null) add("js.call_dynamic", "warning", node, { call: callee });
				else out.methods.push({ method, line: node.loc.start.line });
			}
		}

		if (node.type === "MemberExpression") {
			const name = dotted(node);
			const parentIsMember = parent && parent.type === "MemberExpression" && parent.object === node;
			if (name && STORAGE.has(name)) add("js.storage", "critical", node, { what: name });
			if (name === "frappe.boot" && !parentIsMember) add("js.storage", "critical", node, { what: "frappe.boot" });
		}
		if (node.type === "Identifier" && (node.name === "localStorage" || node.name === "sessionStorage" || node.name === "indexedDB")) {
			const isProperty = parent && parent.type === "MemberExpression" && parent.property === node && !parent.computed;
			const isKey = parent && parent.type === "Property" && parent.key === node;
			if (!isProperty && !isKey) add("js.storage", "critical", node, { what: node.name });
		}

		if (node.type === "AssignmentExpression") {
			const target = dotted(node.left);
			if (target && LOCATION.has(target)) {
				const kind = classifyUrl(node.right);
				if (kind === "absolute" || kind === "unknown") add("js.navigation", "critical", node, { call: target, url: literalString(node.right) || "" });
			}
			if (node.left.type === "MemberExpression" && !node.left.computed && ["innerHTML", "outerHTML"].includes(node.left.property.name) && literalString(node.right) === null)
				add("js.html_injection", "warning", node, { what: node.left.property.name });
			if (node.left.type === "MemberExpression" && dotted(node.left.object) === "frappe.listview_settings") {
				const key = node.left.computed ? literalString(node.left.property) : node.left.property.name;
				out.targets.push(key);
				if (key === doctype) listTarget = true;
				else if (input.view === "List") add("js.list_target", "critical", node, { target: key === null ? "?" : key });
			}
		}

		if (node.type === "Literal" && typeof node.value === "string" && /<\s*(script|iframe)/i.test(node.value))
			add("js.html_injection", "warning", node, { what: "<script>/<iframe>" });
		if (node.type === "TemplateElement" && /<\s*(script|iframe)/i.test(node.value.cooked || ""))
			add("js.html_injection", "warning", node, { what: "<script>/<iframe>" });
	});

	if (input.view === "Form" && !out.targets.some((t) => allowedTargets.has(t)))
		add("js.form_missing", "critical", null, { doctype });
	if (input.view === "List" && !listTarget) add("js.list_missing", "critical", null, { doctype });

	process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
	process.stdout.write(JSON.stringify({ fatal: String((e && e.stack) || e) }));
	process.exitCode = 0;
});
