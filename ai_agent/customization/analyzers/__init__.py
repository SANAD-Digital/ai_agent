"""Static analyzers for customization code: Python (Server Script), JavaScript (Client Script), SQL
(Query Report). Each returns ``list[Check]`` (contract.py); docs/customization/01 §3.5.

``safe_exec`` is a Python sandbox, not a data-security boundary: in exec mode it hands scripts the full
``frappe.get_doc``/``get_all``/``db.set_value``/``sendmail``/``make_post_request``. These analyzers are
the line of defence, so they fail closed.
"""
