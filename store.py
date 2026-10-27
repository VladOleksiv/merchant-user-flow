"""SQLite storage for orders, refunds and the per-order event log."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    reference TEXT PRIMARY KEY,
    integration TEXT NOT NULL,          -- h2h | hpp
    mode TEXT NOT NULL DEFAULT 'initial',
    product TEXT,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    customer_json TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    error_code INTEGER,
    error_reason TEXT,
    identifier TEXT,
    payment_identifier TEXT,
    challenge_json TEXT,
    card_json TEXT,
    card_token TEXT,
    checkout_url TEXT,
    parent_reference TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    identifier TEXT PRIMARY KEY,
    order_reference TEXT NOT NULL,
    reference TEXT,
    amount TEXT,
    currency TEXT,
    status TEXT,
    error_code INTEGER,
    error_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_reference TEXT,
    kind TEXT NOT NULL,                 -- request | response | callback | info
    title TEXT,
    data_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events(order_reference, id);
"""

ORDER_FIELDS = {
    "integration", "mode", "product", "amount", "currency", "customer_json", "status", "error_code",
    "error_reason", "identifier", "payment_identifier", "challenge_json", "card_json", "card_token",
    "checkout_url", "parent_reference",
}


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def create_order(self, reference, **fields):
        now = time.time()
        fields = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
        cols = ["reference", "created_at", "updated_at"] + list(fields)
        vals = [reference, now, now] + list(fields.values())
        self._exec(
            "INSERT INTO orders (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), vals
        )

    def update_order(self, reference, **fields):
        fields = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
        if not fields:
            return
        sets = ",".join("%s=?" % k for k in fields) + ",updated_at=?"
        self._exec("UPDATE orders SET %s WHERE reference=?" % sets, list(fields.values()) + [time.time(), reference])

    def get_order(self, reference):
        rows = self._all("SELECT * FROM orders WHERE reference=?", (reference,))
        return rows[0] if rows else None

    def find_order_by_identifier(self, identifier):
        rows = self._all(
            "SELECT * FROM orders WHERE identifier=? OR payment_identifier=?", (identifier, identifier)
        )
        return rows[0] if rows else None

    def list_orders(self, limit=200):
        return self._all("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))

    def upsert_refund(self, identifier, order_reference, **fields):
        now = time.time()
        allowed = {"reference", "amount", "currency", "status", "error_code", "error_reason"}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if self._all("SELECT 1 FROM refunds WHERE identifier=?", (identifier,)):
            if fields:
                sets = ",".join("%s=?" % k for k in fields) + ",updated_at=?"
                self._exec("UPDATE refunds SET %s WHERE identifier=?" % sets, list(fields.values()) + [now, identifier])
        else:
            cols = ["identifier", "order_reference", "created_at", "updated_at"] + list(fields)
            vals = [identifier, order_reference, now, now] + list(fields.values())
            self._exec("INSERT INTO refunds (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)

    def list_refunds(self, order_reference):
        return self._all("SELECT * FROM refunds WHERE order_reference=? ORDER BY created_at", (order_reference,))

    def get_refund(self, identifier):
        rows = self._all("SELECT * FROM refunds WHERE identifier=?", (identifier,))
        return rows[0] if rows else None

    def add_event(self, order_reference, kind, title, data=None):
        self._exec(
            "INSERT INTO events (order_reference, kind, title, data_json, created_at) VALUES (?,?,?,?,?)",
            (order_reference, kind, title, json.dumps(data, ensure_ascii=False) if data is not None else None, time.time()),
        )

    def list_events(self, order_reference=None, limit=500):
        if order_reference:
            return self._all("SELECT * FROM events WHERE order_reference=? ORDER BY id", (order_reference,))
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
