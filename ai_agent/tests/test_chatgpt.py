"""ChatGPT Subscription: each person's own sign-in (OpenAI's open-source "Sign in with ChatGPT"), tokens kept
encrypted and refreshed, and the Responses API stream turned into the chat's blocks. No call leaves the machine:
OpenAI's endpoints are replaced by fakes."""

from types import SimpleNamespace as NS
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from ai_agent.chatgpt import account
from ai_agent.engine import errors
from ai_agent.engine.providers.chatgpt import ChatGPTProvider, convert_input
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.events import BlockDone, MessageStop, ProviderError
from ai_agent.engine.types import IMAGE, TEXT, TOOL_RESULT, TOOL_USE, Block, Message, ModelRequest, ToolSpec

USER = "chatgpt-test@example.com"
PLAN = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class Reply:
	def __init__(self, status, body):
		self.status_code, self._body = status, body

	def json(self):
		return self._body


def tokens(access="at-1", refresh="rt-1", scope=PLAN):
	return Reply(200, {"access_token": access, "refresh_token": refresh, "id_token": "idt", "expires_in": 3600, "scope": scope})


class TestChatGPTSignIn(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "ChatGPT", "send_welcome_email": 0}).insert(ignore_permissions=True)
		self.addCleanup(self.cleanup)

	def cleanup(self):
		if frappe.db.exists(account.DOCTYPE, USER):
			frappe.delete_doc(account.DOCTYPE, USER, force=True, ignore_permissions=True)
		frappe.cache.delete_value(account.PENDING_KEY.format(USER))
		frappe.db.commit()

	def sign_in(self, scope=PLAN, issued="oaiapp_test"):
		url = account.start(USER)
		state = parse_qs(urlparse(url).query)["state"][0]
		with patch.object(account.requests, "post", return_value=tokens(scope=scope)) as post, \
				patch.object(account, "_verify_id_token", return_value={"sub": "user-sub", "email": "me@chatgpt.test"}):
			account.finish(USER, f"http://127.0.0.1:1455/auth/callback?code=c0de&scope=x&state={state}&client_id={issued}")
		return url, post

	def test_the_authorize_url_is_the_documented_one(self):
		q = {k: v[0] for k, v in parse_qs(urlparse(account.start(USER)).query).items()}
		self.assertEqual((q["client_id"], q["agent_name_hint"], q["response_type"], q["code_challenge_method"]),
			("dynamic_agent_client", "Musaed", "code", "S256"))
		self.assertEqual((q["redirect_uri"], q["resource"], q["scope"]), (account.REDIRECT_URI, account.RESOURCE, account.SCOPES))
		self.assertTrue(q["ext_agent_host_id"].startswith("urn:uuid:"))
		self.assertEqual(q["ext_agent_host_id"], account.host_id(), "one stable host id per site")

	def test_sign_in_keeps_the_tokens_encrypted_and_the_issued_client(self):
		_url, post = self.sign_in()
		sent = post.call_args.kwargs["data"]
		self.assertEqual((sent["grant_type"], sent["client_id"], sent["code"]), ("authorization_code", "oaiapp_test", "c0de"))
		self.assertTrue(sent["code_verifier"])
		doc = frappe.get_doc(account.DOCTYPE, USER)
		self.assertEqual((doc.status, doc.client_id, doc.email), ("Signed in", "oaiapp_test", "me@chatgpt.test"))
		self.assertEqual(account.access_token(USER), "at-1")
		self.assertNotIn("at-1", frappe.as_json(doc.as_dict()), "no token in the form")
		again = {k: v[0] for k, v in parse_qs(urlparse(account.start(USER)).query).items()}
		self.assertEqual(again["client_id"], "oaiapp_test", "a later sign-in reuses the issued client")
		self.assertNotIn("agent_name_hint", again)

	def test_a_wrong_or_refused_callback_is_not_used(self):
		account.start(USER)
		with self.assertRaises(frappe.ValidationError):
			account.finish(USER, "http://127.0.0.1:1455/auth/callback?code=x&state=forged")
		url = account.start(USER)
		state = parse_qs(urlparse(url).query)["state"][0]
		with self.assertRaises(frappe.ValidationError):
			account.finish(USER, f"http://127.0.0.1:1455/auth/callback?error=access_denied&state={state}")

	def test_without_plan_permission_it_does_not_answer(self):
		with self.assertRaises(frappe.ValidationError):
			self.sign_in(scope="openid profile email offline_access")
		with self.assertRaises(account.SignInRequired):
			account.access_token(USER)

	def test_an_expiring_token_is_refreshed_and_the_refresh_token_rotates(self):
		self.sign_in()
		frappe.db.set_value(account.DOCTYPE, USER, "expires_at", add_to_date(now_datetime(), seconds=30))
		with patch.object(account.requests, "post", return_value=tokens(access="at-2", refresh="rt-2")) as post:
			self.assertEqual(account.access_token(USER), "at-2")
		self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
		self.assertEqual(account._secret(USER, "refresh_token"), "rt-2")

	def test_an_ended_session_asks_to_sign_in_again(self):
		self.sign_in()
		frappe.db.set_value(account.DOCTYPE, USER, "expires_at", add_to_date(now_datetime(), seconds=-10))
		with patch.object(account.requests, "post", return_value=Reply(400, {"error": "invalid_grant"})):
			with self.assertRaises(account.SignInRequired):
				account.access_token(USER)
		self.assertEqual(frappe.db.get_value(account.DOCTYPE, USER, "status"), "Sign in again")

	def test_the_chat_explains_each_case(self):
		self.assertEqual(errors.kind_of("error", "chatgpt_sign_in_required: x"), "chatgpt_sign_in")
		self.assertEqual(errors.kind_of("rate_limit", "subscription_sharing_usage_limit_exceeded: limit"), "chatgpt_limit")
		self.assertEqual(errors.kind_of("error", "subscription_sharing_user_not_eligible: no"), "chatgpt_not_eligible")


