#!/usr/bin/env python
"""Seed initial recipe revisions from a WEEK_DATA file or a built-in preset.

This CLI replaces the manual step in the facility rollout (plan Phase
14.3): "Run ``seed_from_week_data`` to import existing WEEK_DATA as the
first immutable recipe revision per room." Each room gets a single new
``draft`` revision via :func:`app.core.recipe_store.create_revision`;
approval is a separate, deliberate QAP action (the tool never approves).

Two input modes
---------------

**1. WEEK_DATA file** (``--week-data PATH``)

The legacy Home Assistant automation stored setpoints as a week-indexed
table. The actual facility file is not in this repo, so the expected
schema is defined here explicitly. YAML or JSON, top-level shape::

    # optional metadata
    cycle_day_count: 84          # default 84
    days_per_week: 7             # default 7
    recipe_name: "Legacy WEEK_DATA import"   # optional

    rooms:
      F1:                        # room_id -> week table
        weeks:
          - week: 1              # 1-based week number
            temp_day: 25.0       # param_name: value pairs
            temp_night: 23.0
            rh_day: 70
            ppfd: 220
            photoperiod_hours: 18
            # ... any param names; unknown params are passed through
          - week: 2
            temp_day: 25.5
            # ...
      F2:
        weeks:
          - week: 1
            # ...

Rules:

* ``rooms`` maps each ``room_id`` to an object with a ``weeks`` list.
* Each week entry must have an integer ``week`` (1-based); all other
  keys are treated as ``param_name: value`` pairs.
* Each week's values are expanded to one row per day in that week
  (``days_per_week`` days). Values are held flat across the week — the
  legacy automation stepped weekly, so no interpolation is applied.
* Optional ``tolerances`` (``{param_name: tolerance}``) and ``units``
  (``{param_name: unit}``) top-level maps annotate every emitted row.
* ``day_index`` is 1-based and runs ``1 .. cycle_day_count``. Weeks
  beyond ``cycle_day_count`` days are truncated; missing trailing weeks
  leave those days unset.

**2. Built-in preset** (``--preset cannabis_12week``)

Seeds every room from a registered preset (see :mod:`app.presets`)
instead of a file. Use ``--room`` one or more times to name the rooms.

Examples
--------

::

    python -m tools.seed_from_week_data --week-data facility/week_data.yaml \\
        --created-by ha-user-uuid

    python -m tools.seed_from_week_data --preset cannabis_12week \\
        --room F1 --room F2 --created-by ha-user-uuid

    python -m tools.seed_from_week_data --preset cannabis_12week \\
        --room F1 --created-by ha-user-uuid --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import structlog
import yaml
from app.core.recipe_store import create_revision
from app.db import dispose_engine, get_session_factory
from app.presets import PRESETS, get_preset

log = structlog.get_logger("tools.seed_from_week_data")

DEFAULT_CYCLE_DAYS = 84
DEFAULT_DAYS_PER_WEEK = 7


class WeekDataError(ValueError):
    """The WEEK_DATA document failed schema validation."""


def load_week_data_file(path: Path) -> dict[str, Any]:
    """Parse a WEEK_DATA file (YAML or JSON) into a dict.

    Args:
        path: Path to a ``.yaml`` / ``.yml`` / ``.json`` file.

    Returns:
        The parsed top-level document.

    Raises:
        WeekDataError: If the file is missing, unparseable, or not a
            mapping at the top level.
    """
    if not path.is_file():
        raise WeekDataError(f"WEEK_DATA file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    try:
        doc = (
            json.loads(raw)
            if path.suffix.lower() == ".json"
            else yaml.safe_load(raw)
        )
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise WeekDataError(f"could not parse {path}: {exc}") from exc

    if not isinstance(doc, dict):
        raise WeekDataError(
            f"{path}: top level must be a mapping, got {type(doc).__name__}"
        )
    return doc


def _week_number(room_id: str, entry: dict[str, Any]) -> int:
    """Validate + extract the 1-based ``week`` from a week entry."""
    if "week" not in entry:
        raise WeekDataError(
            f"room '{room_id}': each week entry needs an integer 'week'"
        )
    try:
        week_no = int(entry["week"])
    except (TypeError, ValueError) as exc:
        raise WeekDataError(
            f"room '{room_id}': 'week' must be an integer"
        ) from exc
    if week_no < 1:
        raise WeekDataError(
            f"room '{room_id}': week numbers are 1-based, got {week_no}"
        )
    return week_no


def _expand_room(
    room_id: str,
    room_obj: Any,
    *,
    cycle_days: int,
    days_per_week: int,
    tolerances: dict[str, Any],
    units: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expand one room's week table into a flat param-row list."""
    if not isinstance(room_obj, dict):
        raise WeekDataError(f"room '{room_id}': value must be a mapping")
    weeks = room_obj.get("weeks")
    if not isinstance(weeks, list) or not weeks:
        raise WeekDataError(
            f"room '{room_id}': must have a non-empty 'weeks' list"
        )

    params: list[dict[str, Any]] = []
    for entry in weeks:
        if not isinstance(entry, dict):
            raise WeekDataError(f"room '{room_id}': week entries must be mappings")
        week_no = _week_number(room_id, entry)
        setpoints = {k: v for k, v in entry.items() if k != "week"}
        if not setpoints:
            raise WeekDataError(
                f"room '{room_id}' week {week_no}: no setpoints given"
            )

        first_day = (week_no - 1) * days_per_week + 1
        for offset in range(days_per_week):
            day_index = first_day + offset
            if day_index > cycle_days:
                break
            for param_name, value in setpoints.items():
                params.append(
                    {
                        "day_index": day_index,
                        "param_name": str(param_name),
                        "value": float(value),
                        "tolerance": (
                            None
                            if tolerances.get(param_name) is None
                            else float(tolerances[param_name])
                        ),
                        "unit": units.get(param_name),
                    }
                )

    if not params:
        raise WeekDataError(f"room '{room_id}': expanded to zero param rows")
    return params


