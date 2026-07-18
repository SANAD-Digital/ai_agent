class MYSYSAgentError(Exception):
    pass


class PermissionDenied(MYSYSAgentError):
    pass


class ToolDenied(MYSYSAgentError):
    pass


class SkillDenied(MYSYSAgentError):
    pass


class SkillNotFound(MYSYSAgentError):
    pass


class QueryValidationError(MYSYSAgentError):
    pass


class QueryCostError(MYSYSAgentError):
    pass


class ApprovalRequired(MYSYSAgentError):
    pass


class ApprovalExpired(MYSYSAgentError):
    pass


class ApprovalAlreadyExecuted(MYSYSAgentError):
    pass


class RunCancelled(MYSYSAgentError):
    pass


class RunTimedOut(MYSYSAgentError):
    pass


class RunLimitExceeded(MYSYSAgentError):
    pass


class ModelProviderError(MYSYSAgentError):
    pass


class SchemaDiscoveryError(MYSYSAgentError):
    pass


class IdempotencyConflict(MYSYSAgentError):
    pass


class ConcurrencyConflict(MYSYSAgentError):
    pass
