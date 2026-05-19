"""Extracción de tareas accionables desde el cuerpo completo de la nota.

Usa LLM (OpenRouter) para identificar tareas con contexto, no solo regex
sobre la sección "Próximos Pasos". Devuelve lista de tareas estructuradas.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ExtractedTask:
    title: str
    context: str  # contexto de la reunión
    estimated_minutes: Optional[int]
    deadline_hint: Optional[str]  # texto libre, ej "antes del viernes"
    source_note: str  # path relativo al .md


def extract_tasks(note_body: str, note_path: str) -> List[ExtractedTask]:
    """Llama al LLM para extraer tareas del cuerpo completo."""
    raise NotImplementedError