def expand_week_data(doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Expand a WEEK_DATA document into per-room ``create_revision`` params.

    See the module docstring for the input schema. Each week's setpoints
    are held flat across that week's days (the legacy automation stepped
    weekly, so no interpolation).

    Args:
        doc: Parsed WEEK_DATA document.

    Returns:
        Mapping of ``room_id`` to a flat list of ``{day_index,
        param_name, value, tolerance, unit}`` dicts.

    Raises:
        WeekDataError: On any structural violation of the schema.
    """
    cycle_days = int(doc.get("cycle_day_count", DEFAULT_CYCLE_DAYS))
    days_per_week = int(doc.get("days_per_week", DEFAULT_DAYS_PER_WEEK))
    if cycle_days < 1 or days_per_week < 1:
        raise WeekDataError("cycle_day_count and days_per_week must be >= 1")

    tolerances: dict[str, Any] = doc.get("tolerances", {}) or {}
    units: dict[str, Any] = doc.get("units", {}) or {}
    if not isinstance(tolerances, dict) or not isinstance(units, dict):
        raise WeekDataError("'tolerances' and 'units' must be mappings")

    rooms = doc.get("rooms")
    if not isinstance(rooms, dict) or not rooms:
        raise WeekDataError("document must have a non-empty 'rooms' mapping")

    return {
        room_id: _expand_room(
            room_id,
            room_obj,
            cycle_days=cycle_days,
            days_per_week=days_per_week,
            tolerances=tolerances,
            units=units,
        )
        for room_id, room_obj in rooms.items()
    }


async def _seed_rooms(
    room_params: dict[str, list[dict[str, Any]]],
    *,
    created_by: str,
    recipe_name: str,
    cycle_day_count: int,
    notes: str | None,
    dry_run: bool,
) -> int:
    """Create one draft revision per room. Returns the count created."""
    if dry_run:
        for room_id, params in room_params.items():
            log.info(
                "seed_dry_run",
                room_id=room_id,
                param_count=len(params),
                recipe_name=recipe_name,
            )
        return 0

    factory = get_session_factory()
    created = 0
    try:
        async with factory() as session:
            for room_id, params in room_params.items():
                revision = await create_revision(
                    session,
                    room_id=room_id,
                    name=recipe_name,
                    params=params,
                    created_by=created_by,
                    cycle_day_count=cycle_day_count,
                    notes=notes,
                )
                created += 1
                log.info(
                    "seed_revision_created",
                    room_id=room_id,
                    revision_id=revision.id,
                    version=revision.version,
                    param_count=len(params),
                )
            await session.commit()
    finally:
        await dispose_engine()
    return created


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="seed_from_week_data",
        description=(
            "Seed an initial draft recipe revision per room from a legacy "
            "WEEK_DATA file or a built-in preset."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--week-data",
        type=Path,
        metavar="PATH",
        help="path to a WEEK_DATA YAML/JSON file (see module docstring)",
    )
    source.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="seed every --room from a built-in preset",
    )
    parser.add_argument(
        "--room",
        action="append",
        dest="rooms",
        metavar="ROOM_ID",
        help="room id to seed (repeatable); required with --preset",
    )
    parser.add_argument(
        "--created-by",
        required=True,
        metavar="USER_ID",
        help="users.id of the cultivator credited with creating the revision",
    )
    parser.add_argument(
        "--recipe-name",
        default=None,
        help="name for the created revision (defaults per source)",
    )
    parser.add_argument(
        "--cycle-day-count",
        type=int,
        default=DEFAULT_CYCLE_DAYS,
        help=f"cultivation cycle length in days (default {DEFAULT_CYCLE_DAYS})",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="optional free-text notes stored on the revision",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + expand + report without writing to the database",
    )
    return parser


def _resolve_params(args: argparse.Namespace) -> tuple[
    dict[str, list[dict[str, Any]]], str, int
]:
    """Resolve CLI args into (room->params, recipe_name, cycle_day_count)."""
    if args.preset:
        if not args.rooms:
            raise WeekDataError("--preset requires at least one --room")
        builder = get_preset(args.preset)
        preset_params = builder()
        recipe_name = args.recipe_name or f"{args.preset} preset"
        room_params = {room: list(preset_params) for room in args.rooms}
        return room_params, recipe_name, args.cycle_day_count

    # WEEK_DATA file path.
    doc = load_week_data_file(args.week_data)
    room_params = expand_week_data(doc)
    if args.rooms:
        # Optional filter: only seed the requested subset.
        missing = sorted(set(args.rooms) - set(room_params))
        if missing:
            raise WeekDataError(
                f"--room values not present in WEEK_DATA file: {missing}"
            )
        room_params = {r: room_params[r] for r in args.rooms}
    recipe_name = (
        args.recipe_name
        or doc.get("recipe_name")
        or "Legacy WEEK_DATA import"
    )
    cycle_day_count = int(doc.get("cycle_day_count", args.cycle_day_count))
    return room_params, recipe_name, cycle_day_count


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``2`` on a usage / schema error.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        room_params, recipe_name, cycle_day_count = _resolve_params(args)
    except (WeekDataError, KeyError) as exc:
        parser.error(str(exc))
        return 2  # parser.error exits, but keep mypy/readers happy

    log.info(
        "seed_start",
        source="preset" if args.preset else "week_data",
        rooms=sorted(room_params),
        recipe_name=recipe_name,
        dry_run=args.dry_run,
    )

    created = asyncio.run(
        _seed_rooms(
            room_params,
            created_by=args.created_by,
            recipe_name=recipe_name,
            cycle_day_count=cycle_day_count,
            notes=args.notes,
            dry_run=args.dry_run,
        )
    )

    if args.dry_run:
        log.info("seed_done_dry_run", rooms=len(room_params))
    else:
        log.info("seed_done", revisions_created=created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
