"""LLM prompt templates. Markdown sources live alongside this module.

Two markdown files ship inside the package so the add-on is fully
self-contained:

* ``system.md`` — the supervisor's system prompt: the condensed
  coupled-systems mental model, the control hierarchy, the 12
  anti-patterns and 15 coupling rules, and the strict output contract.
* ``cultivation_knowledge.md`` — the full cultivation knowledge base, a
  verbatim copy of ``plans/cultivation_knowledge.md``. Served by the
  ``GET /api/knowledge/cultivation`` endpoint and used by the supervisor
  for the system prompt's source material.

Both are read with :func:`functools.lru_cache` so they hit the disk once
per process.
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


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """Return the supervisor system prompt (``system.md``), cached.

    Returns:
        The full system-prompt markdown text.

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
