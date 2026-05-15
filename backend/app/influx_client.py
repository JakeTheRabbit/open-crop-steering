"""InfluxDB 2.x client for sensor history + trend queries.

Configuration is **explicit only** — URL, token, org and bucket are read
from :func:`app.config.get_settings`. There is deliberately no
auto-discovery: the add-on UI presents a "Test connection + schema"
button (backed by :meth:`InfluxClient.test_connection_and_schema`) so the
operator confirms a working configuration before it is persisted.

Schema assumption
-----------------
Home Assistant's built-in InfluxDB integration writes **one measurement
per unit-of-measurement**. A temperature sensor reporting ``°C`` lands in
a measurement literally named ``°C``; relative humidity lands in ``%``;
and so on. Within each measurement:

* the numeric reading is the field ``value``;
* the originating entity is the tag ``entity_id`` (without its domain
  prefix, e.g. ``f1_temp`` not ``sensor.f1_temp``).

Because the measurement name is the unit (and therefore not known ahead
of time for an arbitrary entity), every Flux helper here filters on the
``entity_id`` tag and lets the measurement float, e.g.::

    from(bucket: "...")
      |> range(start: -30m)
      |> filter(fn: (r) => r.entity_id == "f1_temp")
      |> filter(fn: (r) => r._field == "value")

Callers pass the bare ``entity_id`` tag value, not the HA entity id.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import get_settings

if TYPE_CHECKING:
    from influxdb_client.client.flux_table import FluxTable

log = structlog.get_logger(__name__)

# Comparison operators accepted by :meth:`InfluxClient.at_threshold_for`.
ThresholdOp = Literal["gt", "lt", "gte", "lte"]

_FLUX_OPERATOR = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}

# Measurement + tag/field names used by HA's InfluxDB integration.
_VALUE_FIELD = "value"
_ENTITY_TAG = "entity_id"


@dataclass(slots=True)
class InfluxSchemaCheck:
    """Result of :meth:`InfluxClient.test_connection_and_schema`.

    Attributes:
        ok: ``True`` only if both the read smoke and the write smoke
            succeeded.
        can_read: A schema/read query against the bucket succeeded.
        can_write: A tiny probe point was written to the bucket.
        measurements: Measurement names discovered in the bucket (capped).
        error: First error encountered, or ``None`` on full success.
    """

    ok: bool
    can_read: bool
    can_write: bool
    measurements: list[str] = field(default_factory=list)
    error: str | None = None


def _window_to_flux(window: timedelta | str) -> str:
    """Render a query window as a Flux duration literal.

    Accepts a :class:`datetime.timedelta` or an already-formatted Flux
    duration string (e.g. ``"30m"``, ``"-1h"``). The returned value is
    always negative-relative (suitable for ``range(start:)``).
    """
    if isinstance(window, str):
        return window if window.startswith("-") else f"-{window}"
    total = int(window.total_seconds())
    if total <= 0:
        raise ValueError("window must be a positive duration")
    return f"-{total}s"


class InfluxClient:
    """Thin async-friendly wrapper around the InfluxDB 2.x query + write APIs.

    The underlying ``influxdb-client`` SDK is synchronous; its calls are
    short and are invoked directly here. Construct one client and reuse
    it; call :meth:`close` (or use it as a context manager) to release
    the HTTP session.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        org: str | None = None,
        bucket: str | None = None,
    ) -> None:
        """Build the client from explicit values or settings defaults.

        Args:
            url: InfluxDB base URL. Defaults to ``settings.influx_url``.
            token: API token. Defaults to ``settings.influx_token``.
            org: Organisation. Defaults to ``settings.influx_org``.
            bucket: Bucket name. Defaults to ``settings.influx_bucket``.
        """
        settings = get_settings()
        self._url = url if url is not None else settings.influx_url
        self._token = token if token is not None else settings.influx_token
        self._org = org if org is not None else settings.influx_org
        self._bucket = bucket if bucket is not None else settings.influx_bucket
        self._client: InfluxDBClient | None = None

    # -- lifecycle --------------------------------------------------------

    def _influx(self) -> InfluxDBClient:
        """Return the lazily-created InfluxDB SDK client."""
        if self._client is None:
            self._client = InfluxDBClient(
                url=self._url, token=self._token, org=self._org
            )
        return self._client

    def close(self) -> None:
        """Release the underlying InfluxDB HTTP session."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> InfluxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- query helpers ----------------------------------------------------

    def _query(self, flux: str) -> list[FluxTable]:
        """Run a Flux query and return the raw table list."""
        log.debug("influx.query", flux=flux)
        return self._influx().query_api().query(flux, org=self._org)

    @staticmethod
    def _first_value(tables: list[FluxTable]) -> Any:
        """Return the ``_value`` of the first record across all tables."""
        for table in tables:
            for record in table.records:
                return record.get_value()
        return None

    def current_value(self, entity: str) -> float | None:
        """Return the most recent numeric reading for ``entity``.

        Note:
            Per the project's locked decisions, the *authoritative* source
            for current actuator state is the Home Assistant WebSocket
            API; this Influx-backed reading exists as a fallback and for
            trend bootstrapping.

        Args:
            entity: The ``entity_id`` tag value (bare, no domain prefix).

        Returns:
            The latest value, or ``None`` if the entity has no data.
        """
        flux = (
            f'from(bucket: "{self._bucket}")\n'
            "  |> range(start: -24h)\n"
            f'  |> filter(fn: (r) => r.{_ENTITY_TAG} == "{entity}")\n'
            f'  |> filter(fn: (r) => r._field == "{_VALUE_FIELD}")\n'
            "  |> last()"
        )
        value = self._first_value(self._query(flux))
        return None if value is None else float(value)

    def trend_slope(
        self, entity: str, window: timedelta | str
    ) -> float | None:
        """Return the least-squares slope of ``entity`` over ``window``.

        The slope is expressed in *value units per second* so callers can
        reason about it independently of the window length. Implemented
        with Flux ``derivative`` averaged over the window.

        Args:
            entity: The ``entity_id`` tag value.
            window: Look-back window (timedelta or Flux duration string).

        Returns:
            Mean slope in units/second, or ``None`` if there is too little
            data to compute a derivative.
        """
        start = _window_to_flux(window)
        flux = (
            f'from(bucket: "{self._bucket}")\n'
            f"  |> range(start: {start})\n"
            f'  |> filter(fn: (r) => r.{_ENTITY_TAG} == "{entity}")\n'
            f'  |> filter(fn: (r) => r._field == "{_VALUE_FIELD}")\n'
            "  |> derivative(unit: 1s, nonNegative: false)\n"
            "  |> mean()"
        )
        value = self._first_value(self._query(flux))
        return None if value is None else float(value)

    def at_threshold_for(
        self,
        entity: str,
        op: ThresholdOp,
        threshold: float,
        duration: timedelta | str,
    ) -> bool:
        """Return whether ``entity`` has held past ``threshold`` for ``duration``.

        Used by equipment-saturation predicates ("AC fan high for >=15
        min", "RH above setpoint+3% for >=20 min", ...). The query counts
        readings in the window that satisfy the comparison and compares
        that to the total reading count: if *every* reading in the window
        breaches the threshold, the condition has held for the whole
        duration.

        Args:
            entity: The ``entity_id`` tag value.
            op: Comparison operator (``gt`` / ``lt`` / ``gte`` / ``lte``).
            threshold: The value to compare each reading against.
            duration: How far back the condition must have continuously held.

        Returns:
            ``True`` if data exists for the window and every reading in it
            breaches the threshold; ``False`` otherwise.
        """
        start = _window_to_flux(duration)
        flux_op = _FLUX_OPERATOR[op]
        flux = (
            f'breaching = from(bucket: "{self._bucket}")\n'
            f"  |> range(start: {start})\n"
            f'  |> filter(fn: (r) => r.{_ENTITY_TAG} == "{entity}")\n'
            f'  |> filter(fn: (r) => r._field == "{_VALUE_FIELD}")\n'
            f"  |> filter(fn: (r) => r._value {flux_op} {threshold})\n"
            "  |> count()\n"
            f'total = from(bucket: "{self._bucket}")\n'
            f"  |> range(start: {start})\n"
            f'  |> filter(fn: (r) => r.{_ENTITY_TAG} == "{entity}")\n'
            f'  |> filter(fn: (r) => r._field == "{_VALUE_FIELD}")\n'
            "  |> count()\n"
            "join(tables: {b: breaching, t: total}, on: [\"_field\"])\n"
            "  |> map(fn: (r) => ({_value: r._value_b == r._value_t "
            "and r._value_t > 0}))"
        )
        result = self._first_value(self._query(flux))
        return bool(result) if result is not None else False

    def delta_post_event(
        self, entity: str, event_ts: str, window: timedelta | str
    ) -> float | None:
        """Return the change in ``entity`` in the ``window`` after an event.

        Computes ``last_value - first_value`` over the interval
        ``[event_ts, event_ts + window]``. Used to verify that a control
        action moved a sensor — e.g. the VWC rise after a fired irrigation
        shot (saturation indicator ``SAT-IRRIG``).

        Args:
            entity: The ``entity_id`` tag value.
            event_ts: RFC3339 timestamp marking the event (window start).
            window: How far past the event to measure (timedelta or Flux
                duration string).

        Returns:
            The signed delta over the window, or ``None`` if fewer than two
            readings exist in it.
        """
        span = _window_to_flux(window).lstrip("-")
        flux = (
            f'data = from(bucket: "{self._bucket}")\n'
            f"  |> range(start: {event_ts}, stop: "
            f"date.add(d: {span}, to: time(v: {event_ts})))\n"
            f'  |> filter(fn: (r) => r.{_ENTITY_TAG} == "{entity}")\n'
            f'  |> filter(fn: (r) => r._field == "{_VALUE_FIELD}")\n'
            "first_v = data |> first()\n"
            "last_v = data |> last()\n"
            "join(tables: {f: first_v, l: last_v}, on: [\"_field\"])\n"
            "  |> map(fn: (r) => ({_value: r._value_l - r._value_f}))"
        )
        value = self._first_value(self._query(flux))
        return None if value is None else float(value)

    # -- connection + schema smoke test ----------------------------------

    def test_connection_and_schema(self) -> InfluxSchemaCheck:
        """Run read + write smoke tests against the configured InfluxDB.

        Backs the add-on UI's "Test connection + schema" button. The check
        is intentionally non-fatal: any failure is captured into the
        returned dataclass rather than raised, so the UI can render a
        precise status.

        Steps:
            1. **Read** — list the bucket's measurements via
               ``schema.measurements``.
            2. **Write** — write one probe point to a dedicated
               ``_ocs_healthcheck`` measurement.

        Returns:
            An :class:`InfluxSchemaCheck` describing read/write reachability
            and the discovered measurements.
        """
        can_read = False
        can_write = False
        measurements: list[str] = []
        error: str | None = None

        try:
            flux = (
                "import \"influxdata/influxdb/schema\"\n"
                f'schema.measurements(bucket: "{self._bucket}")'
            )
            tables = self._query(flux)
            for table in tables:
                for record in table.records:
                    measurements.append(str(record.get_value()))
            can_read = True
        except Exception as exc:  # surface any failure to the UI rather than raise
            error = f"read smoke failed: {exc}"
            log.warning("influx.schema_check.read_failed", error=str(exc))

        if error is None:
            try:
                point = (
                    Point("_ocs_healthcheck")
                    .tag("source", "open_crop_steering")
                    .field("ts", time.time())
                )
                write_api = self._influx().write_api(write_options=SYNCHRONOUS)
                write_api.write(bucket=self._bucket, org=self._org, record=point)
                can_write = True
            except Exception as exc:  # surface any failure to the UI rather than raise
                error = f"write smoke failed: {exc}"
                log.warning("influx.schema_check.write_failed", error=str(exc))

        ok = can_read and can_write
        log.info(
            "influx.schema_check",
            ok=ok,
            can_read=can_read,
            can_write=can_write,
            measurements=len(measurements),
        )
        return InfluxSchemaCheck(
            ok=ok,
            can_read=can_read,
            can_write=can_write,
            measurements=measurements[:50],
            error=error,
        )
