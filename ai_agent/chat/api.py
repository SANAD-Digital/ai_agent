"""Desk chat API: the whitelisted endpoints the chat client calls (``ai_agent.chat.api.<name>``).

Chat API for the agent workspace (contract: docs/redesign/client-contract.md).

Sessions and messages are private to their owner (ai_agent.permissions).
Every assistant reply is produced by ai_agent.engine.runner in a background
job and streamed to the browser over the `ai_agent:chat` realtime event.

The code lives in common.py (shared shapes and checks), sessions.py, messages.py and extras.py; this module
re-exports every name so the public method paths and existing imports stay the same.
"""

from ai_agent.chat import common, extras, messages, sessions

for _module in (common, sessions, messages, extras):
    globals().update({_k: _v for _k, _v in vars(_module).items() if not _k.startswith("__")})
del _module
