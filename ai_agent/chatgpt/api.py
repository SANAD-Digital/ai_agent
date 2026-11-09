"""The chat's "ChatGPT account" dialog: each person signs in, checks or ends their own sign-in. Nobody reaches
another person's sign-in here; System Managers see the records (AI ChatGPT Account), never the tokens."""

import frappe
from frappe import _

from ai_agent.chatgpt import account


def _user():
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Sign in first."), frappe.PermissionError)
	return user


def available():
	"""Is an enabled AI provider of type ChatGPT Subscription set up on this site?"""
	return bool(frappe.db.exists("AI Provider", {"provider_type": "ChatGPT Subscription", "enabled": 1}))


def shared():
	"""The enabled ChatGPT provider that uses one shared account: (provider, the person who signs in for everyone)."""
	row = frappe.db.get_value("AI Provider", {"provider_type": "ChatGPT Subscription", "enabled": 1, "chatgpt_account": "One shared account"},
		["name", "chatgpt_shared_user"], as_dict=True)
	return row if row and row.chatgpt_shared_user else None


@frappe.whitelist()
def get_status():
	user = _user()
	out = {"available": available(), **account.status(user)}
	row = shared()
	if row:
		out["shared"] = {"signed_in_by": row.chatgpt_shared_user, "is_me": row.chatgpt_shared_user == user,
			**{k: v for k, v in account.status(row.chatgpt_shared_user).items() if k in ("signed_in", "email")}}
	return out


@frappe.whitelist(methods=["POST"])
def start_sign_in():
	user = _user()
	if not available():
		frappe.throw(_("No AI provider of type ChatGPT Subscription is enabled on this site."))
	return {"url": account.start(user), "redirect_uri": account.REDIRECT_URI}


@frappe.whitelist(methods=["POST"])
def finish_sign_in(address):
	user = _user()
	return account.finish(user, address)


@frappe.whitelist(methods=["POST"])
def sign_out():
	user = _user()
	return account.sign_out(user)
