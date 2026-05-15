"""Cultivation-knowledge API.

Serves the bundled cultivation knowledge base
(``prompts/cultivation_knowledge.md``). The supervisor's system prompt
embeds a condensed form of this document and cites its rule ids
(``AP-*`` / ``EC-*`` / ``SAT-*``); this endpoint exposes the *full*
text so the AI — or a human reviewer following a ``reason_code`` — can
retrieve a complete rule definition on demand (plan v3, Phase 7).

The endpoint is read-only and unauthenticated beyond the ingress /
standalone auth the app already applies: the knowledge base is reference
material, not facility data.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.prompts import cultivation_knowledge

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
