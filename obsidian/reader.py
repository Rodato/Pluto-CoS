"""Lee notas Granola del repo sincronizado.

El bot hace `git pull` sobre NOTAS_LOCAL_PATH antes de cada scan, y este
módulo lista archivos .md modificados desde el último run (consultando
`processed_notes` en la DB).
"""

from pathlib import Path
from typing import List


def list_new_or_modified_notes(vault_path: Path, since_mtimes: dict) -> List[Path]:
    """Devuelve .md cuyo mtime es mayor al registrado en `since_mtimes`."""
    raise NotImplementedError


def read_note(path: Path) -> str:
    """Lee el cuerpo completo del archivo .md."""
    return path.read_text(encoding="utf-8")