def ev(type_, **kw):
	return NS(type=type_, **kw)


class FakeStream(list):
	def close(self):
		pass


class TestChatGPTProvider(IntegrationTestCase):
	def request(self, tools=True):
		messages = [
			Message(role="user", blocks=[Block(kind=TEXT, text="كم فاتورة؟"), Block(kind=IMAGE, data={"media_type": "image/png", "data": "QUJD"})]),
			Message(role="assistant", blocks=[Block(kind=TEXT, text="أبحث"), Block(kind=TOOL_USE, id="call_1", name="data__count", data={"doctype": "Sales Invoice"}, signature="ns:erp")]),
			Message(role="user", blocks=[Block(kind=TOOL_RESULT, id="call_1", text="12")]),
		]
		tool_specs = [ToolSpec(name="data__count", description="Count", parameters={"type": "object", "properties": {"doctype": {"type": "string"}}})] if tools else []
		return ModelRequest(model="gpt-test", system="sys", system_dynamic="dyn", messages=messages, tools=tool_specs)

	def provider(self):
		return ChatGPTProvider({"name": "ChatGPT", "model": "gpt-test", "models": {}, "timeout": 30})

	def stream_with(self, events, create=None):
		calls = []

		def responses_create(**payload):
			calls.append(payload)
			if create:
				return create(payload, len(calls))
			return FakeStream(events)

		client = NS(responses=NS(create=responses_create))
		with patch("ai_agent.chatgpt.account.access_token", return_value="tok"), patch.object(ChatGPTProvider, "client", return_value=client):
			out = list(self.provider().stream(self.request(), CancelToken()))
		return out, calls

	def test_the_history_becomes_responses_input(self):
		items = convert_input(self.request().messages)
		self.assertEqual(items[0]["content"][0], {"type": "input_text", "text": "كم فاتورة؟"})
		self.assertEqual(items[0]["content"][1]["type"], "input_image")
		self.assertEqual(items[1], {"role": "assistant", "content": [{"type": "output_text", "text": "أبحث"}]})
		self.assertEqual((items[2]["type"], items[2]["call_id"], items[2]["name"], items[2]["namespace"]), ("function_call", "call_1", "data__count", "erp"))
		self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": "12"})

	def test_the_request_follows_the_route(self):
		_out, calls = self.stream_with([ev("response.completed", response=NS(usage=None))])
		payload = calls[0]
		self.assertEqual((payload["store"], payload["stream"], payload["instructions"]), (False, True, "sys\n\ndyn"))
		for refused in ("temperature", "max_output_tokens", "user", "previous_response_id"):
			self.assertNotIn(refused, payload)
		self.assertEqual((payload["tools"][0]["type"], payload["tools"][0]["tools"][0]["name"]), ("namespace", "data__count"))

	def test_text_and_a_tool_call_come_out_as_blocks(self):
		events = [
			ev("response.output_text.delta", delta="مرحبا"),
			ev("response.output_item.added", output_index=1, item=NS(type="function_call", call_id="call_9", name="data__count", arguments="", namespace="erp")),
			ev("response.function_call_arguments.delta", output_index=1, delta='{"doctype": "Item"}'),
			ev("response.output_item.done", output_index=1, item=NS(type="function_call", arguments='{"doctype": "Item"}')),
			ev("response.completed", response=NS(usage=NS(input_tokens=100, output_tokens=20, input_tokens_details=NS(cached_tokens=40)))),
		]
		out, _ = self.stream_with(events)
		done = [e.block for e in out if isinstance(e, BlockDone)]
		self.assertEqual(done[0]["text"], "مرحبا")
		self.assertEqual((done[1]["name"], done[1]["data"], done[1]["id"]), ("data__count", {"doctype": "Item"}, "call_9"))
		self.assertEqual(out[-1].stop_reason, "tool_use")

	def test_a_usage_limit_is_reported_with_its_code(self):
		out, _ = self.stream_with([ev("response.failed", response=NS(error=NS(code="subscription_sharing_usage_limit_exceeded", message="limit")))])
		error = next(e for e in out if isinstance(e, ProviderError))
		self.assertIn("subscription_sharing_usage_limit_exceeded", error.message)
		self.assertEqual(errors.kind_of(error.kind, error.message), "chatgpt_limit")

	def test_a_server_that_refuses_namespaces_gets_plain_tools(self):
		from ai_agent.engine.providers import chatgpt

		self.addCleanup(chatgpt._plain_tools.discard, "ChatGPT")

		class Refused(Exception):
			body = {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools[0]", "message": "namespace"}}

		def create(payload, n):
			if n == 1:
				raise Refused("refused")
			return FakeStream([ev("response.completed", response=NS(usage=None))])

		out, calls = self.stream_with([], create=create)
		self.assertEqual(calls[1]["tools"][0]["type"], "function")
		self.assertNotIn("namespace", calls[1]["input"][2])
		self.assertIsInstance(out[-1], MessageStop)

	def test_without_a_sign_in_the_chat_says_so(self):
		with patch("ai_agent.chatgpt.account.access_token", side_effect=account.SignInRequired("chatgpt_sign_in_required: x")):
			out = list(self.provider().stream(self.request(), CancelToken()))
		self.assertEqual(errors.kind_of(out[0].kind, out[0].message), "chatgpt_sign_in")
