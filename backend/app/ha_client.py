"""Home Assistant REST + WebSocket client.

This module is the single gateway between the control plane and Home
Assistant. It is deliberately asymmetric:

* **Reads** — ``get_state`` / ``get_states`` (REST) and ``get_states_ws``
  / ``subscribe_events`` (WebSocket) are unrestricted.
* **Writes** — the *only* write path exposed is :meth:`HAClient.call_service`.
  There is intentionally **no** ``set_state`` write path. ``set_state``
  mutates Home Assistant's state machine without driving the underlying
  device; using it for control would make readback meaningless and is
  explicitly forbidden by the project's locked decisions. Every control
  write must therefore land as a real service call.

Auth is mode-aware: :func:`app.config.get_settings` already resolves
``ha_url`` / ``ha_token`` for both add-on mode (Supervisor token) and
standalone mode (long-lived token), so this client just consumes them.

Transient REST failures (``httpx.TransportError`` and HTTP 5xx) are
retried with exponential backoff via :mod:`tenacity`; 4xx responses are
surfaced immediately because retrying a client error never helps.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
import websockets
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings

log = structlog.get_logger(__name__)

# Default timeouts (seconds).
_REST_TIMEOUT = 10.0
_WS_TIMEOUT = 10.0


class HAClientError(RuntimeError):
    """Raised for non-retryable Home Assistant client failures."""


class HAAuthError(HAClientError):
    """Raised when the WebSocket auth handshake is rejected by HA."""


@dataclass(slots=True)
class ReadbackResult:
    """Outcome of a :meth:`HAClient.call_and_readback` call.

    Attributes:
        applied: ``True`` if the observed state matched the expected value
            within the timeout window.
        actual_value: The last state value observed for the entity (the
            value that matched, or the final value seen before timeout).
        attempts: Number of ``get_state`` polls performed.
        elapsed_s: Wall-clock seconds spent in the call + poll loop.
    """

    applied: bool
    actual_value: Any
    attempts: int
    elapsed_s: float


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport errors and HTTP 5xx; never retry 4xx."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500  # noqa: PLR2004 — HTTP 5xx boundary
    return False


_rest_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.25, max=2.0),
    reraise=True,
)


def _values_match(expected: Any, actual: Any) -> bool:
    """Compare two state values, tolerant of float formatting.

    Home Assistant state values are strings, but a setpoint readback of
    ``"28.0"`` should still satisfy an expected value of ``28`` or
    ``"28"``. We first try a numeric comparison; if either side is not
    numeric we fall back to a trimmed string comparison.
    """
    try:
        return float(expected) == float(actual)
    except (TypeError, ValueError):
        return str(expected).strip() == str(actual).strip()


class HAClient:
    """Async Home Assistant client (REST for control + reads, WS for streams).

    The client owns a lazily-created :class:`httpx.AsyncClient`. Use it as
    an async context manager, or call :meth:`aclose` explicitly, so the
    underlying connection pool is released.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        rest_timeout: float = _REST_TIMEOUT,
    ) -> None:
        """Build a client.

        Args:
            base_url: Home Assistant base URL. Defaults to ``settings.ha_url``.
            token: Bearer token. Defaults to ``settings.ha_token`` (already
                mode-resolved by :mod:`app.config`).
            rest_timeout: Per-request REST timeout in seconds.
        """
        settings = get_settings()
        self._base_url = (base_url or settings.ha_url).rstrip("/")
        self._token = token if token is not None else settings.ha_token
        self._rest_timeout = rest_timeout
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _http(self) -> httpx.AsyncClient:
        """Return the lazily-created shared httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._rest_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HAClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- REST reads -------------------------------------------------------

    @_rest_retry
    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """Return the full state object for ``entity_id`` via the REST API.

        This is a read; it never mutates Home Assistant.
        """
        resp = await self._http().get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    @_rest_retry
    async def get_states(self) -> list[dict[str, Any]]:
        """Return all state objects known to Home Assistant via REST."""
        resp = await self._http().get("/api/states")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # -- REST control (the ONLY write path) ------------------------------

    @_rest_retry
    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Call a Home Assistant service — the sole control write path.

        Args:
            domain: Service domain, e.g. ``"input_number"``.
            service: Service name, e.g. ``"set_value"``.
            data: Service payload (entity_id + parameters).

        Returns:
            The list of state objects HA reports as changed by the call.

        Note:
            This deliberately wraps ``POST /api/services/{domain}/{service}``
            only. There is no ``set_state`` counterpart by design — control
            must drive real devices, not spoof the state machine.
        """
        log.info("ha.call_service", domain=domain, service=service, data=data)
        resp = await self._http().post(
            f"/api/services/{domain}/{service}",
            json=data or {},
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def call_and_readback(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        expected_entity: str,
        expected_value: Any,
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 — own poll deadline, not a single awaitable
        poll_interval: float = 0.25,
    ) -> ReadbackResult:
        """Call a service then poll until the entity reaches the expected value.

        Home Assistant service calls are asynchronous with respect to the
        physical device + state machine, so a bare ``call_service`` gives
        no proof the change took effect. This helper closes that loop: it
        issues the call, then polls ``get_state(expected_entity)`` until
        the state matches ``expected_value`` (float-tolerant compare) or
        ``timeout`` elapses.

        Args:
            domain: Service domain.
            service: Service name.
            data: Service payload.
            expected_entity: Entity whose state should reflect the change.
            expected_value: The value the entity's ``state`` should reach.
            timeout: Maximum seconds to wait for the state to settle.
            poll_interval: Seconds between ``get_state`` polls.

        Returns:
            A :class:`ReadbackResult` capturing whether the change applied,
            the last observed value, the poll count, and elapsed time.
        """
        start = time.monotonic()
        await self.call_service(domain, service, data)

        attempts = 0
        actual: Any = None
        while True:
            attempts += 1
            state = await self.get_state(expected_entity)
            actual = state.get("state")
            if _values_match(expected_value, actual):
                elapsed = time.monotonic() - start
                log.info(
                    "ha.readback.applied",
                    entity=expected_entity,
                    expected=expected_value,
                    actual=actual,
                    attempts=attempts,
                    elapsed_s=round(elapsed, 3),
                )
                return ReadbackResult(
                    applied=True,
                    actual_value=actual,
                    attempts=attempts,
                    elapsed_s=elapsed,
                )

            if time.monotonic() - start >= timeout:
                elapsed = time.monotonic() - start
                log.warning(
                    "ha.readback.timeout",
                    entity=expected_entity,
                    expected=expected_value,
                    actual=actual,
                    attempts=attempts,
                    elapsed_s=round(elapsed, 3),
                )
                return ReadbackResult(
                    applied=False,
                    actual_value=actual,
                    attempts=attempts,
                    elapsed_s=elapsed,
                )

            await self._async_sleep(poll_interval)

    @staticmethod
    async def _async_sleep(seconds: float) -> None:
        """Indirection over ``asyncio.sleep`` to keep tests fast + patchable."""
        import asyncio  # noqa: PLC0415 — local import keeps the module surface small

        await asyncio.sleep(seconds)

    # -- WebSocket --------------------------------------------------------

    def _ws_url(self) -> str:
        """Derive the WebSocket API URL from the REST base URL."""
        url = self._base_url
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        return f"{url}/api/websocket"

    async def _ws_authenticate(
        self, ws: websockets.ClientConnection
    ) -> None:
        """Perform the HA WebSocket auth handshake.

        HA sends ``auth_required``; we reply with ``auth`` carrying the
        bearer token; HA responds ``auth_ok`` or ``auth_invalid``.
        """
        greeting = json.loads(await ws.recv())
        if greeting.get("type") != "auth_required":
            raise HAAuthError(
                f"expected auth_required, got {greeting.get('type')!r}"
            )
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        result = json.loads(await ws.recv())
        if result.get("type") != "auth_ok":
            raise HAAuthError(
                f"auth rejected by Home Assistant: {result.get('message', result)}"
            )
        log.info("ha.ws.authenticated", ha_version=result.get("ha_version"))

    async def subscribe_events(
        self, event_type: str = "state_changed"
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to a Home Assistant event stream over WebSocket.

        Connects, authenticates, sends ``subscribe_events`` and yields each
        ``event`` payload as it arrives. The connection is held open for
        the lifetime of the generator and closed on exit.

        Args:
            event_type: HA event type to subscribe to, e.g. ``state_changed``.

        Yields:
            The ``event`` dict from each ``type: "event"`` message.
        """
        async with websockets.connect(
            self._ws_url(), open_timeout=_WS_TIMEOUT
        ) as ws:
            await self._ws_authenticate(ws)
            await ws.send(
                json.dumps(
                    {"id": 1, "type": "subscribe_events", "event_type": event_type}
                )
            )
            ack = json.loads(await ws.recv())
            if not ack.get("success", False):
                raise HAClientError(
                    f"subscribe_events failed: {ack.get('error', ack)}"
                )
            log.info("ha.ws.subscribed", event_type=event_type)
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") == "event":
                    yield message["event"]

    async def get_states_ws(self) -> list[dict[str, Any]]:
        """Fetch all current states over the WebSocket API.

        Connects, authenticates, issues ``get_states`` and returns the
        result list. Useful when a WS connection is preferred over REST
        for current-state reads (locked decision: WS is the source of
        truth for current actuator state).
        """
        async with websockets.connect(
            self._ws_url(), open_timeout=_WS_TIMEOUT
        ) as ws:
            await self._ws_authenticate(ws)
            await ws.send(json.dumps({"id": 1, "type": "get_states"}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 1 and message.get("type") == "result":
                    if not message.get("success", False):
                        raise HAClientError(
                            f"get_states failed: {message.get('error', message)}"
                        )
                    return message.get("result", [])  # type: ignore[no-any-return]

    async def list_registry(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch the area / entity / device registries over the WS API.

        The room-configuration entity picker needs HA's registries to
        show every area and the entities assigned to it. The registries
        are only exposed over the WebSocket API — there is no REST
        equivalent.

        Returns:
            ``{"areas": [...], "entities": [...], "devices": [...]}`` —
            the raw registry rows exactly as HA returns them.
        """
        commands = (
            ("areas", "config/area_registry/list"),
            ("entities", "config/entity_registry/list"),
            ("devices", "config/device_registry/list"),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        async with websockets.connect(
            self._ws_url(), open_timeout=_WS_TIMEOUT, max_size=None
        ) as ws:
            await self._ws_authenticate(ws)
            for idx, (key, cmd) in enumerate(commands, start=1):
                await ws.send(json.dumps({"id": idx, "type": cmd}))
                while True:
                    message = json.loads(await ws.recv())
                    if (
                        message.get("id") == idx
                        and message.get("type") == "result"
                    ):
                        if not message.get("success", False):
                            raise HAClientError(
                                f"{cmd} failed: {message.get('error', message)}"
                            )
                        out[key] = message.get("result", [])
                        break
        return out
