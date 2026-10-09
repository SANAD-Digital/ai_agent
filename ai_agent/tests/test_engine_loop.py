"""Pure unit tests for the chat engine: the loop, providers' message
translation, history and compaction. No site, no database."""

from dataclasses import dataclass, field
from unittest import TestCase

from ai_agent.engine.context.compactor import Compactor, budget_for
from ai_agent.engine.context.history import ensure_tool_results, to_messages
from ai_agent.engine.events import ApprovalRequested, BlockDelta, BlockStart, MessageCancelled, MessageFailed, ToolResultEvent
from ai_agent.engine.loop import Loop, LoopConfig
from ai_agent.engine.providers import anthropic as anthropic_mod
from ai_agent.engine.providers import openai_compat
from ai_agent.engine.providers.base import BlockAssembler, CancelToken
from ai_agent.engine.providers.fake import FakeProvider
from ai_agent.engine.types import APPROVAL, SKILL, TEXT, THINKING, TOOL_RESULT, TOOL_USE, Block, Message, ToolSpec


@dataclass
class Outcome:
    result: object = None
    extras: list = field(default_factory=list)
    approval: str = ""
    approval_block: object = None
    duration_ms: int = 0
    tool_call: str = ""


class FakeExecutor:
    def __init__(self, approval_on=None, extras_on=None):
        self.calls = []
        self.approval_on = approval_on
        self.extras_on = extras_on or {}

    def specs(self):
        return [ToolSpec(name="data__query", description="query", parameters={"type": "object", "properties": {}}),
                ToolSpec(name="actions__request_create_draft", description="write", parameters={"type": "object", "properties": {}}, read_only=False)]

    def is_read_only(self, name):
        return name == "data__query"

    def execute(self, call):
        self.calls.append(call)
        if self.approval_on and call.name == self.approval_on:
            return Outcome(approval="APRQ-1", approval_block=Block(kind=APPROVAL, id=call.id, name=call.name, data={"approval": "APRQ-1", "status": "Pending"}))
        extras = [Block(kind=k, text=v) for k, v in self.extras_on.get(call.name, [])]
        return Outcome(result=Block(kind=TOOL_RESULT, id=call.id, name=call.name, text='{"rows": [{"n": 1}]}'), extras=extras, duration_ms=3)


def run_loop(scripts, executor=None, cancel=None, config=None, messages=None, budget=200_000, summarize=None):
    provider = FakeProvider(scripts)
    loop = Loop(provider, executor or FakeExecutor(), Compactor(budget, summarize=summarize), cancel or CancelToken(), config or LoopConfig(model="fake", retries=1, backoff_seconds=0), "SYSTEM", "DYNAMIC", sleep=lambda s: None)
    messages = messages or [Message(role="user", blocks=[Block(kind=TEXT, text="hello")])]
    events = list(loop.run(messages))
    return provider, events


def types(events):
    return [e.type for e in events if e.type != "block.delta"]


