"""The web app's entrypoint. Vercel looks for a Flask `app` in a file with a default name (index.py, app.py,
main.py, server.py, wsgi.py), and it did not accept run.py. So the app is created here, and run.py re-exports it
for `python run.py` locally.

(Not named app.py on purpose: that would clash with the `app/` package.)
"""
import os

from app import create_app

# On Vercel each request runs in a short-lived serverless function: a background thread would be frozen the
# moment the response is sent, so a procurement run must finish inside the request (run_sync).
app = create_app(run_sync=bool(os.environ.get("VERCEL")))
