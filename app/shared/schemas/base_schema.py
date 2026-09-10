"""Base DTO classes and the shared response envelopes.

WHY DTOs AT ALL — WHY NOT RETURN THE ORM ENTITY?
------------------------------------------------
Returning `User` from an endpoint would serialise **every column**, including
`hashed_password`. That is not a hypothetical: it is one of the most common data
leaks in ORM-backed APIs, and it happens the moment someone adds a column and
forgets that a model is being serialised somewhere.

`UserRead` is an *allowlist*. A new column is invisible to clients until someone
deliberately adds it to the schema. Secure by default rather than secure by
remembering.

Beyond safety, the separation buys decoupling: renaming a database column does
not break every client, because the schema absorbs the change. The API contract
and the storage schema are allowed to evolve at different speeds.

WHY A GENERIC `Page[T]` INSTEAD OF A BARE LIST
----------------------------------------------
A bare `[...]` gives the client no way to know whether more data exists. `Page`
carries `total`, `has_next`, `pages` — and because it is generic, that shape is
defined once and reused by every list endpoint, with correct OpenAPI types for
each (`Page[UserRead]`, `Page[FoodItemRead]`). One definition, N typed
instantiations: DRY *and* precise.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every request/response DTO in the project.

    Configuration decisions, and why each one:

    `from_attributes=True`
        Lets `UserRead.model_validate(user_orm_object)` read attributes off an
        ORM instance. Without it every mapping would be written by hand.

    `extra="forbid"`
        An unknown field in a request body is a **400, not a shrug**. If a client
        sends `{"emial": ...}`, silently ignoring it means the user is created
        with no email and everyone wastes an afternoon. Also blocks mass
        assignment: a client cannot smuggle `{"role": "ADMIN"}` into a schema
        that does not declare it.

    `str_strip_whitespace=True`
        `" alice@example.com "` and `"alice@example.com"` must not become two
        accounts. Normalising at the edge means no downstream code has to
        remember to `.strip()`.

    `validate_assignment=True`
        Mutating a DTO after construction re-validates. Cheap, and catches the
        case where code assigns a value that would never have passed on input.

    `use_enum_values=False`
        Keep real Enum members in Python (so `role is Role.ADMIN` works);
        serialisation to a string is handled at the JSON boundary. Comparing
        enums by string value is how you get bugs from a renamed member.
    """

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
        populate_by_name=True,
    )


class TimestampedSchema(ApiModel):
    """Mixin for responses that expose audit columns.

    Read-only by nature — these are set by the database (see
    shared/db/mixins.py), so they appear in responses and never in requests.
    """

    created_at: datetime
    updated_at: datetime


class PageMeta(ApiModel):
    """Pagination metadata, kept separate from the items.

    `total` costs an extra `COUNT(*)`. That is a deliberate trade: without it a
    UI cannot render "page 3 of 12" or even a correct "next" button. On tables
    large enough for the count to hurt, switch that endpoint to cursor
    pagination — see the note in app/shared/utils/pagination.py.
    """

    total: int = Field(description="Total items matching the query, ignoring pagination")
    page: int = Field(description="Current page, 1-based")
    size: int = Field(description="Items per page")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        """Total page count. Computed rather than stored so it cannot disagree
        with `total`/`size`."""
        return math.ceil(self.total / self.size) if self.size else 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_previous(self) -> bool:
        return self.page > 1


class Page(ApiModel, Generic[T]):
    """Generic paginated response: `Page[FoodItemRead]`.

    FastAPI resolves the type parameter into the OpenAPI schema, so clients get
    a precise `items: FoodItemRead[]` — not `items: object[]`.
    """

    items: list[T]
    meta: PageMeta

    @classmethod
    def create(cls, items: list[T], *, total: int, page: int, size: int) -> Page[T]:
        """Named constructor so no endpoint hand-assembles the envelope.

        The alternative — building `{"items": ..., "meta": {...}}` inline in each
        router — is exactly how `page`/`size` end up swapped in one endpoint out
        of eight.
        """
        return cls(items=items, meta=PageMeta(total=total, page=page, size=size))


class ProblemDetail(ApiModel):
    """RFC 9457 error body. Declared here purely for OpenAPI documentation.

    The runtime responses are produced by `app/core/exceptions.py`; this class is
    what makes the error shape appear in Swagger, so clients can code against it
    instead of discovering it by triggering failures in production.
    """

    model_config = ConfigDict(extra="allow")  # future fields must not break clients

    type: str = Field(description="URI reference identifying the problem type")
    title: str = Field(description="Short, human-readable summary")
    status: int = Field(description="HTTP status code")
    detail: str = Field(description="Human-readable explanation, safe to display")
    instance: str = Field(description="Path of the failing request")
    code: str = Field(
        description="Stable machine-readable code. BRANCH ON THIS, never on `detail`."
    )
    request_id: str = Field(description="Correlation id — quote it in bug reports")
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Field-level details, present on validation failures"
    )


class MessageResponse(ApiModel):
    """For operations with nothing meaningful to return (logout, mark-read).

    Better than `204 No Content` here because the JSON envelope stays uniform:
    a client's response parser never has to special-case an empty body.
    """

    message: str


# Reusable OpenAPI `responses` fragments. Routers spread these into their
# decorators (`responses={**ERROR_RESPONSES_AUTH}`) instead of restating the
# same four dicts on forty endpoints.
_PROBLEM_CONTENT = {"application/problem+json": {"schema": ProblemDetail.model_json_schema()}}


def _problem(status_code: int, description: str) -> dict[int | str, dict[str, Any]]:
    """Build one OpenAPI `responses` entry documenting the problem+json body."""
    return {status_code: {"description": description, "content": _PROBLEM_CONTENT}}


ERROR_RESPONSE_401 = _problem(401, "Missing, invalid, or revoked token")
ERROR_RESPONSE_403 = _problem(403, "Authenticated but not permitted")
ERROR_RESPONSE_404 = _problem(404, "Resource not found")
ERROR_RESPONSE_409 = _problem(409, "Conflict or business rule violation")
ERROR_RESPONSE_422 = _problem(422, "Request validation failed")
ERROR_RESPONSE_429 = _problem(429, "Rate limit exceeded")

ERROR_RESPONSES_AUTH: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSE_401,
    **ERROR_RESPONSE_403,
    **ERROR_RESPONSE_429,
}
ERROR_RESPONSES_READ: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES_AUTH,
    **ERROR_RESPONSE_404,
}
ERROR_RESPONSES_WRITE: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES_READ,
    **ERROR_RESPONSE_409,
    **ERROR_RESPONSE_422,
}
