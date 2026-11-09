"""One person's ChatGPT sign-in, as OpenAI documents it for open-source apps
(developers.openai.com/siwc/token-sharing-open-source):

- the site is the "agent host": one stable `urn:uuid:` host id per site;
- the first sign-in registers the client (`client_id=dynamic_agent_client`) and OpenAI issues its own client id,
  kept for later sign-ins of that account; PKCE, no client secret;
- OpenAI only redirects to a loopback address (http://127.0.0.1:<port>/auth/callback), which on a server reaches
  nothing: the person copies the address the browser shows and pastes it back (`finish`). The port is not Codex's
  1455: Codex and Cline listen there on the person's own computer, catch the redirect and leave it hanging;
- access tokens live an hour and are refreshed here (serialized: the refresh token rotates); refresh tokens 30 days;
- tokens are kept encrypted in Frappe's password store, never in a form field, a log or a URL.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from urllib.parse import parse_qs, urlencode, urlparse

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

DOCTYPE = "AI ChatGPT Account"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
CONFIG_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
REDIRECT_URI = "http://127.0.0.1:48155/auth/callback"  # only the port may vary between sign-ins
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
NEW_CLIENT = "dynamic_agent_client"
APP_NAME = "Musaed"  # agent_name_hint: the app's actual name, the same in every installation
HOST_KEY = "ai_agent_chatgpt_host_id"
PENDING_KEY = "ai_agent:chatgpt:pending:{}"
PENDING_SECONDS = 15 * 60
REFRESH_MARGIN_SECONDS = 120
TOKENS = ("access_token", "refresh_token", "id_token")
DEAD_REFRESH = ("invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
	"refresh_token_invalidated", "refresh_token_reused")


class SignInRequired(frappe.ValidationError):
	"""The person has no usable ChatGPT sign-in: the chat says how to sign in (engine/errors.py chatgpt_sign_in)."""


# -- the host and the stored tokens ----------------------------------------------------------------------------------


def host_id():
	"""This site's stable, opaque host id (a UUIDv4 URN), made once."""
	value = frappe.db.get_default(HOST_KEY)
	if not value:
		value = f"urn:uuid:{uuid.uuid4()}"
		frappe.db.set_default(HOST_KEY, value)
	return value


def _secret(user, field):
	from frappe.utils.password import get_decrypted_password

	return get_decrypted_password(DOCTYPE, user, field, raise_exception=False) or ""


def _keep(user, field, value):
	from frappe.utils.password import remove_encrypted_password, set_encrypted_password

	if value:
		set_encrypted_password(DOCTYPE, user, value, field)
	else:
		remove_encrypted_password(DOCTYPE, user, field)


def _account(user, create=False):
	if frappe.db.exists(DOCTYPE, user):
		return frappe.get_doc(DOCTYPE, user)
	if not create:
		return None
	return frappe.get_doc({"doctype": DOCTYPE, "user": user, "status": "Signed out"})


def status(user):
	doc = _account(user)
	if not doc:
		return {"signed_in": False, "email": None, "status": "Signed out"}
	return {"signed_in": doc.status == "Signed in" and bool(_secret(user, "refresh_token")), "email": doc.email, "status": doc.status,
		"expires_at": str(doc.expires_at or "")}


# -- sign-in ---------------------------------------------------------------------------------------------------------


def _b64url(data):
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(user):
	"""The authorization URL to open in the browser; the attempt's secrets wait in the cache for `finish`."""
	doc = _account(user)
	client_id = (doc.client_id if doc else "") or NEW_CLIENT
	state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(64)
	params = {
		"client_id": client_id,
		"response_type": "code",
		"redirect_uri": REDIRECT_URI,
		"scope": SCOPES,
		"resource": RESOURCE,
		"state": state,
		"nonce": nonce,
		"code_challenge_method": "S256",
		"code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
		"ext_agent_host_id": host_id(),
	}
	if client_id == NEW_CLIENT:
		params["agent_name_hint"] = APP_NAME
	else:
		if _secret(user, "id_token"):
			params["id_token_hint"] = _secret(user, "id_token")
		if doc.email:
			params["login_hint"] = doc.email
	frappe.cache.set_value(PENDING_KEY.format(user), {"state": state, "nonce": nonce, "verifier": verifier, "client_id": client_id,
		"redirect_uri": REDIRECT_URI},
		expires_in_sec=PENDING_SECONDS)
	return AUTHORIZE_URL + "?" + urlencode(params)


