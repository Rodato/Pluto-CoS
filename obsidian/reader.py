"""Lectura del vault de Obsidian.

Granola guarda las notas en `<vault>/Granola/` (recursivo en subcarpetas
por equipo/tema). Cada nota es markdown con frontmatter YAML simple:

    ---
    granola_id: ...
    title: "..."
    created_at: 2026-...
    updated_at: 2026-...
    ---
    # Title
    ...

Algunas notas son "shells" (242 bytes, solo título + link a Granola, sin
cuerpo real). Las filtramos para no quemar tokens del LLM.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from obsidian.git_sync import pull_vault

MIN_BODY_CHARS = 100

GRANOLA_SUBDIR = "Granola"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_GRANOLA_LINK_RE = re.compile(r"https?://notes\.granola\.ai/\S+")


@dataclass
class NoteContent:
    """Representación parseada de una nota Granola."""
    path: Path
    title: str
    body: str
    updated_at: Optional[datetime]
    mtime: datetime


def granola_path(vault_root: Optional[Path] = None) -> Path:
    """Path absoluto a la carpeta Granola dentro del vault."""
    if vault_root is None:
        env = os.environ.get("OBSIDIAN_VAULT_LOCAL_PATH")
        if not env:
            raise RuntimeError("OBSIDIAN_VAULT_LOCAL_PATH no está definido")
        vault_root = Path(env)
    return vault_root / GRANOLA_SUBDIR


def project_from_path(path: Path, vault_root: Optional[Path] = None) -> str:
    """Nombre del proyecto = primer subdirectorio dentro de Granola/.

    `<vault>/Granola/AMA/note.md`     → "AMA"
    `<vault>/Granola/Puddle/x/n.md`   → "Puddle"
    `<vault>/Granola/note.md`         → "Varios"
    """
    base = granola_path(vault_root)
    try:
        relative = path.relative_to(base)
    except ValueError:
        return "Varios"
    parts = relative.parts
    if len(parts) >= 2:
        return parts[0]
    return "Varios"


def _parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    """(frontmatter_dict, body_sin_frontmatter). Parser minimalista key:value."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    fm: Dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"')
    return fm, body


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_footer_links(body: str) -> str:
    return _GRANOLA_LINK_RE.sub("", body).strip()


def _strip_main_heading(body: str) -> str:
    return re.sub(r"^\s*#\s+[^\n]*\n", "", body, count=1)


def _is_shell_note(body_clean: str) -> bool:
    return len(body_clean) < MIN_BODY_CHARS


def read_note(path: Path) -> NoteContent:
    """Lee una nota .md y devuelve su contenido parseado."""
    raw = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(raw)
    body = _strip_main_heading(body)
    body = _strip_footer_links(body).strip()

    title = fm.get("title") or path.stem
    updated_at = _parse_iso(fm.get("updated_at"))
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    return NoteContent(
        path=path,
        title=title,
        body=body,
        updated_at=updated_at,
        mtime=mtime,
    )


def list_new_or_modified_notes(
    vault_path: Optional[Path] = None,
    since_mtimes: Optional[Dict[str, datetime]] = None,
) -> List[Path]:
    """Lista .md en Granola/ (recursivo) nuevos o con mtime mayor al registrado.

    Filtra notas shell (sin contenido real más allá del título).
    """
    pull_vault()
    base = granola_path(vault_path) if vault_path else granola_path()
    if not base.exists():
        return []

    since = since_mtimes or {}
    result: List[Path] = []

    for path in base.rglob("*.md"):
        if not path.is_file():
            continue

        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        prev = since.get(str(path))
        if prev is not None and mtime <= prev:
            continue

        try:
            note = read_note(path)
        except Exception:
            continue
        if _is_shell_note(note.body):
            continue

        result.append(path)

    return result
