"""Built-in cultivation recipe presets.

A *preset* is a named factory returning a ``params`` list shaped for
:func:`app.core.recipe_store.create_revision`. The flagship preset is the
canonical 12-week cannabis cultivation recipe (cultivation_knowledge.md +
plan locked decision: "12-week preset is the canonical example").

Presets are looked up by name via :func:`get_preset` so tools (e.g.
``tools/seed_from_week_data.py``) can seed a recipe revision without
hard-coding the import.
"""

from __future__ import annotations

from collections.abc import Callable

from app.presets.cannabis_12week import build_cannabis_12week

# Registry of preset name -> builder. Builders take no args and return
# the flat ``params`` list ``create_revision`` consumes.
PRESETS: dict[str, Callable[[], list[dict[str, object]]]] = {
    "cannabis_12week": build_cannabis_12week,
}


def get_preset(name: str) -> Callable[[], list[dict[str, object]]]:
    """Return the builder for a named preset.

    Args:
        name: Preset identifier (e.g. ``"cannabis_12week"``).

    Returns:
        A zero-argument callable producing the params list.

    Raises:
        KeyError: If no preset is registered under ``name``.
    """
    try:
        return PRESETS[name]
    except KeyError:
        available = ", ".join(sorted(PRESETS)) or "(none)"
        raise KeyError(
            f"unknown preset '{name}'; available presets: {available}"
        ) from None


__all__ = ["PRESETS", "build_cannabis_12week", "get_preset"]
