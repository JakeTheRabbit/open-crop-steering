"""HA-Irrigation-Strategy bridge.

P1 of the OCS ↔ HA-Irrigation-Strategy integration (see
``docs/concepts/ha-irrigation-strategy-integration.md``). Two pure
functions sit behind the HTTP surface:

* :func:`discovery.discover` — read the HA entity registry + current
  states, find every ``crop_steering_*`` entity, infer zone count from
  naming patterns, and group entities by role.
* :func:`registry.register_mapping` — take an operator-confirmed mapping
  and idempotently create OCS-side ``sites`` / ``buildings`` / ``rooms``
  / ``locations`` / ``sensors`` / ``equipment`` rows.

Neither function writes to Home Assistant. Discovery is a read-only
registry walk; registration only writes to the OCS Postgres.
"""
