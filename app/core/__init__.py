"""Application-wide wiring: config, logging, security primitives, middleware.

`core` knows nothing about the domain — no import of `app.modules` is allowed
here. See app/core/README.md.
"""
