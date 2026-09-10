"""Domain errors for the notifications module."""

from __future__ import annotations

from app.core.exceptions import NotFoundError, PermissionDeniedError


class NotificationNotFoundError(NotFoundError):
    """404."""

    def __init__(self, identifier: object = None) -> None:
        super().__init__("Notification", identifier)
        self.code = "NOTIFICATION_NOT_FOUND"


class NotificationAccessDeniedError(PermissionDeniedError):
    """403 when reading or dismissing someone else's notification.

    Its own class rather than a generic 403 because notifications are the most
    IDOR-prone resource in the application: they are enumerable by id, they are
    numerous, and their content leaks who is receiving food from whom. Naming the
    error makes it greppable and gives the audit log a distinct signal.
    """

    code = "NOTIFICATION_ACCESS_DENIED"

    def __init__(self) -> None:
        super().__init__("You do not have access to this notification")
