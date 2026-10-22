"""Tool names: the registry and AI Tool Call use ``data.query``; providers and MCP hosts need ``data__query``
(a dot is not allowed in a tool name there). AI Message Block keeps the name the provider sent, so history
replays unchanged. Convert only through these two functions."""


def wire_name(registry_name):
	return str(registry_name or "").replace(".", "__")


def registry_name(wire):
	return str(wire or "").replace("__", ".")
