"""Presentation layer: ERPNext data -> business view model -> presentation schema.

Presenters are deterministic and server-defined. The LLM decides what to ask and
what to conclude; column order, labels, formatting, badges and actions come from here.
"""

from ai_agent.presentation.registry import present  # noqa: F401
