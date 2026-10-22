"""How the runner saves the blocks of a message: streamed blocks are written in batches, finished blocks
verbatim (Document.insert would HTML-sanitize them), and the live text a reconnecting browser reads is kept in
a Redis tail. A mixin of ``runner.Runner``: it uses ``self.msg``, ``self.blocks``, ``self.tail`` and
``self.last_flush``."""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import cint, now_datetime

TAIL_SECONDS = 0.3
FLUSH_SECONDS = 2.0


def stored_kind(kind):
    """The block type as the site's schema knows it. Code deployed before `bench migrate` may emit a type the
    Select field does not list yet; storing it would fail the whole reply ("Block Type cannot be ..."), so it is
    kept as a status block and logged once per type instead."""
    options = (frappe.get_meta("AI Message Block").get_field("block_type").options or "").split("\n")
    if kind in options:
        return kind
    key = f"ai_agent:unknown_block_type:{kind}"
    if not frappe.cache.get_value(key):
        frappe.cache.set_value(key, 1, expires_in_sec=3600)
        frappe.log_error(f"Block type '{kind}' is not in AI Message Block.block_type on this site: run bench migrate.", "AI Chat schema behind code")
    return "status"


class BlockStore:
    def open_block(self, seq, kind, id="", name=""):
        doc = frappe.get_doc({
            "doctype": "AI Message Block",
            "message": self.msg.name,
            "seq": seq,
            "block_type": stored_kind(kind),
            "status": "streaming",
            "tool_call_id": id or None,
            "tool_name": name or None,
            "started_at": now_datetime(),
        })
        doc.insert(ignore_permissions=True)
        self.blocks[seq] = {"name": doc.name, "kind": kind, "text": "", "started": time.monotonic(), "dirty": False}

    def close_block(self, seq, block):
        entry = self.blocks.get(seq)
        if entry is None:
            self.insert_block(seq, block)
            return
        values = {
            "status": "failed" if block.get("is_error") else "completed",
            "content": block.get("text") or entry["text"],
            "completed_at": now_datetime(),
            "duration_ms": int((time.monotonic() - entry["started"]) * 1000),
        }
        if block.get("data"):
            values["content_json"] = json.dumps(block["data"], ensure_ascii=False, default=str)
        if block.get("signature"):
            values["signature"] = block["signature"]
        if block.get("name") and not entry.get("named"):
            values["tool_name"] = block["name"]
        if block.get("id"):
            values["tool_call_id"] = block["id"]
        frappe.db.set_value("AI Message Block", entry["name"], values, update_modified=False)
        entry["dirty"] = False
        entry["closed"] = True
        self.tail_delete(seq)
        frappe.db.commit()

    def insert_block(self, seq, block, extra=None):
        extra = extra or {}
        # Document.insert() would HTML-sanitize the content ("<<<UNTRUSTED_DATA" markers,
        # JSON with "<"), so text lands through db.set_value, which stores it verbatim.
        doc = frappe.get_doc({
            "doctype": "AI Message Block",
            "message": self.msg.name,
            "seq": seq,
            "block_type": stored_kind(block.get("kind")),
            "status": "failed" if block.get("is_error") else "completed",
            "tool_call_id": block.get("id") or None,
            "tool_name": block.get("name") or None,
            "tool_call": extra.get("tool_call") or None,
            "approval": extra.get("approval") or None,
            "artifact": (block.get("data") or {}).get("artifact") if block.get("kind") == "artifact" else None,
            "started_at": now_datetime(),
            "completed_at": now_datetime(),
            "duration_ms": cint(extra.get("duration_ms")),
        })
        doc.insert(ignore_permissions=True)
        frappe.db.set_value("AI Message Block", doc.name, {
            "content": block.get("text") or "",
            "content_json": json.dumps(block["data"], ensure_ascii=False, default=str) if block.get("data") else None,
        }, update_modified=False)
        self.blocks[seq] = {"name": doc.name, "kind": block.get("kind"), "text": block.get("text") or "", "started": time.monotonic(), "dirty": False, "closed": True}

    def maybe_flush(self, force=False):
        if not force and time.monotonic() - self.last_flush < FLUSH_SECONDS:
            return
        self.last_flush = time.monotonic()
        dirty = False
        for entry in self.blocks.values():
            if entry.get("dirty") and not entry.get("closed"):
                frappe.db.set_value("AI Message Block", entry["name"], "content", entry["text"], update_modified=False)
                entry["dirty"] = False
                dirty = True
        if dirty:
            frappe.db.commit()

    def tail_write(self, seq, text):
        """The live text a reconnecting browser reads. It is the whole block each time, so it is written at most
        every TAIL_SECONDS: per delta it grew quadratically with long answers (128K-token replies)."""
        now = time.monotonic()
        stamps = self.__dict__.setdefault("tail_at", {})
        if now - stamps.get(seq, 0) < TAIL_SECONDS:
            return
        stamps[seq] = now
        try:
            frappe.cache.hset(self.tail, str(seq), text)
        except Exception:
            pass   # live tail is a reconnect hint; the block itself is saved

    def tail_delete(self, seq):
        try:
            frappe.cache.hdel(self.tail, str(seq))
        except Exception:
            pass   # cache cleanup only: the tail key expires on its own

    def message_text(self):
        rows = frappe.get_all("AI Message Block", filters={"message": self.msg.name, "block_type": "text"}, fields=["content"], order_by="seq asc")
        return "\n\n".join(r.content for r in rows if r.content)
