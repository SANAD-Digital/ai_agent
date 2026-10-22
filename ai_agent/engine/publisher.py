"""Realtime events of one assistant message: delta coalescing and a monotonic event sequence."""

from __future__ import annotations

import time

import frappe

from ai_agent.engine.events import BlockDelta

REALTIME_EVENT = "ai_agent:chat"
TAIL_KEY = "ai:chat:tail:{}"
DELTA_COALESCE_CHARS = 24
DELTA_COALESCE_SECONDS = 0.06


class Publisher:
    """Realtime publisher with delta coalescing and a monotonic event sequence."""

    def __init__(self, session, message, user, start_seq=0):
        self.session, self.message, self.user = session, message, user
        self.seq = start_seq
        self._delta = None      # (block_seq, text, t0)

    def publish(self, event):
        if isinstance(event, BlockDelta):
            if self._delta and self._delta[0] == event.block_seq and len(self._delta[1]) < DELTA_COALESCE_CHARS and time.monotonic() - self._delta[2] < DELTA_COALESCE_SECONDS:
                self._delta = (event.block_seq, self._delta[1] + event.delta, self._delta[2])
                return
            self.flush()
            self._delta = (event.block_seq, event.delta, time.monotonic())
            if len(event.delta) >= DELTA_COALESCE_CHARS:
                self.flush()
            return
        self.flush()
        self._send(event.to_dict())

    def flush(self):
        if self._delta:
            block_seq, text, _ = self._delta
            self._delta = None
            self._send({"type": "block.delta", "block_seq": block_seq, "delta": text})

    def _send(self, payload):
        self.seq += 1
        try:
            frappe.publish_realtime(REALTIME_EVENT, {"session": self.session, "message": self.message, "seq": self.seq, "event": payload}, user=self.user)
        except Exception:
            pass   # realtime is best-effort: the browser resyncs from the saved blocks
        # The live sequence rides in the delta tail so a resync (get_message) can
        # tell the client exactly which realtime events are already included.
        try:
            frappe.cache.hset(TAIL_KEY.format(self.message), "_seq", self.seq)
        except Exception:
            pass   # cache hint only: a resync without it replays from the saved blocks
