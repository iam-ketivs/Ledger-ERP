import sqlite3
import os
from flask import g

import paths

# The database lives beside the .exe (or beside app.py when run from source) so it
# survives restarts; schema.sql ships inside the bundle and is only ever read.
DB_PATH = paths.DB_PATH
SCHEMA_PATH = paths.SCHEMA_PATH


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    _migrate_columns(conn)
    conn.close()


# Columns added to existing tables after the initial release. CREATE TABLE IF NOT EXISTS
# won't add a column to a table that already exists, so new columns need to be added here
# by name - this runs every startup and is a no-op once a database already has them.
_NEW_COLUMNS = [
    ("orders", "refunded_amount", "REAL NOT NULL DEFAULT 0"),
    ("orders", "cancelled_date", "TEXT"),
    ("customers", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("items", "category", "TEXT"),
    ("customers", "customer_type", "TEXT NOT NULL DEFAULT 'regular'"),
    ("purchases", "supplier_id", "INTEGER REFERENCES suppliers(id)"),
    ("purchases", "paid_amount", "REAL NOT NULL DEFAULT 0"),
    ("purchases", "payment_status", "TEXT NOT NULL DEFAULT 'paid'"),
    ("sales", "discount_amount", "REAL NOT NULL DEFAULT 0"),
    ("purchases", "batch_ref", "TEXT"),
    ("items", "photo", "TEXT"),
    ("employee_advances", "expense_id", "INTEGER REFERENCES expenses(id)"),
    # Whether an order was a redo of earlier work - a spoiled print run, a wrong colour
    # sent back. Set by hand, because nothing else in the data can honestly infer it.
    ("orders", "is_reprint", "INTEGER NOT NULL DEFAULT 0"),
    # How much material a production run wasted, in the raw material's own unit.
    # Nullable on purpose: NULL means "not logged for this run", 0 means "logged as
    # zero waste" - the two must stay distinguishable or a waste percentage computed
    # from mostly-unlogged runs would look falsely clean.
    ("production_runs", "waste_qty", "REAL"),
]


_NEW_TABLES = """
-- One entry per shift or per day for a machine or line. Machine utilisation and OEE
-- cannot be worked out from sales or purchases - they need this logged by hand, so the
-- table exists but stays empty, and therefore the metric stays honestly "not ready",
-- until someone actually starts filling it in.
CREATE TABLE IF NOT EXISTS machine_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date TEXT NOT NULL DEFAULT (date('now')),
    machine TEXT,
    hours_run REAL NOT NULL DEFAULT 0,
    hours_available REAL NOT NULL DEFAULT 0,
    good_units REAL NOT NULL DEFAULT 0,
    reject_units REAL NOT NULL DEFAULT 0,
    notes TEXT,
    by_user TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_machine_logs_date ON machine_logs(log_date);

CREATE TABLE IF NOT EXISTS material_rolls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    purchase_id INTEGER REFERENCES purchases(id),
    roll_no TEXT,
    weight_kg REAL NOT NULL,
    remaining_kg REAL NOT NULL,
    cost_per_kg REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_stock',
    received_date TEXT NOT NULL DEFAULT (date('now')),
    used_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rolls_item ON material_rolls(item_id, status);
CREATE INDEX IF NOT EXISTS idx_rolls_purchase ON material_rolls(purchase_id);

CREATE TABLE IF NOT EXISTS order_outsourced (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id),
    supplier_name TEXT,
    description TEXT,
    amount REAL NOT NULL,
    paid_amount REAL NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'credit',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_outsourced_order ON order_outsourced(order_id);
CREATE INDEX IF NOT EXISTS idx_outsourced_supplier ON order_outsourced(supplier_id);

CREATE TABLE IF NOT EXISTS order_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    caption TEXT,
    kind TEXT NOT NULL DEFAULT 'reference',
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_order_images ON order_images(order_id);

-- Who did what, and when. Recorded for anything that moves money or stock, so when a
-- figure looks wrong you can see which login entered it rather than having to guess.
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    role TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id INTEGER,
    detail TEXT,
    at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_log_at ON activity_log(at);
CREATE INDEX IF NOT EXISTS idx_log_user ON activity_log(username);

-- Covering indexes: the index carries the amounts as well as the date, so a total for a
-- period never has to touch the table itself. On a large database this is the difference
-- between about 2.5 seconds and about 0.1.
CREATE INDEX IF NOT EXISTS idx_sales_cover ON sales(sale_date, total_amount, total_cost, total_profit, paid_amount);
CREATE INDEX IF NOT EXISTS idx_payments_cover ON payments(payment_date, amount);
CREATE INDEX IF NOT EXISTS idx_purchases_cover ON purchases(purchase_date, total_cost, paid_amount);
CREATE INDEX IF NOT EXISTS idx_expenses_cover ON expenses(expense_date, amount);
CREATE INDEX IF NOT EXISTS idx_suppay_cover ON supplier_payments(payment_date, amount);
CREATE INDEX IF NOT EXISTS idx_orders_cover ON orders(status, completed_date, agreed_total, total_cost, profit, balance_received);
"""

_ITEM_PHOTO = [("items", "photo", "TEXT")]


def _migrate_columns(conn):
    conn.executescript(_NEW_TABLES)
    # A picture of the product, for the visual catalogue.
    for table, col, decl in _ITEM_PHOTO:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    changed = False
    for table, column, coltype in _NEW_COLUMNS:
        existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            changed = True
    if changed:
        conn.commit()

    # One-time backfill: purchases recorded before the payable-tracking columns existed
    # had no concept of "unpaid" - treat them as fully paid on the day they were entered,
    # so no old purchase suddenly shows up as money you owe.
    conn.execute(
        "UPDATE purchases SET paid_amount = total_cost, payment_status = 'paid' "
        "WHERE payment_status = 'paid' AND paid_amount = 0 AND total_cost > 0"
    )
    conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
