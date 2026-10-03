import os

from app import create_app

# On Vercel each request runs in a short-lived serverless function: a background thread would be frozen
# the moment the response is sent, so a procurement run must finish inside the request (run_sync).
app = create_app(run_sync=bool(os.environ.get("VERCEL")))

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
