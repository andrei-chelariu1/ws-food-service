# ADR 0002 — `bcrypt` directly, behind a `PasswordHasher` Protocol

**Status:** accepted · **Deviates from:** `ARCHITECTURE.md`, which specifies `passlib[bcrypt]`

## Context

`ARCHITECTURE.md` specifies `passlib[bcrypt]`. passlib is the conventional choice and gives a
uniform interface over many schemes plus `CryptContext` migration support.

## Decision

Use the **`bcrypt` package directly**, wrapped in a `PasswordHasher` Protocol with a
`BcryptPasswordHasher` implementation.

## Why

**1. passlib 1.7.4 is broken against modern bcrypt.** It reads `bcrypt.__about__.__version__`,
which bcrypt removed in 4.1. The result is a startup warning — and depending on the path, a
failure — in the code that hashes passwords. passlib itself has had no release since 2020, so
this is not a wait-for-the-patch situation.

**2. We use exactly one scheme.** passlib's value is a uniform interface over many; a
`CryptContext` configured with a single scheme is abstraction we pay for and do not use.

**3. The Protocol is a better answer than the library, either way.** This is the real reason.

```python
class PasswordHasher(Protocol):
    def hash(self, plain: str) -> str: ...
    def verify(self, plain: str, hashed: str) -> bool: ...
    def needs_rehash(self, hashed: str) -> bool: ...
```

Three methods — the complete list of what `UserService` may assume. From that:

* **argon2 is a 20-line adapter** with zero service changes. That is the migration story passlib
  is usually chosen for, and we get it without the dependency.
* **Tests inject a cheap fake** instead of paying ~250ms per bcrypt hash. Cost-12 hashing in a
  suite that hashes hundreds of times is the difference between a fast suite and one people skip.
* `needs_rehash()` is what makes a **cost increase actually reach existing accounts** — they are
  upgraded transparently on their next successful login. Without it, raising `BCRYPT_ROUNDS`
  protects only users who register afterwards.

Choosing the library directly and inverting the dependency ourselves is a strictly better lesson
than importing an abstraction: the seam is visible, three methods long, and in this repo.

## Consequences

* We own two bcrypt details passlib would have handled:
  * **the 72-byte limit** — bcrypt silently truncates beyond it, so `BcryptPasswordHasher`
    **rejects** over-long input rather than accepting a password whose tail is ignored;
  * **encoding** — explicit UTF-8 encode/decode at the boundary.
  Both are in one class, tested, and documented where they happen.
* `needs_rehash` is our own cost-factor comparison rather than passlib's scheme-aware check. Fine
  for one scheme; it is the thing to revisit when a second one appears.
* Not what `ARCHITECTURE.md` says.

## Alternatives

| Option | Rejected because |
|---|---|
| `passlib[bcrypt]` (as specified) | crashes on bcrypt ≥ 4.1; unmaintained since 2020 |
| `argon2-cffi` as the default | a defensible and arguably better default; bcrypt keeps parity with `ARCHITECTURE.md`'s *intent*, and the Protocol makes the switch cheap |
| `hashlib.scrypt` (stdlib) | no dependency, but no rehash story and easier to misconfigure |
