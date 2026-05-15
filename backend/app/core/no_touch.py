"""No-touch windows — recurring intervals where the AI must not act.

A *no-touch window* is an operator-declared recurring block of time
during which the supervisor leaves a room alone — no proposals, no
bounded auto-adjust (plan locked decision #14: global ``no_touch_windows``).
Typical uses: the daily lights-on/off transition (the room is in flux,
not a stable state to reason about), or scheduled human work.

This module is deliberately minimal — a window is a daily recurring
``[start, end)`` interval expressed as ``HH:MM`` local clock times, plus
an optional day-of-week restriction. :func:`in_no_touch_window` answers
"is ``now`` inside any window?". Windows come from configuration
(:attr:`app.config.Settings` or a passed-in list).

A window that wraps past midnight (``start > end``, e.g. 22:00-02:00) is
supported: it matches the late-evening span *and* the early-morning span.

This is intentionally not a full cron parser — the requirement is
recurring daily clock windows, and a small explicit dataclass is easier
to validate, test, and render in the admin UI than a cron string.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

#: Minutes in a day — the modulus for wrap-past-midnight arithmetic.
_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True, slots=True)
class NoTouchWindow:
    """A recurring daily no-touch interval.

    Attributes:
        start: Window start as ``HH:MM`` (24-hour, local clock).
        end: Window end as ``HH:MM`` (exclusive). If ``end <= start`` the
            window wraps past midnight.
        weekdays: Restricting set of weekday numbers (``0`` = Monday ...
            ``6`` = Sunday, matching :meth:`datetime.date.weekday`). Empty
            means "every day".
        label: Optional human-readable name for the admin UI / logs.
    """

    start: str
    end: str
    weekdays: frozenset[int] = field(default_factory=frozenset)
    label: str = ""

    def __post_init__(self) -> None:
        # Validate the clock strings eagerly so a bad config fails at
        # load time, not silently at tick time.
        _parse_hhmm(self.start)
        _parse_hhmm(self.end)
        for wd in self.weekdays:
            if not 0 <= wd <= 6:  # noqa: PLR2004 — Mon..Sun bounds
                raise ValueError(f"weekday out of range 0..6: {wd}")

    @classmethod
    def from_mapping(cls, raw: dict[str, object]) -> NoTouchWindow:
        """Build a window from a config mapping.

        Args:
            raw: ``{"start": "HH:MM", "end": "HH:MM", "weekdays": [...],
                "label": "..."}``. ``weekdays`` and ``label`` are
                optional.

        Returns:
            The parsed :class:`NoTouchWindow`.
        """
        weekdays = raw.get("weekdays") or []
        if not isinstance(weekdays, Iterable) or isinstance(weekdays, str | bytes):
            raise TypeError("weekdays must be a list of integers")
        return cls(
            start=str(raw["start"]),
            end=str(raw["end"]),
            weekdays=frozenset(int(d) for d in weekdays),
            label=str(raw.get("label", "")),
        )

    def contains(self, now: dt.datetime) -> bool:
        """Return whether ``now`` falls inside this window.

        Args:
            now: The instant to test. Its date drives the weekday check
                and its time drives the clock check.

        Returns:
            ``True`` iff ``now`` is within the window's clock interval on
            a permitted weekday.
        """
        if self.weekdays and now.weekday() not in self.weekdays:
            return False

        start_m = _parse_hhmm(self.start)
        end_m = _parse_hhmm(self.end)
        now_m = now.hour * 60 + now.minute

        if start_m == end_m:
            # Degenerate zero-length window — never matches.
            return False
        if start_m < end_m:
            # Same-day window: [start, end).
            return start_m <= now_m < end_m
        # Wraps past midnight: matches [start, 24:00) OR [00:00, end).
        return now_m >= start_m or now_m < end_m


def _parse_hhmm(value: str) -> int:
    """Parse an ``HH:MM`` clock string to minutes-since-midnight.

    Args:
        value: A 24-hour ``HH:MM`` string.

    Returns:
        Minutes past midnight in ``[0, 1440)``.

    Raises:
        ValueError: If ``value`` is not a valid ``HH:MM`` time.
    """
    parts = value.split(":")
    if len(parts) != 2:  # noqa: PLR2004 — HH:MM has exactly two parts
        raise ValueError(f"invalid HH:MM time: {value!r}")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid HH:MM time: {value!r}") from exc
    total = hours * 60 + minutes
    if not 0 <= total < _MINUTES_PER_DAY:
        raise ValueError(f"time out of range 00:00..23:59: {value!r}")
    return total


def in_no_touch_window(
    now: dt.datetime,
    windows: Sequence[NoTouchWindow | dict[str, object]],
) -> bool:
    """Return whether ``now`` falls inside any configured no-touch window.

    The supervisor calls this per room each tick and skips the room when
    it returns ``True`` (plan locked decision #14 — no-touch windows
    suppress AI action).

    Args:
        now: The instant to test (the supervisor passes the tick time).
        windows: The configured windows. Each entry may be a
            :class:`NoTouchWindow` or a raw config mapping (parsed via
            :meth:`NoTouchWindow.from_mapping`).

    Returns:
        ``True`` iff at least one window contains ``now``. An empty
        ``windows`` list always returns ``False``.
    """
    for window in windows:
        parsed = (
            window
            if isinstance(window, NoTouchWindow)
            else NoTouchWindow.from_mapping(window)
        )
        if parsed.contains(now):
            log.debug(
                "no_touch_window_active",
                label=parsed.label or f"{parsed.start}-{parsed.end}",
            )
            return True
    return False
