"""
SQLite persistence (history, saved projects, settings) plus the automatic
clean-up of generated files.

Clean-up rules
  * Outputs of finished jobs are deleted `retention_hours` after they finished
    (default 24h, 0 = keep until the storage cap is hit). Pinned jobs are exempt.
  * If unpinned outputs exceed `max_storage_gb`, the oldest are deleted first.
  * Output folders with no matching history row, and old uploads, are deleted.
  * History rows and tiny thumbnails survive, flagged "expired", so the
    settings of an old conversion can still be re-used.
"""

import json
import os
import shutil
import sqlite3
import threading
import time

DEFAULT_SETTINGS = {"retention_hours": 24, "max_storage_gb": 5}
UPLOAD_TTL_SECONDS = 12 * 3600


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Store:
    def __init__(self, db_path, generated_dir, upload_dir, thumb_dir):
        self.db_path, self.generated_dir = db_path, generated_dir
        self.upload_dir, self.thumb_dir = upload_dir, thumb_dir
        self._lock = threading.Lock()
        for d in (generated_dir, upload_dir, thumb_dir, os.path.dirname(db_path)):
            os.makedirs(d, exist_ok=True)
        with self._db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT, label TEXT, params TEXT,
                    status TEXT, message TEXT, created REAL, finished REAL,
                    pinned INTEGER DEFAULT 0, expired INTEGER DEFAULT 0,
                    files TEXT DEFAULT '[]', size INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT,
                    params TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """)
            # a job still marked running at startup was killed with the old process
            c.execute("UPDATE jobs SET status='error', message='interrupted (app restarted)', finished=? "
                      "WHERE status IN ('queued','starting','running')", (time.time(),))

    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return _Conn(conn, self._lock)

    # ---- settings
    def get_settings(self):
        with self._db() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return out

    def set_settings(self, retention_hours=None, max_storage_gb=None):
        vals = {}
        if retention_hours is not None:
            vals["retention_hours"] = min(max(float(retention_hours), 0), 24 * 365)
        if max_storage_gb is not None:
            vals["max_storage_gb"] = min(max(float(max_storage_gb), 0.1), 1000)
        with self._db() as c:
            for k, v in vals.items():
                c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
        return self.get_settings()

    # ---- jobs
    def add_job(self, job_id, kind, label, params):
        with self._db() as c:
            c.execute("INSERT INTO jobs(id, kind, label, params, status, message, created) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (job_id, kind, label, json.dumps(params), "queued", "queued...", time.time()))

    def finish_job(self, job_id, status, message, files=None):
        files = files or []
        size = sum(f.get("size", 0) for f in files)
        with self._db() as c:
            c.execute("UPDATE jobs SET status=?, message=?, finished=?, files=?, size=? WHERE id=?",
                      (status, message, time.time(), json.dumps(files), size, job_id))

    @staticmethod
    def _job(row):
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        d["files"] = json.loads(d["files"] or "[]")
        d["pinned"], d["expired"] = bool(d["pinned"]), bool(d["expired"])
        return d

    def get_job(self, job_id):
        with self._db() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, limit=200):
        with self._db() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._job(r) for r in rows]

    def set_pinned(self, job_id, pinned):
        with self._db() as c:
            c.execute("UPDATE jobs SET pinned=? WHERE id=?", (1 if pinned else 0, job_id))

    def delete_job(self, job_id):
        self._remove_outputs(job_id)
        try:
            os.remove(os.path.join(self.thumb_dir, f"{job_id}.jpg"))
        except OSError:
            pass
        with self._db() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def _remove_outputs(self, job_id):
        shutil.rmtree(os.path.join(self.generated_dir, job_id), ignore_errors=True)

    def _expire(self, job_id):
        self._remove_outputs(job_id)
        with self._db() as c:
            c.execute("UPDATE jobs SET expired=1, files='[]', size=0 WHERE id=?", (job_id,))

    # ---- projects (saved settings)
    def add_project(self, name, kind, params):
        with self._db() as c:
            cur = c.execute("INSERT INTO projects(name, kind, params, created) VALUES(?,?,?,?)",
                            (name[:80], kind, json.dumps(params), time.time()))
            return cur.lastrowid

    def list_projects(self):
        with self._db() as c:
            rows = c.execute("SELECT * FROM projects ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"] or "{}")
            out.append(d)
        return out

    def delete_project(self, pid):
        with self._db() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))

    # ---- storage + clean-up
    def storage_used(self):
        return dir_size(self.generated_dir)

    def cleanup(self, now=None):
        """Apply all retention rules. Safe to call at any time; returns a summary."""
        now = now or time.time()
        st = self.get_settings()
        removed = {"expired_jobs": 0, "orphans": 0, "uploads": 0}

        with self._db() as c:
            rows = c.execute("SELECT id, finished, pinned, size, status FROM jobs "
                             "WHERE expired=0 AND status IN ('done','error','cancelled')").fetchall()
            known = {r["id"] for r in c.execute("SELECT id FROM jobs").fetchall()}

        # 1. time-based retention
        hours = st["retention_hours"]
        keep = []
        for r in rows:
            if r["pinned"]:
                continue
            if hours > 0 and r["finished"] and now - r["finished"] > hours * 3600:
                self._expire(r["id"])
                removed["expired_jobs"] += 1
            else:
                keep.append(r)

        # 2. storage cap: oldest unpinned first
        cap = st["max_storage_gb"] * 1024 ** 3
        sized = []
        for r in keep:
            sized.append((r["finished"] or 0, r["id"], dir_size(os.path.join(self.generated_dir, r["id"]))))
        sized.sort()
        total = sum(s for _, _, s in sized)
        for _t, job_id, size in sized:
            if total <= cap:
                break
            self._expire(job_id)
            total -= size
            removed["expired_jobs"] += 1

        # 3. output folders nobody knows about
        for name in os.listdir(self.generated_dir):
            path = os.path.join(self.generated_dir, name)
            if name == ".gitkeep" or name in known:
                continue
            try:
                if now - os.path.getmtime(path) > 600:
                    shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
                    removed["orphans"] += 1
            except OSError:
                pass

        # 4. stale uploads
        for name in os.listdir(self.upload_dir):
            path = os.path.join(self.upload_dir, name)
            try:
                if name != ".gitkeep" and now - os.path.getmtime(path) > UPLOAD_TTL_SECONDS:
                    os.remove(path)
                    removed["uploads"] += 1
            except OSError:
                pass
        return removed


class _Conn:
    """`with store._db() as c:` -> serialised, committed, always closed."""
    def __init__(self, conn, lock):
        self.conn, self.lock = conn, lock

    def __enter__(self):
        self.lock.acquire()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            self.lock.release()
