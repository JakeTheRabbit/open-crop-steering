"""External-system integration packages.

One sub-package per upstream system that OCS bridges into
(``ha_irrigation`` is the first). Each integration owns the discovery
logic (read-only HA registry walk) and the bootstrap registration logic
(idempotent insert into OCS-side tables) for one external tool.

This package itself is intentionally empty so importing
``app.integrations`` does not pull in every integration's transitive
dependencies — callers must import the specific sub-package they need.
"""
