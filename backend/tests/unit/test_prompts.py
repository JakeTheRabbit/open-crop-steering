"""Unit tests for the bundled prompt + knowledge loaders.

Pins the contract every other test relies on:

* All three Markdown files ship inside the package (not just in
  ``docs/``) — the wheel / Docker image must be self-contained.
* ``full_system_prompt`` is the OCS ``system.md`` followed by the
  separator and the full playbook — so what the supervisor sends to the
  LLM and audits is exactly the same string.
* ``GET /api/knowledge/grow-room-agent-playbook`` returns the verbatim
  playbook.
"""

from __future__ import annotations

import httpx
import pytest
from app.api import knowledge
from app.prompts import (
    cultivation_knowledge,
    full_system_prompt,
    grow_room_agent_playbook,
    system_prompt,
)
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


class TestPromptLoaders:
    """The three bundled markdown files load + cache correctly."""

    def test_system_prompt_loads_with_ocs_schema_and_playbook_pointer(
        self,
    ) -> None:
        text = system_prompt()
        # OCS schema is the authoritative output contract.
        assert "ocs.llm_decision.v1" in text
        # Preamble must call out that the playbook is appended.
        assert "Grow Room Agent Playbook" in text

    def test_cultivation_knowledge_loads(self) -> None:
        text = cultivation_knowledge()
        # Cultivation KB carries the rule-id taxonomy the system prompt cites.
        assert "AP-" in text or "EC-" in text

    def test_playbook_loads_with_versioning_block(self) -> None:
        text = grow_room_agent_playbook()
        # The §14 versioning block pins the schema; assert key anchors.
        assert "Playbook version" in text
        # Levers, chain, failure modes — the §-headed structure the AI relies on.
        assert "## 3. Levers" in text
        assert "## 4. The chain" in text
        assert "## 5. Failure modes" in text
        # The §8 illustrative output JSON is reference only; authority over
        # the actual response shape lives in system.md's OCS schema. The
        # separator preamble (asserted in TestFullSystemPrompt below) is
        # what tells the AI which one wins.


class TestFullSystemPrompt:
    """The supervisor's effective system prompt = system.md ++ separator ++ playbook."""

    def test_concatenation_order(self) -> None:
        full = full_system_prompt()
        sys_text = system_prompt()
        playbook = grow_room_agent_playbook()

        # system.md content appears first.
        sys_idx = full.find(sys_text)
        playbook_idx = full.find(playbook)
        assert sys_idx == 0, "system.md must lead the full prompt"
        assert playbook_idx > sys_idx, "playbook follows system.md"

    def test_separator_reasserts_ocs_schema_authority(self) -> None:
        full = full_system_prompt()
        # The separator between system.md and the playbook must re-anchor
        # the AI on the OCS output contract so it doesn't drift to the
        # playbook's §8 illustrative shape.
        assert "END OCS SYSTEM PROMPT" in full
        assert "BEGIN OPERATIONAL PLAYBOOK" in full
        assert "ocs.llm_decision.v1" in full

    def test_full_prompt_is_idempotent_and_cached(self) -> None:
        # lru_cache: same object reference across calls.
        assert full_system_prompt() is full_system_prompt()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(knowledge.router)
    return app


async def _get(path: str) -> httpx.Response:
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.get(path)


class TestKnowledgeAPI:
    """Knowledge endpoints serve the verbatim markdown texts."""

    async def test_cultivation_returns_full_text(self) -> None:
        resp = await _get("/api/knowledge/cultivation")
        assert resp.status_code == 200
        assert resp.text == cultivation_knowledge()
        assert resp.headers["content-type"].startswith("text/plain")

    async def test_playbook_returns_full_text(self) -> None:
        resp = await _get("/api/knowledge/grow-room-agent-playbook")
        assert resp.status_code == 200
        assert resp.text == grow_room_agent_playbook()
        assert resp.headers["content-type"].startswith("text/plain")
