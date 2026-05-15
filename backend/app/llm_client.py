"""OpenAI-compatible LLM client + the strict decision parser.

The supervisor talks to a LiteLLM-compatible endpoint
(``{settings.llm_base_url}/v1/chat/completions``) using the OpenAI chat
schema, asking for a JSON object. The model's reply is run through
:func:`parse_decision`, which is *strict* (plan locked decision #12):

* malformed JSON → :class:`~app.models.llm_call_log.LLMCallOutcome.schema_invalid`
* a payload that violates :class:`~app.schemas.llm_decision.LLMDecision`
  (missing / extra field, bad ``confidence``) → ``schema_invalid``
* an echoed ``snapshot_id`` that does not match the snapshot the call
  was made for → ``snapshot_stale`` (mismatch) or ``snapshot_unknown``
  (unknown id)
* a ``proposed_changes`` / ``recommended_action_id`` referencing a
  parameter or action outside the allowed set → ``schema_invalid``
* a decision for an unknown room → ``schema_invalid``

The model's free text is **never** executable intent — only a decision
that survives :func:`parse_decision` is usable.

Injectability
-------------
The supervisor depends on :class:`LLMClientProtocol`, not the concrete
client, so tests pass a :class:`FakeLLMClient` that returns canned
completions without any network. The real :class:`LLMClient` uses
:mod:`httpx` with the same retry posture as the HA client (retry
transport errors + HTTP 5xx; never retry 4xx).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
import structlog
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.models.llm_call_log import LLMCallOutcome
from app.schemas.llm_decision import LLMDecision

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

#: Per-request timeout for the chat-completions call (seconds).
_LLM_TIMEOUT = 60.0


class LLMClientError(RuntimeError):
    """Raised for a non-retryable LLM transport / API failure."""


@dataclass(slots=True)
class ChatMessage:
    """One OpenAI-format chat message.

    Attributes:
        role: ``system`` / ``user`` / ``assistant``.
        content: The message text.
    """

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """Serialise to the OpenAI message dict."""
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class LLMCompletion:
    """The outcome of one chat-completions call.

    Attributes:
        content: The model's reply text (the JSON we will parse), or
            ``None`` if the call failed before producing content.
        model: The model name the endpoint reported.
        prompt_tokens: Prompt token count, if the endpoint reported it.
        completion_tokens: Completion token count, if reported.
        latency_ms: Wall-clock latency of the call.
        error: Transport / API error string, or ``None`` on success.
        timed_out: ``True`` if the call failed specifically on timeout.
    """

    content: str | None
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """``True`` iff the call returned usable content."""
        return self.content is not None and self.error is None


@dataclass(slots=True)
class ParseResult:
    """The result of :func:`parse_decision`.

    Exactly one of ``decision`` / ``errors`` is meaningful: on success
    ``decision`` is set and ``outcome`` is
    :attr:`~LLMCallOutcome.parsed`; on failure ``decision`` is ``None``,
    ``outcome`` is the failure reason, and ``errors`` explains it.

    Attributes:
        outcome: The :class:`~app.models.llm_call_log.LLMCallOutcome`.
        decision: The validated :class:`LLMDecision`, or ``None``.
        errors: Human-readable validation errors (empty on success).
    """

    outcome: LLMCallOutcome
    decision: LLMDecision | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` iff parsing produced a usable decision."""
        return self.decision is not None


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Structural type the supervisor depends on for the LLM call.

    Both :class:`LLMClient` (real) and :class:`FakeLLMClient` (tests)
    satisfy this, so the supervisor never imports a concrete client.
    """

    async def complete(
        self, messages: Sequence[ChatMessage]
    ) -> LLMCompletion:  # pragma: no cover - protocol
        """Run a chat completion and return its :class:`LLMCompletion`."""
        ...


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport errors + HTTP 5xx; never retry a 4xx."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500  # noqa: PLR2004 — HTTP 5xx boundary
    return False


_llm_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4.0),
    reraise=True,
)


class LLMClient:
    """Async client for an OpenAI-compatible (LiteLLM) chat endpoint.

    Posts to ``{base_url}/v1/chat/completions``. Use as an async context
    manager (or call :meth:`aclose`) so the connection pool is released.

    Args:
        base_url: Endpoint base URL. Defaults to ``settings.llm_base_url``.
        api_key: Bearer key. Defaults to ``settings.llm_api_key``.
        model: Model name. Defaults to ``settings.llm_model``.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        *,
        timeout: float = _LLM_TIMEOUT,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.llm_api_key
        self._model = model or settings.llm_model
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        """The model name this client requests."""
        return self._model

    def _http(self) -> httpx.AsyncClient:
        """Return the lazily-created shared httpx client."""
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @_llm_retry
    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST the chat-completions request (retry-wrapped)."""
        resp = await self._http().post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        return resp

    async def complete(
        self, messages: Sequence[ChatMessage]
    ) -> LLMCompletion:
        """Run one chat completion, requesting a JSON-object reply.

        Sends the OpenAI chat schema with ``response_format`` set to
        ``json_object`` so a compliant endpoint returns parseable JSON.
        Any transport / API failure is captured into the returned
        :class:`LLMCompletion` (``error`` set) rather than raised — the
        supervisor records the failed call and moves on.

        Args:
            messages: The system + user messages, in order.

        Returns:
            An :class:`LLMCompletion`. ``ok`` is ``False`` on failure.
        """
        import time  # noqa: PLC0415 — local import keeps module surface small

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [m.to_dict() for m in messages],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        start = time.monotonic()
        try:
            resp = await self._post(body)
        except httpx.TimeoutException as exc:
            log.warning("llm.timeout", error=str(exc))
            return LLMCompletion(
                content=None,
                model=self._model,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=f"timeout: {exc}",
                timed_out=True,
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            log.warning("llm.api_error", error=str(exc))
            return LLMCompletion(
                content=None,
                model=self._model,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        data = resp.json()
        content = (
            data.get("choices", [{}])[0].get("message", {}).get("content")
        )
        usage = data.get("usage", {}) or {}
        log.info(
            "llm.completion",
            model=self._model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return LLMCompletion(
            content=content,
            model=data.get("model", self._model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
        )


class FakeLLMClient:
    """Test double for :class:`LLMClientProtocol`.

    Returns a pre-seeded :class:`LLMCompletion` (or one built from a
    JSON-able payload) without any network. Records each call's messages
    in :attr:`calls` for assertions.

    Args:
        completion: A :class:`LLMCompletion` to return verbatim.
        response_payload: A dict serialised to JSON and returned as the
            completion content (a convenient way to seed a decision).
        model: Model name reported in the completion.
        echo_snapshot_id: When ``True`` and ``response_payload`` is used,
            the fake reads the ``snapshot_id`` out of the user message it
            is given and substitutes it into the response payload — so
            the canned decision always echoes the *real* snapshot id even
            when the test cannot predict it ahead of time.
    """

    def __init__(
        self,
        *,
        completion: LLMCompletion | None = None,
        response_payload: dict[str, Any] | None = None,
        model: str = "fake-model",
        echo_snapshot_id: bool = False,
    ) -> None:
        self._model = model
        self._response_payload = response_payload
        self._echo_snapshot_id = echo_snapshot_id
        self.calls: list[list[ChatMessage]] = []
        if completion is not None:
            self._completion: LLMCompletion | None = completion
        elif response_payload is not None:
            self._completion = LLMCompletion(
                content=json.dumps(response_payload),
                model=model,
                prompt_tokens=100,
                completion_tokens=50,
                latency_ms=5,
            )
        else:
            self._completion = LLMCompletion(
                content=None, model=model, error="no canned response"
            )

    async def complete(
        self, messages: Sequence[ChatMessage]
    ) -> LLMCompletion:
        """Record the call and return the canned completion.

        With ``echo_snapshot_id`` set, the response payload's
        ``snapshot_id`` is rewritten to whatever the user message's
        embedded snapshot carries, so the decision echoes the real id.
        """
        self.calls.append(list(messages))
        if not (self._echo_snapshot_id and self._response_payload is not None):
            assert self._completion is not None
            return self._completion

        snapshot_id = _extract_snapshot_id(messages)
        payload = {**self._response_payload, "snapshot_id": snapshot_id}
        return LLMCompletion(
            content=json.dumps(payload),
            model=self._model,
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=5,
        )


def _extract_snapshot_id(messages: Sequence[ChatMessage]) -> int | None:
    """Pull the ``snapshot_id`` out of a supervisor user message.

    The supervisor's user message is a JSON object whose ``snapshot``
    key is the snapshot payload — and that payload carries
    ``snapshot_id``. Returns ``None`` if it cannot be found (the fake
    then echoes whatever was seeded).
    """
    for message in messages:
        if message.role != "user":
            continue
        try:
            body = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            continue
        snapshot = body.get("snapshot", {}) if isinstance(body, dict) else {}
        if isinstance(snapshot, dict) and "snapshot_id" in snapshot:
            return int(snapshot["snapshot_id"])
    return None


def parse_decision(  # noqa: PLR0911 — strict guard chain, one return per check
    raw: str | None,
    *,
    expected_snapshot_id: int,
    known_snapshot_ids: set[int] | None = None,
    allowed_action_ids: set[str] | None = None,
    allowed_params: set[str] | None = None,
    known_room_ids: set[str] | None = None,
    decision_room_id: str | None = None,
) -> ParseResult:
    """Strictly parse + validate an LLM reply into an :class:`LLMDecision`.

    The check chain (each failure short-circuits to a specific
    :class:`~app.models.llm_call_log.LLMCallOutcome`):

    1. ``raw`` is non-empty, valid JSON, an object → else ``schema_invalid``.
    2. The object validates against :class:`LLMDecision` (schema version,
       all fields, ``confidence`` range, no extra fields) → else
       ``schema_invalid``.
    3. The echoed ``snapshot_id`` equals ``expected_snapshot_id``. A
       mismatch is ``snapshot_stale`` when the id is a *known* (older)
       snapshot, ``snapshot_unknown`` otherwise.
    4. ``recommended_action_id`` (if set) is in ``allowed_action_ids``;
       every ``proposed_changes`` ``param_name`` is in ``allowed_params``
       — else ``schema_invalid``.
    5. ``decision_room_id`` (if given) is a known room — else
       ``schema_invalid``.

    Args:
        raw: The model's reply text.
        expected_snapshot_id: The id of the snapshot the call was for.
        known_snapshot_ids: Snapshot ids that exist (to distinguish a
            stale-but-real id from an invented one). Optional.
        allowed_action_ids: ``action_id`` values from the allowed action
            set. Optional — when ``None`` the action id is not checked.
        allowed_params: Parameter names a proposal may target. Optional —
            when ``None`` the params are not checked.
        known_room_ids: Room ids that exist. Optional.
        decision_room_id: The room the decision is for (cross-checked
            against ``known_room_ids`` when both are given).

    Returns:
        A :class:`ParseResult`.
    """
    if not raw or not raw.strip():
        return ParseResult(
            LLMCallOutcome.schema_invalid, errors=["empty LLM response"]
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ParseResult(
            LLMCallOutcome.schema_invalid,
            errors=[f"malformed JSON: {exc}"],
        )
    if not isinstance(data, dict):
        return ParseResult(
            LLMCallOutcome.schema_invalid,
            errors=[f"expected a JSON object, got {type(data).__name__}"],
        )

    try:
        decision = LLMDecision.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        return ParseResult(LLMCallOutcome.schema_invalid, errors=errors)

    # --- snapshot_id echo check -------------------------------------------
    if decision.snapshot_id != expected_snapshot_id:
        is_known = (
            known_snapshot_ids is not None
            and decision.snapshot_id in known_snapshot_ids
        )
        outcome = (
            LLMCallOutcome.snapshot_stale
            if is_known
            else LLMCallOutcome.snapshot_unknown
        )
        return ParseResult(
            outcome,
            errors=[
                f"snapshot_id {decision.snapshot_id} does not match the "
                f"snapshot sent ({expected_snapshot_id})"
            ],
        )

    # --- allowed-action / allowed-param checks ----------------------------
    structural_errors: list[str] = []
    if (
        allowed_action_ids is not None
        and decision.recommended_action_id is not None
        and decision.recommended_action_id not in allowed_action_ids
    ):
        structural_errors.append(
            f"recommended_action_id "
            f"{decision.recommended_action_id!r} is not in the allowed "
            "action set"
        )
    if allowed_params is not None:
        for change in decision.proposed_changes:
            if change.param_name not in allowed_params:
                structural_errors.append(
                    f"proposed change targets unknown / disallowed "
                    f"parameter {change.param_name!r}"
                )

    # --- unknown-room check -----------------------------------------------
    if (
        known_room_ids is not None
        and decision_room_id is not None
        and decision_room_id not in known_room_ids
    ):
        structural_errors.append(f"unknown room {decision_room_id!r}")

    if structural_errors:
        return ParseResult(
            LLMCallOutcome.schema_invalid, errors=structural_errors
        )

    return ParseResult(LLMCallOutcome.parsed, decision=decision)