class TestLoop(TestCase):
    def test_text_only_reply(self):
        provider, events = run_loop([[("text", "Hello there"), ("stop", "end_turn")]])
        self.assertEqual(types(events), ["message.started", "block.start", "block.done", "usage", "message.done"])
        done = events[-1]
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.steps, 1)
        deltas = [e for e in events if isinstance(e, BlockDelta)]
        self.assertEqual("".join(d.delta for d in deltas), "Hello there")

    def test_thinking_then_tool_then_answer(self):
        provider, events = run_loop([
            [("thinking", "I should query."), ("tool", "data__query", {"doctype": "Customer"})],
            [("text", "Two rows."), ("stop", "end_turn")],
        ])
        kinds = [e.kind for e in events if isinstance(e, BlockStart)]
        self.assertEqual(kinds, [THINKING, TOOL_USE, TEXT])
        seqs = [e.block_seq for e in events if isinstance(e, (BlockStart, ToolResultEvent))]
        self.assertEqual(seqs, [0, 1, 2, 3], "block sequence is message-global")
        second = provider.requests[1].messages
        self.assertEqual([m.role for m in second], ["user", "assistant", "user"])
        self.assertEqual(second[-1].blocks[0].kind, TOOL_RESULT)
        self.assertEqual(second[-1].blocks[0].id, second[1].blocks[1].id)
        self.assertEqual(events[-1].tool_calls, 1)

    def test_two_tool_calls_in_one_response(self):
        executor = FakeExecutor()
        provider, events = run_loop([
            [("tool", "data__query", {"a": 1}), ("tool", "data__query", {"b": 2})],
            [("text", "done")],
        ], executor=executor)
        self.assertEqual(len(executor.calls), 2)
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(provider.requests[1].messages[-1].blocks), 2)

    def test_approval_suspends_the_message(self):
        executor = FakeExecutor(approval_on="actions__request_create_draft")
        provider, events = run_loop([[("text", "I will create it."), ("tool", "actions__request_create_draft", {"doctype": "Sales Invoice"})]], executor=executor)
        self.assertEqual(len(provider.requests), 1, "no second model call while waiting")
        approval = [e for e in events if isinstance(e, ApprovalRequested)][0]
        self.assertEqual(approval.approval, "APRQ-1")
        self.assertEqual(events[-1].status, "awaiting_approval")

    def test_extras_are_emitted_and_skill_goes_back_to_the_model(self):
        executor = FakeExecutor(extras_on={"data__query": [(SKILL, "SKILL BODY")]})
        provider, events = run_loop([[("tool", "data__query", {})], [("text", "ok")]], executor=executor)
        self.assertIn("skill.loaded", types(events))
        last_user = provider.requests[1].messages[-1]
        self.assertEqual([b.kind for b in last_user.blocks], [TOOL_RESULT, SKILL])

    def test_cancel_during_stream(self):
        cancel = CancelToken()
        provider = FakeProvider([[("text", "a" * 200)]], chunk=10)
        loop = Loop(provider, FakeExecutor(), Compactor(100000), cancel, LoopConfig(model="fake"), "S", "D", sleep=lambda s: None)
        events = []
        for ev in loop.run([Message(role="user", blocks=[Block(kind=TEXT, text="go")])]):
            events.append(ev)
            if isinstance(ev, BlockDelta) and len(events) > 4:
                cancel.cancel()
        self.assertIsInstance(events[-1], MessageCancelled)
        self.assertLess(len([e for e in events if isinstance(e, BlockDelta)]), 20)

    def test_max_tokens_continues_once(self):
        provider, events = run_loop([[("text", "part one"), ("stop", "max_tokens")], [("text", " part two"), ("stop", "end_turn")]])
        self.assertEqual(len(provider.requests), 2)
        self.assertIn("Continue", provider.requests[1].messages[-1].blocks[0].text)
        self.assertEqual(events[-1].status, "completed")

    def test_retryable_error_then_success(self):
        provider, events = run_loop([[("error", "rate limited", "rate_limit", True)], [("text", "fine")]])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(events[-1].type, "message.done")

    def test_fatal_error_fails_the_message(self):
        provider, events = run_loop([[("error", "bad key", "auth", False)]])
        self.assertIsInstance(events[-1], MessageFailed)
        self.assertEqual(events[-1].kind, "auth")

    def test_empty_response_is_nudged_once_then_fails(self):
        provider, events = run_loop([[("stop", "end_turn")], [("stop", "end_turn")]])
        self.assertEqual(len(provider.requests), 2)
        self.assertIsInstance(events[-1], MessageFailed)
        self.assertEqual(events[-1].kind, "empty")

    def test_step_limit_removes_tools_on_last_step(self):
        config = LoopConfig(model="fake", max_steps=2)
        provider, events = run_loop([[("tool", "data__query", {})], [("text", "final")]], config=config)
        self.assertEqual(provider.requests[1].tools, [])
        self.assertEqual(events[-1].status, "completed")

    def test_compaction_event_when_over_budget(self):
        big = Message(role="user", blocks=[Block(kind=TEXT, text="x" * 30000)])
        old_assistant = Message(role="assistant", blocks=[Block(kind=TEXT, text="ok")])
        current = Message(role="user", blocks=[Block(kind=TEXT, text="now?")])
        provider, events = run_loop([[("text", "short")]], messages=[big, old_assistant, current], budget=3000, summarize=lambda msgs, prompt: "SUMMARY")
        self.assertIn("compaction", types(events))
        self.assertIn("SUMMARY", provider.requests[0].messages[0].blocks[0].text)


