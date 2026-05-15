"""Unit tests for :mod:`app.influx_client` (InfluxDB SDK mocked).

The synchronous ``influxdb-client`` query API is replaced with a fake
that records the Flux string it was handed and returns canned tables, so
these tests assert both *query construction* and *result parsing* without
any network or Docker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from app.influx_client import (
    InfluxClient,
    InfluxSchemaCheck,
    _window_to_flux,
)

pytestmark = pytest.mark.unit


# -- fakes ----------------------------------------------------------------


class _FakeRecord:
    """Stand-in for an influxdb-client FluxRecord."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def get_value(self) -> Any:
        return self._value


class _FakeTable:
    """Stand-in for an influxdb-client FluxTable."""

    def __init__(self, values: list[Any]) -> None:
        self.records = [_FakeRecord(v) for v in values]


class _FakeQueryApi:
    """Captures the Flux query string and returns pre-seeded tables."""

    def __init__(self, tables: list[_FakeTable]) -> None:
        self._tables = tables
        self.last_flux: str | None = None
        self.call_count = 0

    def query(self, flux: str, org: str | None = None) -> list[_FakeTable]:
        self.last_flux = flux
        self.call_count += 1
        return self._tables


class _FakeWriteApi:
    """Records write() calls for the schema-check write smoke."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.written: list[Any] = []

    def write(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("write blew up")
        self.written.append(kwargs)


class _FakeInfluxClient:
    """Stand-in for influxdb_client.InfluxDBClient."""

    def __init__(
        self,
        query_tables: list[_FakeTable] | None = None,
        *,
        write_fails: bool = False,
        query_raises: Exception | None = None,
    ) -> None:
        self._query_api = _FakeQueryApi(query_tables or [])
        self._write_api = _FakeWriteApi(fail=write_fails)
        self._query_raises = query_raises

    def query_api(self) -> _FakeQueryApi:
        if self._query_raises is not None:
            raise self._query_raises
        return self._query_api

    def write_api(self, **kwargs: Any) -> _FakeWriteApi:
        return self._write_api

    def close(self) -> None:
        pass


def _client_with(fake: _FakeInfluxClient) -> InfluxClient:
    """Build an InfluxClient whose SDK client is the supplied fake."""
    client = InfluxClient(
        url="http://influx.test",
        token="tok",
        org="myorg",
        bucket="homeassistant",
    )
    client._client = fake  # type: ignore[assignment]
    return client


# -- window rendering -----------------------------------------------------


class TestWindowToFlux:
    def test_timedelta_becomes_negative_seconds(self) -> None:
        assert _window_to_flux(timedelta(minutes=30)) == "-1800s"

    def test_string_without_sign_gets_negated(self) -> None:
        assert _window_to_flux("15m") == "-15m"

    def test_already_negative_string_is_passed_through(self) -> None:
        assert _window_to_flux("-1h") == "-1h"

    def test_non_positive_timedelta_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive duration"):
            _window_to_flux(timedelta(seconds=0))


# -- current_value --------------------------------------------------------


class TestCurrentValue:
    def test_builds_last_query_and_parses_float(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([28.4])])
        client = _client_with(fake)
        value = client.current_value("f1_temp")
        assert value == 28.4
        flux = fake._query_api.last_flux or ""
        assert 'from(bucket: "homeassistant")' in flux
        assert 'r.entity_id == "f1_temp"' in flux
        assert 'r._field == "value"' in flux
        assert "|> last()" in flux

    def test_returns_none_when_no_data(self) -> None:
        fake = _FakeInfluxClient(query_tables=[])
        assert _client_with(fake).current_value("f1_temp") is None


# -- trend_slope ----------------------------------------------------------


class TestTrendSlope:
    def test_builds_derivative_query_with_window(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([0.05])])
        client = _client_with(fake)
        slope = client.trend_slope("f1_temp", timedelta(minutes=30))
        assert slope == 0.05
        flux = fake._query_api.last_flux or ""
        assert "|> range(start: -1800s)" in flux
        assert "derivative(unit: 1s, nonNegative: false)" in flux
        assert "|> mean()" in flux

    def test_accepts_flux_duration_string(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([1.0])])
        client = _client_with(fake)
        client.trend_slope("f1_temp", "45m")
        assert "|> range(start: -45m)" in (fake._query_api.last_flux or "")

    def test_returns_none_without_enough_data(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([])])
        assert _client_with(fake).trend_slope("f1_temp", "30m") is None


# -- at_threshold_for -----------------------------------------------------


class TestAtThresholdFor:
    def test_builds_comparison_query_and_returns_true(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([True])])
        client = _client_with(fake)
        held = client.at_threshold_for(
            "f1_ac_fan", "gte", 9.0, timedelta(minutes=15)
        )
        assert held is True
        flux = fake._query_api.last_flux or ""
        assert "|> range(start: -900s)" in flux
        assert "r._value >= 9.0" in flux
        assert "|> count()" in flux

    @pytest.mark.parametrize(
        ("op", "symbol"),
        [("gt", ">"), ("lt", "<"), ("gte", ">="), ("lte", "<=")],
    )
    def test_operator_mapping(self, op: str, symbol: str) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([False])])
        client = _client_with(fake)
        client.at_threshold_for("e", op, 1.0, "5m")  # type: ignore[arg-type]
        assert f"r._value {symbol} 1.0" in (fake._query_api.last_flux or "")

    def test_returns_false_when_condition_not_held(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([False])])
        client = _client_with(fake)
        assert (
            client.at_threshold_for("e", "gt", 1.0, "5m") is False
        )

    def test_returns_false_when_no_data(self) -> None:
        fake = _FakeInfluxClient(query_tables=[])
        client = _client_with(fake)
        assert client.at_threshold_for("e", "gt", 1.0, "5m") is False


# -- delta_post_event -----------------------------------------------------


class TestDeltaPostEvent:
    def test_builds_windowed_delta_query(self) -> None:
        fake = _FakeInfluxClient(query_tables=[_FakeTable([12.5])])
        client = _client_with(fake)
        delta = client.delta_post_event(
            "z1_vwc", "2026-05-15T10:00:00Z", timedelta(minutes=10)
        )
        assert delta == 12.5
        flux = fake._query_api.last_flux or ""
        assert "2026-05-15T10:00:00Z" in flux
        assert 'r.entity_id == "z1_vwc"' in flux
        assert "date.add(d: 600s" in flux
        assert "|> first()" in flux
        assert "|> last()" in flux

    def test_returns_none_when_insufficient_readings(self) -> None:
        fake = _FakeInfluxClient(query_tables=[])
        client = _client_with(fake)
        assert (
            client.delta_post_event("z1_vwc", "2026-05-15T10:00:00Z", "10m")
            is None
        )


# -- test_connection_and_schema -------------------------------------------


class TestConnectionAndSchema:
    def test_ok_when_read_and_write_succeed(self) -> None:
        fake = _FakeInfluxClient(
            query_tables=[_FakeTable(["°C", "%", "ppm"])]
        )
        check = _client_with(fake).test_connection_and_schema()
        assert isinstance(check, InfluxSchemaCheck)
        assert check.ok is True
        assert check.can_read is True
        assert check.can_write is True
        assert check.measurements == ["°C", "%", "ppm"]
        assert check.error is None
        # The read query targets the schema package.
        assert "schema.measurements" in (fake._query_api.last_flux or "")
        # A probe point was written.
        assert len(fake._write_api.written) == 1

    def test_read_failure_short_circuits_and_reports_error(self) -> None:
        fake = _FakeInfluxClient(query_raises=RuntimeError("dns boom"))
        check = _client_with(fake).test_connection_and_schema()
        assert check.ok is False
        assert check.can_read is False
        assert check.can_write is False
        assert check.error is not None
        assert "read smoke failed" in check.error
        # Write must not be attempted once the read failed.
        assert fake._write_api.written == []

    def test_write_failure_is_captured(self) -> None:
        fake = _FakeInfluxClient(
            query_tables=[_FakeTable(["°C"])], write_fails=True
        )
        check = _client_with(fake).test_connection_and_schema()
        assert check.ok is False
        assert check.can_read is True
        assert check.can_write is False
        assert check.error is not None
        assert "write smoke failed" in check.error

    def test_measurements_are_capped(self) -> None:
        fake = _FakeInfluxClient(
            query_tables=[_FakeTable([f"m{i}" for i in range(80)])]
        )
        check = _client_with(fake).test_connection_and_schema()
        assert len(check.measurements) == 50


# -- context manager ------------------------------------------------------


class TestLifecycle:
    def test_context_manager_closes_client(self) -> None:
        fake = _FakeInfluxClient()
        client = _client_with(fake)
        with client as c:
            assert c is client
        assert client._client is None
