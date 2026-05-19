"""Entry point: FastAPI (OAuth callback) + APScheduler + Telegram bot.

Los tres componentes corren en el mismo proceso (mismo event loop) para Railway:
- FastAPI sirve `/oauth/login` y `/oauth/callback`
- APScheduler dispara el chequeo de invitaciones cada 15 min
- python-telegram-bot queda en polling
"""

import asyncio
import contextlib
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import google_auth
from scheduler import build_scheduler, check_new_invitations
from telegram_bot import bot as tg_bot
from telegram_bot import handlers as tg_handlers

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("calendar-planner")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tg_app = tg_bot.build_app()
    tg_handlers.register(tg_app)

    async def _job():
        await check_new_invitations(tg_app)

    scheduler = build_scheduler(_job)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    scheduler.start()

    app.state.tg_app = tg_app
    app.state.scheduler = scheduler
    log.info("Bot + scheduler + FastAPI corriendo")

    try:
        yield
    finally:
        log.info("Shutting down…")
        scheduler.shutdown(wait=False)
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "calendar-planner", "status": "ok"}


@app.get("/oauth/login")
async def oauth_login():
    """Redirige al consentimiento de Google."""
    import secrets
    state = secrets.token_urlsafe(16)
    url = google_auth.build_auth_url(state=state)
    return RedirectResponse(url=url)


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str = ""):
    """Intercambia el code por tokens y los guarda en DB."""
    try:
        google_auth.exchange_code(code)
    except Exception as exc:
        log.exception("Error en OAuth callback")
        return HTMLResponse(
            f"<h1>Error</h1><pre>{exc}</pre>", status_code=500
        )
    return HTMLResponse(
        "<h1>✅ Autorizado</h1><p>Ya podés cerrar esta pestaña y volver a Telegram.</p>"
    )


def _run() -> None:
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    _run()
