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


@frappe.whitelist()
def get_status():
	user = _user()
	return {"available": available(), **account.status(user)}


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
