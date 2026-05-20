"""Out-of-band tools (data migrations, seed scripts, …).

Imported as a package so unit tests can exercise the pure helpers
inside without invoking the script's ``asyncio.run`` entrypoint.
Each module is also runnable directly (``python -m tools.<name>``).
"""
