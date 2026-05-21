"""Sync del vault de Obsidian desde GitHub cuando corremos en Railway.

En local el vault vive en iCloud y se lee directo del disco; un launchd agent
en la máquina del usuario sincroniza `<vault>/Granola/` → repo
`Rodato/obsidian-estudio-plural` cada 15 min.

En Railway no hay iCloud: clonamos ese repo a `OBSIDIAN_VAULT_LOCAL_PATH` al
startup y hacemos `git pull` antes de listar notas. Si las env vars no están
seteadas (caso local) las funciones son no-op.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _on_railway() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def _vault_path() -> Path:
    env = os.environ.get("OBSIDIAN_VAULT_LOCAL_PATH")
    if not env:
        raise RuntimeError("OBSIDIAN_VAULT_LOCAL_PATH no está definido")
    return Path(env)


def bootstrap_vault() -> None:
    """Clona el repo del vault si todavía no existe localmente.

    No-op fuera de Railway. Falla si `OBSIDIAN_VAULT_GIT_REPO` no está seteado
    en Railway (no queremos arrancar sin acceso a las notas).
    """
    if not _on_railway():
        return

    repo_url = os.environ.get("OBSIDIAN_VAULT_GIT_REPO")
    if not repo_url:
        raise RuntimeError(
            "OBSIDIAN_VAULT_GIT_REPO debe estar seteado en Railway "
            "(URL HTTPS+token del repo del vault)"
        )

    vault = _vault_path()
    if (vault / ".git").exists():
        log.info("Vault ya clonado en %s", vault)
        return

    vault.parent.mkdir(parents=True, exist_ok=True)
    log.info("Clonando vault desde GitHub a %s", vault)
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(vault)],
        check=True,
    )


def pull_vault() -> None:
    """`git pull` sobre el vault. No-op fuera de Railway.

    Loggea pero no levanta excepción si falla — preferimos servir notas
    posiblemente viejas que romper el briefing entero.
    """
    if not _on_railway():
        return

    vault = _vault_path()
    if not (vault / ".git").exists():
        log.warning("pull_vault llamado pero %s no es un repo git", vault)
        return

    try:
        subprocess.run(
            ["git", "-C", str(vault), "pull", "--ff-only", "--quiet"],
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("git pull del vault falló: %s", e)
