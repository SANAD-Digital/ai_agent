"""Token benchmark for the agent (Phase B).

Runs each reference question through the real orchestrator as a dedicated
benchmark user and records, per model call: input/output/cached tokens and
where the prompt characters come from (system prompt, tool schemas, tool
results...). Per question: steps, tool calls, totals, wall time and the three
largest tool results.

    bench --site <site> execute ai_agent.benchmarks.run.run \
        --kwargs "{'label': 'baseline'}"

Options: label, ids (list of question ids), repeat, agent, company.
Results land in ai_agent/benchmarks/results/<label>.json.
"""

import json
import os
import time
from contextlib import ExitStack
from unittest.mock import patch

import frappe
import yaml

BENCH_USER = "ai-bench@example.com"
BENCH_ROLES = ("Accounts User", "Sales User", "Purchase User", "Stock User", "Desk User")
HERE = os.path.dirname(__file__)

# USD per 1M tokens. Assumed list prices; adjust when the provider changes.
PRICES = {
	"default": {"input": 0.27, "cached_input": 0.07, "output": 1.10},
	"claude-sonnet-5": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
	"claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00},
}


def load_questions(ids=None, questions_file="questions.yaml"):
	with open(os.path.join(HERE, os.path.basename(questions_file))) as handle:
		questions = yaml.safe_load(handle)["questions"]
	if ids:
		questions = [q for q in questions if q["id"] in set(ids)]
	return questions


def run(label="baseline", ids=None, repeat=1, agent=None, company=None, questions_file="questions.yaml"):
	frappe.set_user("Administrator")
	agent = agent or frappe.db.get_value("AI Agent", {"is_default": 1, "enabled": 1}, "name")
	company = company or frappe.db.get_single_value("Global Defaults", "default_company")
	_enable_bench_user()
	results = []
	try:
		for question in load_questions(ids, questions_file):
			for attempt in range(int(repeat)):
				result = run_question(question, agent, company)
				result["attempt"] = attempt + 1
				results.append(result)
				print(_one_line(result))
	finally:
		frappe.set_user("Administrator")
		_disable_bench_user()
		frappe.db.commit()

	report = {
		"label": label,
		"site": frappe.local.site,
		"agent": agent,
		"company": company,
		"created": str(frappe.utils.now_datetime()),
		"results": results,
		"summary": summarize(results),
	}
	path = os.path.join(HERE, "results", f"{label}.json")
	with open(path, "w") as handle:
		json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
	print(f"saved {path}")
	print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
	return report["summary"]


def run_question(question, agent, company):
	from ai_agent.runtime import model_provider, tool_gateway
	from ai_agent.runtime.orchestrator import execute_run

	calls, tools = [], []
	original_step = model_provider.OpenAICompatibleProvider.create_step
	original_tool = tool_gateway.ToolGateway.authorize_and_execute

	def record_step(provider, *, instructions, input_items, tools, state):
		started = time.monotonic()
		response = original_step(provider, instructions=instructions, input_items=input_items, tools=tools, state=state)
		calls.append(_call_record(instructions, tools, response, time.monotonic() - started))
		return response

	def record_tool(gateway, tool_name, arguments, *args, **kwargs):
		result = original_tool(gateway, tool_name, arguments, *args, **kwargs)
		tools.append({
			"tool": tool_name,
			"arguments": arguments,
			"result_chars": len(json.dumps(result, ensure_ascii=False, default=str)),
			"error": (result or {}).get("error") if isinstance(result, dict) else None,
		})
		return result

	frappe.set_user("Administrator")
	# Memories stored by earlier benchmark runs would skew later measurements.
	frappe.db.delete("AI Memory", {"user": BENCH_USER})
	run_doc = frappe.get_doc({
		"doctype": "AI Run",
		"agent": agent,
		"user": BENCH_USER,
		"company": company,
		"prompt": question["text"],
		"context_doctype": question.get("context_doctype"),
		"context_name": question.get("context_name"),
		"status": "Queued",
		"started_at": frappe.utils.now_datetime(),
	})
	run_doc.insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.set_user(BENCH_USER)
	started = time.monotonic()
	with ExitStack() as stack:
		stack.enter_context(patch.object(model_provider.OpenAICompatibleProvider, "create_step", record_step))
		stack.enter_context(patch.object(tool_gateway.ToolGateway, "authorize_and_execute", record_tool))
		execute_run(run_doc.name)
	elapsed = time.monotonic() - started
	frappe.set_user("Administrator")

	run_doc.reload()
	totals = {
		"input_tokens": sum(c["input_tokens"] for c in calls),
		"cached_input_tokens": sum(c["cached_input_tokens"] for c in calls),
		"output_tokens": sum(c["output_tokens"] for c in calls),
	}
	totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
	model = calls[0]["model"] if calls else None
	return {
		"id": question["id"],
		"kind": question.get("kind"),
		"question": question["text"],
		"run": run_doc.name,
		"route": run_doc.get("route"),
		"status": run_doc.status,
		"model": model,
		"model_calls": len(calls),
		"tool_calls": len(tools),
		"seconds": round(elapsed, 1),
		**totals,
		"cost_usd": _cost(model, totals),
		"calls": calls,
		"tools": tools,
		"largest_tool_results": sorted(tools, key=lambda t: t["result_chars"], reverse=True)[:3],
		"answer_preview": (run_doc.final_answer or run_doc.error_summary or "")[:400],
	}


