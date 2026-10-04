"""Run the app locally: `python run.py`. The app itself is created in index.py (that is the file Vercel finds)."""
import os

from index import app  # noqa: F401  (so `from run import app` still works)

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
