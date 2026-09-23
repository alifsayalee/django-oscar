class ApiProblem(Exception):
    """An error answer from this app's API: HTTP status, stable code, readable message."""

    def __init__(self, status_code: int, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, **self.extra}
