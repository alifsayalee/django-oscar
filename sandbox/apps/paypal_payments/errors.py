from typing import Any


class ApiProblem(Exception):
    """
    The one failure type the API views turn into a JSON error response.

    ``outcome_unknown`` is true when a write may have taken effect at PayPal
    even though no answer was read. The caller must not treat the request as
    failed; repeating it is safe, because the repeat is answered from the
    stored claim and never sends a second write.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        issue: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.issue = issue
        self.extra = extra or {}

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.issue:
            body["paypalIssue"] = self.issue
        if self.outcome_unknown:
            body["outcomeUnknown"] = True
        body.update(self.extra)
        return body


def bad_request(message: str) -> ApiProblem:
    return ApiProblem(400, "invalid_request", message)


def not_found(what: str) -> ApiProblem:
    return ApiProblem(404, "not_found", "%s not found." % what)


def conflict(code: str, message: str, **extra: Any) -> ApiProblem:
    return ApiProblem(409, code, message, extra=extra)
