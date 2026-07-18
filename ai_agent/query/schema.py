"""Query Specification schema for the dynamic query engine."""
from ai_agent.runtime.constants import ALLOWED_AGGREGATES, ALLOWED_OPERATORS

QUERY_SPEC_SCHEMA = {
    "type": "object",
    "required": ["doctype", "fields"],
    "properties": {
        "doctype": {"type": "string"},
        "parent_doctype": {
            "type": "string",
            "description": "Optional parent DocType when querying child-table rows and filtering with parent.<field>.",
        },
        "fields": {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "aggregate": {"type": "string", "enum": list(ALLOWED_AGGREGATES)},
                            "alias": {"type": "string"},
                        },
                    },
                ]
            },
            "minItems": 1,
            "maxItems": 50,
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "operator": {"type": "string", "enum": list(ALLOWED_OPERATORS)},
                    "value": {},
                },
                "required": ["field", "operator"],
            },
            "maxItems": 20,
        },
        "group_by": {"type": "array", "items": {"type": "string"}},
        "order_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                },
            },
        },
        "distinct": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 500},
        "offset": {"type": "integer", "default": 0, "minimum": 0},
    },
}