def _call_record(instructions, tool_schemas, response, seconds):
	usage = (response or {}).get("usage") or {}
	cached = (
		usage.get("prompt_cache_hit_tokens")
		or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
		or usage.get("cache_read_input_tokens")
		or 0
	)
	breakdown = {"system": 0, "user": 0, "assistant": 0, "tool_results": 0}
	for message in instructions.get("messages") or []:
		role = message.get("role")
		size = len(json.dumps(message.get("content"), ensure_ascii=False, default=str))
		if role == "assistant" and message.get("tool_calls"):
			size += len(json.dumps(message["tool_calls"], ensure_ascii=False))
		key = {"tool": "tool_results"}.get(role, role if role in breakdown else "system")
		breakdown[key] += size
	breakdown["tool_schemas"] = len(json.dumps(tool_schemas or [], ensure_ascii=False))
	message = (((response or {}).get("choices") or [{}])[0] or {}).get("message") or {}
	return {
		"model": instructions.get("model"),
		"seconds": round(seconds, 1),
		"input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
		"cached_input_tokens": int(cached or 0),
		"output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
		"message_count": len(instructions.get("messages") or []),
		"tool_schema_count": len(tool_schemas or []),
		"prompt_chars": breakdown,
		"tool_calls_requested": [
			((call.get("function") or {}).get("name")) for call in message.get("tool_calls") or []
		],
	}


def _cost(model, totals):
	prices = PRICES.get(model or "", PRICES["default"])
	fresh = totals["input_tokens"] - totals["cached_input_tokens"]
	return round(
		(fresh * prices["input"] + totals["cached_input_tokens"] * prices["cached_input"]
		 + totals["output_tokens"] * prices["output"]) / 1_000_000,
		5,
	)


def summarize(results):
	if not results:
		return {}
	chars = {"system": 0, "user": 0, "assistant": 0, "tool_results": 0, "tool_schemas": 0}
	for result in results:
		for call in result["calls"]:
			for key, value in call["prompt_chars"].items():
				chars[key] = chars.get(key, 0) + value
	total_chars = sum(chars.values()) or 1
	return {
		"questions": len(results),
		"avg_total_tokens": round(sum(r["total_tokens"] for r in results) / len(results)),
		"avg_model_calls": round(sum(r["model_calls"] for r in results) / len(results), 1),
		"avg_tool_calls": round(sum(r["tool_calls"] for r in results) / len(results), 1),
		"total_cost_usd": round(sum(r["cost_usd"] for r in results), 4),
		"prompt_share_percent": {k: round(v * 100 / total_chars, 1) for k, v in chars.items()},
		"per_question": [
			{k: r[k] for k in ("id", "attempt", "status", "total_tokens", "cached_input_tokens",
			                   "model_calls", "tool_calls", "seconds", "cost_usd")}
			for r in results
		],
	}


def _one_line(result):
	return (
		f"{result['id']}#{result.get('attempt', 1)} {result['status']}: {result['total_tokens']} tokens "
		f"({result['cached_input_tokens']} cached), {result['model_calls']} model calls, "
		f"{result['tool_calls']} tools, {result['seconds']}s"
	)


def _enable_bench_user():
	from ai_agent.tests.utils import quota_bypass

	with quota_bypass():
		if not frappe.db.exists("User", BENCH_USER):
			frappe.get_doc({
				"doctype": "User",
				"email": BENCH_USER,
				"first_name": "AI Benchmark",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("User", BENCH_USER, "enabled", 1, update_modified=False)
		user = frappe.get_doc("User", BENCH_USER)
		user.add_roles(*[role for role in BENCH_ROLES if frappe.db.exists("Role", role)])
	frappe.db.commit()


def _disable_bench_user():
	frappe.db.set_value("User", BENCH_USER, "enabled", 0, update_modified=False)
