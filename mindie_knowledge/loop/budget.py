"""Admission deferral shared by the bounded material index queue."""


class BudgetExceeded(RuntimeError):
    def __init__(self, message, *, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at
