# Architecture Decision Records

## Purpose

One file per decision that was **not obvious** and would otherwise be re-litigated every few
months. An ADR answers the question a future reader actually has: *"why is it like this, and what
did you already consider?"*

## What belongs here

| ✅ | ❌ |
|---|---|
| A choice with a real, defensible alternative | how something works — that is a topic doc |
| A deviation from `ARCHITECTURE.md` | a decision nobody would question |
| The alternatives, and why each lost | a to-do list |

Three of the four here are deviations from [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md). That
document is the design brief and is left untouched; **a design doc that silently disagrees with
the code is worse than one that is openly amended.**

## The rule: ADRs are immutable

> Never rewrite an ADR. If a decision is reversed, write a new one that supersedes it and mark
> the old one `superseded by 000N`.

An amended ADR loses the only thing it was for — telling you what was known *at the time the call
was made*. "We chose X because Y was unmaintained" stays true and useful even after Y is revived;
edited to match today, it becomes a description of the present, which is what topic docs are for.

## Format

```markdown
# ADR NNNN — one-line decision

**Status:** accepted | superseded by 000N · **Revisit when:** <trigger>

## Context      the constraint or question. No solution yet.
## Decision     what we did, stated in one or two sentences.
## Why          the argument. This is the whole document.
## Consequences what we now own, including the bad parts.
## Alternatives a table: option | rejected because
```

`Consequences` is the section people skip and the one that pays. ADR 0002 owns bcrypt's 72-byte
limit; ADR 0004 owns "notification delivery is not durable". Writing the cost down is what makes
the trade-off real rather than rhetorical.

## Index

| ADR | Decision | Deviates from `ARCHITECTURE.md` |
|---|---|---|
| [0001](0001-pyjwt-over-python-jose.md) | PyJWT instead of python-jose | yes |
| [0002](0002-bcrypt-direct-over-passlib.md) | `bcrypt` directly, behind a Protocol | yes |
| [0003](0003-async-alembic-single-driver.md) | Async Alembic, one driver | yes |
| [0004](0004-backgroundtasks-behind-protocol.md) | `BackgroundTasks` behind a `TaskDispatcher` | no — a scope decision |

## Adding one

Next number, never reuse. Written *when the decision is made*, not reconstructed later — the
alternatives you actually weighed are the part that cannot be recovered afterwards.
