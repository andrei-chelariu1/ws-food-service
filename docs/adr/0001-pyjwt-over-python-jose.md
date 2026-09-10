# ADR 0001 — PyJWT instead of python-jose

**Status:** accepted · **Deviates from:** `ARCHITECTURE.md`, which specifies
`python-jose[cryptography]`

## Context

`ARCHITECTURE.md` lists `python-jose[cryptography]` for JWT handling. We need to sign and verify
HS256 tokens with `typ`, `jti`, `iat` and `exp` claims. Nothing more — no JWE, no JWK sets, no
key rotation endpoints.

## Decision

Use **PyJWT**.

## Why

**Maintenance.** python-jose's last release was 3.3.0 in 2021. A library on the authentication
path is the last place to accept an unmaintained dependency: when a signature-verification issue
is found, "wait for upstream" is not an option. PyJWT is actively released and is the
implementation most JWT documentation and most security advisories are written against.

**Scope.** python-jose implements the whole JOSE suite — JWS, JWE, JWK, JWA. We use one
algorithm on one token type. Paying for four specifications to use part of one is surface area
without benefit, and every extra algorithm a library supports is an algorithm-confusion
consideration you inherit.

**The one API detail that matters more than either point:**

```python
jwt.decode(token, key, algorithms=["HS256"], options={"require": ["exp", "iat", "jti", "typ"]})
```

PyJWT lets us make claims **mandatory**. A token missing `exp` is rejected as malformed rather
than treated as non-expiring, and a token missing `typ` cannot slip past the access-vs-refresh
check. Defaulting to strict is a property of the library, not of our discipline.

`algorithms=` is always passed explicitly. Accepting the header's `alg` is the classic
algorithm-confusion vulnerability — `{"alg": "none"}` or an RS256 public key replayed as an HS256
shared secret.

## Consequences

* Not what `ARCHITECTURE.md` says. That is the point of this file.
* If JWE or a JWKS-backed asymmetric setup is ever needed, revisit — PyJWT supports RS256/ES256,
  so the likelier path is a key change, not a library change.
* Isolated in `app/core/security.py`. `JwtTokenService` is the only place that imports `jwt`; the
  rest of the application sees `IssuedToken` and `TokenPayload`.

## Alternatives

| Option | Rejected because |
|---|---|
| `python-jose` (as specified) | unmaintained since 2021, on the auth path |
| `authlib` | excellent, but it is an OAuth/OIDC framework — far more than signing a token |
| hand-rolled HMAC | signing is easy; constant-time comparison, claim validation and `alg` handling are where implementations go wrong |
