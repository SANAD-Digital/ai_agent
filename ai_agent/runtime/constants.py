MAX_STEPS_DEFAULT = 12
MAX_TOOL_CALLS_DEFAULT = 25
MAX_ROWS_DEFAULT = 500
MAX_RESULT_BYTES_DEFAULT = 102400
MAX_RUN_SECONDS_DEFAULT = 300
APPROVAL_TTL_MINUTES_DEFAULT = 30
MEMORY_RETENTION_DAYS_DEFAULT = 30

ALLOWED_AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})
ALLOWED_OPERATORS = frozenset({
    "=", "!=", ">", "<", ">=", "<=", "in", "not in",
    "like", "not like", "between", "is set", "is not set",
    "descendants of", "ancestors of", "like_normalized",
})

# Arabic orthographic variants folded together for like_normalized lookups
# (kept consistent on both the SQL column and the search value).
ARABIC_NORMALIZATION_MAP = (
    ("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي"),
)

SIDE_EFFECT_READ_ONLY = "read_only"
SIDE_EFFECT_PREPARE = "prepare_only"
SIDE_EFFECT_WRITE = "write"
SIDE_EFFECT_DESTRUCTIVE = "destructive"
SIDE_EFFECT_EXTERNAL = "external_side_effect"

TOOL_ACCESS_READ = "Read"
TOOL_ACCESS_PREPARE = "Prepare"
TOOL_ACCESS_WRITE = "Write"
TOOL_ACCESS_DESTRUCTIVE = "Destructive"

DENYLIST_DOCTYPES = frozenset({
    "Activity Log", "Comment", "Version", "DocShare",
    "User", "Role", "Has Role", "User Permission",
    "Permission Log", "Access Log", "Session",
    "OAuth Bearer Token", "OAuth Client",
    "Connected App", "Token Cache",
    "Integration Request", "Webhook",
    "File", "Print Format",
})

RUN_STATUS_QUEUED = "Queued"
RUN_STATUS_PLANNING = "Planning"
RUN_STATUS_RUNNING = "Running"
RUN_STATUS_WAITING = "Waiting Approval"
RUN_STATUS_RESUMING = "Resuming"
RUN_STATUS_COMPLETED = "Completed"
RUN_STATUS_FAILED = "Failed"
RUN_STATUS_CANCELLED = "Cancelled"
RUN_STATUS_TIMED_OUT = "Timed Out"

ACTIVE_RUN_STATUSES = frozenset({
    RUN_STATUS_QUEUED, RUN_STATUS_PLANNING, RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING, RUN_STATUS_RESUMING,
})

REALTIME_EVENTS = {
    "run_updated": "ai_agent:run_updated",
    "step_added": "ai_agent:step_added",
    "approval_required": "ai_agent:approval_required",
    "artifact_created": "ai_agent:artifact_created",
    "run_completed": "ai_agent:run_completed",
    "run_failed": "ai_agent:run_failed",
}

SKILL_INSTRUCTIONS_DIR = "skills"
