"""Durable job identities and outcomes; restart never replays physical actions."""
import hashlib
import json
import sqlite3
import time
import uuid
from .errors import HarnessError
from .transport import json_safe


def encoded(value):
    return json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False, allow_nan=False)


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
            plan TEXT NOT NULL, status TEXT NOT NULL, results TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at REAL NOT NULL, data TEXT NOT NULL);
        """)
        self.db.execute("UPDATE jobs SET status='unknown', updated=? WHERE status IN ('queued','running','cancelling')", (time.time(),))
        self.db.commit()

    def create(self, key, plan):
        text = encoded(plan)
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = self.db.execute("SELECT id,digest FROM jobs WHERE request_key=?", (key,)).fetchone()
        if old:
            if old[1] != digest:
                raise HarnessError("idempotency key reused with different payload")
            return old[0], False
        job_id, now = "job-"+uuid.uuid4().hex, time.time()
        self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)", (job_id, key, digest, text, "queued", "{}", now, now))
        self.db.commit()
        return job_id, True

    def update(self, job_id, status=None, action_id=None, result=None):
        row = self.get(job_id, events=False)
        results = row["results"]
        if action_id:
            results[action_id] = result
        self.db.execute("UPDATE jobs SET status=?, results=?, updated=? WHERE id=?", (status or row["status"], encoded(results), time.time(), job_id))
        self.db.commit()

    def get(self, job_id, events=True):
        row = self.db.execute("SELECT id,request_key,plan,status,results,created,updated FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HarnessError("unknown job")
        result = dict(zip(("job_id", "request_key", "plan", "status", "results", "created", "updated"), row))
        result["plan"], result["results"] = json.loads(result["plan"]), json.loads(result["results"])
        if events:
            result["events"] = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM events WHERE job_id=? ORDER BY seq DESC LIMIT 500", (job_id,))][::-1]
        return result

    def event(self, job_id, data):
        self.db.execute("INSERT INTO events(job_id,at,data) VALUES(?,?,?)", (job_id, time.time(), encoded(data)))
        self.db.commit()

    def unknown_vehicles(self):
        return {a["vehicle_id"] for r in self.db.execute("SELECT plan FROM jobs WHERE status='unknown'")
                for a in json.loads(r[0])["actions"]}

    def close(self):
        self.db.close()