def _query(pasted):
	"""The callback's query from what the person pasted: the whole address, or just its ?query part."""
	text = (pasted or "").strip()
	if "?" in text:
		text = text.split("?", 1)[1]
	return {k: v[0] for k, v in parse_qs(urlparse("?" + text).query).items()}


def finish(user, pasted):
	"""Complete the sign-in from the pasted callback address: validate, exchange, verify, store."""
	pending = frappe.cache.get_value(PENDING_KEY.format(user))
	if not pending:
		frappe.throw(_("The sign-in took too long or was not started here. Start the sign-in again."))
	query = _query(pasted)
	if query.get("state") != pending["state"]:
		frappe.throw(_("This address is not from the sign-in you just started. Start the sign-in again and paste the new address."))
	if query.get("error"):
		frappe.cache.delete_value(PENDING_KEY.format(user))
		if query["error"] == "access_denied":
			frappe.throw(_("The sign-in was cancelled in ChatGPT, or using the ChatGPT plan was not allowed."))
		frappe.throw(_("ChatGPT refused the sign-in: {0}").format(query.get("error_description") or query["error"]))
	code = query.get("code")
	client_id = query.get("client_id") or (pending["client_id"] if pending["client_id"] != NEW_CLIENT else "")
	if not code or not client_id:
		frappe.throw(_("The pasted address has no sign-in code. Paste the whole address shown after signing in."))
	frappe.cache.delete_value(PENDING_KEY.format(user))  # a code is used once

	tokens = _token_request({"grant_type": "authorization_code", "client_id": client_id, "code": code,
		"code_verifier": pending["verifier"], "redirect_uri": pending.get("redirect_uri") or REDIRECT_URI, "resource": RESOURCE})
	scopes = set((tokens.get("scope") or "").split())
	claims = _verify_id_token(tokens.get("id_token"), client_id, pending["nonce"])
	_store(user, client_id, tokens, claims, scopes)
	if PLAN_SCOPE not in scopes:
		frappe.throw(_("Signed in, but using your ChatGPT plan was not allowed. Sign in again and allow it."))
	fill_provider_models(user)
	return status(user)


def fill_provider_models(user):
	"""The models of `user`'s plan on the ChatGPT providers that answer through this sign-in and have none yet (or are
	the shared account's), so the chat lists them without a trip to the provider's form. Best effort."""
	providers = frappe.get_all("AI Provider", filters={"provider_type": "ChatGPT Subscription", "enabled": 1},
		fields=["name", "chatgpt_account", "chatgpt_shared_user"])
	targets = [p.name for p in providers if (p.chatgpt_account == "One shared account" and p.chatgpt_shared_user == user)
		or (p.chatgpt_account != "One shared account" and not frappe.db.count("AI Provider Model", {"parent": p.name}))]
	if not targets:
		return
	try:
		listed = models(user)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ChatGPT models after sign-in")
		return
	for name in targets:
		doc = frappe.get_doc("AI Provider", name)
		have = {row.model_name for row in doc.models}
		for m in listed:
			if m["slug"] not in have:
				doc.append("models", {"model_name": m["slug"], "label": m["display_name"], "model_type": "Chat",
					"supports_tools": 1, "supports_vision": 1, "supports_thinking": 1})
		if not doc.default_model and doc.models:
			doc.default_model = doc.models[0].model_name
		doc.flags.ignore_permissions = True
		doc.save()
	frappe.db.commit()


def _token_request(data):
	try:
		response = requests.post(TOKEN_URL, data=data, timeout=30)
	except requests.RequestException as exc:
		frappe.throw(_("Could not reach ChatGPT's sign-in service: {0}").format(str(exc)[:200]))
	body = _json(response)
	if response.status_code != 200 or not body.get("access_token"):
		code = body.get("error") or response.status_code
		raise _TokenError(str(code), body.get("error_description") or "")
	return body


class _TokenError(Exception):
	def __init__(self, code, description=""):
		super().__init__(f"{code}: {description}".strip(": "))
		self.code = code


def _json(response):
	try:
		data = response.json()
		return data if isinstance(data, dict) else {}
	except ValueError:
		return {}


def _verify_id_token(id_token, client_id, nonce):
	"""The ID token's signature (OpenAI's published keys), issuer, audience, expiry and nonce."""
	import jwt

	if not id_token:
		frappe.throw(_("ChatGPT returned no identity for this sign-in. Try again."))
	config = requests.get(CONFIG_URL, timeout=30).json()
	key = jwt.PyJWKClient(config["jwks_uri"]).get_signing_key_from_jwt(id_token)
	claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=client_id, issuer=config.get("issuer") or ISSUER)
	if claims.get("nonce") != nonce:
		frappe.throw(_("The sign-in could not be verified. Start it again."))
	return claims


