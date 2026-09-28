"""Domain errors of the AML adapter, each bound to the HTTP status the contract expects."""

from __future__ import annotations


class AdapterError(Exception):
    status = 500
    code = "internal"


class ContractError(AdapterError):
    """Request violates the AML schema; the platform does not retry it."""

    status = 422
    code = "invalid_request"


class MalformedBody(AdapterError):
    status = 400
    code = "malformed_body"


class Unauthorized(AdapterError):
    status = 401
    code = "unauthorized"


class PayloadTooLarge(AdapterError):
    status = 413
    code = "payload_too_large"


class Conflict(AdapterError):
    """Same request_id reused with a different payload."""

    status = 409
    code = "conflict"


class Busy(AdapterError):
    """Capacity exhausted; answered with 429 + Retry-After."""

    status = 429
    code = "busy"


class Unavailable(AdapterError):
    """Transient failure (embedding API, worker restart); safe to retry."""

    status = 503
    code = "unavailable"


ERRORS_BY_CODE: dict[str, type[AdapterError]] = {
    cls.code: cls for cls in (ContractError, Conflict, Busy, Unavailable, AdapterError)
}
