"""Cultivation-knowledge API.

Serves the bundled cultivation reference texts:

* ``GET /api/knowledge/cultivation`` — ``prompts/cultivation_knowledge.md``.
  The supervisor's ``system.md`` prompt embeds a condensed form of this
  document and cites its rule ids (``AP-*`` / ``EC-*`` / ``SAT-*``); this
  endpoint exposes the *full* text so the AI — or a human reviewer
  following a ``reason_code`` — can retrieve a complete rule definition
  on demand (plan v3, Phase 7).
* ``GET /api/knowledge/grow-room-agent-playbook`` —
  ``prompts/grow_room_agent_playbook.md``. The Legacy Ag operational
  playbook the supervisor concatenates into its system prompt every
  tick. Same purpose: a human reviewer can pull the exact reference the
  AI is reading.

Both endpoints are read-only and unauthenticated beyond the ingress /
standalone auth the app already applies: the knowledge base is reference
material, not facility data.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.prompts import cultivation_knowledge, grow_room_agent_playbook

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get(
    "/cultivation",
    response_class=PlainTextResponse,
    summary="Full cultivation knowledge base",
)
async def get_cultivation_knowledge() -> str:
    """Return the full cultivation knowledge base as Markdown text.

    The response is the verbatim ``cultivation_knowledge.md`` shipped
    inside the add-on — the same source the supervisor's system prompt
    is built from.

    Returns:
        The knowledge-base Markdown, served as ``text/plain``.
    """
    return cultivation_knowledge()


@router.get(
    "/grow-room-agent-playbook",
    response_class=PlainTextResponse,
    summary="Grow-room agent playbook (Legacy Ag)",
)
async def get_grow_room_agent_playbook() -> str:
    """Return the grow-room agent playbook as Markdown text.

    The response is the verbatim ``grow_room_agent_playbook.md``
    shipped inside the add-on — the dense, machine-oriented operational
    reference (capability map, stage envelopes, lever step limits and
    cool-downs, cause-effect chain, failure modes, decision algorithm,
    safety rails, worked examples) that is concatenated into the
    supervisor's system prompt every tick.

    Returns:
        The playbook Markdown, served as ``text/plain``.
    """
    return grow_room_agent_playbook()
