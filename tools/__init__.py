"""Operational CLI tools for Open Crop Steering.

These are standalone scripts (run directly, not imported by the app)
that operate against a configured database. Currently:

* ``seed_from_week_data.py`` — seed an initial immutable recipe revision
  per room from a legacy WEEK_DATA file or a built-in preset.
"""

from __future__ import annotations