def _store(user, client_id, tokens, claims, scopes):
	doc = _account(user, create=True)
	doc.update({
		"client_id": client_id,
		"subject": claims.get("sub"),
		"email": claims.get("email") or doc.email,
		"scopes": " ".join(sorted(scopes)),
		"status": "Signed in" if PLAN_SCOPE in scopes else "Sign in again",
		"signed_in_at": now_datetime(),
		"expires_at": add_to_date(now_datetime(), seconds=int(tokens.get("expires_in") or 3600)),
		"last_error": "",
	})
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)
	for field in TOKENS:
		_keep(user, field, tokens.get(field) or (_secret(user, field) if field == "id_token" else ""))
	frappe.db.commit()


# -- use -------------------------------------------------------------------------------------------------------------


def access_token(user):
	"""A valid access token for `user`, refreshed when it is about to expire. Raises SignInRequired."""
	doc = _account(user)
	if not doc or doc.status != "Signed in" or not _secret(user, "refresh_token"):
		raise SignInRequired(_("chatgpt_sign_in_required: {0} has not signed in with ChatGPT.").format(user))
	if doc.expires_at and get_datetime(doc.expires_at) > add_to_date(now_datetime(), seconds=REFRESH_MARGIN_SECONDS):
		token = _secret(user, "access_token")
		if token:
			return token
	with frappe.cache.lock(frappe.cache.make_key(f"ai_agent:chatgpt:refresh:{user}"), timeout=60):
		doc.reload()  # another process may have refreshed while this one waited
		if doc.expires_at and get_datetime(doc.expires_at) > add_to_date(now_datetime(), seconds=REFRESH_MARGIN_SECONDS):
			token = _secret(user, "access_token")
			if token:
				return token
		return _refresh(user, doc)


def _refresh(user, doc):
	try:
		tokens = _token_request({"grant_type": "refresh_token", "client_id": doc.client_id,
			"refresh_token": _secret(user, "refresh_token"), "resource": RESOURCE})
	except _TokenError as exc:
		if exc.code in DEAD_REFRESH:
			for field in ("access_token", "refresh_token"):
				_keep(user, field, "")
			doc.db_set({"status": "Sign in again", "last_error": str(exc)[:500]})
			frappe.db.commit()
			raise SignInRequired(_("chatgpt_sign_in_required: the ChatGPT sign-in of {0} has ended.").format(user))
		doc.db_set("last_error", str(exc)[:500])
		frappe.db.commit()
		raise
	_keep(user, "access_token", tokens["access_token"])
	if tokens.get("refresh_token"):
		_keep(user, "refresh_token", tokens["refresh_token"])  # it rotates: always the latest
	if tokens.get("id_token"):
		_keep(user, "id_token", tokens["id_token"])
	doc.db_set({"expires_at": add_to_date(now_datetime(), seconds=int(tokens.get("expires_in") or 3600)), "last_error": ""})
	frappe.db.commit()
	return tokens["access_token"]


def sign_out(user):
	"""Revoke the renewable session at OpenAI, then clear the tokens (the registration stays for a later sign-in)."""
	doc = _account(user)
	if not doc:
		return {"revoked": True}
	refresh = _secret(user, "refresh_token")
	revoked = not refresh
	if refresh:
		try:
			endpoint = requests.get(CONFIG_URL, timeout=30).json().get("revocation_endpoint")
			if endpoint:
				r = requests.post(endpoint, data={"token": refresh, "token_type_hint": "refresh_token", "client_id": doc.client_id}, timeout=30)
				revoked = r.status_code == 200
		except requests.RequestException:
			revoked = False
	for field in ("access_token", "refresh_token"):
		_keep(user, field, "")
	doc.db_set({"status": "Signed out", "expires_at": None})
	frappe.db.commit()
	return {"revoked": revoked}


def models(user):
	"""The models this person's ChatGPT plan offers: [{slug, display_name}], in the server's order."""
	response = requests.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {access_token(user)}"}, timeout=30)
	body = _json(response)
	if response.status_code != 200:
		frappe.throw(_("ChatGPT did not list the models: {0}").format((body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("detail") or response.status_code))
	rows = body.get("models") if isinstance(body.get("models"), list) else body.get("data") or []
	return [{"slug": m.get("slug") or m.get("id"), "display_name": m.get("display_name") or m.get("slug") or m.get("id")}
		for m in rows if m.get("visibility", "list") == "list" and (m.get("slug") or m.get("id"))]
