import frappe

from ai_agent.metrics.engine import MetricError, query
from ai_agent.metrics.periods import PeriodError


def handle_query(arguments, permission_context):
    """Handle metrics.query: one compact, permission-scoped answer from AI Metric."""
    company = (
        arguments.get("company")
        or getattr(permission_context, "company", None)
        or frappe.defaults.get_user_default("Company", permission_context.user)
    )
    try:
        return query(
            metric=arguments.get("metric"),
            period=arguments.get("period") or "this_month",
            dimensions=arguments.get("dimensions") or [],
            filters=arguments.get("filters") or {},
            compare=arguments.get("compare"),
            top=arguments.get("top"),
            company=company,
            sort=arguments.get("sort"),
            user=permission_context.user,
        )
    except (MetricError, PeriodError) as exc:
        return {"error": str(exc)}