class TestLoopResilience(TestCase):
    def test_context_overflow_compacts_and_retries(self):
        big = Message(role="user", blocks=[Block(kind=TEXT, text="x" * 9000)])
        old_assistant = Message(role="assistant", blocks=[Block(kind=TEXT, text="ok")])
        current = Message(role="user", blocks=[Block(kind=TEXT, text="now?")])
        provider, events = run_loop(
            [[("error", "This model's maximum context length is exceeded", "context_overflow", False)], [("text", "after compaction")]],
            messages=[big, old_assistant, current], budget=200_000, summarize=lambda msgs, prompt: "SUMMARY",
        )
        self.assertEqual(len(provider.requests), 2)
        self.assertIn("compaction", types(events))
        self.assertEqual(events[-1].type, "message.done")
        self.assertIn("SUMMARY", provider.requests[1].messages[0].blocks[0].text)

    def test_tool_set_refreshes_after_enable(self):
        class GrowingExecutor(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.extra = False
                self.specs_changed = False

            def specs(self):
                base = super().specs()
                return base + ([ToolSpec(name="erp__fiscal_year", description="fy", parameters={})] if self.extra else [])

            def execute(self, call):
                if call.name == "tools__enable":
                    self.extra = True
                    self.specs_changed = True
                    return Outcome(result=Block(kind=TOOL_RESULT, id=call.id, name=call.name, text="{\"enabled\": \"erp_details\"}"))
                return super().execute(call)

        provider, events = run_loop([[("tool", "tools__enable", {"group": "erp_details"})], [("text", "done")]], executor=GrowingExecutor())
        self.assertEqual([t.name for t in provider.requests[0].tools][-1], "actions__request_create_draft")
        self.assertIn("erp__fiscal_year", [t.name for t in provider.requests[1].tools])


class TestHistory(TestCase):
    def test_assistant_turn_splits_at_tool_results(self):
        turns = [
            ("user", 1, "u1", [Block(kind=TEXT, text="q")]),
            ("assistant", 2, "a1", [
                Block(kind=THINKING, text="t", seq=0),
                Block(kind=TOOL_USE, id="c1", name="data__query", data={"x": 1}, seq=1),
                Block(kind=TOOL_RESULT, id="c1", text="r", seq=2),
                Block(kind=TEXT, text="answer", seq=3),
                Block(kind="artifact", data={"artifact": "A"}, seq=4),
            ]),
        ]
        msgs = to_messages(turns)
        self.assertEqual([m.role for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual([b.kind for b in msgs[1].blocks], [THINKING, TOOL_USE])
        self.assertEqual(msgs[2].blocks[0].kind, TOOL_RESULT)
        self.assertEqual(msgs[3].blocks[0].text, "answer")

    def test_missing_tool_result_is_synthesized(self):
        msgs = [Message(role="user", blocks=[Block(kind=TEXT, text="q")]), Message(role="assistant", blocks=[Block(kind=TOOL_USE, id="c9", name="x")]), Message(role="user", blocks=[Block(kind=TEXT, text="next")])]
        fixed = ensure_tool_results(msgs)
        self.assertEqual(fixed[2].blocks[0].kind, TOOL_RESULT)
        self.assertEqual(fixed[2].blocks[0].id, "c9")
        self.assertTrue(fixed[2].blocks[0].is_error)


class TestProviderTranslation(TestCase):
    def messages(self):
        return [
            Message(role="user", blocks=[Block(kind=TEXT, text="q")]),
            Message(role="assistant", blocks=[Block(kind=THINKING, text="think", signature="sig"), Block(kind=TOOL_USE, id="c1", name="data__query", data={"a": 1})]),
            Message(role="user", blocks=[Block(kind=TOOL_RESULT, id="c1", text="{\"rows\":[]}")]),
            Message(role="assistant", blocks=[Block(kind=TEXT, text="done")]),
        ]

    def test_openai_shape(self):
        out = openai_compat.convert_messages(self.messages(), keep_reasoning=True)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "data__query")
        self.assertEqual(out[1]["reasoning_content"], "think")
        self.assertEqual(out[2]["tool_call_id"], "c1")
        without = openai_compat.convert_messages(self.messages(), keep_reasoning=False)
        self.assertNotIn("reasoning_content", without[1])

    def test_anthropic_shape_and_cache_marker(self):
        out = anthropic_mod.convert_messages(self.messages())
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user", "assistant"])
        self.assertEqual(out[1]["content"][0], {"type": "thinking", "thinking": "think", "signature": "sig"})
        self.assertEqual(out[1]["content"][1]["type"], "tool_use")
        self.assertEqual(out[2]["content"][0]["type"], "tool_result")
        anthropic_mod.mark_last_user(out)
        self.assertEqual(out[2]["content"][-1].get("cache_control"), {"type": "ephemeral"})

    def test_assembler_parses_tool_input(self):
        asm = BlockAssembler()
        ev = asm.start(TOOL_USE, id="c", name="t")
        asm.delta(ev.block_seq, '{"do')
        asm.delta(ev.block_seq, 'ctype": "X"}')
        done = asm.stop(ev.block_seq)
        self.assertEqual(done.block["data"], {"doctype": "X"})

    def test_budget(self):
        self.assertEqual(budget_for(128000, 8000), int(128000 * 0.85) - 8000)
