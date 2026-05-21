"""LLM prompt templates. Markdown sources live alongside this module.

Three markdown files ship inside the package so the add-on is fully
self-contained:

* ``system.md`` — the supervisor's OCS-side system prompt: role, the
  condensed coupled-systems mental model, the control hierarchy, the
  12 anti-patterns + 15 coupling rules, and the **authoritative output
  contract** (``ocs.llm_decision.v1``).
* ``cultivation_knowledge.md`` — the full cultivation knowledge base, a
  verbatim copy of ``docs/concepts/cultivation_knowledge.md``. Served
  by ``GET /api/knowledge/cultivation``.
* ``grow_room_agent_playbook.md`` — Legacy Ag's dense, machine-oriented
  grow-room operational playbook (capability map, stage envelopes,
  lever step limits / cool-downs / forbidden combos, cause-effect
  chain, failure modes, decision algorithm, safety rails, worked
  examples). Verbatim copy of ``docs/concepts/grow-room-agent-playbook.md``.
  Served by ``GET /api/knowledge/grow-room-agent-playbook``.

The supervisor's effective system prompt is the **concatenation** of
``system.md`` and the playbook (see :func:`full_system_prompt`) so the
AI has the full operational reference in context every tick. The OCS
schema in ``system.md`` is the authoritative output contract; the
playbook's §8 JSON shape is illustrative reference only — the
preamble in ``system.md`` makes this explicit.

All files are read with :func:`functools.lru_cache` so they hit the disk
once per process.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: Directory holding the markdown prompt sources (this package's dir).
_PROMPTS_DIR = Path(__file__).resolve().parent

#: File name of the supervisor system prompt.
SYSTEM_PROMPT_FILE = "system.md"

#: File name of the bundled cultivation knowledge base.
CULTIVATION_KNOWLEDGE_FILE = "cultivation_knowledge.md"

#: File name of the bundled grow-room agent playbook (Legacy Ag).
GROW_ROOM_AGENT_PLAYBOOK_FILE = "grow_room_agent_playbook.md"

#: Separator that marks where the playbook is appended in the full
#: system prompt. The wording makes clear which side owns the output
#: schema (OCS) and which side is operational reference (playbook).
_PLAYBOOK_SEPARATOR = (
    "\n\n=== END OCS SYSTEM PROMPT — BEGIN OPERATIONAL PLAYBOOK ===\n\n"
    "The remainder of this document is your full grow-room operational\n"
    "playbook. Use it as reference for levers, step limits, cool-downs,\n"
    "forbidden combinations, failure-mode patterns, chain reasoning, and\n"
    "worked examples. Your response shape is the OCS schema defined\n"
    "above (``ocs.llm_decision.v1``), NOT the playbook's §8 illustrative\n"
    "JSON. Reason in the playbook's terms; produce the OCS schema.\n\n"
)


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """Return the OCS supervisor system prompt (``system.md``), cached.

    This is the OCS-side prompt only — the role-setting preamble, the
    coupled-systems mental model, the anti-pattern / coupling-rule
    citations, and the authoritative ``ocs.llm_decision.v1`` output
    contract. For the supervisor's *effective* system prompt (this plus
    the playbook), use :func:`full_system_prompt`.

    Returns:
        The full ``system.md`` markdown text.

    Raises:
        FileNotFoundError: If ``system.md`` is missing from the package
            (a packaging error — the prompt must ship inside the repo).
    """
    return (_PROMPTS_DIR / SYSTEM_PROMPT_FILE).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def cultivation_knowledge() -> str:
    """Return the bundled cultivation knowledge base, cached.

    This is the verbatim text served by ``GET /api/knowledge/cultivation``
    and the source of the system prompt's domain context.

    Returns:
        The full ``cultivation_knowledge.md`` markdown text.

    Raises:
        FileNotFoundError: If the knowledge file is missing from the
            package.
    """
    return (_PROMPTS_DIR / CULTIVATION_KNOWLEDGE_FILE).read_text(
        encoding="utf-8"
    )


@lru_cache(maxsize=1)
def grow_room_agent_playbook() -> str:
    """Return the bundled grow-room agent playbook, cached.

    Legacy Ag's dense machine-oriented operational playbook for an LLM
    agent running a medicinal-cannabis grow room. Verbatim text served
    by ``GET /api/knowledge/grow-room-agent-playbook`` and concatenated
    into the supervisor's effective system prompt (see
    :func:`full_system_prompt`).

    Returns:
        The full ``grow_room_agent_playbook.md`` markdown text.

    Raises:
        FileNotFoundError: If the playbook file is missing from the
            package.
    """
    return (_PROMPTS_DIR / GROW_ROOM_AGENT_PLAYBOOK_FILE).read_text(
        encoding="utf-8"
    )


@lru_cache(maxsize=1)
def full_system_prompt() -> str:
    """Return the supervisor's full effective system prompt, cached.

    Concatenates :func:`system_prompt` (OCS contract — role, coupling
    rules, output schema) with :func:`grow_room_agent_playbook` (Legacy
    Ag operational reference) under a clear separator that reasserts
    the OCS output contract is authoritative.

    The supervisor's LLM tick sends exactly this string as the
    ``system`` message content, so the AI sees the OCS schema first
    (top of file), reads the operational playbook as reference, and is
    re-anchored by the separator before producing its response.

    Returns:
        The concatenated prompt.
    """
    return (
        system_prompt()
        + _PLAYBOOK_SEPARATOR
        + grow_room_agent_playbook()
    )
