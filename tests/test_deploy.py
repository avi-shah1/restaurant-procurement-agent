"""Deployment to Vercel: the entrypoint, serverless-safe storage, the optional password gate, dependencies."""
import base64
import importlib
import tomllib
from pathlib import Path

import pytest

import app.data.local_repo as local_repo
from app import create_app

ROOT = Path(__file__).resolve().parents[1]


def basic(password: str, user: str = "anyone") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


# ---------- the Vercel entrypoint ----------
def test_there_is_no_half_configured_pyproject():
    """Vercel installs a Python app's packages from pyproject.toml whenever one exists. A pyproject.toml with no
    [project] table made every deploy fail ("uv lock ... No `project` table found"). Either there is none (Vercel
    then uses requirements.txt), or it is a complete project that lists the dependencies itself."""
    path = ROOT / "pyproject.toml"
    if not path.exists():
        return
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    assert project is not None and project.get("dependencies"), "pyproject.toml must be complete, or deleted"


def test_vercel_can_find_the_app_by_its_default_file_name():
    """The deploy failed with "No Flask entrypoint found in default locations ... run.py". Vercel only accepts a few
    file names, so the app must be created, as a plain `app = ...` assignment, in one of them."""
    import ast
    vercel_defaults = ["app.py", "index.py", "server.py", "main.py", "wsgi.py", "asgi.py"]
    found = [n for n in vercel_defaults if (ROOT / n).exists()]
    assert found == ["index.py"], found            # exactly one, and never app.py (it would clash with the app/ package)
    tree = ast.parse((ROOT / "index.py").read_text(encoding="utf-8"))
    assigned = [t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)]
    assert "app" in assigned
    import index
    assert type(index.app).__name__ == "Flask"


def test_run_py_and_index_py_are_the_same_app():
    import index
    import run
    assert run.app is index.app                     # one app, created in one place


def test_on_vercel_runs_finish_inside_the_request(monkeypatch):
    """A background thread is frozen when a serverless response is sent, so Vercel must run jobs inline."""
    import index
    monkeypatch.delenv("VERCEL", raising=False)
    assert importlib.reload(index).app.config["RUN_SYNC"] is False      # locally: background thread
    monkeypatch.setenv("VERCEL", "1")
    assert importlib.reload(index).app.config["RUN_SYNC"] is True       # on Vercel: inline
    monkeypatch.delenv("VERCEL")
    importlib.reload(index)


def test_a_whole_run_works_inline_the_way_vercel_will_run_it():
    client = create_app(run_sync=True).test_client()
    view = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert view["status"] == "awaiting_approval" and view["recommendation"]    # finished within the one request
    assert client.post(f"/api/runs/{view['id']}/approve").get_json()["status"] == "approved"


