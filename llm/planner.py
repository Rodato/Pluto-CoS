"""Cliente OpenRouter (vía SDK openai apuntando a OpenRouter).

REGLA DURA: nunca usar SDK anthropic ni API directa de OpenAI.
"""

import os
from typing import List

from openai import OpenAI

_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)

DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6")


def chat(messages: List[dict], model: str = DEFAULT_MODEL, **kwargs) -> str:
    """Helper genérico para llamadas chat."""
    resp = _client.chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
    )
    return resp.choices[0].message.content


def propose_work_blocks(tasks: list, free_slots: list, horizon_days: int) -> list:
    """Dado tareas extraídas + slots libres, propone bloques de trabajo concretos.

    Devuelve lista de propuestas: {title, start, end, source_task_id, rationale}.
    """
    raise NotImplementedError
