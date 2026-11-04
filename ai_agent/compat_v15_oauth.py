# On the version-15 branch only: the OAuth pieces an AI tool (Claude, ChatGPT, Claude Code, Cursor, VS Code) needs
# to sign in to the MCP server, which Frappe 16 has and 15 lacks (BRANCHES.md). Wired in hooks.py
# (`page_renderer`, `after_request`) and loaded with compat_v15.
#
# 15 already has the OAuth server itself (authorize / get_token with PKCE, OAuth Client, OAuth Bearer Token);
# missing are the two discovery documents (RFC 8414, RFC 9728), dynamic client registration (RFC 7591) and the
# CORS answers a browser-side client needs. They are served the way Frappe 16 serves them, and only while MCP is on
# in AI Settings: 15 has no OAuth Settings, so that switch stands for its three flags.

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

OAUTH = "/api/method/frappe.integrations.oauth2."
REGISTER = "/api/method/ai_agent.compat_v15_oauth.register_client"
# browser-side clients that may call the registration and token endpoints (Frappe 16: allowed_public_client_origins)
PUBLIC_CLIENT_ORIGINS = ("https://chatgpt.com",)
SIGN_IN_PATHS = (REGISTER, f"{OAUTH}get_token", f"{OAUTH}revoke_token", f"{OAUTH}introspect_token",
	f"{OAUTH}openid_profile")


def enabled():
	return bool(frappe.db.get_single_value("AI Settings", "enable_mcp"))


def resource_url():
	"""The site as the request reached it (Frappe 16's get_resource_url)."""
	url = urlparse(frappe.request.url)
	return f"{url.scheme}://{url.netloc}"


def authorization_server_metadata():
	issuer = resource_url()
	return {
		"issuer": issuer,
		"authorization_endpoint": f"{issuer}{OAUTH}authorize",
		"token_endpoint": f"{issuer}{OAUTH}get_token",
		"registration_endpoint": f"{issuer}{REGISTER}",
		"revocation_endpoint": f"{issuer}{OAUTH}revoke_token",
		"introspection_endpoint": f"{issuer}{OAUTH}introspect_token",
		"userinfo_endpoint": f"{issuer}{OAUTH}openid_profile",
		"response_types_supported": ["code"],
		"response_modes_supported": ["query"],
		"grant_types_supported": ["authorization_code", "refresh_token"],
		"token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
		"revocation_endpoint_auth_methods_supported": ["client_secret_basic"],
		"code_challenge_methods_supported": ["S256"],
	}


def protected_resource_metadata():
	resource = resource_url()
	# ai_agent.mcp.http.after_request narrows `resource` to the MCP endpoint, as on 16
	return {"resource": resource, "authorization_servers": [resource], "bearer_methods_supported": ["header"]}


class WellKnown(BaseRenderer):
	"""`/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource[/<path>]`."""

	def can_render(self):
		return self.path.startswith((".well-known/oauth-authorization-server", ".well-known/oauth-protected-resource")) \
			and enabled()

	def render(self):
		auth_server = self.path.startswith(".well-known/oauth-authorization-server")
		body = authorization_server_metadata() if auth_server else protected_resource_metadata()
		return Response(json.dumps(body), status=200, mimetype="application/json")


def _secure_redirect(uri):
	"""https, or http on this machine (RFC 8252 loopback; MCP clients register `localhost`)."""
	try:
		url = urlparse(uri)
	except ValueError:
		return False
	if not url.netloc or url.fragment:
		return False
	if url.scheme == "https":
		return True
	if url.scheme != "http":
		return False
	if url.hostname == "localhost":
		return True
	try:
		return ipaddress.ip_address(url.hostname or "").is_loopback
	except ValueError:
		return False


def _answer(status, body):
	"""Reply with `body` as the whole JSON object (a whitelisted method's return value would be wrapped in
	{"message": ...}; RFC 7591 wants the bare object)."""
	frappe.local.response.update(body)
	frappe.local.response.http_status_code = status


def _registration_error(message):
	_answer(400, {"error": "invalid_client_metadata", "error_description": message})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_client():
	"""RFC 7591 dynamic client registration, as Frappe 16's `frappe.integrations.oauth2.register_client`: each call
	makes a new OAuth Client (no lookup by name, which would hand out another client's id)."""
	if not enabled():
		raise frappe.PageDoesNotExistError
	data = frappe.request.get_json(silent=True)
	if not isinstance(data, dict):
		return _registration_error("Request body is empty")
	uris = data.get("redirect_uris")
	if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u.strip() for u in uris):
		return _registration_error("redirect_uris is required")
	uris = [u.strip() for u in uris][:20]
	if not frappe.conf.developer_mode and not all(_secure_redirect(u) for u in uris):
		return _registration_error("redirect_uris must be https")
	if not set(data.get("grant_types") or ["authorization_code"]) <= {"authorization_code", "refresh_token"}:
		return _registration_error("only 'authorization_code' and 'refresh_token' grant types are supported")
	if any(rt != "code" for rt in data.get("response_types") or ["code"]):
		return _registration_error("only 'code' response_type is supported")
	method = data.get("token_endpoint_auth_method") or "client_secret_basic"
	if method not in ("none", "client_secret_basic", "client_secret_post"):
		return _registration_error("unsupported token_endpoint_auth_method")

	name = str(data.get("client_name") or "MCP client").strip()[:140] or "MCP client"
	doc = frappe.get_doc({"doctype": "OAuth Client", "app_name": name, "scopes": "all", "redirect_uris": "\n".join(uris),
		"default_redirect_uri": uris[0], "response_type": "Code", "grant_type": "Authorization Code",
		"skip_authorization": 0})
	doc.insert(ignore_permissions=True)
	out = {"client_id": doc.client_id, "client_id_issued_at": int(frappe.utils.get_datetime(doc.creation).timestamp()),
		"client_secret_expires_at": 0, "client_name": name, "redirect_uris": uris, "grant_types": ["authorization_code"],
		"response_types": ["code"], "token_endpoint_auth_method": method, "scope": "all"}
	if method != "none":
		out["client_secret"] = doc.client_secret
	_answer(201, out)


def after_request(response, request):
	"""CORS for the discovery documents (any origin, as on 16) and for registration and token calls from the
	browser-side clients in PUBLIC_CLIENT_ORIGINS. Frappe 15 sends CORS headers only from site config."""
	origin = request.headers.get("Origin")
	if not origin or frappe.conf.allow_cors == "*":
		return
	if request.path.startswith("/.well-known/oauth-") and request.method in ("GET", "OPTIONS"):
		allowed = True
	elif request.path.startswith(SIGN_IN_PATHS) and request.method in ("POST", "OPTIONS"):
		allowed = origin.rstrip("/") in PUBLIC_CLIENT_ORIGINS and enabled()
	else:
		return
	if not allowed:
		return
	response.headers["Access-Control-Allow-Origin"] = origin
	response.headers["Vary"] = "Origin"
	if request.method == "OPTIONS":
		response.headers["Access-Control-Allow-Methods"] = request.headers.get("Access-Control-Request-Method") or "GET, POST"
		if wanted := request.headers.get("Access-Control-Request-Headers"):
			response.headers["Access-Control-Allow-Headers"] = wanted
		response.headers["Access-Control-Max-Age"] = "86400"


def _setup_answers():
	"""The MCP setup page's OAuth checks: on 15 sign-in is on whenever MCP is, and ChatGPT's origin is built in."""
	from ai_agent.mcp import setup

	setup.oauth_ready = enabled
	setup.chatgpt_allowed = lambda: True
	setup.enable_oauth = lambda name: None


_setup_answers()
