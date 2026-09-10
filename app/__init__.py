"""Food Waste App backend.

Layering (see app/README.md):

    api  ->  service  ->  repository  ->  database

Dependencies point inward and downward only. A repository never calls a
service; a service never imports FastAPI; `shared/` never imports `modules/`.
"""

__version__ = "0.1.0"
