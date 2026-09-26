"""承诺门禁服务向 API 和 CLI 暴露的稳定错误。"""


class GateError(RuntimeError):
    code = "gate_error"
    status = 400


class NotFound(GateError):
    code = "not_found"
    status = 404


class Conflict(GateError):
    code = "conflict"
    status = 409


class Forbidden(GateError):
    code = "forbidden"
    status = 403


class InvalidState(GateError):
    code = "invalid_state"
    status = 409


class ValidationFailed(GateError):
    code = "validation_failed"
    status = 422
