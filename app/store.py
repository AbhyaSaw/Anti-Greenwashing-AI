"""SQLite persistence for projects, audit jobs and evidence reports."""
import json
import sqlite3
import threading
import time
import uuid

from . import config

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY, name TEXT, developer TEXT, geojson TEXT, planting_year INTEGER,
            claimed_trees INTEGER, claimed_tco2e REAL, created_at REAL, register_tx TEXT,
            status TEXT DEFAULT 'PENDING');
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, project_id TEXT, status TEXT, progress INTEGER, log TEXT,
            report TEXT, chain TEXT, error TEXT, created_at REAL, params TEXT);
        """)


def _project(row):
    if row is None:
        return None
    d = dict(row)
    d["geojson"] = json.loads(d["geojson"])
    return d


def create_project(data):
    pid = uuid.uuid4().hex[:12]
    with _lock, _conn() as c:
        c.execute("INSERT INTO projects(id,name,developer,geojson,planting_year,claimed_trees,claimed_tco2e,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (pid, data["name"], data["developer"], json.dumps(data["geojson"]), int(data["planting_year"]),
                   int(data["claimed_trees"]), float(data["claimed_tco2e"]), time.time()))
    return get_project(pid)


def update_project(pid, **kw):
    with _lock, _conn() as c:
        for k, v in kw.items():
            c.execute(f"UPDATE projects SET {k}=? WHERE id=?", (v, pid))


def delete_project(pid):
    with _lock, _conn() as c:
        c.execute("DELETE FROM jobs WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def get_project(pid):
    with _conn() as c:
        return _project(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())


def list_projects():
    with _conn() as c:
        return [_project(r) for r in c.execute("SELECT * FROM projects ORDER BY created_at DESC")]


def create_job(project_id, params):
    jid = uuid.uuid4().hex[:12]
    with _lock, _conn() as c:
        c.execute("INSERT INTO jobs(id,project_id,status,progress,log,created_at,params) VALUES(?,?,?,?,?,?,?)",
                  (jid, project_id, "running", 0, "", time.time(), json.dumps(params)))
    return jid


def update_job(jid, **kw):
    with _lock, _conn() as c:
        for k, v in kw.items():
            if k in ("report", "chain") and v is not None:
                v = json.dumps(v)
            c.execute(f"UPDATE jobs SET {k}=? WHERE id=?", (v, jid))


def append_log(jid, line):
    with _lock, _conn() as c:
        c.execute("UPDATE jobs SET log = log || ? WHERE id=?", (line + "\n", jid))


def _job(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("report", "chain", "params"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def get_job(jid):
    with _conn() as c:
        return _job(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def latest_job(project_id, finished_only=False):
    q = "SELECT * FROM jobs WHERE project_id=?" + (" AND status='done'" if finished_only else "") + \
        " ORDER BY created_at DESC LIMIT 1"
    with _conn() as c:
        return _job(c.execute(q, (project_id,)).fetchone())


def finished_jobs_in_order():
    with _conn() as c:
        return [_job(r) for r in c.execute(
            "SELECT * FROM jobs WHERE status='done' AND chain IS NOT NULL ORDER BY created_at")]
