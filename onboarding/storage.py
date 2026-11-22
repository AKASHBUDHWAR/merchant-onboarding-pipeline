import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config/onboarding.yaml"


def json_text(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def uid():
    return str(uuid4())


def one(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None


def many(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params)]


def insert(db, table, **fields):
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    return db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def update(db, table, where, params, **fields):
    assignments = ", ".join(f"{column}=?" for column in fields)
    return db.execute(f"UPDATE {table} SET {assignments} WHERE {where}", (*fields.values(), *params))


def unjson(row, *fields):
    for field in fields:
        if row.get(field) is not None:
            row[field] = json.loads(row[field])
    return row


class Store:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.directory / "onboarding.sqlite3"
        self.clock = clock
        self.defaults = yaml.safe_load(CONFIG_PATH.read_text())["onboarding"]
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript((Path(__file__).parent / "schema.sql").read_text())
        finally:
            conn.close()
        key_path = self.directory / ".demo-key"
        if not key_path.exists():
            draft = key_path.with_name(f".key-{uid()}")
            fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(Fernet.generate_key().decode())
            try:
                os.link(draft, key_path)
            except FileExistsError:
                pass
            finally:
                draft.unlink()
        key = os.environ.get("ONBOARDING_DATA_KEY") or key_path.read_text().strip()
        self.cipher = Fernet(key.encode())
        self.secret = hashlib.sha256(("match:" + key).encode()).digest()
        self.callback_key = (os.environ.get("ONBOARDING_CALLBACK_SECRET") or ("callback:" + key)).encode()

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=True):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(json_text(value).encode()).decode()

    def open(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def fingerprint(self, value):
        return hmac.new(self.secret, json_text(value).encode(), hashlib.sha256).hexdigest()

    def config(self, db):
        row = one(db, "SELECT value FROM app_settings WHERE key='policy'")
        return json.loads(row["value"]) if row else self.defaults

    def sign(self, provider, timestamp, event_id, body):
        prefix = f"{provider}.{timestamp}.{event_id}.".encode()
        return "sha256=" + hmac.new(self.callback_key, prefix + body, hashlib.sha256).hexdigest()


AUDIT_FIELDS = ("application_id", "actor_type", "actor_id", "action", "target", "before", "after", "occurred_at")


def audit(db, now, app_id, action, actor=("SYSTEM", "workflow"), target=None, before=None, after=None):
    previous = one(db, "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
    data = dict(zip(AUDIT_FIELDS, (app_id, *actor, action, json_text(target), json_text(before), json_text(after), now)))
    prev_hash = previous["hash"] if previous else None
    digest = hashlib.sha256(json_text([prev_hash, data]).encode()).hexdigest()
    insert(db, "audit_log", **data, prev_hash=prev_hash, hash=digest)


def verify_audit(db):
    previous = None
    count = 0
    for row in db.execute("SELECT * FROM audit_log ORDER BY id"):
        data = {key: row[key] for key in AUDIT_FIELDS}
        expected = hashlib.sha256(json_text([previous, data]).encode()).hexdigest()
        if row["prev_hash"] != previous or row["hash"] != expected:
            return {"valid": False, "first_invalid_id": row["id"], "entries": count}
        previous = row["hash"]
        count += 1
    return {"valid": True, "entries": count}
