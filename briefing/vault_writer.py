"""Escribe el briefing como página Markdown en el vault de Obsidian.

Path: `<vault>/Briefings/YYYY-MM-DD.md`. Sobrescribe si ya existe (los
briefings son regenerables).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BRIEFINGS_SUBDIR = "Briefings"


def briefings_dir(vault_root: Optional[Path] = None) -> Path:
    if vault_root is None:
        env = os.environ.get("OBSIDIAN_VAULT_LOCAL_PATH")
        if not env:
            raise RuntimeError("OBSIDIAN_VAULT_LOCAL_PATH no está definido")
        vault_root = Path(env)
    return vault_root / BRIEFINGS_SUBDIR


def write_briefing(briefing_date: date, markdown_content: str) -> Path:
    """Escribe el markdown al vault. Devuelve el path absoluto."""
    target_dir = briefings_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{briefing_date.isoformat()}.md"
    out.write_text(markdown_content, encoding="utf-8")
    log.info("Briefing escrito: %s", out)
    return out
