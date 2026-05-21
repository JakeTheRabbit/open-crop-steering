"""External-integration HTTP surface.

One module per integration under ``/api/integrations/<system>/*``. Each
module exposes a small FastAPI router that drives the pure-function
logic in ``app.integrations.<system>``.
"""
