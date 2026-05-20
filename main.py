"""Entry point: FastAPI (healthcheck) + APScheduler + Telegram bot.

Los tres componentes corren en el mismo proceso (mismo event loop) para Railway:
- FastAPI sirve solo `/` como healthcheck (Railway necesita PORT abierto)
- APScheduler dispara el chequeo de invitaciones cada 15 min
- python-telegram-bot queda en polling

La autorización OAuth se hace **localmente** corriendo `oauth_local.py` una
sola vez; el token resultante se pega como `GOOGLE_TOKEN_JSON` en Railway.
"""

import contextlib
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

from scheduler import build_scheduler, check_new_invitations, run_daily_briefing
from telegram_bot import bot as tg_bot
from telegram_bot import handlers as tg_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("calendar-planner")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tg_app = tg_bot.build_app()
    tg_handlers.register(tg_app)

    async def _invitations_job():
        await check_new_invitations(tg_app)

    async def _briefing_job():
        await run_daily_briefing(tg_app)

    scheduler = build_scheduler(_invitations_job, _briefing_job)

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


def _run() -> None:
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    _run()
