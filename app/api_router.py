"""Aggregates every module's router into one versioned API router.

WHY THIS FILE EXISTS
--------------------
`main.py` mounts exactly one router. Adding a module means adding one line here,
not editing the application factory — so `create_app()` stays about *application
concerns* (middleware, lifespan, handlers) and never grows a list of feature
imports. Open/Closed, applied to the composition of the API surface.

It is also the only place that knows the full route inventory, which makes it the
natural place to audit "what does this service expose?".

WHY THE VERSION PREFIX IS APPLIED HERE AND NOT IN EACH MODULE
-------------------------------------------------------------
Each module declares a prefix relative to the API root (`/users`, `/food-items`).
The `/api/v1` part is added once, here. Consequences:

* Introducing `/api/v2` means a second aggregator that re-exports unchanged
  routers and overrides only what changed — no module edits.
* No module can accidentally hardcode a version, so there is no chance of
  `/api/v1/users` and `/api/v2/food-items` being served from one aggregator.
"""

from fastapi import APIRouter

from app.core.config import get_settings
from app.modules.donations.api import router as donations_router
from app.modules.food_items.api import router as food_items_router
from app.modules.notifications.api import router as notifications_router
from app.modules.users.api import auth_router, users_router

settings = get_settings()

api_router = APIRouter(prefix=settings.API_V1_PREFIX)

# Registration order determines the order of tag groups in Swagger, so it is
# arranged as a reader would work through the API: authenticate, browse food,
# request it, get told what happened.
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(food_items_router)
api_router.include_router(donations_router)
api_router.include_router(notifications_router)

__all__ = ["api_router"]
