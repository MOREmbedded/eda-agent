"""Typed errors. Every error carries a stable machine-readable code that is
returned to MCP clients, written to the audit log and used by tests."""


class AltiumAIError(Exception):
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error_code": self.code, "message": self.message, "details": self.details}


class ValidationFailed(AltiumAIError):
    code = "VALIDATION_FAILED"


class AuthenticationFailed(AltiumAIError):
    code = "AUTHENTICATION_FAILED"


class PermissionDenied(AltiumAIError):
    code = "PERMISSION_DENIED"


class NotFound(AltiumAIError):
    code = "NOT_FOUND"


class Conflict(AltiumAIError):
    code = "CONFLICT"


class InvalidStateTransition(AltiumAIError):
    code = "INVALID_STATE_TRANSITION"


class IntegrityFailure(AltiumAIError):
    code = "INTEGRITY_FAILURE"


class WorkerNotResponding(AltiumAIError):
    code = "WORKER_NOT_RESPONDING"


class ExecutionTimeout(AltiumAIError):
    code = "EXECUTION_TIMEOUT"


class WorkerQuarantined(AltiumAIError):
    code = "WORKER_QUARANTINED"


class NativeOperationFailed(AltiumAIError):
    code = "NATIVE_OPERATION_FAILED"


class VerificationFailed(AltiumAIError):
    code = "VERIFICATION_FAILED"


class ProtocolError(AltiumAIError):
    code = "PROTOCOL_ERROR"
