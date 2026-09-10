# `app/shared/schemas/` — Base DTOs and Response Envelopes

## Purpose

`ApiModel` (the base every DTO inherits), `Page[T]` (the paginated envelope),
`ProblemDetail` (the documented error shape), and reusable OpenAPI `responses` fragments.

---

## Why DTOs at all — why not return the ORM entity?

Returning `User` from an endpoint serialises **every column**, including
`hashed_password`. That is not hypothetical: it is one of the most common data leaks in
ORM-backed APIs, and it happens the moment someone adds a column and forgets that a model is
serialised somewhere.

`UserRead` is an **allowlist**. A new column is invisible to clients until someone
deliberately adds it. Secure by default rather than secure by remembering.

Compare a denylist (`exclude={"hashed_password"}`): it protects only the places somebody
remembered to write it.

Beyond safety, the split decouples: renaming a database column does not break every client,
because the schema absorbs the change. The API contract and the storage schema evolve at
different speeds.

---

## `ApiModel` — four config choices, each load-bearing

| Setting | Why |
|---|---|
| `from_attributes=True` | `UserRead.model_validate(orm_object)` reads attributes off an entity, so no mapping is hand-written |
| `extra="forbid"` | **an unknown request field is a 422, not a shrug** |
| `str_strip_whitespace=True` | `" a@b.com "` and `"a@b.com"` must not become two accounts |
| `validate_assignment=True` | mutating a DTO re-validates; catches assigning a value that would never have passed on input |
| `use_enum_values=False` | keep real Enum members in Python, so `role is Role.ADMIN` works |

### `extra="forbid"` is doing security work

It blocks two distinct classes of problem:

**Typos.** `{"emial": "..."}` would otherwise be silently ignored, creating a user with no
email while everyone wastes an afternoon.

**Mass assignment.** A client cannot smuggle `{"role": "ADMIN"}` into a schema that does not
declare it. Combined with narrow input schemas, privilege escalation via the registration
endpoint is not something we *validate against* — it is unexpressible. Tested by
`tests/modules/users/test_api.py::test_register_rejects_client_supplied_role`.

---

## The Create / Update / Read split is not boilerplate

It is the mechanism that makes the above true. A single shared `UserSchema` used for both
input and output would have to contain `role` (responses show it) — at which point a client
can `POST /auth/register {"role": "ADMIN"}` and self-promote. And `id`, so a client can try
to choose its own primary key. And if it ever carried `hashed_password` for internal use,
one careless `return schema` leaks the hash.

Three narrow schemas make each of those *structurally impossible*.

---

## `Page[T]` — why not a bare list

A bare `[...]` gives the client no way to know whether more data exists. `Page` carries
`total`, `pages`, `has_next`, `has_previous` — and because it is generic, that shape is
defined **once** and reused by every list endpoint with correct OpenAPI types
(`Page[UserRead]`, `Page[FoodItemRead]`). One definition, N precise instantiations: DRY
*and* precise.

`pages`/`has_next`/`has_previous` are `computed_field` properties, not stored columns, so
they cannot contradict `total`/`page`/`size`. Storing them is how a response ends up saying
`has_next: true` on the last page.

`Page.create(...)` is a named constructor so no endpoint hand-assembles the envelope.
Building `{"items": ..., "meta": {...}}` inline in eight routers is exactly how `page` and
`size` end up swapped in one of them.

`total` costs an extra `COUNT(*)`. A deliberate trade: without it a UI cannot render "page 3
of 12" or even a correct "next" button. On tables large enough for the count to hurt, switch
that endpoint to cursor pagination — see the note in `app/shared/utils/pagination.py`.

---

## `ProblemDetail` and the error fragments

`ProblemDetail` exists **purely for OpenAPI documentation** — the runtime responses come
from `app/core/exceptions.py`. Declaring it is what makes the error shape appear in Swagger,
so clients can code against it instead of discovering it by triggering failures in
production.

It sets `extra="allow"`, so adding a field later cannot break a strict client.

The `ERROR_RESPONSES_*` fragments are spread into route decorators:

```python
@router.patch("/{item_id}", responses=ERROR_RESPONSES_WRITE)
```

instead of restating the same four dicts on forty endpoints. Composed by layering — `WRITE`
includes `READ` includes `AUTH` — so each is defined once.

---

## `MessageResponse`

For operations with nothing meaningful to return (logout, mark-read). Preferred over `204 No
Content` here because the JSON envelope stays uniform: a client's response parser never has
to special-case an empty body.
