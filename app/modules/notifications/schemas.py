"""DTOs for the notifications module.

There is no `NotificationCreate`. That is not an omission — notifications are
created by *the system* in response to domain events, never by a client. Exposing a
create endpoint would let any user send any other user an arbitrary message, which
is a spam and phishing vector, not a feature.

The absence of an input schema is therefore part of the security design.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.modules.notifications.models import NotificationType
from app.shared.schemas.base_schema import ApiModel, TimestampedSchema


class NotificationRead(TimestampedSchema):
    """One notification."""

    id: uuid.UUID
    type: NotificationType
    title: str
    body: str
    related_donation_id: uuid.UUID | None
    read_at: datetime | None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class UnreadCountResponse(ApiModel):
    """Just the badge number.

    A dedicated endpoint because this is the most frequently polled value in the
    application. Returning it via the full list endpoint would transfer a page of
    rows to render a single integer; here it is one indexed `COUNT(*)` against the
    partial unread index (see models.py).
    """

    unread: int = Field(description="Number of unread notifications")
