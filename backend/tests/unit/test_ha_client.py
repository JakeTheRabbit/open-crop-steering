"""Unit tests for :mod:`app.ha_client` (HA REST mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.ha_client import HAClient, ReadbackResult, _is_retryable, _values_match

pytestmark = pytest.mark.unit

_BASE = "http://ha.test"


def _client() -> HAClient:
    """Build an HAClient pinned to the mock base URL + a fake token."""
    return HAClient(base_url=_BASE, token="test-token")


class TestValueMatching:
    """Float-tolerant comparison used by readback."""

    @pytest.mark.parametrize(
        ("expected", "actual"),
        [
            ("28.0", "28"),
            (28.0, "28"),
            ("28", 28.0),
            (28, "28.00"),
            ("  28.0 ", "28"),
        ],
    )
    def test_numeric_equivalents_match(self, expected: object, actual: object) -> None:
        assert _values_match(expected, actual) is True

    @pytest.mark.parametrize(
        ("expected", "actual"),
        [
            ("28.0", "29"),
            ("on", "off"),
            ("heat", "cool"),
        ],
    )
    def test_non_equal_values_do_not_match(
        self, expected: object, actual: object
    ) -> None:
        assert _values_match(expected, actual) is False

    def test_string_states_match_exactly(self) -> None:
        assert _values_match("on", "on") is True


class TestRetryClassification:
    """Only transport errors + HTTP 5xx should be retried."""

    def test_transport_error_is_retryable(self) -> None:
        assert _is_retryable(httpx.ConnectError("boom")) is True

    def test_5xx_is_retryable(self) -> None:
        resp = httpx.Response(503, request=httpx.Request("GET", _BASE))
        exc = httpx.HTTPStatusError("err", request=resp.request, response=resp)
        assert _is_retryable(exc) is True

    def test_4xx_is_not_retryable(self) -> None:
        resp = httpx.Response(404, request=httpx.Request("GET", _BASE))
        exc = httpx.HTTPStatusError("err", request=resp.request, response=resp)
        assert _is_retryable(exc) is False

    def test_unrelated_exception_is_not_retryable(self) -> None:
        assert _is_retryable(ValueError("nope")) is False


class TestGetState:
    @respx.mock
    async def test_get_state_returns_state_object(self) -> None:
        route = respx.get(f"{_BASE}/api/states/input_number.f1_temp").mock(
            return_value=httpx.Response(
                200,
                json={"entity_id": "input_number.f1_temp", "state": "28.0"},
            )
        )
        async with _client() as client:
            state = await client.get_state("input_number.f1_temp")
        assert route.called
        assert state["state"] == "28.0"

    @respx.mock
    async def test_get_states_returns_list(self) -> None:
        respx.get(f"{_BASE}/api/states").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"entity_id": "sensor.a", "state": "1"},
                    {"entity_id": "sensor.b", "state": "2"},
                ],
            )
        )
        async with _client() as client:
            states = await client.get_states()
        assert len(states) == 2

    @respx.mock
    async def test_get_state_retries_on_5xx_then_succeeds(self) -> None:
        route = respx.get(f"{_BASE}/api/states/sensor.x").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(200, json={"entity_id": "sensor.x", "state": "ok"}),
            ]
        )
        async with _client() as client:
            state = await client.get_state("sensor.x")
        assert route.call_count == 2
        assert state["state"] == "ok"

    @respx.mock
    async def test_get_state_does_not_retry_on_404(self) -> None:
        route = respx.get(f"{_BASE}/api/states/sensor.missing").mock(
            return_value=httpx.Response(404)
        )
        async with _client() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_state("sensor.missing")
        assert route.call_count == 1


class TestCallService:
    @respx.mock
    async def test_call_service_posts_payload(self) -> None:
        route = respx.post(f"{_BASE}/api/services/input_number/set_value").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with _client() as client:
            await client.call_service(
                "input_number",
                "set_value",
                {"entity_id": "input_number.f1_temp", "value": 28.0},
            )
        assert route.called
        sent = route.calls.last.request
        assert sent.method == "POST"
        body = sent.content.decode()
        assert "input_number.f1_temp" in body
        assert "28.0" in body

    @respx.mock
    async def test_call_service_sends_empty_object_when_no_data(self) -> None:
        route = respx.post(f"{_BASE}/api/services/homeassistant/restart").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with _client() as client:
            await client.call_service("homeassistant", "restart")
        assert route.calls.last.request.content.decode() == "{}"

    def test_no_set_state_write_path_exists(self) -> None:
        """Control must go through call_service only — never set_state."""
        client = _client()
        assert not hasattr(client, "set_state")


class TestCallAndReadback:
    @respx.mock
    async def test_readback_success_first_poll(self) -> None:
        respx.post(f"{_BASE}/api/services/input_number/set_value").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(f"{_BASE}/api/states/input_number.f1_temp").mock(
            return_value=httpx.Response(
                200, json={"entity_id": "input_number.f1_temp", "state": "28.0"}
            )
        )
        async with _client() as client:
            result = await client.call_and_readback(
                "input_number",
                "set_value",
                {"entity_id": "input_number.f1_temp", "value": 28.0},
                expected_entity="input_number.f1_temp",
                expected_value=28.0,
                timeout=2.0,
                poll_interval=0.01,
            )
        assert isinstance(result, ReadbackResult)
        assert result.applied is True
        assert result.attempts == 1
        assert _values_match(28.0, result.actual_value)
        assert result.elapsed_s >= 0

    @respx.mock
    async def test_readback_eventual_consistency_matches_on_third_poll(self) -> None:
        respx.post(f"{_BASE}/api/services/input_number/set_value").mock(
            return_value=httpx.Response(200, json=[])
        )
        # First two polls report the stale value, third reports the target.
        respx.get(f"{_BASE}/api/states/input_number.f1_temp").mock(
            side_effect=[
                httpx.Response(200, json={"state": "26.0"}),
                httpx.Response(200, json={"state": "26.0"}),
                httpx.Response(200, json={"state": "28"}),
            ]
        )
        async with _client() as client:
            result = await client.call_and_readback(
                "input_number",
                "set_value",
                {"entity_id": "input_number.f1_temp", "value": 28.0},
                expected_entity="input_number.f1_temp",
                expected_value="28.0",
                timeout=5.0,
                poll_interval=0.01,
            )
        assert result.applied is True
        assert result.attempts == 3
        assert _values_match("28.0", result.actual_value)

    @respx.mock
    async def test_readback_timeout_when_state_never_matches(self) -> None:
        respx.post(f"{_BASE}/api/services/input_number/set_value").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(f"{_BASE}/api/states/input_number.f1_temp").mock(
            return_value=httpx.Response(200, json={"state": "26.0"})
        )
        async with _client() as client:
            result = await client.call_and_readback(
                "input_number",
                "set_value",
                {"entity_id": "input_number.f1_temp", "value": 28.0},
                expected_entity="input_number.f1_temp",
                expected_value=28.0,
                timeout=0.05,
                poll_interval=0.01,
            )
        assert result.applied is False
        assert result.actual_value == "26.0"
        assert result.attempts >= 1
        assert result.elapsed_s >= 0.05


class TestWebSocketHelpers:
    """The WS URL derivation is pure and worth pinning."""

    def test_https_base_yields_wss(self) -> None:
        client = HAClient(base_url="https://ha.example:8123", token="t")
        assert client._ws_url() == "wss://ha.example:8123/api/websocket"

    def test_http_base_yields_ws(self) -> None:
        client = HAClient(base_url="http://supervisor/core", token="t")
        assert client._ws_url() == "ws://supervisor/core/api/websocket"
