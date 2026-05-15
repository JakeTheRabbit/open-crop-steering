"""Background worker processes (run as separate s6 services).

Each worker coordinates via Postgres advisory locks so only one
instance of each loop is active at a time, even across add-on restarts.
"""
