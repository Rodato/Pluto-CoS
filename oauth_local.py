"""Re-generar token.json corriendo el OAuth flow local con UI web.

Uso:
    .venv/bin/python oauth_local.py

Arranca Flask en http://localhost:8765, abre el navegador, autorizás con
Google y muestra el token nuevo en base64 listo para pegar en Railway como
GOOGLE_TOKEN_JSON. Calcado de ai-mail-forwarder/oauth_local.py — single-user,
solo localhost.
"""
import base64
import html
import json
import os
import secrets
import tempfile
import threading
import traceback
import webbrowser

# Google a veces devuelve un subset de scopes y oauthlib falla con
# "Scope has changed". Esto lo desactiva.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
# Permitir http en redirect_uri para localhost (oauthlib lo bloquea por defecto).
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from flask import Flask, redirect, request
from google_auth_oauthlib.flow import Flow

from auth.google_auth import SCOPES, TOKEN_PATH, _get_client_config

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/oauth/callback"

app = Flask(__name__)
_pending: dict[str, Flow] = {}


def _build_flow() -> Flow:
    """Construye un Flow desde credentials.json usando un archivo temporal
    (Flow.from_client_secrets_file requiere un path)."""
    config = _get_client_config()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        tmp_path = f.name
    try:
        return Flow.from_client_secrets_file(tmp_path, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    finally:
        os.unlink(tmp_path)


@app.route("/")
def root():
    return redirect("/oauth/start")


@app.route("/oauth/start")
def oauth_start():
    flow = _build_flow()
    state = secrets.token_urlsafe(16)
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        prompt="consent",
    )
    _pending[state] = flow
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    state = request.args.get("state")
    flow = _pending.pop(state, None)
    if not flow:
        return "Estado invalido o expirado.", 400

    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        tb = html.escape(traceback.format_exc())
        return (
            f"<h2>Error en fetch_token</h2><pre>{html.escape(str(e))}</pre>"
            f"<details><summary>Traceback</summary><pre>{tb}</pre></details>",
            500,
        )

    token_json = flow.credentials.to_json()
    TOKEN_PATH.write_text(token_json)

    token_b64 = base64.b64encode(token_json.encode()).decode()
    return f"""
    <!doctype html>
    <html><head><meta charset="utf-8"><title>Token regenerado</title>
    <style>
      body {{ font-family: -apple-system, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; }}
      h2 {{ color: #0a7d2c; }}
      textarea {{ width: 100%; height: 160px; font-family: monospace; font-size: 12px; padding: 8px; }}
      .step {{ background: #f4f4f4; padding: 12px 16px; border-radius: 6px; margin: 16px 0; }}
      code {{ background: #eee; padding: 2px 6px; border-radius: 3px; }}
    </style></head>
    <body>
      <h2>Token regenerado</h2>
      <p>Guardado localmente en <code>token.json</code>.</p>
      <div class="step">
        <strong>Pegar en Railway</strong> como variable <code>GOOGLE_TOKEN_JSON</code> (formato base64):
        <textarea readonly onclick="this.select()">{token_b64}</textarea>
      </div>
      <p>Ya podés cerrar esta ventana.</p>
    </body></html>
    """


def _open_browser():
    webbrowser.open(f"http://localhost:{PORT}/oauth/start")


if __name__ == "__main__":
    threading.Timer(1.0, _open_browser).start()
    print(f"Servidor OAuth en http://localhost:{PORT}")
    print("Abriendo navegador en 1s... (Ctrl+C para abortar)")
    app.run(host="localhost", port=PORT, debug=False, use_reloader=False)
