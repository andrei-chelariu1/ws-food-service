"""Reusable, domain-agnostic plumbing.

HARD RULE: nothing under `app/shared/` may import from `app/modules/`.
The dependency arrow points one way — modules depend on shared, never the
reverse. See app/shared/README.md.
"""
