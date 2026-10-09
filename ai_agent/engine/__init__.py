"""The chat engine: a pure event-producing agent loop (loop.py) driven by a
Frappe job (runner.py). Providers stream native events; the loop turns them
into typed engine events; the runner persists blocks and publishes realtime.

Nothing under engine/ except runner.py, tools.py, skills.py, memory.py,
approvals.py and providers/factory.py touches frappe. loop.py, types.py,
events.py, context/compactor.py and the provider stream adapters are plain
Python and are unit-tested without a site.
"""