# ---------- storage on a read-only filesystem ----------
def test_the_demo_database_goes_to_the_temp_folder_on_vercel(monkeypatch, tmp_path):
    monkeypatch.delenv("DEMO_DB_PATH", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert local_repo._default_path() == ROOT / "data" / "demo_db.json"
    monkeypatch.setenv("VERCEL", "1")
    on_vercel = local_repo._default_path()
    assert on_vercel.name == "demo_db.json" and ROOT not in on_vercel.parents      # never the (read-only) project folder
    monkeypatch.setenv("DEMO_DB_PATH", str(tmp_path / "custom.json"))
    assert local_repo._default_path() == tmp_path / "custom.json"                  # an explicit path wins


def test_the_database_can_be_created_and_written_from_a_fresh_temp_folder(tmp_path):
    path = tmp_path / "not-yet-created" / "procurement-demo" / "demo_db.json"       # like a cold serverless start
    repo = local_repo.LocalRepository(path)
    run = repo.create_procurement_run({"inventory_item_id": 1, "required_quantity": 5})
    assert path.exists() and repo.get_procurement_run(run["id"])["required_quantity"] == 5


# ---------- the optional password gate ----------
def test_no_gate_unless_a_password_is_set(monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    client = create_app().test_client()
    assert client.get("/").status_code == 200 and client.get("/api/runs").status_code == 200


def test_with_a_password_everything_but_health_is_locked(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "open-sesame")
    client = create_app(run_sync=True).test_client()
    assert client.get("/health").status_code == 200                                  # for uptime checks
    for method, path in [("get", "/"), ("get", "/api/runs"), ("get", "/api/forecast"), ("get", "/static/js/app.js"),
                         ("post", "/api/runs"), ("post", "/api/runs/1/approve"), ("post", "/api/runs/1/send"),
                         ("get", "/debug/data")]:
        res = getattr(client, method)(path)
        assert res.status_code == 401, path
        assert res.headers["WWW-Authenticate"].startswith("Basic realm=")            # makes the browser ask for it
    assert get_json_error(client) is None                                            # and no data leaks in the 401


def get_json_error(client):
    res = client.get("/api/runs")
    return res.get_json(silent=True)


def test_the_right_password_unlocks_it_and_the_wrong_one_does_not(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "open-sesame")
    client = create_app(run_sync=True).test_client()
    assert client.get("/", headers=basic("open-sesame")).status_code == 200
    assert client.get("/static/js/app.js", headers=basic("open-sesame", user="")).status_code == 200   # any username
    assert client.get("/api/runs", headers=basic("open-sesame")).status_code == 200
    for wrong in ("", "open-sesam", "OPEN-SESAME", "open-sesame ", "x" * 500):
        assert client.get("/", headers=basic(wrong)).status_code == 401, repr(wrong)
    assert client.get("/", headers={"Authorization": "Bearer open-sesame"}).status_code == 401   # only Basic counts
    assert client.get("/", headers={"Authorization": "Basic !!!not-base64"}).status_code == 401
    started = client.post("/api/runs", json={"sku": "MOZ-001"}, headers=basic("wrong")).status_code
    assert started == 401 and client.get("/api/runs", headers=basic("open-sesame")).get_json()["runs"] == []   # nothing started


def test_the_password_is_not_in_the_page_or_the_api(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "open-sesame")
    client = create_app(run_sync=True).test_client()
    body = client.get("/", headers=basic("open-sesame")).get_data(as_text=True)
    status = client.get("/api/status", headers=basic("open-sesame")).get_data(as_text=True)
    assert "open-sesame" not in body and "open-sesame" not in status
    assert "DEMO_PASSWORD" not in status                  # the status page lists configuration names, not this one


# ---------- what gets uploaded and installed ----------
def test_runtime_requirements_exclude_test_tooling_but_dev_requirements_include_it():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    for needed in ("flask", "zoowork", "tavily-python", "supabase", "python-dotenv"):
        assert needed in runtime, needed
    assert "pytest" not in runtime and "-r requirements.txt" in dev and "pytest" in dev


def test_the_upload_excludes_secrets_tests_and_local_data():
    ignored = (ROOT / ".vercelignore").read_text(encoding="utf-8").split()
    for entry in (".env", ".venv/", "tests/", "data/", "node_modules/", ".git/"):
        assert entry in ignored, entry


SOURCE_FILES = [p for folder in ("app", "tests", "scripts", "supabase") for p in (ROOT / folder).rglob("*")
                if p.is_file() and "node_modules" not in p.parts and "__pycache__" not in p.parts
                and p.suffix in {".py", ".js", ".html", ".css", ".sql", ".json", ".md", ".txt"}]
SOURCE_FILES += [ROOT / n for n in ("README.md", "ARCHITECTURE.md", ".env.example", "pyproject.toml", "requirements.txt")
                 if (ROOT / n).exists()]


def test_no_secret_shaped_strings_are_in_the_source():
    import re
    pattern = re.compile(r"(zwp_live_[A-Za-z0-9]{16,}|tvly-[A-Za-z0-9]{16,}|eyJ[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{24,}"
                         r"|-----BEGIN [A-Z ]*PRIVATE KEY)")
    hits = [(str(p.relative_to(ROOT)), m.group(0)[:12] + "...") for p in SOURCE_FILES
            for m in pattern.finditer(p.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == []


def test_none_of_the_real_values_in_dot_env_appear_in_the_source():
    """If you have a .env, none of its secret values may be written into any file that could be committed."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        pytest.skip("no .env on this machine")
    import re

    def parse(path):
        out = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_]+)=(.*)", line)
            if m:
                out[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip()
        return out

    placeholders = set(parse(ROOT / ".env.example").values())
    secrets = {k: v for k, v in parse(env_file).items()
               if len(v) >= 12 and v not in placeholders and re.search(r"KEY|PASSWORD|SECRET|TOKEN|AGENT_ID", k)}
    corpus = {p: p.read_text(encoding="utf-8", errors="ignore") for p in SOURCE_FILES}
    leaked = [(k, str(p.relative_to(ROOT))) for k, v in secrets.items() for p, text in corpus.items() if v in text]
    assert leaked == [], leaked        # (names and files only: the values are never printed)
