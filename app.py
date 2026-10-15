import os
import sys
import json
import secrets
from datetime import datetime, date, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, g, Response
from werkzeug.security import generate_password_hash, check_password_hash

import paths
import db as dbm

# Shown in Settings. Because templates and code are compiled into Ledger.exe, copying
# files is not enough - build_exe.bat must be run. This makes it obvious which build is
# actually running rather than which files are sitting in the folder.
LEDGER_VERSION = "5.11"
import exports
import metrics as M
import intel

APP_DIR = paths.DATA_DIR
SECRET_FILE = paths.SECRET_PATH


def get_or_create_secret():
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(key)
    return key


# Templates and static files come from the bundle when frozen into an .exe.
app = Flask(__name__, template_folder=paths.TEMPLATE_DIR, static_folder=paths.STATIC_DIR)
app.secret_key = get_or_create_secret()
dbm.init_app(app)


# ---------- helpers ----------

def inr0(value):
    """Rupees with no paise, for headline figures.

    On a big number the paise are noise - "Rs 1,44,682" reads instantly where
    "Rs 1,44,681.67" makes you stop and parse it, and is long enough to wrap.
    Detail views keep the full figure.
    """
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    neg = n < 0
    n = abs(round(n))
    s = str(int(n))
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        s = ",".join(parts) + "," + last3
    return ("\u2212\u20b9" if neg else "\u20b9") + s


def inr(value):
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    neg = n < 0
    n = abs(n)
    s = f"{n:.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        last3 = whole[-3:]
        rest = whole[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        whole = ",".join(parts) + "," + last3
    return ("-" if neg else "") + "\u20b9" + whole + "." + dec


app.jinja_env.filters["inr"] = inr
app.jinja_env.filters["inr0"] = inr0


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            flash("Only an admin can do that.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


# ---------- backup ----------

BACKUP_DIR_NAME = "backups"


def backup_dir():
    d = os.path.join(paths.INSTANCE_DIR, BACKUP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def make_backup(tag="manual", keep=20):
    """Copy the live database to instance/backups with a timestamp.

    SQLite's own backup API is used rather than a file copy, so a backup taken while
    the app is running is always a consistent snapshot, never a half-written file.
    """
    import sqlite3 as _sq
    os.makedirs(backup_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir(), f"erp-{stamp}-{tag}.db")
    src = _sq.connect(paths.DB_PATH)
    dst = _sq.connect(dest)
    with dst:
        src.backup(dst)
    dst.close(); src.close()
    prune_backups(keep)
    return dest


def prune_backups(keep=20):
    """Keep the newest N so the folder can't grow forever."""
    try:
        files = sorted(
            (f for f in os.listdir(backup_dir()) if f.endswith(".db")),
            key=lambda f: os.path.getmtime(os.path.join(backup_dir(), f)),
            reverse=True)
        for f in files[keep:]:
            os.remove(os.path.join(backup_dir(), f))
    except OSError:
        pass


def list_backups():
    out = []
    try:
        for f in sorted(os.listdir(backup_dir()), reverse=True):
            if not f.endswith(".db"):
                continue
            full = os.path.join(backup_dir(), f)
            out.append({
                "name": f,
                "size_kb": round(os.path.getsize(full) / 1024, 1),
                "when": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%d %b %Y, %I:%M %p"),
            })
    except OSError:
        pass
    return out


def local_ip():
    """The address this PC has on the shop wifi, so a phone can reach Ledger."""
    import socket as _s
    try:
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))     # no traffic is sent; this just picks the route
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return "127.0.0.1"


def db_info():
    """Which database file is actually open, and how big it is. Shown on the setup and
    settings screens so there is never any doubt which file the app is reading - the
    usual cause of 'it asks me to set up again' is a second, empty database."""
    exists = os.path.exists(paths.DB_PATH)
    size = os.path.getsize(paths.DB_PATH) if exists else 0
    return {
        "path": paths.DB_PATH,
        "folder": paths.INSTANCE_DIR,
        "exists": exists,
        "size_kb": round(size / 1024, 1),
        "frozen": getattr(sys, "frozen", False),
    }


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_error(e):
    """Show what actually broke, and write it to error-log.txt.

    Deliberate HTTP responses - a 404 for a photo that does not exist, a 403 - are
    passed through untouched. Only genuine faults get the error page.

    Without this the desktop app shows a blank 'Internal Server Error' and the
    traceback vanishes, because there is no console window to print it to.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e

    import traceback
    tb = traceback.format_exc()
    try:
        paths.ensure_dirs()
        with open(os.path.join(paths.DATA_DIR, "error-log.txt"), "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 70 + "\n")
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + request.path + "\n")
            f.write(tb + "\n")
    except Exception:
        pass

    hint = ""
    msg = str(e)
    if "Could not build url for endpoint" in msg:
        missing = msg.split("'")[1] if "'" in msg else "?"
        hint = (f"A page is linking to <code>{missing}</code>, which this version of "
                f"app.py does not have. This almost always means the template files were "
                f"updated but <code>app.py</code> (or another .py file) was not copied "
                f"across. Copy <strong>all</strong> the files from the update and rebuild.")
    elif "TemplateNotFound" in type(e).__name__ or "TemplateNotFound" in msg:
        hint = ("A template file is missing. Copy the whole <code>templates</code> folder "
                "from the update, then rebuild.")
    elif "no such table" in msg.lower() or "no such column" in msg.lower():
        hint = ("The database is missing something the code expects. Make sure "
                "<code>db.py</code> and <code>schema.sql</code> were copied too, then restart.")
    elif "No module named" in msg:
        hint = ("A Python file from the update is missing. Copy every file from the update "
                "zip, then rebuild.")

    try:
        return render_template("error.html", error=msg, hint=hint, path=request.path,
                               log=os.path.join(paths.DATA_DIR, "error-log.txt")), 500
    except Exception:
        # Even the error page needs a fallback - if templates are missing it would
        # otherwise fail while trying to report that templates are missing.
        return (f"<h2>Something went wrong</h2><p>{msg}</p>"
                f"<p>{hint}</p><p>Details saved to error-log.txt</p>"), 500


# Endpoints that change or remove an existing record. Checked centrally before every
# request, so a new edit route can never be added and silently miss the guard.
CHANGE_ENDPOINTS = {
    "item_edit", "item_delete", "purchase_edit", "purchase_delete",
    "expense_edit", "expense_delete", "order_image_delete", "roll_delete",
    "supplier_edit", "supplier_payment_edit", "supplier_payment_delete", "supplier_delete",
    "customer_edit", "customer_delete", "sale_edit", "sale_delete",
    "order_edit", "order_cancel", "employee_edit", "employee_advance_delete",
    "employee_delete", "item_photo", "item_photo_delete", "supplier_adjust_balance",
}


@app.before_request
def block_changes_if_locked():
    """Second line of defence for edit-blocking.

    The per-route decorator already covers these, but a route added later could easily
    be forgotten. This catches every one of them in one place.
    """
    if request.endpoint in CHANGE_ENDPOINTS and request.method == "POST":
        if not can_change():
            log_action("blocked from editing", request.endpoint)
            flash("Only the owner can change or remove an entry. Ask them to correct it, "
                  "or enter a fresh one.", "error")
            return redirect(request.referrer or url_for("dashboard"))


@app.before_request
def ensure_setup():
    if request.endpoint in ("setup", "static"):
        return
    conn = dbm.get_db()
    count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if count == 0:
        return redirect(url_for("setup"))


# Roles, from least to most access. Everyone below admin is barred from profit, cost
# prices, cash figures and reports - the split is about WHICH work they can enter.
ROLES = {
    "admin":    "Owner - everything",
    "manager":  "Manager - purchases, suppliers, stock and rolls, plus all counter work",
    "purchase": "Purchase clerk - purchases, suppliers, stock and rolls only",
    "staff":    "Counter staff - sales, orders and customers only",
}
SELL_ROLES = ("admin", "manager", "staff")
BUY_ROLES = ("admin", "manager", "purchase")


def is_admin():
    return session.get("role") == "admin"


def can_sell():
    return session.get("role") in SELL_ROLES


def can_buy():
    return session.get("role") in BUY_ROLES


def log_action(action, entity=None, entity_id=None, detail=""):
    """Record who did something. Never allowed to break the thing it is recording, so
    any failure here is swallowed - a lost log line must not lose a sale."""
    try:
        conn = dbm.get_db()
        conn.execute(
            "INSERT INTO activity_log (username, role, action, entity, entity_id, detail) "
            "VALUES (?,?,?,?,?,?)",
            (session.get("username", "?"), session.get("role", "?"),
             action, entity, entity_id, detail))
        conn.commit()
    except Exception:
        pass


def can_change():
    """Whether this login may edit or delete existing records.

    The owner always can. For everyone else it is a single switch in Settings, rather
    than a permission per person - with two or three staff, one honest setting beats a
    grid of checkboxes nobody maintains. When it is off, staff can still CREATE freely:
    they just cannot go back and quietly change or remove something afterwards.
    """
    if is_admin():
        return True
    try:
        conn = dbm.get_db()
        return get_setting(conn, "staff_can_edit", "yes") == "yes"
    except Exception:
        return False


def change_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not can_change():
            log_action("blocked from editing", request.endpoint)
            flash("Only the owner can change or remove an entry. Ask them to correct it, "
                  "or enter a fresh one.", "error")
            return redirect(request.referrer or url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def sell_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not can_sell():
            flash("Your login doesn't cover sales and orders.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def buy_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not can_buy():
            flash("Your login doesn't cover purchases and stock.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"current_user": session.get("username"),
            "current_role": session.get("role"),
            "is_admin": is_admin(),
            "can_sell": can_sell(),
            "can_buy": can_buy(),
            "can_change": can_change(),
            "ROLES": ROLES,
            "ledger_version": LEDGER_VERSION,
            "order_urgency": order_urgency,
            "ORDER_FLOW": ORDER_FLOW,
            "ORDER_LABELS": ORDER_LABELS,
            "OPEN_STATUSES": OPEN_STATUSES,
            "next_stage": next_stage}


def parse_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# A 'service' item (printing charge, delivery, design fee...) is sold like any other line
# but has no physical stock, so stock movements are skipped entirely for it.
STOCK_TRACKED_TYPES = ("raw_material", "finished_good")


def tracks_stock(conn, item_id):
    row = conn.execute("SELECT item_type FROM items WHERE id=?", (item_id,)).fetchone()
    return bool(row) and row["item_type"] in STOCK_TRACKED_TYPES


def update_stock_add(conn, item_id, qty, unit_cost):
    """Receive stock in (purchase or production output) - recompute weighted average cost."""
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item or item["item_type"] not in STOCK_TRACKED_TYPES:
        return
    old_stock = item["stock_qty"]
    old_cost = item["avg_cost"]
    new_stock = old_stock + qty
    # Weighted average only means anything when there was real stock to average against.
    # If the book stock is zero or negative (an item sold before its production or purchase
    # was entered), there is no existing value to blend, and blending anyway produces a
    # badly wrong cost - e.g. -200 units at 5 plus 400 at 6 would read 7.00 instead of 6.00,
    # inflating cost of goods on every later sale. In that case the incoming price IS the cost.
    if old_stock > 0 and new_stock > 0:
        new_avg = ((old_stock * old_cost) + (qty * unit_cost)) / new_stock
    else:
        new_avg = unit_cost
    conn.execute("UPDATE items SET stock_qty=?, avg_cost=? WHERE id=?", (new_stock, new_avg, item_id))


def update_stock_remove(conn, item_id, qty):
    """Issue stock out (sale or production consumption). Average cost is unchanged."""
    if not tracks_stock(conn, item_id):
        return
    conn.execute("UPDATE items SET stock_qty = stock_qty - ? WHERE id=?", (qty, item_id))


def update_stock_return(conn, item_id, qty):
    """Put stock back (undoing a sale). Average cost is unchanged."""
    if not tracks_stock(conn, item_id):
        return
    conn.execute("UPDATE items SET stock_qty = stock_qty + ? WHERE id=?", (qty, item_id))


def _in_range(d, since, until):
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def _as_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def opening_cash(conn):
    """What was in the drawer on the day you started using Ledger. Without it, cash in
    hand is only the change since day one, which is why it could read negative."""
    try:
        amt = float(get_setting(conn, "opening_cash", "0") or 0)
    except ValueError:
        amt = 0.0
    return amt, (get_setting(conn, "opening_cash_date", "") or "")


# Above this many rows, summing in Python stops being sensible and the totals are done
# in SQL instead. Below it the old path is kept - it is simpler, and at a few thousand
# rows the difference is not measurable.
BIG_TABLE = 50_000


def _row_count(conn, table):
    try:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    except Exception:
        return 0


def is_big(conn):
    """Whether this database is large enough to need SQL-side aggregation."""
    if not hasattr(g, "_ledger_big"):
        g._ledger_big = _row_count(conn, "sales") > BIG_TABLE
    return g._ledger_big


def load_revenue_sources(conn):
    """Everything that counts as revenue: regular sales plus completed custom orders.

    Loaded once and reused so the dashboard and the reports page can never disagree.

    On a large database the rows are NOT loaded - period_stats and period_cash switch to
    SQL sums instead. Pulling two million rows into Python to add them up takes tens of
    seconds; the same sum in SQLite takes about a tenth of a second.
    """
    if is_big(conn):
        return None, None, None
    sales = conn.execute("SELECT * FROM sales").fetchall()
    all_orders = conn.execute("SELECT * FROM orders").fetchall()
    orders_completed = [o for o in (all_orders or []) if o["status"] == "completed" and o["completed_date"]]
    return sales, all_orders, orders_completed


def _sql_period_stats(conn, since=None, until=None):
    """Revenue, cost and profit straight from SQL - used on large databases."""
    lo = since.isoformat() if since else "0000-01-01"
    hi = until.isoformat() if until else "9999-12-31"
    r = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) rev, COALESCE(SUM(total_cost),0) cost, "
        "COALESCE(SUM(total_profit),0) prof, COUNT(*) n FROM sales "
        "WHERE sale_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    o = conn.execute(
        "SELECT COALESCE(SUM(agreed_total),0) rev, COALESCE(SUM(total_cost),0) cost, "
        "COALESCE(SUM(profit),0) prof, COUNT(*) n FROM orders "
        "WHERE status='completed' AND completed_date BETWEEN ? AND ?", (lo, hi)).fetchone()
    return {
        "revenue": r["rev"] + o["rev"],
        "cost": r["cost"] + o["cost"],
        "profit": r["prof"] + o["prof"],
        "count": r["n"] + o["n"],
    }


def period_stats(sales, orders_completed, since=None, until=None):
    """Revenue / cost / profit for a date window. Sales count on their sale date,
    custom orders count on the date they were completed and handed over.

    When sales is None the database is large enough that the rows were never loaded -
    the same totals are then taken straight from SQL.
    """
    if sales is None:
        return _sql_period_stats(dbm.get_db(), since, until)
    rev = cost = profit = 0.0
    count = 0
    for s in sales:
        if _in_range(_as_date(s["sale_date"]), since, until):
            rev += s["total_amount"]
            cost += s["total_cost"]
            profit += s["total_profit"]
            count += 1
    for o in orders_completed:
        if _in_range(_as_date(o["completed_date"]), since, until):
            rev += o["agreed_total"]
            cost += o["total_cost"]
            profit += o["profit"]
            count += 1
    return {"revenue": rev, "cost": cost, "profit": profit, "count": count}


CASH_IN_KINDS = ("opening", "sale", "customer_payment", "order_advance", "order_balance")
CASH_OUT_KINDS = ("order_refund", "expense", "purchase", "supplier_payment")


def cash_movements(conn, since=None, until=None):
    """Every individual movement of money in or out of the business, chronologically.

    This is the single source of truth for cash: the Cash Book page lists these rows, and
    period_cash() below just adds them up. Because both come from here, the running total
    on the Cash Book can never disagree with the "Cash in hand" figure on the dashboard.
    """
    lo = since.isoformat() if since else "0000-01-01"
    hi = until.isoformat() if until else "9999-12-31"
    rows = []

    # Opening cash counts as the very first money in, on the date trading started.
    oc_amt, oc_date = opening_cash(conn)
    if oc_amt and abs(oc_amt) > 0.005:
        d = oc_date or "0000-01-01"
        if lo <= d <= hi:
            rows.append({
                "date": d, "kind": "opening", "direction": "in", "amount": oc_amt,
                "label": "Opening cash balance",
                "detail": "what was in the drawer when you started using Ledger",
                "endpoint": "business_settings", "args": {},
            })

    for r in conn.execute(
        "SELECT s.id, s.sale_date d, s.paid_amount amt, s.total_amount, c.name cname "
        "FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
        "WHERE s.paid_amount > 0 AND s.sale_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "sale", "direction": "in", "amount": r["amt"],
            "label": f"Sale #{r['id']}" + (f" \u2014 {r['cname']}" if r["cname"] else " \u2014 walk-in"),
            "detail": "Part payment" if r["amt"] < r["total_amount"] - 0.005 else "Paid in full",
            "endpoint": "sale_detail", "args": {"sale_id": r["id"]},
        })

    for r in conn.execute(
        "SELECT p.payment_date d, p.amount amt, p.notes, c.id cid, c.name cname FROM payments p "
        "JOIN customers c ON c.id = p.customer_id WHERE p.payment_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "customer_payment", "direction": "in", "amount": r["amt"],
            "label": f"Credit repaid \u2014 {r['cname']}", "detail": r["notes"] or "",
            "endpoint": "customer_detail", "args": {"customer_id": r["cid"]},
        })

    for r in conn.execute(
        "SELECT o.id, o.advance_date d, o.advance_amount amt, c.name cname FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "WHERE o.advance_amount > 0 AND o.advance_date IS NOT NULL AND o.advance_date BETWEEN ? AND ?",
        (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "order_advance", "direction": "in", "amount": r["amt"],
            "label": f"Order #{r['id']} advance \u2014 {r['cname']}", "detail": "Booking advance",
            "endpoint": "order_detail", "args": {"order_id": r["id"]},
        })

    for r in conn.execute(
        "SELECT o.id, o.completed_date d, o.balance_received amt, c.name cname FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.status='completed' "
        "AND o.balance_received > 0 AND o.completed_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "order_balance", "direction": "in", "amount": r["amt"],
            "label": f"Order #{r['id']} balance \u2014 {r['cname']}", "detail": "Collected on delivery",
            "endpoint": "order_detail", "args": {"order_id": r["id"]},
        })

    for r in conn.execute(
        "SELECT o.id, o.cancelled_date d, o.refunded_amount amt, c.name cname FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.status='cancelled' "
        "AND o.refunded_amount > 0 AND o.cancelled_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "order_refund", "direction": "out", "amount": r["amt"],
            "label": f"Refund \u2014 order #{r['id']} cancelled ({r['cname']})", "detail": "Advance returned",
            "endpoint": "order_detail", "args": {"order_id": r["id"]},
        })

    for r in conn.execute(
        "SELECT id, expense_date d, amount amt, category, notes FROM expenses "
        "WHERE id != 0 AND expense_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "expense", "direction": "out", "amount": r["amt"],
            "label": r["category"] or "Expense", "detail": r["notes"] or "",
            "endpoint": "expenses_list", "args": {},
        })

    for r in conn.execute(
        "SELECT p.id, p.purchase_date d, p.paid_amount amt, p.total_cost, p.supplier_name, "
        "i.name iname, s.name sname FROM purchases p JOIN items i ON i.id = p.item_id "
        "LEFT JOIN suppliers s ON s.id = p.supplier_id "
        "WHERE p.paid_amount > 0 AND p.purchase_date BETWEEN ? AND ?", (lo, hi),
    ):
        who = r["sname"] or r["supplier_name"] or "supplier"
        rows.append({
            "date": r["d"], "kind": "purchase", "direction": "out", "amount": r["amt"],
            "label": f"Bought {r['iname']} \u2014 {who}",
            "detail": "Part payment" if r["amt"] < r["total_cost"] - 0.005 else "Paid in full",
            "endpoint": "purchases_list", "args": {},
        })

    for r in conn.execute(
        "SELECT oo.amount amt, o.completed_date d, oo.supplier_name, oo.description "
        "FROM order_outsourced oo JOIN orders o ON o.id = oo.order_id "
        "WHERE oo.payment_status='paid' AND oo.paid_amount > 0 "
        "AND o.completed_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "purchase", "direction": "out", "amount": r["amt"],
            "label": f"Outsourced work \u2014 {r['supplier_name'] or 'supplier'}",
            "detail": r["description"] or "paid on completion",
            "endpoint": "orders_list", "args": {},
        })

    for r in conn.execute(
        "SELECT sp.id, sp.payment_date d, sp.amount amt, sp.notes, s.id sid, s.name sname "
        "FROM supplier_payments sp JOIN suppliers s ON s.id = sp.supplier_id "
        "WHERE sp.payment_date BETWEEN ? AND ?", (lo, hi),
    ):
        rows.append({
            "date": r["d"], "kind": "supplier_payment", "direction": "out", "amount": r["amt"],
            "label": f"Paid supplier \u2014 {r['sname']}", "detail": r["notes"] or "Against credit purchase",
            "endpoint": "supplier_detail", "args": {"supplier_id": r["sid"]},
        })

    rows.sort(key=lambda x: (x["date"], x["kind"]))
    return rows


def _sql_period_cash(conn, since=None, until=None):
    """The same totals cash_movements produces, done as SQL sums."""
    lo = since.isoformat() if since else "0000-01-01"
    hi = until.isoformat() if until else "9999-12-31"
    q = lambda sql: conn.execute(sql, (lo, hi)).fetchone()["t"] or 0.0

    opening = 0.0
    amt, odate = opening_cash(conn)
    if amt and (not since or (odate or "0000-01-01") >= lo) and (odate or "0000-01-01") <= hi:
        opening = amt

    sale_cash = q("SELECT COALESCE(SUM(paid_amount),0) t FROM sales WHERE sale_date BETWEEN ? AND ?")
    cust_pay = q("SELECT COALESCE(SUM(amount),0) t FROM payments WHERE payment_date BETWEEN ? AND ?")
    advances = q("SELECT COALESCE(SUM(advance_amount),0) t FROM orders WHERE advance_date BETWEEN ? AND ?")
    balances = q("SELECT COALESCE(SUM(balance_received),0) t FROM orders "
                 "WHERE status='completed' AND completed_date BETWEEN ? AND ?")
    refunds = q("SELECT COALESCE(SUM(refunded_amount),0) t FROM orders "
                "WHERE status='cancelled' AND cancelled_date BETWEEN ? AND ?")
    expenses = q("SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE expense_date BETWEEN ? AND ?")
    pur_paid = q("SELECT COALESCE(SUM(paid_amount),0) t FROM purchases WHERE purchase_date BETWEEN ? AND ?")
    sup_paid = q("SELECT COALESCE(SUM(amount),0) t FROM supplier_payments WHERE payment_date BETWEEN ? AND ?")
    outsourced = conn.execute(
        "SELECT COALESCE(SUM(oo.paid_amount),0) t FROM order_outsourced oo "
        "JOIN orders o ON o.id=oo.order_id WHERE oo.payment_status='paid' "
        "AND o.completed_date BETWEEN ? AND ?", (lo, hi)).fetchone()["t"] or 0.0

    cash_in = opening + sale_cash + cust_pay + advances + balances
    to_suppliers = pur_paid + sup_paid + outsourced
    cash_out = expenses + to_suppliers + refunds
    return {
        "cash_in": cash_in, "cash_out": cash_out, "net": cash_in - cash_out,
        "expenses": expenses, "to_suppliers": to_suppliers, "refunds": refunds,
        "from_sales": sale_cash, "from_payments": cust_pay,
        "from_advances": advances, "from_balances": balances,
    }


def period_cash(conn, sales, all_orders, orders_completed, since=None, until=None):
    """Money in and out of the till for a date window - totalled straight from
    cash_movements(), so the Cash Book and every cash figure elsewhere always agree.

    Note money paid to a supplier is cash out but NOT a hit to profit - the cost of those
    goods is charged to profit when they're sold (via the item's average cost), not when
    they're bought. Charging it here as well would count the same rupee twice.
    """
    # cash_movements builds one Python row per movement, which is right for the Cash Book
    # but wasteful when only the totals are wanted. On a large database sum in SQL instead.
    if is_big(conn):
        return _sql_period_cash(conn, since, until)
    moves = cash_movements(conn, since, until)
    by_kind = {}
    for m in moves:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0.0) + m["amount"]

    cash_in = sum(by_kind.get(k, 0.0) for k in CASH_IN_KINDS)
    refunds = by_kind.get("order_refund", 0.0)
    expenses = by_kind.get("expense", 0.0)
    to_suppliers = by_kind.get("purchase", 0.0) + by_kind.get("supplier_payment", 0.0)

    return {
        "cash_in": cash_in,
        "refunds": refunds,
        "expenses": expenses,
        "to_suppliers": to_suppliers,
        "net": cash_in - refunds - expenses - to_suppliers,
    }


def _sql_money_position(conn):
    """Money position from SQL - used on large databases."""
    st = _sql_period_stats(conn)
    cash = period_cash(conn, None, None, None)
    receivable = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) t FROM customers WHERE credit_balance>0"
    ).fetchone()["t"]
    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE payable_balance>0"
    ).fetchone()["t"]
    stock = conn.execute(
        "SELECT COALESCE(SUM(stock_qty*avg_cost),0) t FROM items "
        "WHERE is_active=1 AND item_type!='service'").fetchone()["t"]
    exp = conn.execute("SELECT COALESCE(SUM(amount),0) t FROM expenses").fetchone()["t"]
    unearned = conn.execute(
        "SELECT COALESCE(SUM(advance_amount),0) t FROM orders "
        "WHERE status IN ('pending','design','printing','ready')").fetchone()["t"]
    return {
        "cash_in_hand": cash["net"], "receivable": receivable, "payable": payable,
        "stock_value": stock, "revenue_booked": st["revenue"],
        "cost_of_goods": st["cost"], "gross_profit": st["profit"],
        "expenses": exp, "net_profit": st["profit"] - exp,
        "business_worth": cash["net"] + receivable + stock - payable,
        "unearned_advances": unearned,
        "collected": cash["cash_in"], "to_suppliers": cash["to_suppliers"],
        "refunds": cash["refunds"],
    }


def money_position(conn, sales, all_orders, orders_completed):
    if sales is None:
        return _sql_money_position(conn)
    """Where the business's money actually is, all-time.

    Answers the question "I made a profit, so why isn't that much cash in the drawer?" -
    the gap is money tied up in unpaid customer bills and in stock sitting in the godown,
    offset by goods you're holding but haven't paid your supplier for yet.
    """
    earned = period_stats(sales, orders_completed)          # revenue / cost / profit, all time
    cash = period_cash(conn, sales, all_orders, orders_completed)
    total_expenses = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE id != 0"
    ).fetchone()["t"]

    receivable = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) t FROM customers WHERE credit_balance > 0"
    ).fetchone()["t"]
    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE payable_balance > 0"
    ).fetchone()["t"]
    stock_value = conn.execute(
        "SELECT COALESCE(SUM(stock_qty * avg_cost),0) t FROM items "
        "WHERE is_active=1 AND item_type != 'service' AND stock_qty > 0"
    ).fetchone()["t"]

    # Advances held for orders not yet delivered: cash you're holding but haven't earned.
    unearned_advances = sum(
        o["advance_amount"] for o in (all_orders or []) if o["status"] in OPEN_STATUSES
    )

    net_profit = earned["profit"] - total_expenses

    return {
        "revenue_booked": earned["revenue"],
        "cost_of_goods": earned["cost"],
        "gross_profit": earned["profit"],
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "collected": cash["cash_in"],
        "paid_suppliers": cash["to_suppliers"],
        "paid_expenses": cash["expenses"],
        "refunds": cash["refunds"],
        "cash_in_hand": cash["net"],
        "receivable": receivable,
        "payable": payable,
        "stock_value": stock_value,
        "unearned_advances": unearned_advances,
        "business_worth": cash["net"] + receivable + stock_value - payable,
    }


def product_breakdown(conn, since, until, limit=None, order_by="revenue"):
    """Per-product revenue / profit for a date window.

    Combines two sources so custom orders don't vanish from the product charts:
      - regular sale lines, which carry their own cost and profit
      - lines on *completed* custom orders. An order's cost is recorded once for the whole
        job, so it's split across that order's lines in proportion to each line's value.
    One-off custom lines with no catalog item are grouped under a single label.
    """
    totals = {}

    def bucket(name, unit):
        key = (name, unit or "")
        if key not in totals:
            totals[key] = {"name": name, "unit": unit or "", "qty": 0.0, "revenue": 0.0, "profit": 0.0}
        return totals[key]

    sale_rows = conn.execute(
        "SELECT i.name, i.unit, si.quantity, si.line_total, si.line_profit "
        "FROM sale_items si JOIN items i ON i.id = si.item_id JOIN sales s ON s.id = si.sale_id "
        "WHERE s.sale_date BETWEEN ? AND ?",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    for r in sale_rows:
        b = bucket(r["name"], r["unit"])
        b["qty"] += r["quantity"]
        b["revenue"] += r["line_total"]
        b["profit"] += r["line_profit"]

    order_rows = conn.execute(
        "SELECT o.id AS order_id, o.total_cost, o.agreed_total, oi.quantity, oi.line_total, "
        "oi.description, i.name AS item_name, i.unit AS item_unit "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "LEFT JOIN items i ON i.id = oi.item_id "
        "WHERE o.status='completed' AND o.completed_date BETWEEN ? AND ?",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    for r in order_rows:
        name = r["item_name"] or "Custom order work"
        unit = r["item_unit"] or "job"
        share = (r["line_total"] / r["agreed_total"]) if r["agreed_total"] else 0.0
        line_cost = (r["total_cost"] or 0.0) * share
        b = bucket(name, unit)
        b["qty"] += r["quantity"]
        b["revenue"] += r["line_total"]
        b["profit"] += r["line_total"] - line_cost

    rows = sorted(totals.values(), key=lambda x: x[order_by], reverse=True)
    return rows[:limit] if limit else rows


def period_bounds(today):
    start_of_week = today - timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)
    start_of_year = today.replace(month=1, day=1)
    return start_of_week, start_of_month, start_of_year


def month_bounds_ym(year, month):
    start = date(year, month, 1)
    end = date(year + (1 if month == 12 else 0), (1 if month == 12 else month + 1), 1) - timedelta(days=1)
    return start, end


def employee_month_summary(conn, employee, year, month):
    """Salary math for one employee for one calendar month: daily rate is the monthly
    salary spread over however many days are in that month, leave/half-days deduct at
    that rate, and any advances taken that month come off the top too."""
    start, end = month_bounds_ym(year, month)
    days_in_month = (end - start).days + 1
    daily_rate = employee["monthly_salary"] / days_in_month if days_in_month else 0

    attendance = conn.execute(
        "SELECT status FROM employee_attendance WHERE employee_id=? AND date BETWEEN ? AND ?",
        (employee["id"], start.isoformat(), end.isoformat()),
    ).fetchall()
    leave_days = sum(1 for a in attendance if a["status"] == "leave")
    half_days = sum(1 for a in attendance if a["status"] == "half_day")
    present_days = sum(1 for a in attendance if a["status"] == "present")

    deduction = leave_days * daily_rate + half_days * (daily_rate / 2)
    advances_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM employee_advances WHERE employee_id=? "
        "AND advance_date BETWEEN ? AND ?",
        (employee["id"], start.isoformat(), end.isoformat()),
    ).fetchone()["t"]
    net_payable = employee["monthly_salary"] - deduction - advances_total

    return {
        "days_in_month": days_in_month,
        "daily_rate": daily_rate,
        "leave_days": leave_days,
        "half_days": half_days,
        "present_days": present_days,
        "deduction": deduction,
        "advances_total": advances_total,
        "net_payable": net_payable,
    }


def catalog_for_role(items):
    """Item data for the sale/order forms. Staff must never receive avg_cost: it is sent
    to the browser as JSON, so hiding it only in the HTML would still leave it readable
    in the page source."""
    out = []
    for i in items:
        d = dict(i)
        if not is_admin():
            d.pop("avg_cost", None)
        out.append(d)
    return out


def bounded_customer_list(conn, selected_id=None):
    """Customers to pre-render into a page's dropdown: regular ones (capped - this is meant
    to stay a short, curated list of people you deal with repeatedly) plus whichever specific
    customer is already selected (e.g. editing an old order for a one-time customer), so the
    field shows the right name. Anyone else is found through the search box instead - see
    customer_search_json - so the page never has to load thousands of one-time customers."""
    rows = conn.execute(
        "SELECT * FROM customers WHERE is_active=1 AND customer_type='regular' ORDER BY name LIMIT 300"
    ).fetchall()
    if selected_id:
        already = any(str(r["id"]) == str(selected_id) for r in rows)
        if not already:
            extra = conn.execute("SELECT * FROM customers WHERE id=?", (selected_id,)).fetchone()
            if extra:
                rows = [extra] + list(rows)
    return rows


def bounded_supplier_list(conn, selected_id=None):
    rows = conn.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name LIMIT 300").fetchall()
    if selected_id:
        already = any(str(r["id"]) == str(selected_id) for r in rows)
        if not already:
            extra = conn.execute("SELECT * FROM suppliers WHERE id=?", (selected_id,)).fetchone()
            if extra:
                rows = [extra] + list(rows)
    return rows


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"]:
        return row["value"]
    return default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_company(conn):
    name = get_setting(conn, "company_name", "")
    if not name:
        # backward-compat: earlier setup versions stashed the name in the expenses table
        row = conn.execute(
            "SELECT notes FROM expenses WHERE id=0 AND category='_company_name_'"
        ).fetchone()
        name = row["notes"] if row and row["notes"] else "My Company"
    return {
        "name": name,
        "address": get_setting(conn, "company_address", ""),
        "phone": get_setting(conn, "company_phone", ""),
        "gstin": get_setting(conn, "company_gstin", ""),
    }


# The stages an order moves through, in order. Defined once here so the list page, the
# detail page, the filters and the dashboard all agree - adding a stage means changing
# this and nothing else.
ORDER_FLOW = [
    ("pending",  "Pending",              "Booked, not started yet"),
    ("design",   "Design & tracing",     "Design and tracing paper taken"),
    ("printing", "Sent to print",        "At the printer"),
    ("ready",    "Ready for pickup",     "Finished, waiting for the customer"),
]
OPEN_STATUSES = tuple(k for k, _l, _d in ORDER_FLOW)
ORDER_LABELS = {k: l for k, l, _d in ORDER_FLOW}
ORDER_LABELS.update({"completed": "Completed", "cancelled": "Cancelled"})


def next_stage(status):
    """The stage that follows this one, or None if it's the last open stage."""
    keys = [k for k, _l, _d in ORDER_FLOW]
    if status not in keys:
        return None
    i = keys.index(status)
    return keys[i + 1] if i + 1 < len(keys) else None


def order_urgency(order, today=None):
    """How an open order stands against its promised date.

    Returns one of: late, today, soon (within 2 days), ok, or none when no date was
    given. An order quietly sitting past its date is a customer who turns up to
    nothing - during a busy season that is the mistake worth catching.
    """
    if order["status"] in ("completed", "cancelled"):
        return None, 0
    d = order["expected_ready_date"]
    if not d:
        return None, 0
    today = today or date.today()
    try:
        due = _as_date(d)
    except Exception:
        return None, 0
    days = (due - today).days
    if days < 0:
        return "late", days
    if days == 0:
        return "today", 0
    if days <= 2:
        return "soon", days
    return "ok", days


def open_order_counts(conn, today=None):
    """A quick count of what is late, due today and due soon, for the dashboard."""
    today = today or date.today()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status IN ('pending','design','printing','ready')"
    ).fetchall()
    out = {"late": 0, "today": 0, "soon": 0, "ok": 0, "nodate": 0, "total": len(rows)}
    late_value = 0.0
    for r in rows:
        kind, _ = order_urgency(r, today)
        if kind is None:
            out["nodate"] += 1
        else:
            out[kind] += 1
            if kind == "late":
                late_value += r["agreed_total"] or 0
    out["late_value"] = late_value
    return out


def negative_stock_warning(conn, prepared):
    """After a sale/order takes stock out, flag anything that has gone below zero. The app
    deliberately allows selling stock you haven't entered yet (production often gets recorded
    late), but it should never happen silently - negative stock distorts stock value and
    means a production or purchase entry is missing."""
    names = []
    for row in prepared:
        item_id = row[0]
        it = conn.execute(
            "SELECT name, unit, stock_qty, item_type FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if it and it["item_type"] in STOCK_TRACKED_TYPES and it["stock_qty"] < -0.001:
            names.append(f"{it['name']} ({it['stock_qty']:.0f} {it['unit']})")
    if names:
        flash(
            "Stock is now negative for: " + ", ".join(names) +
            ". The sale was saved, but this usually means a production run or purchase "
            "hasn't been entered yet. Fix it on the item's page so stock value and profit stay right.",
            "error",
        )


def compute_sale_lines(conn, lines, discount_amount=0.0):
    """Turn submitted {item_id, quantity, unit_price} lines into priced rows using each
    item's current average cost, and return the prepared rows plus totals.

    A whole-sale discount is spread across the lines in proportion to each line's value,
    rather than being stored as one separate number, so line_total on every sale_items row
    still adds up to the sale's actual total_amount - that's what keeps per-product figures
    on Analytics and Reports matching the real discounted revenue instead of overstating it.
    """
    total_amount = 0.0
    total_cost = 0.0
    prepared = []
    for line in lines:
        item_id = int(line["item_id"])
        qty = parse_float(line["quantity"])
        price = parse_float(line["unit_price"])
        if qty <= 0:
            continue
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            continue
        unit_cost = item["avg_cost"]
        line_total = qty * price
        line_profit = (price - unit_cost) * qty
        total_amount += line_total
        total_cost += unit_cost * qty
        prepared.append([item_id, qty, price, unit_cost, line_total, line_profit])

    discount_amount = max(0.0, min(discount_amount, total_amount))
    if discount_amount > 0.005 and total_amount > 0.005:
        remaining = discount_amount
        for i, row in enumerate(prepared):
            is_last = i == len(prepared) - 1
            share = discount_amount if is_last else round(discount_amount * (row[4] / total_amount), 2)
            share = min(share, remaining)
            row[4] -= share          # line_total
            row[5] -= share          # line_profit - discount comes straight out of profit, cost is unchanged
            remaining -= share
        total_amount -= discount_amount

    prepared = [tuple(row) for row in prepared]
    return prepared, total_amount, total_cost, discount_amount


def reverse_sale(conn, sale_id):
    """Undo a sale's effect on stock and customer credit, and clear its line items.
    Used by both delete and edit (edit = reverse the old version, then re-apply the new one)."""
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        return
    line_rows = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
    for si in line_rows:
        update_stock_return(conn, si["item_id"], si["quantity"])
    due = sale["total_amount"] - sale["paid_amount"]
    if due > 0.005 and sale["customer_id"]:
        conn.execute(
            "UPDATE customers SET credit_balance = credit_balance - ? WHERE id=?", (due, sale["customer_id"])
        )
    conn.execute("DELETE FROM sale_items WHERE sale_id=?", (sale_id,))


# ---------- setup / auth ----------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    conn = dbm.get_db()
    count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if count > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        company = request.form.get("company_name", "").strip() or "My Company"
        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("setup.html", db_info=db_info())
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, generate_password_hash(password)),
        )
        set_setting(conn, "company_name", company)
        conn.commit()
        flash("Setup complete. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("setup.html", db_info=db_info())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = dbm.get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            log_action("logged in")
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    conn = dbm.get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")
        if username and password:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), role),
                )
                conn.commit()
                flash(f"User '{username}' created.", "success")
            except Exception:
                flash("That username is already taken.", "error")
        return redirect(url_for("users"))
    all_users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return render_template("users.html", users=all_users)


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def business_settings():
    conn = dbm.get_db()
    if request.method == "POST":
        set_setting(conn, "company_name", request.form.get("company_name", "").strip() or "My Company")
        set_setting(conn, "company_address", request.form.get("company_address", "").strip())
        set_setting(conn, "invoice_paper", request.form.get("invoice_paper", "a4"))
        set_setting(conn, "staff_can_edit",
                    "yes" if request.form.get("staff_can_edit") == "yes" else "no")
        set_setting(conn, "opening_cash", str(parse_float(request.form.get("opening_cash"), 0)))
        set_setting(conn, "opening_cash_date", request.form.get("opening_cash_date", "").strip())
        set_setting(conn, "company_phone", request.form.get("company_phone", "").strip())
        set_setting(conn, "company_gstin", request.form.get("company_gstin", "").strip())
        conn.commit()
        flash("Business details updated.", "success")
        return redirect(url_for("business_settings"))
    company = get_company(conn)
    oc_amt, oc_date = opening_cash(conn)
    return render_template(
        "business_settings.html", company=company, db_info=db_info(),
        opening_cash_amount=oc_amt, opening_cash_date=oc_date,
        backups=list_backups(), lan_ip=local_ip(), backup_folder=backup_dir(),
        staff_can_edit=get_setting(conn, "staff_can_edit", "yes"),
        invoice_paper=get_setting(conn, "invoice_paper", "a4"),
        ledger_version=LEDGER_VERSION)


# ---------- dashboard ----------

@app.route("/")
@login_required
def dashboard():
    conn = dbm.get_db()
    today = date.today()
    today_str = today.isoformat()
    sow, som, soy = period_bounds(today)

    sales, all_orders, orders_completed = load_revenue_sources(conn)

    # Identical windows to the Reports page - both call the same helper, so the two
    # pages always show the same revenue and profit for the same period.
    stats = {
        "today": period_stats(sales, orders_completed, today, today),
        "week": period_stats(sales, orders_completed, sow, today),
        "month": period_stats(sales, orders_completed, som, today),
        "year": period_stats(sales, orders_completed, soy, today),
    }

    cash = period_cash(conn, sales, all_orders, orders_completed, today, today)
    cash_today = cash["net"]
    expenses_today = cash["expenses"]
    expenses_month = period_cash(conn, sales, all_orders, orders_completed, som, today)["expenses"]

    # Revenue trend. Beyond a month, daily bars get too thin to read, so longer ranges
    # are grouped into weeks - same information, still legible.
    TREND_RANGES = {"14": 14, "30": 30, "90": 90, "180": 180, "365": 365}
    trend_days = TREND_RANGES.get(request.args.get("trend", "14"), 14)

    def day_totals(dstr):
        if sales is None:      # large database - one small SQL sum per day
            r = conn.execute(
                "SELECT COALESCE(SUM(total_amount),0) rev, COALESCE(SUM(total_profit),0) pro "
                "FROM sales WHERE sale_date=?", (dstr,)).fetchone()
            o = conn.execute(
                "SELECT COALESCE(SUM(agreed_total),0) rev, COALESCE(SUM(profit),0) pro "
                "FROM orders WHERE status='completed' AND completed_date=?", (dstr,)).fetchone()
            return r["rev"] + o["rev"], r["pro"] + o["pro"]
        rev = sum(s["total_amount"] for s in sales if s["sale_date"] == dstr)
        rev += sum(o["agreed_total"] for o in orders_completed if o["completed_date"] == dstr)
        pro = sum(s["total_profit"] for s in sales if s["sale_date"] == dstr)
        pro += sum(o["profit"] for o in orders_completed if o["completed_date"] == dstr)
        return rev, pro

    trend = []
    if trend_days <= 31:
        for i in range(trend_days - 1, -1, -1):
            d = today - timedelta(days=i)
            rev, pro = day_totals(d.strftime("%Y-%m-%d"))
            # A short label fits in a narrow column; "24 Jul" wraps onto two lines and
            # pushes the chart out of shape once there are 30 of them.
            label = d.strftime("%d %b") if trend_days <= 16 else d.strftime("%d/%m")
            trend.append({"date": label, "revenue": rev, "profit": pro})
        trend_unit = "day"
    else:
        # Round UP so the whole period is covered - 90 days needs 13 weeks, not 12,
        # otherwise the first few days are silently dropped from the total.
        weeks = max(1, -(-trend_days // 7))
        window_start = today - timedelta(days=trend_days - 1)
        for w in range(weeks - 1, -1, -1):
            wk_end = today - timedelta(days=w * 7)
            wk_start = max(window_start, wk_end - timedelta(days=6))
            rev = pro = 0.0
            d = wk_start
            while d <= wk_end:
                r_, p_ = day_totals(d.strftime("%Y-%m-%d"))
                rev += r_; pro += p_
                d += timedelta(days=1)
            trend.append({"date": wk_start.strftime("%d/%m"), "revenue": rev, "profit": pro})
        trend_unit = "week"
    max_trend = max([t["revenue"] for t in trend] + [1])
    trend_total = sum(t["revenue"] for t in trend)

    low_stock = conn.execute(
        "SELECT * FROM items WHERE is_active=1 AND item_type != 'service' "
        "AND stock_qty <= reorder_level ORDER BY name"
    ).fetchall()

    outstanding = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) c FROM customers WHERE credit_balance > 0"
    ).fetchone()["c"]
    top_debtors = conn.execute(
        "SELECT * FROM customers WHERE credit_balance > 0 ORDER BY credit_balance DESC LIMIT 5"
    ).fetchall()

    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) c FROM suppliers WHERE payable_balance > 0"
    ).fetchone()["c"]
    top_payables = conn.execute(
        "SELECT * FROM suppliers WHERE payable_balance > 0 ORDER BY payable_balance DESC LIMIT 5"
    ).fetchall()

    recent_sales = conn.execute(
        "SELECT s.*, c.name as customer_name FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
        "ORDER BY s.id DESC LIMIT 8"
    ).fetchall()

    pending_orders = conn.execute(
        "SELECT o.*, c.name as customer_name FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status IN ('pending','design','printing','ready') "
        "ORDER BY o.expected_ready_date IS NULL, o.expected_ready_date LIMIT 8"
    ).fetchall()

    if not is_admin():
        # Staff see what they need to serve customers - never revenue, profit or cash.
        return render_template(
            "dashboard_staff.html",
            sales_today=conn.execute(
                "SELECT COUNT(*) c FROM sales WHERE sale_date=?", (today_str,)).fetchone()["c"],
            pending_orders=conn.execute(
                "SELECT o.*, c.name cname FROM orders o JOIN customers c ON c.id=o.customer_id "
                "WHERE o.status IN ('pending','design','printing','ready') "
                "ORDER BY o.expected_ready_date").fetchall(),
            low_stock=low_stock,
            order_counts=open_order_counts(conn),
            recent=conn.execute(
                "SELECT s.id, s.sale_date, s.total_amount, s.payment_status, c.name cname "
                "FROM sales s LEFT JOIN customers c ON c.id=s.customer_id "
                "ORDER BY s.id DESC LIMIT 8").fetchall(),
            debtors=conn.execute(
                "SELECT name, credit_balance FROM customers WHERE credit_balance > 0 "
                "ORDER BY credit_balance DESC LIMIT 5").fetchall(),
        )

    return render_template(
        "dashboard.html",
        stats=stats,
        cash_today=cash_today,
        cash=cash,
        expenses_today=expenses_today,
        expenses_month=expenses_month,
        order_counts=open_order_counts(conn),
        trend=trend,
        max_trend=max_trend,
        trend_days=trend_days,
        trend_unit=trend_unit,
        trend_total=trend_total,
        low_stock=low_stock,
        outstanding=outstanding,
        top_debtors=top_debtors,
        payable=payable,
        top_payables=top_payables,
        recent_sales=recent_sales,
        pending_orders=pending_orders,
    )



# ---------- global search ----------

@app.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    results = {}
    if q:
        conn = dbm.get_db()
        like = f"%{q}%"

        results["inventory_items"] = conn.execute(
            "SELECT * FROM items WHERE name LIKE ? OR category LIKE ? OR unit LIKE ? "
            "ORDER BY name LIMIT 20",
            (like, like, like),
        ).fetchall()

        results["customers"] = conn.execute(
            "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? OR address LIKE ? "
            "ORDER BY (customer_type='regular') DESC, name LIMIT 20",
            (like, like, like),
        ).fetchall()

        if is_admin():
          results["suppliers"] = conn.execute(
            "SELECT * FROM suppliers WHERE name LIKE ? OR phone LIKE ? OR address LIKE ? "
            "ORDER BY name LIMIT 20",
            (like, like, like),
        ).fetchall()

        results["sales"] = conn.execute(
            "SELECT s.*, c.name as customer_name FROM sales s "
            "LEFT JOIN customers c ON c.id = s.customer_id "
            "WHERE c.name LIKE ? OR s.notes LIKE ? "
            "ORDER BY s.id DESC LIMIT 20",
            (like, like),
        ).fetchall()

        results["orders"] = conn.execute(
            "SELECT o.*, c.name as customer_name FROM orders o "
            "JOIN customers c ON c.id = o.customer_id "
            "WHERE c.name LIKE ? OR o.notes LIKE ? OR o.status LIKE ? "
            "ORDER BY o.id DESC LIMIT 20",
            (like, like, like),
        ).fetchall()

        if is_admin():
          results["purchases"] = conn.execute(
            "SELECT p.*, i.name as item_name, s.name as supplier_link_name FROM purchases p "
            "JOIN items i ON i.id = p.item_id LEFT JOIN suppliers s ON s.id = p.supplier_id "
            "WHERE p.supplier_name LIKE ? OR s.name LIKE ? OR i.name LIKE ? OR p.notes LIKE ? "
            "ORDER BY p.id DESC LIMIT 20",
            (like, like, like, like),
        ).fetchall()

        results["employees"] = conn.execute(
            "SELECT * FROM employees WHERE name LIKE ? OR phone LIKE ? ORDER BY name LIMIT 20",
            (like, like),
        ).fetchall()

        if is_admin():
          results["expenses"] = conn.execute(
            "SELECT * FROM expenses WHERE category LIKE ? OR notes LIKE ? "
            "ORDER BY id DESC LIMIT 20",
            (like, like),
        ).fetchall()

        if is_admin():
          results["production"] = conn.execute(
            "SELECT pr.*, i.name as output_name FROM production_runs pr "
            "JOIN items i ON i.id = pr.output_item_id "
            "WHERE i.name LIKE ? OR pr.notes LIKE ? "
            "ORDER BY pr.id DESC LIMIT 20",
            (like, like),
        ).fetchall()

    total_count = sum(len(v) for v in results.values())
    return render_template("search_results.html", q=q, results=results, total_count=total_count)


@app.route("/money")
@admin_required
def money_page():
    conn = dbm.get_db()
    sales, all_orders, orders_completed = load_revenue_sources(conn)
    pos = money_position(conn, sales, all_orders, orders_completed)

    today = date.today()
    sow, som, soy = period_bounds(today)
    month_cash = period_cash(conn, sales, all_orders, orders_completed, som, today)
    month_stats = period_stats(sales, orders_completed, som, today)
    month_expenses = month_cash["expenses"]

    return render_template(
        "money.html", pos=pos, month_cash=month_cash,
        month_stats=month_stats, month_expenses=month_expenses,
    )


@app.route("/cashbook")
@admin_required
def cashbook():
    """Plain chronological record of every rupee in and out, with a running balance."""
    conn = dbm.get_db()
    today = date.today()
    sow, som, soy = period_bounds(today)

    preset = request.args.get("preset", "month")
    from_raw = request.args.get("from", "")
    to_raw = request.args.get("to", "")

    def parse_d(s, fallback):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return fallback

    if from_raw or to_raw:
        preset = "custom"
        start = parse_d(from_raw, som)
        end = parse_d(to_raw, today)
    elif preset == "today":
        start, end = today, today
    elif preset == "week":
        start, end = sow, today
    elif preset == "year":
        start, end = soy, today
    elif preset == "all":
        start, end = None, None
    else:
        preset = "month"
        start, end = som, today

    # On a very large database, "Everything" would build millions of movement rows to
    # show 300 of them. Fall back to the last year, and say so - the totals for all time
    # are still on the Money Position page.
    capped = False
    if start is None and is_big(conn):
        start = (end or date.today()) - timedelta(days=365)
        capped = True

    moves = cash_movements(conn, start, end)

    # Everything that happened before this window, so the running balance starts from the
    # money already in hand rather than from zero.
    opening = 0.0
    if start:
        # Only the TOTAL is wanted here, not the individual rows - building every
        # movement since the beginning just to add them up took about eighteen seconds
        # on a large database. period_cash gives the same figure from SQL sums.
        opening = period_cash(conn, None, None, None, None, start - timedelta(days=1))["net"]

    running = opening
    for m in moves:
        running += m["amount"] if m["direction"] == "in" else -m["amount"]
        m["balance"] = running

    total_in = sum(m["amount"] for m in moves if m["direction"] == "in")
    total_out = sum(m["amount"] for m in moves if m["direction"] == "out")

    # Show a page at a time. Years of trading can run to tens of thousands of movements,
    # and putting them all in one page makes it enormous - unusable on a phone. The
    # totals above are still calculated across the whole period, not just this page.
    PER_PAGE = 300
    newest_first = list(reversed(moves))
    total_rows = len(newest_first)
    pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), pages))
    except ValueError:
        page = 1
    slice_ = newest_first[(page - 1) * PER_PAGE: page * PER_PAGE]

    return render_template(
        "cashbook.html", moves=slice_, opening=opening,
        closing=running, total_in=total_in, total_out=total_out,
        preset=preset, start=start, end=end,
        page=page, pages=pages, total_rows=total_rows, per_page=PER_PAGE, capped=capped,
    )


# ---------- items ----------

@app.route("/nonwoven-calculator")
@buy_required
def nonwoven_calculator():
    """Nonwoven fabric bag costing - works from a roll or from pre-cut sheets."""
    conn = dbm.get_db()
    rolls = conn.execute(
        "SELECT id, name, unit, avg_cost, stock_qty FROM items "
        "WHERE is_active=1 AND item_type='raw_material' ORDER BY name"
    ).fetchall()
    return render_template(
        "nonwoven_calculator.html",
        rolls=[dict(r) for r in rolls],
        rolls_json=json.dumps([dict(r) for r in rolls]),
    )


def cash_cycle(conn, days=90):
    """How long your money is tied up before it comes back.

    Three plain numbers, each in days:
      DSO  how long customers take to pay you
      DIO  how long stock sits before it sells
      DPO  how long you take to pay suppliers
      CCC  = DSO + DIO - DPO

    A positive cycle means you fund the gap yourself. Negative means your suppliers
    fund it, which is the comfortable side to be on.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    lo, hi = start.isoformat(), end.isoformat()

    sales, all_orders, comp = load_revenue_sources(conn)
    st = period_stats(sales, comp, start, end)
    revenue = st["revenue"]
    cogs = st["cost"]

    receivable = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) t FROM customers WHERE credit_balance > 0"
    ).fetchone()["t"]
    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE payable_balance > 0"
    ).fetchone()["t"]
    stock_value = conn.execute(
        "SELECT COALESCE(SUM(stock_qty * avg_cost),0) t FROM items "
        "WHERE is_active=1 AND item_type != 'service' AND stock_qty > 0"
    ).fetchone()["t"]

    # Purchases are the right base for DPO - that is what creates the payable.
    purchases = conn.execute(
        "SELECT COALESCE(SUM(total_cost),0) t FROM purchases WHERE purchase_date BETWEEN ? AND ?",
        (lo, hi)).fetchone()["t"]

    def per_day(total):
        return total / days if days else 0

    dso = receivable / per_day(revenue) if revenue > 0 else None
    dio = stock_value / per_day(cogs) if cogs > 0 else None
    dpo = payable / per_day(purchases) if purchases > 0 else None
    ccc = None
    if dso is not None and dio is not None and dpo is not None:
        ccc = dso + dio - dpo

    return {
        "days": days, "start": start, "end": end,
        "revenue": revenue, "cogs": cogs, "purchases": purchases,
        "receivable": receivable, "payable": payable, "stock_value": stock_value,
        "dso": dso, "dio": dio, "dpo": dpo, "ccc": ccc,
        "margin": (st["profit"] / revenue * 100) if revenue else 0,
        "profit": st["profit"],
    }


def balance_sheet_data(conn, as_at=None):
    """A proper balance sheet, built from what Ledger already knows.

    The accounting identity must hold: assets = liabilities + equity. Rather than
    plugging a difference, equity is derived as assets minus liabilities and then split
    into what was put in and what the trading has earned - so the two sides balance by
    construction, and the retained earnings figure is the honest residual.
    """
    as_at = as_at or date.today()
    hi = as_at.isoformat()

    sales, all_orders, comp = load_revenue_sources(conn)
    cash = period_cash(conn, sales, all_orders, comp, None, as_at)["net"]

    receivable = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) t FROM customers WHERE credit_balance > 0"
    ).fetchone()["t"]
    # A customer in credit with you is money you owe them, not an asset.
    customer_advances = conn.execute(
        "SELECT COALESCE(SUM(-credit_balance),0) t FROM customers WHERE credit_balance < 0"
    ).fetchone()["t"]

    stock_finished = conn.execute(
        "SELECT COALESCE(SUM(stock_qty*avg_cost),0) t FROM items "
        "WHERE is_active=1 AND item_type='finished_good' AND stock_qty > 0").fetchone()["t"]
    stock_raw = conn.execute(
        "SELECT COALESCE(SUM(stock_qty*avg_cost),0) t FROM items "
        "WHERE is_active=1 AND item_type='raw_material' AND stock_qty > 0").fetchone()["t"]

    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE payable_balance > 0"
    ).fetchone()["t"]
    # Advances taken on jobs not yet delivered are a liability - you owe the work.
    unearned = conn.execute(
        "SELECT COALESCE(SUM(advance_amount),0) t FROM orders "
        "WHERE status IN ('pending','design','printing','ready')").fetchone()["t"]
    staff_advances = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM employee_advances").fetchone()["t"]

    st = period_stats(sales, comp, None, as_at)
    expenses = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE expense_date <= ?",
        (hi,)).fetchone()["t"]
    earned = st["profit"] - expenses

    current_assets = [
        ("Cash in hand", cash),
        ("Money owed by customers", receivable),
        ("Advances to staff", staff_advances),
    ]
    stock_assets = [
        ("Raw material (rolls)", stock_raw),
        ("Finished goods", stock_finished),
    ]
    total_current = sum(v for _n, v in current_assets)
    total_stock = sum(v for _n, v in stock_assets)
    total_assets = total_current + total_stock

    current_liabs = [
        ("Owed to suppliers", payable),
        ("Advances taken on undelivered orders", unearned),
        ("Customer credit balances", customer_advances),
    ]
    total_liabs = sum(v for _n, v in current_liabs)

    equity_total = total_assets - total_liabs
    # Whatever equity is not explained by trading was put in (or drawn out) by the owner.
    owner_capital = equity_total - earned

    return {
        "as_at": as_at,
        "current_assets": current_assets, "total_current": total_current,
        "stock_assets": stock_assets, "total_stock": total_stock,
        "total_assets": total_assets,
        "current_liabs": current_liabs, "total_liabs": total_liabs,
        "earned": earned, "owner_capital": owner_capital, "equity_total": equity_total,
        "balances": abs(total_assets - (total_liabs + equity_total)) < 0.01,
        "revenue": st["revenue"], "gross": st["profit"], "expenses": expenses,
        "working_capital": total_current + total_stock - total_liabs,
        "current_ratio": ((total_current + total_stock) / total_liabs) if total_liabs else None,
    }


@app.route("/balance-sheet")
@admin_required
def balance_sheet():
    conn = dbm.get_db()
    try:
        as_at = datetime.strptime(request.args.get("as_at", ""), "%Y-%m-%d").date()
    except ValueError:
        as_at = date.today()
    return render_template(
        "balance_sheet.html", b=balance_sheet_data(conn, as_at),
        company=get_setting(conn, "company_name", ""),
        company_address=get_setting(conn, "company_address", ""),
        company_phone=get_setting(conn, "company_phone", ""),
        company_gstin=get_setting(conn, "company_gstin", ""),
        printed=datetime.now().strftime("%d %b %Y, %I:%M %p"))


# ---------------------------------------------------------------- intelligence

@app.route("/graph/<kind>/<int:eid>")
@admin_required
def entity_graph(kind, eid):
    conn = dbm.get_db()
    builders = {"customer": intel.customer_graph, "supplier": intel.supplier_graph,
                "item": intel.item_graph}
    fn = builders.get(kind)
    if not fn:
        flash("That isn't something Ledger can graph.", "error")
        return redirect(url_for("dashboard"))
    g = fn(conn, eid)
    if not g:
        flash("Not found.", "error")
        return redirect(url_for("dashboard"))
    # radial_layout mutates g["nodes"] in place, giving each a sensible starting x,y -
    # the browser then takes over with a live physics simulation from there, so a good
    # seed position matters (a graph that starts scattered randomly looks like chaos
    # for the first second; starting from the ring layout looks alive from frame one).
    intel.radial_layout(g, width=920, height=720)
    return render_template("entity_graph.html", g=g, kind=kind, eid=eid, gw=920, gh=720)


@app.route("/signals")
@admin_required
def signals_page():
    conn = dbm.get_db()
    return render_template("signals.html", a=intel.all_anomalies(conn))


@app.route("/late-analysis")
@admin_required
def late_analysis_page():
    conn = dbm.get_db()
    return render_template("late_analysis.html", la=intel.late_order_analysis(conn))


def daily_revenue_trend(conn, days=14):
    """The last N days of revenue and profit, one point per day.

    A small, self-contained version of the dashboard's trend logic - kept separate so
    a change here can never affect the dashboard, and vice versa. Same source data
    either way: sales on their sale date, completed custom orders on the day they were
    handed over.
    """
    sales, all_orders, comp = load_revenue_sources(conn)
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        dstr = d.isoformat()
        if sales is None:
            r = conn.execute(
                "SELECT COALESCE(SUM(total_amount),0) rev FROM sales WHERE sale_date=?",
                (dstr,)).fetchone()
            o = conn.execute(
                "SELECT COALESCE(SUM(agreed_total),0) rev FROM orders "
                "WHERE status='completed' AND completed_date=?", (dstr,)).fetchone()
            rev = r["rev"] + o["rev"]
        else:
            rev = sum(x["total_amount"] for x in sales if x["sale_date"] == dstr)
            rev += sum(o["agreed_total"] for o in comp if o["completed_date"] == dstr)
        out.append({"date": d.strftime("%d %b"), "revenue": rev})
    return out


@app.route("/wall")
@admin_required
def ops_wall():
    conn = dbm.get_db()
    sales, all_orders, comp = load_revenue_sources(conn)
    today = date.today()
    som = today.replace(day=1)
    st_today = period_stats(sales, comp, today, today)
    st_month = period_stats(sales, comp, som, today)
    pos = money_position(conn, sales, all_orders, comp)
    counts = open_order_counts(conn)
    la = intel.late_order_analysis(conn)
    an = intel.all_anomalies(conn)
    activity = conn.execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT 24").fetchall()
    trend = daily_revenue_trend(conn, 14)
    growth = M.customer_growth_table(sys.modules[__name__], conn, sort="revenue", direction="desc")
    top_customer = growth["customers"][0] if growth["customers"] else None
    return render_template(
        "wall.html", st_today=st_today, st_month=st_month, pos=pos, counts=counts,
        la=la, an=an, activity=activity, now=datetime.now(), trend=trend,
        top_customer=top_customer, top20_share=growth["top20_share"] if growth["customers"] else None)


@app.route("/growth/customers")
@admin_required
def customer_growth_page():
    conn = dbm.get_db()
    sort = request.args.get("sort", "revenue")
    direction = request.args.get("dir", "desc")
    q = (request.args.get("q") or "").strip()
    data = M.customer_growth_table(sys.modules[__name__], conn, sort, direction, q)

    PER_PAGE = 100
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = len(data["customers"])
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    data["page_rows"] = data["customers"][(page - 1) * PER_PAGE: page * PER_PAGE]
    data["page"] = page
    data["pages"] = pages
    return render_template("customer_growth.html", d=data)


@app.route("/machine-log", methods=["GET", "POST"])
@buy_required
def machine_log():
    conn = dbm.get_db()
    if request.method == "POST":
        try:
            hrun = float(request.form.get("hours_run") or 0)
            havail = float(request.form.get("hours_available") or 0)
            good = float(request.form.get("good_units") or 0)
            reject = float(request.form.get("reject_units") or 0)
        except ValueError:
            flash("Please enter numbers for the hours and unit counts.", "error")
            return redirect(url_for("machine_log"))
        if havail <= 0:
            flash("Hours available must be more than zero.", "error")
            return redirect(url_for("machine_log"))
        conn.execute(
            "INSERT INTO machine_logs (log_date, machine, hours_run, hours_available, "
            "good_units, reject_units, notes, by_user) VALUES (?,?,?,?,?,?,?,?)",
            (request.form.get("log_date") or date.today().isoformat(),
             (request.form.get("machine") or "").strip(), hrun, havail, good, reject,
             (request.form.get("notes") or "").strip(), session.get("username")))
        conn.commit()
        flash("Shift logged.", "success")
        return redirect(url_for("machine_log"))

    logs = conn.execute(
        "SELECT * FROM machine_logs ORDER BY log_date DESC, id DESC LIMIT 60").fetchall()
    return render_template("machine_log.html", logs=logs, today=date.today())


@app.route("/settings/growth", methods=["GET", "POST"])
@admin_required
def growth_settings():
    conn = dbm.get_db()
    if request.method == "POST":
        set_setting(conn, "fixed_expense_categories", request.form.get("fixed_categories", ""))
        set_setting(conn, "acquisition_expense_categories", request.form.get("acquisition_categories", ""))
        conn.commit()
        flash("Saved.", "success")
        return redirect(url_for("growth_settings"))
    known = [r["c"] for r in conn.execute(
        "SELECT DISTINCT trim(category) c FROM expenses "
        "WHERE trim(COALESCE(category,'')) != '' ORDER BY c")]
    return render_template(
        "growth_settings.html", known=known,
        fixed=get_setting(conn, "fixed_expense_categories", "rent, salary"),
        acquisition=get_setting(conn, "acquisition_expense_categories", ""))


@app.route("/metrics")
@admin_required
def metrics_page():
    """Everything the business can tell you about itself, in one place."""
    conn = dbm.get_db()
    tab = request.args.get("tab", "overview")
    me = sys.modules[__name__]
    # Only work out what this tab actually shows. Computing all seven on every load meant
    # every tab paid for the slowest one.
    blank = {"ready": False, "why": ""}
    data = {"tab": tab, "model": None, "salary": blank, "breakeven": blank,
            "customers": blank, "production": blank, "machines": M.machine_metrics(me, conn),
            "forecast": blank, "cycle": None,
            "contrib": blank, "cac": blank, "aov": blank, "ontime": blank,
            "rev_emp": blank, "reprint": blank, "waste": blank, "machine": blank}
    if tab == "overview":
        data["model"] = M.financial_model(me, conn)
        data["breakeven"] = M.break_even(me, conn)
    elif tab == "salary":
        data["salary"] = M.owner_salary(me, conn)
    elif tab == "customers":
        data["customers"] = M.customer_metrics(me, conn)
    elif tab == "production":
        data["production"] = M.production_metrics(me, conn)
    elif tab == "cash":
        data["cycle"] = cash_cycle(conn, 90)
    elif tab == "forecast":
        data["forecast"] = M.forecast(me, conn)
    elif tab == "growth":
        data["contrib"] = M.contribution_margin(me, conn)
        data["cac"] = M.customer_acquisition_cost(me, conn)
        data["aov"] = M.average_order_value(me, conn)
        data["ontime"] = M.on_time_delivery(me, conn)
        data["rev_emp"] = M.revenue_per_employee(me, conn)
        data["reprint"] = M.reprint_rate(me, conn)
        data["waste"] = M.waste_rate(me, conn)
        data["machine"] = M.machine_utilisation(conn)
    return render_template("metrics.html", **data)


@app.route("/cash-cycle")
@admin_required
def cash_cycle_page():
    conn = dbm.get_db()
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        days = 90
    days = days if days in (30, 90, 180, 365) else 90
    return render_template("cash_cycle.html", c=cash_cycle(conn, days))


@app.route("/credit-aging")
@login_required
def credit_aging():
    """Who owes you, and for how long. Old debt is the debt that goes bad, so the age
    matters as much as the amount."""
    conn = dbm.get_db()
    today = date.today()
    buckets = [("0-30 days", 0, 30), ("31-60 days", 31, 60),
               ("61-90 days", 61, 90), ("Over 90 days", 91, 99999)]
    rows = []
    totals = {b[0]: 0.0 for b in buckets}
    grand = 0.0

    for c in conn.execute(
            "SELECT * FROM customers WHERE credit_balance > 0.005 "
            "ORDER BY credit_balance DESC LIMIT 500"):
        # Age the balance against the customer's unpaid bills, oldest first.
        unpaid = []
        for r in conn.execute(
            "SELECT sale_date d, (total_amount - paid_amount) due FROM sales "
            "WHERE customer_id=? AND (total_amount - paid_amount) > 0.005 ORDER BY sale_date",
                (c["id"],)):
            unpaid.append((r["d"], r["due"]))
        for r in conn.execute(
            "SELECT completed_date d, (agreed_total - advance_amount - balance_received) due "
            "FROM orders WHERE customer_id=? AND status='completed' "
            "AND (agreed_total - advance_amount - balance_received) > 0.005 "
            "AND completed_date IS NOT NULL ORDER BY completed_date", (c["id"],)):
            unpaid.append((r["d"], r["due"]))
        unpaid.sort()

        row = {"name": c["name"], "phone": c["phone"] or "", "id": c["id"],
               "total": c["credit_balance"], "oldest": None}
        for label, _lo, _hi in buckets:
            row[label] = 0.0

        remaining = c["credit_balance"]
        for d, due in unpaid:
            if remaining <= 0.005:
                break
            take = min(due, remaining)
            age = (today - _as_date(d)).days
            for label, lo, hi in buckets:
                if lo <= age <= hi:
                    row[label] += take
                    totals[label] += take
                    break
            if row["oldest"] is None or age > row["oldest"]:
                row["oldest"] = age
            remaining -= take
        if remaining > 0.005:
            # Balance with no bill behind it (an opening balance, or a manual correction).
            row["Over 90 days"] += remaining
            totals["Over 90 days"] += remaining
        grand += c["credit_balance"]
        rows.append(row)

    rows.sort(key=lambda r: (-(r["oldest"] or 0), -r["total"]))
    return render_template("credit_aging.html", rows=rows, buckets=[b[0] for b in buckets],
                           totals=totals, grand=grand, today=today)


@app.route("/customers/<int:customer_id>/statement")
@login_required
def customer_statement(customer_id):
    """A printable account statement - everything bought, everything paid, balance."""
    conn = dbm.get_db()
    cust = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        flash("Customer not found.", "error")
        return redirect(url_for("customers_list"))

    lines = []
    for r in conn.execute(
        "SELECT id, sale_date d, total_amount, paid_amount FROM sales WHERE customer_id=?",
            (customer_id,)):
        lines.append({"date": r["d"], "ref": f"Sale #{r['id']}", "desc": "Goods sold",
                      "charge": r["total_amount"], "paid": r["paid_amount"]})
    for r in conn.execute(
        "SELECT id, order_date, completed_date, status, agreed_total, advance_amount, "
        "balance_received, refunded_amount FROM orders WHERE customer_id=?", (customer_id,)):
        if r["advance_amount"] and r["advance_amount"] > 0:
            lines.append({"date": r["order_date"], "ref": f"Order #{r['id']}",
                          "desc": "Booking advance", "charge": 0, "paid": r["advance_amount"]})
        if r["status"] == "completed":
            lines.append({"date": r["completed_date"] or r["order_date"], "ref": f"Order #{r['id']}",
                          "desc": "Order delivered", "charge": r["agreed_total"],
                          "paid": r["balance_received"] or 0})
        if r["status"] == "cancelled" and (r["refunded_amount"] or 0) > 0:
            lines.append({"date": r["order_date"], "ref": f"Order #{r['id']}",
                          "desc": "Cancelled - advance refunded",
                          "charge": r["refunded_amount"], "paid": 0})
    for r in conn.execute(
        "SELECT payment_date d, amount, notes FROM payments WHERE customer_id=?", (customer_id,)):
        lines.append({"date": r["d"], "ref": "Payment", "desc": r["notes"] or "Received with thanks",
                      "charge": 0, "paid": r["amount"]})

    lines.sort(key=lambda x: (x["date"] or "", x["ref"]))

    # A statement must reconcile to what the customer actually owes. If the running total
    # of these transactions does not reach their balance, the difference came from an
    # opening balance or a manual correction - show it rather than leaving the customer
    # looking at a total that disagrees with the amount being asked for.
    derived = sum((l["charge"] or 0) - (l["paid"] or 0) for l in lines)
    gap = (cust["credit_balance"] or 0) - derived
    if abs(gap) > 0.005:
        lines.insert(0, {
            "date": (cust["created_at"] or "")[:10] or "",
            "ref": "Opening",
            "desc": "Balance brought forward",
            "charge": gap if gap > 0 else 0,
            "paid": -gap if gap < 0 else 0,
        })

    bal = 0.0
    for l in lines:
        bal += (l["charge"] or 0) - (l["paid"] or 0)
        l["balance"] = bal

    conn2 = conn
    return render_template(
        "customer_statement.html", cust=cust, lines=lines,
        charged=sum(l["charge"] or 0 for l in lines),
        received=sum(l["paid"] or 0 for l in lines),
        closing=bal, company=get_setting(conn2, "company_name", ""),
        company_address=get_setting(conn2, "company_address", ""),
        company_phone=get_setting(conn2, "company_phone", ""),
        printed=datetime.now().strftime("%d %b %Y, %I:%M %p"), today=date.today())


@app.route("/activity")
@admin_required
def activity_log_page():
    """Who entered what. Only the owner sees this."""
    conn = dbm.get_db()
    who = request.args.get("who", "")
    q = "SELECT * FROM activity_log WHERE 1=1"
    params = []
    if who:
        q += " AND username = ?"
        params.append(who)
    q += " ORDER BY id DESC LIMIT 400"
    rows = conn.execute(q, params).fetchall()
    users = conn.execute(
        "SELECT DISTINCT username FROM activity_log ORDER BY username").fetchall()
    return render_template("activity_log.html", rows=rows, users=users, who=who)


@app.route("/suppliers/<int:supplier_id>/statement")
@buy_required
def supplier_statement(supplier_id):
    """What you bought from them, what you paid, and what is still owed.

    The mirror of the customer statement. Three things can put money on a supplier's
    account: a purchase on credit, outsourced work on credit, and any opening balance
    that was there before Ledger. Payments take it off.
    """
    conn = dbm.get_db()
    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not sup:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers_list"))

    lines = []
    for r in conn.execute(
        "SELECT p.*, i.name iname, i.unit FROM purchases p JOIN items i ON i.id = p.item_id "
        "WHERE p.supplier_id=?", (supplier_id,)):
        lines.append({
            "date": r["purchase_date"], "ref": f"Purchase #{r['id']}",
            "desc": f"{r['iname']} \u2014 {r['quantity']:,.2f} {r['unit']} at {inr(r['unit_cost'])}",
            "charge": r["total_cost"], "paid": r["paid_amount"] or 0,
        })

    for r in conn.execute(
        "SELECT oo.*, o.completed_date, o.id oid FROM order_outsourced oo "
        "JOIN orders o ON o.id = oo.order_id WHERE oo.supplier_id=?", (supplier_id,)):
        lines.append({
            "date": r["completed_date"] or "", "ref": f"Order #{r['oid']}",
            "desc": r["description"] or "work done for a job",
            "charge": r["amount"], "paid": r["paid_amount"] or 0,
        })

    for r in conn.execute(
        "SELECT * FROM supplier_payments WHERE supplier_id=?", (supplier_id,)):
        lines.append({
            "date": r["payment_date"], "ref": "Payment",
            "desc": r["notes"] or "paid to them",
            "charge": 0, "paid": r["amount"],
        })

    lines.sort(key=lambda x: (x["date"] or "", x["ref"]))

    # If the running total does not reach what they are actually owed, the difference
    # came from an opening balance - show it rather than leaving the totals disagreeing.
    derived = sum((l["charge"] or 0) - (l["paid"] or 0) for l in lines)
    gap = (sup["payable_balance"] or 0) - derived
    if abs(gap) > 0.005:
        lines.insert(0, {
            "date": (sup["created_at"] or "")[:10] or "",
            "ref": "Opening", "desc": "Balance brought forward",
            "charge": gap if gap > 0 else 0, "paid": -gap if gap < 0 else 0,
        })

    bal = 0.0
    for l in lines:
        bal += (l["charge"] or 0) - (l["paid"] or 0)
        l["balance"] = bal

    return render_template(
        "supplier_statement.html", sup=sup, lines=lines,
        charged=sum(l["charge"] or 0 for l in lines),
        paid=sum(l["paid"] or 0 for l in lines),
        closing=bal, company=get_setting(conn, "company_name", ""),
        company_address=get_setting(conn, "company_address", ""),
        company_phone=get_setting(conn, "company_phone", ""),
        printed=datetime.now().strftime("%d %b %Y, %I:%M %p"), today=date.today())


@app.route("/backup", methods=["POST"])
@admin_required
def backup_now():
    try:
        dest = make_backup("manual")
        flash(f"Backup saved: {os.path.basename(dest)}", "success")
    except Exception as e:
        flash(f"Backup failed: {e}", "error")
    return redirect(url_for("business_settings"))


@app.route("/backup/download")
@admin_required
def backup_download():
    """Download a copy of the live database - the safest backup is one kept off this PC."""
    from flask import send_file
    try:
        tmp = make_backup("download")
        return send_file(tmp, as_attachment=True,
                         download_name=f"erp-backup-{date.today().isoformat()}.db")
    except Exception as e:
        flash(f"Could not prepare the download: {e}", "error")
        return redirect(url_for("business_settings"))


# ---------- exports ----------

@app.route("/exports")
@admin_required
def exports_page():
    """One place listing every report, with the date range and format to hand."""
    today = date.today()
    sow, som, soy = period_bounds(today)
    return render_template(
        "exports.html",
        labels=exports.REPORT_LABELS,
        today=today.isoformat(),
        month_start=som.isoformat(),
        week_start=sow.isoformat(),
        year_start=soy.isoformat(),
    )


@app.route("/export/<name>.<fmt>")
@admin_required
def export_report(name, fmt):
    conn = dbm.get_db()
    rep = exports.build(sys.modules[__name__], conn, name, request.args)
    if rep is None:
        flash("That report does not exist.", "error")
        return redirect(url_for("exports_page"))

    company = get_setting(conn, "company_name", "")
    stamp = date.today().isoformat()
    base = f"{name}-{stamp}"

    if fmt == "csv":
        return Response(
            exports.to_csv(rep, company),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base}.csv"'},
        )

    if fmt == "xlsx":
        try:
            data = exports.to_xlsx(rep, company)
        except ImportError:
            flash("Excel export needs the openpyxl package. Use CSV instead \u2014 "
                  "it opens in Excel too.", "error")
            return redirect(url_for("exports_page"))
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{base}.xlsx"'},
        )

    if fmt in ("print", "pdf", "html"):
        return render_template(
            "report_print.html", rep=rep, company=company,
            company_address=get_setting(conn, "company_address", ""),
            company_phone=get_setting(conn, "company_phone", ""),
            printed=datetime.now().strftime("%d %b %Y, %I:%M %p"),
        )

    flash("Unknown export format.", "error")
    return redirect(url_for("exports_page"))


@app.route("/roll-calculator")
@buy_required
def roll_calculator():
    """Works out how many bags come off a paper roll, and what one bag actually costs.
    Pure calculator - it reads your roll items to prefill costs but saves nothing."""
    conn = dbm.get_db()
    rolls = conn.execute(
        "SELECT id, name, unit, avg_cost, stock_qty FROM items "
        "WHERE is_active=1 AND item_type='raw_material' ORDER BY name"
    ).fetchall()
    bags = conn.execute(
        "SELECT id, name, unit, selling_price FROM items "
        "WHERE is_active=1 AND item_type='finished_good' ORDER BY name"
    ).fetchall()
    return render_template(
        "roll_calculator.html",
        rolls=[dict(r) for r in rolls],
        rolls_json=json.dumps([dict(r) for r in rolls]),
        bags=[dict(b) for b in bags],
        bags_json=json.dumps([dict(b) for b in bags]),
    )


@app.route("/items")
@login_required
def items_list():
    conn = dbm.get_db()
    type_filter = request.args.get("type", "")
    category_filter = request.args.get("category", "")
    q = "SELECT * FROM items WHERE is_active=1"
    params = []
    if type_filter in ("raw_material", "finished_good", "service"):
        q += " AND item_type=?"
        params.append(type_filter)
    if category_filter:
        q += " AND category=?"
        params.append(category_filter)
    q += " ORDER BY name"
    all_items = conn.execute(q, params).fetchall()
    # Page it: a very large catalogue would otherwise build one enormous page.
    PER_PAGE = 200
    total_rows = len(all_items)
    pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), pages))
    except ValueError:
        page = 1
    items = all_items[(page - 1) * PER_PAGE: page * PER_PAGE]
    categories = [
        r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM items WHERE is_active=1 AND category IS NOT NULL "
            "AND category != '' ORDER BY category"
        ).fetchall()
    ]
    return render_template(
        "items_list.html", items=items, type_filter=type_filter,
        category_filter=category_filter, categories=categories,
        page=page, pages=pages, total_rows=total_rows,
    )


@app.route("/items/new", methods=["GET", "POST"])
@buy_required
def item_new():
    conn = dbm.get_db()
    if request.method == "POST":
        conn.execute(
            "INSERT INTO items (name, item_type, category, unit, stock_qty, avg_cost, selling_price, reorder_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.form.get("name", "").strip(),
                request.form.get("item_type"),
                request.form.get("category", "").strip(),
                request.form.get("unit", "").strip(),
                parse_float(request.form.get("opening_stock"), 0),
                parse_float(request.form.get("opening_cost"), 0),
                parse_float(request.form.get("selling_price"), 0),
                parse_float(request.form.get("reorder_level"), 0),
            ),
        )
        conn.commit()
        flash("Item added.", "success")
        return redirect(url_for("items_list"))
    categories = [
        r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM items WHERE category IS NOT NULL AND category != '' ORDER BY category"
        ).fetchall()
    ]
    return render_template("item_form.html", item=None, categories=categories)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@buy_required
@change_required
def item_edit(item_id):
    conn = dbm.get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("items_list"))
    if request.method == "POST":
        item_type = request.form.get("item_type")
        # Stock and average cost normally maintained by purchases/production/sales, but they
        # can be corrected by hand here (stock count, wrong purchase price, opening balance).
        new_stock = parse_float(request.form.get("stock_qty"), item["stock_qty"])
        new_cost = parse_float(request.form.get("avg_cost"), item["avg_cost"])
        if item_type not in STOCK_TRACKED_TYPES:
            new_stock = 0.0
        stock_changed = abs(new_stock - item["stock_qty"]) > 0.0001
        cost_changed = abs(new_cost - item["avg_cost"]) > 0.0001

        conn.execute(
            "UPDATE items SET name=?, item_type=?, category=?, unit=?, selling_price=?, "
            "reorder_level=?, stock_qty=?, avg_cost=? WHERE id=?",
            (
                request.form.get("name", "").strip(),
                item_type,
                request.form.get("category", "").strip(),
                request.form.get("unit", "").strip(),
                parse_float(request.form.get("selling_price"), 0),
                parse_float(request.form.get("reorder_level"), 0),
                new_stock,
                new_cost,
                item_id,
            ),
        )
        conn.commit()
        if stock_changed or cost_changed:
            bits = []
            if stock_changed:
                bits.append(f"stock set to {new_stock:g} {request.form.get('unit', '').strip()}")
            if cost_changed:
                bits.append(f"average cost set to {inr(new_cost)}")
            flash("Item updated \u2014 " + " and ".join(bits) + ".", "success")
        else:
            flash("Item updated.", "success")
        return redirect(url_for("items_list"))
    categories = [
        r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM items WHERE category IS NOT NULL AND category != '' ORDER BY category"
        ).fetchall()
    ]
    return render_template("item_form.html", item=item, categories=categories)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@buy_required
@change_required
def item_delete(item_id):
    conn = dbm.get_db()
    conn.execute("UPDATE items SET is_active=0 WHERE id=?", (item_id,))
    conn.commit()
    flash("Item archived.", "success")
    return redirect(url_for("items_list"))


# ---------- purchases ----------

@app.route("/purchases")
@buy_required
def purchases_list():
    conn = dbm.get_db()
    rows = conn.execute(
        "SELECT p.*, i.name as item_name, i.unit as item_unit, s.name as supplier_link_name "
        "FROM purchases p JOIN items i ON i.id = p.item_id "
        "LEFT JOIN suppliers s ON s.id = p.supplier_id ORDER BY p.id DESC LIMIT 200"
    ).fetchall()
    return render_template("purchases_list.html", purchases=rows)


@app.route("/purchases/new", methods=["GET", "POST"])
@buy_required
def purchase_new():
    conn = dbm.get_db()
    if request.method == "POST":
        supplier_id = request.form.get("supplier_id") or None
        payment_status = request.form.get("payment_status", "paid")
        purchase_date = request.form.get("purchase_date") or date.today().isoformat()
        notes = request.form.get("notes", "").strip()
        try:
            lines = json.loads(request.form.get("lines_json", "[]"))
        except ValueError:
            lines = []

        prepared = []
        grand_total = 0.0
        for line in lines:
            try:
                item_id = int(line.get("item_id"))
            except (TypeError, ValueError):
                continue
            qty = parse_float(line.get("quantity"))
            unit_cost = parse_float(line.get("unit_cost"))
            if qty <= 0 or unit_cost < 0:
                continue
            total = round(qty * unit_cost, 2)
            grand_total += total
            prepared.append((item_id, qty, unit_cost, total))

        if not prepared:
            flash("Add at least one item with a quantity and cost.", "error")
            return redirect(url_for("purchase_new"))
        if payment_status in ("partial", "credit") and not supplier_id:
            flash("Select a supplier to buy on credit \u2014 needed to track what you owe them.", "error")
            return redirect(url_for("purchase_new"))

        if payment_status == "paid":
            paid_total = grand_total
        elif payment_status == "credit":
            paid_total = 0.0
        else:
            paid_total = max(0.0, min(parse_float(request.form.get("paid_amount"), 0), grand_total))

        supplier_name = ""
        if supplier_id:
            srow = conn.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            supplier_name = srow["name"] if srow else ""
        else:
            supplier_name = request.form.get("supplier_name", "").strip()

        batch_ref = datetime.now().strftime("PUR-%Y%m%d-%H%M%S")

        try:
            # One row per item - stock, average cost and supplier balances are all worked
            # out per item, so keeping that shape means none of it has to change. The batch
            # reference ties them together as one bill.
            remaining_paid = paid_total
            for n, (item_id, qty, unit_cost, total) in enumerate(prepared):
                last = n == len(prepared) - 1
                # Split what was paid across the items in proportion to their value, so each
                # row's paid amount and status are individually correct.
                share = remaining_paid if last else round(paid_total * (total / grand_total), 2)
                share = max(0.0, min(share, total, remaining_paid))
                remaining_paid = round(remaining_paid - share, 2)
                if share >= total - 0.005:
                    row_status = "paid"
                elif share <= 0.005:
                    row_status = "credit"
                else:
                    row_status = "partial"

                conn.execute(
                    "INSERT INTO purchases (item_id, supplier_id, supplier_name, quantity, unit_cost, "
                    "total_cost, paid_amount, payment_status, purchase_date, notes, batch_ref) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, supplier_id, supplier_name, qty, unit_cost, total, share,
                     row_status, purchase_date, notes, batch_ref),
                )
                update_stock_add(conn, item_id, qty, unit_cost)

            owed = round(grand_total - paid_total, 2)
            if owed > 0.005 and supplier_id:
                conn.execute(
                    "UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
                    (owed, supplier_id))
            conn.commit()

            msg = f"Purchase saved \u2014 {len(prepared)} item{'s' if len(prepared) != 1 else ''}, {inr(grand_total)}."
            if owed > 0.005:
                msg += f" {inr(owed)} added to what you owe {supplier_name}."
            log_action("recorded a purchase", "purchase", None, inr(grand_total))
            flash(msg, "success")
        except Exception as e:
            conn.rollback()
            flash(f"Could not save purchase: {e}", "error")
        return redirect(url_for("purchases_list"))

    items = conn.execute(
        "SELECT *, COALESCE(NULLIF(category,''), 'Uncategorized') as category_label "
        "FROM items WHERE is_active=1 ORDER BY category_label, name"
    ).fetchall()
    suppliers = bounded_supplier_list(conn)
    return render_template("purchase_form.html", items=items, suppliers=suppliers,
                           items_json=json.dumps(catalog_for_role(items)))


def recompute_avg_cost(conn, item_id):
    """Rebuild an item's average cost from every receipt it has ever had.

    Average cost only ever changes when stock comes IN (purchases and production output),
    so it is the total value received divided by the total quantity received. Recomputing
    from scratch like this means editing or deleting an old purchase leaves the cost
    correct, instead of carrying the mistake forever.
    """
    row = conn.execute("SELECT item_type FROM items WHERE id=?", (item_id,)).fetchone()
    if not row or row["item_type"] not in STOCK_TRACKED_TYPES:
        return
    qty = val = 0.0
    for r in conn.execute("SELECT quantity, unit_cost FROM purchases WHERE item_id=?", (item_id,)):
        qty += r["quantity"]; val += r["quantity"] * r["unit_cost"]
    for r in conn.execute(
            "SELECT output_qty, cost_per_unit FROM production_runs WHERE output_item_id=?", (item_id,)):
        qty += r["output_qty"]; val += r["output_qty"] * (r["cost_per_unit"] or 0)
    if qty > 0:
        conn.execute("UPDATE items SET avg_cost=? WHERE id=?", (val / qty, item_id))


def reverse_purchase(conn, pur):
    """Undo a purchase: take the stock back out and cancel what it added to the supplier."""
    if tracks_stock(conn, pur["item_id"]):
        conn.execute("UPDATE items SET stock_qty = stock_qty - ? WHERE id=?",
                     (pur["quantity"], pur["item_id"]))
    owed = pur["total_cost"] - (pur["paid_amount"] or 0)
    if owed > 0.005 and pur["supplier_id"]:
        conn.execute("UPDATE suppliers SET payable_balance = payable_balance - ? WHERE id=?",
                     (owed, pur["supplier_id"]))


@app.route("/purchases/<int:purchase_id>/edit", methods=["GET", "POST"])
@buy_required
@change_required
def purchase_edit(purchase_id):
    conn = dbm.get_db()
    pur = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    if not pur:
        flash("Purchase not found.", "error")
        return redirect(url_for("purchases_list"))

    if request.method == "POST":
        item_id = int(request.form.get("item_id"))
        qty = parse_float(request.form.get("quantity"))
        unit_cost = parse_float(request.form.get("unit_cost"))
        total_cost = qty * unit_cost
        supplier_id = request.form.get("supplier_id") or None
        payment_status = request.form.get("payment_status", "paid")
        if qty <= 0 or unit_cost < 0:
            flash("Enter a valid quantity and cost.", "error")
            return redirect(url_for("purchase_edit", purchase_id=purchase_id))
        if payment_status in ("partial", "credit") and not supplier_id:
            flash("Select a supplier for a credit purchase.", "error")
            return redirect(url_for("purchase_edit", purchase_id=purchase_id))

        if payment_status == "paid":
            paid = total_cost
        elif payment_status == "credit":
            paid = 0.0
        else:
            paid = max(0.0, min(parse_float(request.form.get("paid_amount"), 0), total_cost))

        supplier_name = ""
        if supplier_id:
            srow = conn.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            supplier_name = srow["name"] if srow else ""
        else:
            supplier_name = request.form.get("supplier_name", "").strip()

        try:
            reverse_purchase(conn, pur)          # undo the old version
            conn.execute(
                "UPDATE purchases SET item_id=?, supplier_id=?, supplier_name=?, quantity=?, "
                "unit_cost=?, total_cost=?, paid_amount=?, payment_status=?, purchase_date=?, "
                "notes=? WHERE id=?",
                (item_id, supplier_id, supplier_name, qty, unit_cost, total_cost, paid,
                 payment_status, request.form.get("purchase_date") or pur["purchase_date"],
                 request.form.get("notes", "").strip(), purchase_id))
            if tracks_stock(conn, item_id):
                conn.execute("UPDATE items SET stock_qty = stock_qty + ? WHERE id=?", (qty, item_id))
            owed = total_cost - paid
            if owed > 0.005 and supplier_id:
                conn.execute("UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
                             (owed, supplier_id))
            recompute_avg_cost(conn, item_id)
            if item_id != pur["item_id"]:
                recompute_avg_cost(conn, pur["item_id"])
            conn.commit()
            log_action("edited a purchase", "purchase", purchase_id, inr(total_cost))
            flash("Purchase updated. Stock, cost and supplier balance corrected.", "success")
            return redirect(url_for("purchases_list"))
        except Exception as e:
            conn.rollback()
            flash(f"Could not update purchase: {e}", "error")

    items = conn.execute(
        "SELECT *, COALESCE(NULLIF(category,''), 'Uncategorized') as category_label "
        "FROM items WHERE is_active=1 ORDER BY category_label, name").fetchall()
    suppliers = bounded_supplier_list(conn, pur["supplier_id"])
    return render_template("purchase_edit_form.html", items=items, suppliers=suppliers, purchase=pur)


@app.route("/purchases/<int:purchase_id>/delete", methods=["POST"])
@buy_required
@change_required
def purchase_delete(purchase_id):
    conn = dbm.get_db()
    pur = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    if not pur:
        flash("Purchase not found.", "error")
        return redirect(url_for("purchases_list"))
    try:
        reverse_purchase(conn, pur)
        conn.execute("DELETE FROM purchases WHERE id=?", (purchase_id,))
        recompute_avg_cost(conn, pur["item_id"])
        conn.commit()
        log_action("deleted a purchase", "purchase", purchase_id, inr(pur["total_cost"]))
        flash("Purchase deleted. Stock, cost and supplier balance put back.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not delete: {e}", "error")
    return redirect(url_for("purchases_list"))


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@admin_required
@change_required
def expense_edit(expense_id):
    conn = dbm.get_db()
    exp = conn.execute("SELECT * FROM expenses WHERE id=? AND id!=0", (expense_id,)).fetchone()
    if not exp:
        flash("Expense not found.", "error")
        return redirect(url_for("expenses_list"))
    linked = conn.execute(
        "SELECT id FROM employee_advances WHERE expense_id=?", (expense_id,)).fetchone()
    if linked:
        flash("This expense is a staff advance. Change it on the employee's page so the "
              "advance and the expense stay in step.", "error")
        return redirect(url_for("expenses_list"))

    if request.method == "POST":
        amount = parse_float(request.form.get("amount"))
        if amount <= 0:
            flash("Enter a valid amount.", "error")
            return redirect(url_for("expense_edit", expense_id=expense_id))
        conn.execute(
            "UPDATE expenses SET category=?, amount=?, expense_date=?, notes=? WHERE id=?",
            (request.form.get("category", "").strip(), amount,
             request.form.get("expense_date") or exp["expense_date"],
             request.form.get("notes", "").strip(), expense_id))
        conn.commit()
        log_action("edited an expense", "expense", expense_id, inr(amount))
        flash("Expense updated.", "success")
        return redirect(url_for("expenses_list"))
    return render_template("expense_form.html", expense=exp)


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@admin_required
@change_required
def expense_delete(expense_id):
    conn = dbm.get_db()
    exp = conn.execute("SELECT * FROM expenses WHERE id=? AND id!=0", (expense_id,)).fetchone()
    if not exp:
        flash("Expense not found.", "error")
        return redirect(url_for("expenses_list"))
    if conn.execute("SELECT id FROM employee_advances WHERE expense_id=?", (expense_id,)).fetchone():
        flash("This is a staff advance \u2014 remove it from the employee's page instead.", "error")
        return redirect(url_for("expenses_list"))
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    log_action("deleted an expense", "expense", expense_id, inr(exp["amount"]))
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses_list"))


# ---------- roll register ----------
#
# Kraft and nonwoven are bought by weight and sold by the piece. The mill bills a lot
# total, so cost per kg is the only real unit price - individual rolls of the same size
# have different weights but the same rate. So:
#   - the ITEM holds stock in kg and a weighted average cost per kg
#   - this register holds the physical rolls, with the weight written on each one
# Consuming a roll converts kg to money using that roll's own lot rate.

# ---------- order photos ----------
#
# Reference pictures for a job: the artwork the customer sent, and a photo of the
# finished bag they approved. Stored as files beside the database - only the filename
# goes in the table, so erp.db stays small and quick to back up.

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@app.route("/orders/<int:order_id>/images", methods=["POST"])
@login_required
def order_image_add(order_id):
    conn = dbm.get_db()
    order = conn.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("orders_list"))

    files = request.files.getlist("photos")
    kind = request.form.get("kind", "reference")
    caption = (request.form.get("caption") or "").strip()
    saved = skipped = 0

    paths.ensure_dirs()
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT:
            skipped += 1
            continue
        data = f.read()
        if not data or len(data) > MAX_IMAGE_BYTES:
            skipped += 1
            continue
        # Never trust the name the browser sends - build our own.
        name = f"order{order_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}{ext}"
        with open(os.path.join(paths.UPLOAD_DIR, name), "wb") as out:
            out.write(data)
        conn.execute(
            "INSERT INTO order_images (order_id, filename, caption, kind) VALUES (?,?,?,?)",
            (order_id, name, caption, kind if kind in ("reference", "approved") else "reference"))
        saved += 1
    conn.commit()

    if saved:
        flash(f"{saved} photo{'s' if saved != 1 else ''} added to order #{order_id}."
              + (f" {skipped} skipped." if skipped else ""), "success")
    else:
        flash("Nothing was saved. Pictures only (jpg, png, webp, heic), up to 8 MB each.", "error")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/images/webcam", methods=["POST"])
@login_required
def order_image_webcam(order_id):
    """Save a still taken with the laptop webcam.

    Phones handle this with capture= on a file input, but laptops ignore that, so the
    picture is grabbed in the browser and posted here as base64.
    """
    import base64
    conn = dbm.get_db()
    if not conn.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone():
        return {"error": "Order not found."}, 404

    data_url = request.form.get("image_data", "")
    if not data_url.startswith("data:image/"):
        return {"error": "That was not a picture."}, 400
    try:
        header, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64)
    except (ValueError, TypeError):
        return {"error": "Could not read the picture."}, 400
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return {"error": "Picture too large."}, 400

    kind = request.form.get("kind", "reference")
    caption = (request.form.get("caption") or "").strip()
    paths.ensure_dirs()
    name = f"order{order_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}.jpg"
    with open(os.path.join(paths.UPLOAD_DIR, name), "wb") as out:
        out.write(raw)
    conn.execute(
        "INSERT INTO order_images (order_id, filename, caption, kind) VALUES (?,?,?,?)",
        (order_id, name, caption, kind if kind in ("reference", "approved") else "reference"))
    conn.commit()
    return {"ok": True, "filename": name}


@app.route("/orders/images/<int:image_id>/delete", methods=["POST"])
@login_required
@change_required
def order_image_delete(image_id):
    conn = dbm.get_db()
    img = conn.execute("SELECT * FROM order_images WHERE id=?", (image_id,)).fetchone()
    if not img:
        flash("Photo not found.", "error")
        return redirect(url_for("orders_list"))
    order_id = img["order_id"]
    try:
        os.remove(os.path.join(paths.UPLOAD_DIR, img["filename"]))
    except OSError:
        pass
    conn.execute("DELETE FROM order_images WHERE id=?", (image_id,))
    conn.commit()
    flash("Photo removed.", "success")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """Serve a stored photo.

    Only ever serves files we ourselves wrote into the uploads folder: the name must
    match a row in order_images. That is stricter than checking the path, and means a
    crafted address cannot reach anything else on the disk.
    """
    from flask import send_from_directory, abort
    conn = dbm.get_db()
    known = conn.execute(
        "SELECT 1 FROM order_images WHERE filename=? "
        "UNION ALL SELECT 1 FROM items WHERE photo=?", (filename, filename)).fetchone()
    if not known:
        abort(404)
    try:
        return send_from_directory(paths.UPLOAD_DIR, filename)
    except Exception:
        abort(404)


@app.route("/catalogue")
@login_required
def catalogue():
    """A visual list of what you sell - picture, name, price, stock.

    Made for showing a customer across the counter, or checking on the phone what is
    actually in stock, rather than for entering anything.
    """
    conn = dbm.get_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "")
    in_stock_only = request.args.get("stock", "") == "yes"

    sql = ("SELECT * FROM items WHERE is_active=1 AND item_type != 'raw_material'")
    params = []
    if q:
        sql += " AND name LIKE ?"
        params.append(f"%{q}%")
    if category:
        sql += " AND COALESCE(NULLIF(category,''),'Uncategorized') = ?"
        params.append(category)
    if in_stock_only:
        sql += " AND (stock_qty > 0 OR item_type = 'service')"
    sql += " ORDER BY COALESCE(NULLIF(category,''),'Uncategorized'), name LIMIT 400"
    items = conn.execute(sql, params).fetchall()

    categories = [r["c"] for r in conn.execute(
        "SELECT DISTINCT COALESCE(NULLIF(category,''),'Uncategorized') c FROM items "
        "WHERE is_active=1 AND item_type != 'raw_material' ORDER BY c")]

    return render_template("catalogue.html", items=items, categories=categories,
                           q=q, category=category, in_stock_only=in_stock_only)


@app.route("/items/<int:item_id>/photo", methods=["POST"])
@buy_required
@change_required
def item_photo(item_id):
    conn = dbm.get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("catalogue"))

    f = request.files.get("photo")
    if not f or not f.filename:
        flash("No picture chosen.", "error")
        return redirect(request.referrer or url_for("catalogue"))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        flash("Pictures only (jpg, png, webp, heic).", "error")
        return redirect(request.referrer or url_for("catalogue"))
    data = f.read()
    if not data or len(data) > MAX_IMAGE_BYTES:
        flash("That picture is too large - 8 MB is the limit.", "error")
        return redirect(request.referrer or url_for("catalogue"))

    paths.ensure_dirs()
    name = f"item{item_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}{ext}"
    with open(os.path.join(paths.UPLOAD_DIR, name), "wb") as out:
        out.write(data)
    # Replacing a picture removes the old file, so the folder does not fill up.
    if item["photo"]:
        try:
            os.remove(os.path.join(paths.UPLOAD_DIR, item["photo"]))
        except OSError:
            pass
    conn.execute("UPDATE items SET photo=? WHERE id=?", (name, item_id))
    conn.commit()
    log_action("added an item picture", "item", item_id, item["name"])
    flash(f"Picture saved for {item['name']}.", "success")
    return redirect(request.referrer or url_for("catalogue"))


@app.route("/items/<int:item_id>/photo/delete", methods=["POST"])
@buy_required
@change_required
def item_photo_delete(item_id):
    conn = dbm.get_db()
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item and item["photo"]:
        try:
            os.remove(os.path.join(paths.UPLOAD_DIR, item["photo"]))
        except OSError:
            pass
        conn.execute("UPDATE items SET photo=NULL WHERE id=?", (item_id,))
        conn.commit()
        flash("Picture removed.", "success")
    return redirect(request.referrer or url_for("catalogue"))


@app.route("/rolls")
@buy_required
def rolls_list():
    conn = dbm.get_db()
    item_filter = request.args.get("item", "")
    status = request.args.get("status", "in_stock")

    q = ("SELECT r.*, i.name item_name, i.unit item_unit FROM material_rolls r "
         "JOIN items i ON i.id = r.item_id WHERE 1=1")
    params = []
    if item_filter:
        q += " AND r.item_id = ?"
        params.append(item_filter)
    if status in ("in_stock", "part_used", "used", "scrapped"):
        q += " AND r.status = ?"
        params.append(status)
    q += " ORDER BY i.name, r.received_date, r.id"
    rolls = conn.execute(q, params).fetchall()

    # Summary per size: how many rolls, how many kg, what it is worth
    summary = conn.execute(
        "SELECT i.id, i.name, i.unit, COUNT(*) n, COALESCE(SUM(r.remaining_kg),0) kg, "
        "COALESCE(SUM(r.remaining_kg * r.cost_per_kg),0) value "
        "FROM material_rolls r JOIN items i ON i.id = r.item_id "
        "WHERE r.status IN ('in_stock','part_used') GROUP BY r.item_id ORDER BY i.name"
    ).fetchall()

    raw_items = conn.execute(
        "SELECT id, name, unit, stock_qty, avg_cost FROM items "
        "WHERE is_active=1 AND item_type='raw_material' ORDER BY name").fetchall()

    return render_template("rolls_list.html", rolls=rolls, summary=summary,
                           raw_items=raw_items, item_filter=item_filter, status=status)


@app.route("/rolls/add", methods=["GET", "POST"])
@buy_required
def rolls_add():
    """Log the rolls that came in on a lot, with the weight written on each.

    The lot total and rate come from the bill; the individual weights come off the rolls.
    Stock and cost are updated from the total, exactly like any other purchase.
    """
    conn = dbm.get_db()
    if request.method == "POST":
        item_id = request.form.get("item_id")
        if not item_id:
            flash("Choose which material these rolls are.", "error")
            return redirect(url_for("rolls_add"))
        item_id = int(item_id)

        try:
            rolls = json.loads(request.form.get("rolls_json", "[]"))
        except ValueError:
            rolls = []
        prepared = []
        total_kg = 0.0
        for r in rolls:
            w = parse_float(r.get("weight_kg"))
            if w <= 0:
                continue
            prepared.append(((r.get("roll_no") or "").strip(), w))
            total_kg += w
        if not prepared:
            flash("Add at least one roll with a weight.", "error")
            return redirect(url_for("rolls_add"))

        lot_cost = parse_float(request.form.get("lot_cost"), 0)
        rate_entered = parse_float(request.form.get("cost_per_kg"), 0)
        # Either figure can be given - whichever you have off the bill.
        if rate_entered > 0:
            cost_per_kg = rate_entered
            lot_cost = round(total_kg * cost_per_kg, 2)
        elif lot_cost > 0:
            cost_per_kg = lot_cost / total_kg
        else:
            flash("Enter either the rate per kg or the total cost of the lot.", "error")
            return redirect(url_for("rolls_add"))

        supplier_id = request.form.get("supplier_id") or None
        payment_status = request.form.get("payment_status", "paid")
        received = request.form.get("received_date") or date.today().isoformat()
        if payment_status in ("partial", "credit") and not supplier_id:
            flash("Select a supplier to buy on credit.", "error")
            return redirect(url_for("rolls_add"))
        if payment_status == "paid":
            paid = lot_cost
        elif payment_status == "credit":
            paid = 0.0
        else:
            paid = max(0.0, min(parse_float(request.form.get("paid_amount"), 0), lot_cost))

        supplier_name = ""
        if supplier_id:
            srow = conn.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            supplier_name = srow["name"] if srow else ""
        else:
            supplier_name = request.form.get("supplier_name", "").strip()

        try:
            cur = conn.execute(
                "INSERT INTO purchases (item_id, supplier_id, supplier_name, quantity, unit_cost, "
                "total_cost, paid_amount, payment_status, purchase_date, notes, batch_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, supplier_id, supplier_name, total_kg, cost_per_kg, lot_cost, paid,
                 payment_status, received,
                 f"{len(prepared)} rolls \u2014 " + (request.form.get("notes", "").strip() or "lot"),
                 datetime.now().strftime("LOT-%Y%m%d-%H%M%S")),
            )
            purchase_id = cur.lastrowid
            for roll_no, w in prepared:
                conn.execute(
                    "INSERT INTO material_rolls (item_id, purchase_id, roll_no, weight_kg, "
                    "remaining_kg, cost_per_kg, status, received_date) "
                    "VALUES (?,?,?,?,?,?,'in_stock',?)",
                    (item_id, purchase_id, roll_no, w, w, cost_per_kg, received))
            update_stock_add(conn, item_id, total_kg, cost_per_kg)
            owed = round(lot_cost - paid, 2)
            if owed > 0.005 and supplier_id:
                conn.execute("UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
                             (owed, supplier_id))
            conn.commit()
            msg = (f"{len(prepared)} rolls logged \u2014 {total_kg:,.2f} kg at "
                   f"{inr(cost_per_kg)}/kg, {inr(lot_cost)} total.")
            if owed > 0.005:
                msg += f" {inr(owed)} added to what you owe {supplier_name}."
            log_action("logged rolls", "rolls", item_id, f"{total_kg:,.2f} kg")
            flash(msg, "success")
            return redirect(url_for("rolls_list", item=item_id))
        except Exception as e:
            conn.rollback()
            flash(f"Could not save the rolls: {e}", "error")

    raw_items = conn.execute(
        "SELECT * FROM items WHERE is_active=1 AND item_type='raw_material' ORDER BY name").fetchall()
    suppliers = bounded_supplier_list(conn)
    return render_template("rolls_add.html", raw_items=raw_items, suppliers=suppliers)


@app.route("/rolls/quick-item", methods=["POST"])
@buy_required
def roll_item_quick_add():
    """Create a roll material without leaving the Log Rolls page.

    The item has to exist before rolls can point at it, but making someone go to
    Items, create it, then come back is an unnecessary detour - especially when a
    lorry is waiting.
    """
    conn = dbm.get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        return {"error": "Name is required."}, 400
    unit = (request.form.get("unit") or "kg").strip() or "kg"
    existing = conn.execute(
        "SELECT id FROM items WHERE lower(name)=lower(?) AND is_active=1", (name,)).fetchone()
    if existing:
        return {"id": existing["id"], "name": name, "existing": True}
    cur = conn.execute(
        "INSERT INTO items (name, item_type, category, unit, stock_qty, avg_cost, "
        "selling_price, reorder_level, is_active) VALUES (?,?,?,?,0,0,0,?,1)",
        (name, "raw_material", (request.form.get("category") or "Rolls").strip(), unit,
         parse_float(request.form.get("reorder_level"), 0)))
    conn.commit()
    return {"id": cur.lastrowid, "name": name, "existing": False}


@app.route("/rolls/<int:roll_id>/use", methods=["POST"])
@buy_required
def roll_use(roll_id):
    """Mark a roll used - fully, or partly with the rest going back on the rack."""
    conn = dbm.get_db()
    roll = conn.execute("SELECT * FROM material_rolls WHERE id=?", (roll_id,)).fetchone()
    if not roll:
        flash("Roll not found.", "error")
        return redirect(url_for("rolls_list"))
    if roll["status"] in ("used", "scrapped"):
        flash("That roll is already finished.", "error")
        return redirect(url_for("rolls_list"))

    used = parse_float(request.form.get("used_kg"), 0) or roll["remaining_kg"]
    used = max(0.0, min(used, roll["remaining_kg"]))
    if used <= 0:
        flash("Enter how many kg were used.", "error")
        return redirect(url_for("rolls_list"))

    left = round(roll["remaining_kg"] - used, 3)
    status = "used" if left <= 0.001 else "part_used"
    conn.execute(
        "UPDATE material_rolls SET remaining_kg=?, status=?, used_date=? WHERE id=?",
        (max(0.0, left), status, date.today().isoformat() if status == "used" else None, roll_id))
    # Stock is held in kg on the item, so take the kg out there too.
    update_stock_remove(conn, roll["item_id"], used)
    conn.commit()
    log_action("used a roll", "roll", roll_id, f"{used:,.2f} kg")
    flash(f"{used:,.2f} kg used from roll {roll['roll_no'] or '#' + str(roll_id)} "
          f"\u2014 {inr(used * roll['cost_per_kg'])} of material.", "success")
    return redirect(url_for("rolls_list", item=roll["item_id"]))


@app.route("/rolls/<int:roll_id>/delete", methods=["POST"])
@buy_required
@change_required
def roll_delete(roll_id):
    conn = dbm.get_db()
    roll = conn.execute("SELECT * FROM material_rolls WHERE id=?", (roll_id,)).fetchone()
    if not roll:
        flash("Roll not found.", "error")
        return redirect(url_for("rolls_list"))
    # Only the register entry goes - the purchase and its stock stay, since the material
    # was genuinely bought. Correct the purchase itself if the weight was wrong.
    conn.execute("DELETE FROM material_rolls WHERE id=?", (roll_id,))
    conn.commit()
    flash("Roll removed from the register. The purchase and stock are unchanged.", "success")
    return redirect(url_for("rolls_list", item=roll["item_id"]))


# ---------- suppliers (who you buy from / what you owe them) ----------

@app.route("/suppliers/search.json")
@buy_required
def supplier_search_json():
    conn = dbm.get_db()
    q = (request.args.get("q") or "").strip()
    if q:
        rows = conn.execute(
            "SELECT id, name, phone, payable_balance FROM suppliers "
            "WHERE is_active=1 AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 50",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, phone, payable_balance FROM suppliers "
            "WHERE is_active=1 ORDER BY name LIMIT 50"
        ).fetchall()
    results = []
    for r in rows:
        label = r["name"]
        if r["phone"]:
            label += f" \u2014 {r['phone']}"
        if r["payable_balance"] and r["payable_balance"] > 0.005:
            label += f" (you owe {inr(r['payable_balance'])})"
        results.append({"value": str(r["id"]), "label": label})
    return {"results": results}


@app.route("/suppliers")
@buy_required
def suppliers_list():
    conn = dbm.get_db()
    all_rows = conn.execute(
        "SELECT * FROM suppliers WHERE is_active=1 "
        "ORDER BY payable_balance DESC, name").fetchall()
    PER_PAGE = 200
    total_rows = len(all_rows)
    pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), pages))
    except ValueError:
        page = 1
    rows = all_rows[(page - 1) * PER_PAGE: page * PER_PAGE]
    total_payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE is_active=1"
    ).fetchone()["t"]
    return render_template("suppliers_list.html", suppliers=rows, total_payable=total_payable,
                           page=page, pages=pages, total_rows=total_rows)


@app.route("/suppliers/new", methods=["GET", "POST"])
@buy_required
def supplier_new():
    if request.method == "POST":
        conn = dbm.get_db()
        name = request.form.get("name", "").strip()
        if not name:
            flash("Supplier name is required.", "error")
            return render_template("supplier_form.html", supplier=None)
        conn.execute(
            "INSERT INTO suppliers (name, phone, address, payable_balance) VALUES (?, ?, ?, ?)",
            (
                name, request.form.get("phone", "").strip(),
                request.form.get("address", "").strip(),
                max(0.0, parse_float(request.form.get("opening_balance"), 0)),
            ),
        )
        conn.commit()
        opening = max(0.0, parse_float(request.form.get("opening_balance"), 0))
        if opening > 0.005:
            flash(
                f"Supplier added with an opening balance of {inr(opening)} owed. "
                "No stock or cash was changed \u2014 record a payment when you actually pay them.",
                "success",
            )
        else:
            flash("Supplier added.", "success")
        return redirect(url_for("suppliers_list"))
    return render_template("supplier_form.html", supplier=None)


@app.route("/suppliers/quick-add", methods=["POST"])
@buy_required
def supplier_quick_add():
    """Used by the + New supplier box on the purchase form so a supplier can be added
    without leaving the page - same idea as the customer quick-add."""
    conn = dbm.get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        return {"error": "Name is required."}, 400
    cur = conn.execute(
        "INSERT INTO suppliers (name, phone) VALUES (?, ?)",
        (name, (request.form.get("phone") or "").strip()),
    )
    conn.commit()
    return {"id": cur.lastrowid, "name": name}


@app.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@buy_required
@change_required
def supplier_edit(supplier_id):
    conn = dbm.get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return render_template("supplier_form.html", supplier=supplier)
        conn.execute(
            "UPDATE suppliers SET name=?, phone=?, address=? WHERE id=?",
            (name, request.form.get("phone", "").strip(), request.form.get("address", "").strip(), supplier_id),
        )
        conn.commit()
        flash("Supplier updated.", "success")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))
    return render_template("supplier_form.html", supplier=supplier)


@app.route("/suppliers/<int:supplier_id>")
@buy_required
def supplier_detail(supplier_id):
    conn = dbm.get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers_list"))
    purchases = conn.execute(
        "SELECT p.*, i.name as item_name, i.unit as item_unit FROM purchases p "
        "JOIN items i ON i.id = p.item_id WHERE p.supplier_id=? ORDER BY p.id DESC", (supplier_id,)
    ).fetchall()
    payments = conn.execute(
        "SELECT * FROM supplier_payments WHERE supplier_id=? ORDER BY id DESC", (supplier_id,)
    ).fetchall()
    return render_template("supplier_detail.html", supplier=supplier, purchases=purchases, payments=payments)


@app.route("/suppliers/<int:supplier_id>/payment", methods=["POST"])
@buy_required
def supplier_payment(supplier_id):
    conn = dbm.get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Enter a valid payment amount.", "error")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))
    conn.execute(
        "INSERT INTO supplier_payments (supplier_id, amount, payment_date, notes) VALUES (?, ?, ?, ?)",
        (supplier_id, amount, request.form.get("payment_date") or date.today().isoformat(),
         request.form.get("notes", "").strip()),
    )
    conn.execute("UPDATE suppliers SET payable_balance = payable_balance - ? WHERE id=?", (amount, supplier_id))
    conn.commit()
    log_action("paid a supplier", "supplier", supplier_id, inr(amount))
    flash(f"Payment of {inr(amount)} recorded.", "success")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


@app.route("/suppliers/<int:supplier_id>/payment/<int:payment_id>/edit", methods=["POST"])
@buy_required
@change_required
def supplier_payment_edit(supplier_id, payment_id):
    """Change a recorded payment.

    A payment touches exactly two things: what you owe the supplier, and cash in hand.
    So editing means undoing the old amount and applying the new one - the balance moves
    by the difference, and the Cash Book follows automatically because it reads straight
    from this table.
    """
    conn = dbm.get_db()
    pay = conn.execute(
        "SELECT * FROM supplier_payments WHERE id=? AND supplier_id=?",
        (payment_id, supplier_id)).fetchone()
    if not pay:
        flash("Payment not found.", "error")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    amount = parse_float(request.form.get("amount"), 0)
    if amount <= 0:
        flash("Enter a valid amount.", "error")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))
    pay_date = request.form.get("payment_date") or pay["payment_date"]
    notes = request.form.get("notes", "").strip()

    diff = amount - pay["amount"]
    conn.execute(
        "UPDATE supplier_payments SET amount=?, payment_date=?, notes=? WHERE id=?",
        (amount, pay_date, notes, payment_id))
    # Paying more reduces what you owe further; paying less puts it back.
    conn.execute(
        "UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
        (-diff, supplier_id))
    conn.commit()
    if abs(diff) > 0.005:
        flash(f"Payment updated to {inr(amount)}. What you owe changed by "
              f"{inr(abs(diff))} {'less' if diff > 0 else 'more'}, and cash in hand "
              f"followed.", "success")
    else:
        flash("Payment updated.", "success")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


@app.route("/suppliers/<int:supplier_id>/payment/<int:payment_id>/delete", methods=["POST"])
@buy_required
@change_required
def supplier_payment_delete(supplier_id, payment_id):
    """Remove a payment that never happened - the money goes back in the drawer and the
    supplier is owed it again."""
    conn = dbm.get_db()
    pay = conn.execute(
        "SELECT * FROM supplier_payments WHERE id=? AND supplier_id=?",
        (payment_id, supplier_id)).fetchone()
    if not pay:
        flash("Payment not found.", "error")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    conn.execute("DELETE FROM supplier_payments WHERE id=?", (payment_id,))
    conn.execute(
        "UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
        (pay["amount"], supplier_id))
    conn.commit()
    log_action("deleted a supplier payment", "supplier", supplier_id, inr(pay["amount"]))
    flash(f"Payment of {inr(pay['amount'])} on {pay['payment_date']} removed. "
          f"That amount is owed again and back in cash in hand.", "success")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


@app.route("/suppliers/<int:supplier_id>/adjust-balance", methods=["POST"])
@admin_required
def supplier_adjust_balance(supplier_id):
    conn = dbm.get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers_list"))
    new_balance = parse_float(request.form.get("new_balance"), supplier["payable_balance"])
    conn.execute("UPDATE suppliers SET payable_balance=? WHERE id=?", (new_balance, supplier_id))
    conn.commit()
    flash(f"Amount owed corrected to {inr(new_balance)}.", "success")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


@app.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
@buy_required
@change_required
def supplier_delete(supplier_id):
    conn = dbm.get_db()
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers_list"))
    has_history = conn.execute(
        "SELECT COUNT(*) c FROM purchases WHERE supplier_id=?", (supplier_id,)
    ).fetchone()["c"]
    if has_history:
        conn.execute("UPDATE suppliers SET is_active=0 WHERE id=?", (supplier_id,))
        conn.commit()
        flash(
            "This supplier has purchase history, so they've been archived rather than deleted \u2014 "
            "records stay intact but they're hidden from lists and the purchase form.", "success",
        )
        return redirect(url_for("suppliers_list"))
    conn.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
    conn.commit()
    flash("Supplier deleted.", "success")
    return redirect(url_for("suppliers_list"))


# ---------- production ----------

@app.route("/production")
@buy_required
def production_list():
    conn = dbm.get_db()
    rows = conn.execute(
        "SELECT pr.*, i.name as output_name, i.unit as output_unit FROM production_runs pr "
        "JOIN items i ON i.id = pr.output_item_id ORDER BY pr.id DESC LIMIT 200"
    ).fetchall()
    return render_template("production_list.html", runs=rows)


@app.route("/production/<int:run_id>")
@buy_required
def production_detail(run_id):
    conn = dbm.get_db()
    run = conn.execute(
        "SELECT pr.*, i.name as output_name, i.unit as output_unit FROM production_runs pr "
        "JOIN items i ON i.id = pr.output_item_id WHERE pr.id=?", (run_id,)
    ).fetchone()
    consumption = conn.execute(
        "SELECT pc.*, i.name as item_name, i.unit as item_unit FROM production_consumption pc "
        "JOIN items i ON i.id = pc.raw_item_id WHERE pc.production_id=?", (run_id,)
    ).fetchall()
    return render_template("production_detail.html", run=run, consumption=consumption)


@app.route("/production/new", methods=["GET", "POST"])
@buy_required
def production_new():
    conn = dbm.get_db()
    if request.method == "POST":
        output_item_id = int(request.form.get("output_item_id"))
        output_qty = parse_float(request.form.get("output_qty"))
        labor_cost = parse_float(request.form.get("labor_cost"))
        other_cost = parse_float(request.form.get("other_cost"))
        raw_ids = request.form.getlist("raw_item_id[]")
        raw_qtys = request.form.getlist("raw_qty[]")
        raw_rolls = request.form.getlist("raw_roll_id[]")   # optional: which roll it came off

        if output_qty <= 0:
            flash("Output quantity must be greater than zero.", "error")
            return redirect(url_for("production_new"))
        if not raw_ids:
            flash("Add at least one raw material consumed.", "error")
            return redirect(url_for("production_new"))

        try:
            total_material_cost = 0.0
            consumption_rows = []
            rolls_used = []
            for idx, (rid, rqty) in enumerate(zip(raw_ids, raw_qtys)):
                if not rid:
                    continue
                rid = int(rid)
                rqty = parse_float(rqty)
                roll_id = raw_rolls[idx] if idx < len(raw_rolls) else ""
                if roll_id:
                    # A roll was picked from the register - use its actual remaining weight,
                    # so you never have to work the kg out yourself.
                    roll = conn.execute(
                        "SELECT * FROM material_rolls WHERE id=?", (roll_id,)).fetchone()
                    if roll:
                        if rqty <= 0:
                            rqty = roll["remaining_kg"]
                        rqty = min(rqty, roll["remaining_kg"])
                        rolls_used.append((roll["id"], rqty))
                if rqty <= 0:
                    continue
                raw_item = conn.execute("SELECT * FROM items WHERE id=?", (rid,)).fetchone()
                line_cost = rqty * raw_item["avg_cost"]
                total_material_cost += line_cost
                consumption_rows.append((rid, rqty, raw_item["avg_cost"], line_cost))

            total_cost = total_material_cost + labor_cost + other_cost
            cost_per_unit = total_cost / output_qty if output_qty else 0

            # A run with (almost) no material cost is nearly always a typing mistake -
            # entering 1 instead of 6 kg, or picking a raw material with no cost on it.
            # Left alone it writes a near-zero cost onto the finished item, which then
            # makes every later sale of that item look far more profitable than it is.
            if total_material_cost <= 0.005:
                flash(
                    "Nothing was saved. The material cost worked out to zero, which usually means "
                    "the quantity used is wrong, or the raw material has no cost recorded yet. "
                    "Check how much roll this run actually consumed \u2014 remember it is in the "
                    "raw material's own unit (kg for rolls), not rolls or bags.", "error")
                conn.rollback()
                return redirect(url_for("production_new"))

            # Optional - left blank (None) rather than 0 when not given, so an unlogged
            # run is distinguishable from a run that genuinely wasted nothing.
            waste_raw = (request.form.get("waste_qty") or "").strip()
            waste_qty = float(waste_raw) if waste_raw else None

            cur = conn.execute(
                "INSERT INTO production_runs (output_item_id, output_qty, labor_cost, other_cost, "
                "total_material_cost, cost_per_unit, production_date, notes, waste_qty) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    output_item_id, output_qty, labor_cost, other_cost,
                    total_material_cost, cost_per_unit,
                    request.form.get("production_date") or date.today().isoformat(),
                    request.form.get("notes", "").strip(), waste_qty,
                ),
            )
            run_id = cur.lastrowid

            for rid, rqty, ucost, lcost in consumption_rows:
                conn.execute(
                    "INSERT INTO production_consumption (production_id, raw_item_id, qty_consumed, "
                    "unit_cost_at_time, line_cost) VALUES (?,?,?,?,?)",
                    (run_id, rid, rqty, ucost, lcost),
                )
                update_stock_remove(conn, rid, rqty)

            update_stock_add(conn, output_item_id, output_qty, cost_per_unit)

            # Mark whichever rolls were picked as used, so the register stays honest.
            for roll_id, used in rolls_used:
                roll = conn.execute("SELECT * FROM material_rolls WHERE id=?", (roll_id,)).fetchone()
                if not roll:
                    continue
                left = round(roll["remaining_kg"] - used, 3)
                conn.execute(
                    "UPDATE material_rolls SET remaining_kg=?, status=?, used_date=? WHERE id=?",
                    (max(0.0, left), "used" if left <= 0.001 else "part_used",
                     date.today().isoformat() if left <= 0.001 else None, roll_id))

            conn.commit()
            out_item = conn.execute("SELECT name, unit FROM items WHERE id=?", (output_item_id,)).fetchone()
            flash(
                f"Production saved \u2014 {output_qty:g} {out_item['unit']} of {out_item['name']} "
                f"at {inr(cost_per_unit)} each. Material {inr(total_material_cost)} + labour "
                f"{inr(labor_cost + other_cost)} = {inr(total_cost)}.", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Could not save production run: {e}", "error")
        return redirect(url_for("production_list"))

    items = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
    raw_items = [dict(i) for i in items if i["item_type"] == "raw_material"]
    finished_items = [dict(i) for i in items if i["item_type"] == "finished_good"]
    rolls = conn.execute(
        "SELECT r.*, i.name item_name FROM material_rolls r JOIN items i ON i.id = r.item_id "
        "WHERE r.status IN ('in_stock','part_used') AND r.remaining_kg > 0 "
        "ORDER BY i.name, r.received_date, r.id").fetchall()
    return render_template(
        "production_form.html",
        raw_items=raw_items,
        finished_items=finished_items,
        raw_items_json=json.dumps(raw_items),
        rolls_json=json.dumps([dict(r) for r in rolls]),
    )


# ---------- customers ----------

@app.route("/customers/search.json")
@login_required
def customer_search_json():
    """Backs the searchable customer picker on the sale/order forms. Never loads the whole
    customer table into the page - even with thousands of one-time customers this stays a
    single indexed-ish LIKE query capped at 25 rows, so the form stays fast either way."""
    conn = dbm.get_db()
    q = (request.args.get("q") or "").strip()
    if q:
        rows = conn.execute(
            "SELECT id, name, phone, credit_balance, customer_type FROM customers "
            "WHERE is_active=1 AND (name LIKE ? OR phone LIKE ?) "
            "ORDER BY (customer_type='regular') DESC, name LIMIT 50",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        # Nothing typed yet - show regular customers first (the ones worth finding fast),
        # not a random slice of thousands of one-time entries.
        rows = conn.execute(
            "SELECT id, name, phone, credit_balance, customer_type FROM customers "
            "WHERE is_active=1 AND customer_type='regular' ORDER BY name LIMIT 50"
        ).fetchall()
    results = []
    for r in rows:
        label = r["name"]
        if r["phone"]:
            label += f" \u2014 {r['phone']}"
        if r["credit_balance"] and r["credit_balance"] > 0.005:
            label += f" (owes {inr(r['credit_balance'])})"
        results.append({"value": str(r["id"]), "label": label})
    return {"results": results}


@app.route("/customers")
@login_required
def customers_list():
    conn = dbm.get_db()
    view = request.args.get("view", "regular")  # 'regular' (default) or 'all'
    q = "SELECT * FROM customers WHERE is_active=1"
    if view == "regular":
        q += " AND customer_type='regular'"
    q += " ORDER BY name"
    all_rows = conn.execute(q).fetchall()
    # A card per customer is fine at thirty; at fifty thousand the page runs to megabytes
    # and the browser stalls. Show a page at a time.
    PER_PAGE = 200
    total_rows = len(all_rows)
    pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), pages))
    except ValueError:
        page = 1
    rows = all_rows[(page - 1) * PER_PAGE: page * PER_PAGE]
    one_time_count = conn.execute(
        "SELECT COUNT(*) c FROM customers WHERE is_active=1 AND customer_type='one_time'"
    ).fetchone()["c"]
    return render_template("customers_list.html", customers=rows, view=view,
                           one_time_count=one_time_count,
                           page=page, pages=pages, total_rows=total_rows)


@app.route("/customers/quick-add", methods=["POST"])
@login_required
def customer_quick_add():
    """Used by the + New customer box on the sale/order forms so a one-off customer can be
    added without leaving the page. Defaults to 'one_time' so it doesn't clutter the main
    customer list - tick 'regular customer' in the box to have it show up there too."""
    conn = dbm.get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        return {"error": "Name is required."}, 400
    customer_type = "regular" if request.form.get("regular") else "one_time"
    cur = conn.execute(
        "INSERT INTO customers (name, phone, customer_type) VALUES (?, ?, ?)",
        (name, (request.form.get("phone") or "").strip(), customer_type),
    )
    conn.commit()
    return {"id": cur.lastrowid, "name": name}


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    if request.method == "POST":
        conn = dbm.get_db()
        opening = max(0.0, parse_float(request.form.get("opening_balance"), 0))
        conn.execute(
            "INSERT INTO customers (name, phone, address, customer_type, credit_balance) VALUES (?, ?, ?, ?, ?)",
            (
                request.form.get("name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("address", "").strip(),
                "regular" if request.form.get("customer_type") != "one_time" else "one_time",
                opening,
            ),
        )
        conn.commit()
        if opening > 0.005:
            flash(
                f"Customer added, already owing you {inr(opening)}. "
                "Record a payment when they pay \u2014 that's when it counts as cash in.",
                "success",
            )
        else:
            flash("Customer added.", "success")
        return redirect(url_for("customers_list"))
    return render_template("customer_form.html", customer=None)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
@change_required
def customer_edit(customer_id):
    conn = dbm.get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        flash("Customer not found.", "error")
        return redirect(url_for("customers_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return render_template("customer_form.html", customer=customer)
        conn.execute(
            "UPDATE customers SET name=?, phone=?, address=?, customer_type=? WHERE id=?",
            (
                name, request.form.get("phone", "").strip(), request.form.get("address", "").strip(),
                "regular" if request.form.get("customer_type") != "one_time" else "one_time",
                customer_id,
            ),
        )
        conn.commit()
        flash("Customer updated.", "success")
        return redirect(url_for("customer_detail", customer_id=customer_id))
    return render_template("customer_form.html", customer=customer)


@app.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    conn = dbm.get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        flash("Customer not found.", "error")
        return redirect(url_for("customers_list"))
    sales = conn.execute(
        "SELECT * FROM sales WHERE customer_id=? ORDER BY id DESC", (customer_id,)
    ).fetchall()
    payments = conn.execute(
        "SELECT * FROM payments WHERE customer_id=? ORDER BY id DESC", (customer_id,)
    ).fetchall()
    orders = conn.execute(
        "SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC", (customer_id,)
    ).fetchall()
    return render_template(
        "customer_detail.html", customer=customer, sales=sales, payments=payments, orders=orders
    )


@app.route("/customers/<int:customer_id>/payment", methods=["POST"])
@sell_required
def customer_payment(customer_id):
    conn = dbm.get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Enter a valid payment amount.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))
    conn.execute(
        "INSERT INTO payments (customer_id, amount, payment_date, notes) VALUES (?, ?, ?, ?)",
        (customer_id, amount, request.form.get("payment_date") or date.today().isoformat(),
         request.form.get("notes", "").strip()),
    )
    conn.execute("UPDATE customers SET credit_balance = credit_balance - ? WHERE id=?", (amount, customer_id))
    conn.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.route("/customers/<int:customer_id>/adjust-credit", methods=["POST"])
@login_required
def customer_adjust_credit(customer_id):
    conn = dbm.get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        flash("Customer not found.", "error")
        return redirect(url_for("customers_list"))
    new_balance = parse_float(request.form.get("new_balance"), customer["credit_balance"])
    conn.execute("UPDATE customers SET credit_balance=? WHERE id=?", (new_balance, customer_id))
    conn.commit()
    flash(f"Credit balance corrected to {inr(new_balance)}.", "success")
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
@login_required
@change_required
def customer_delete(customer_id):
    conn = dbm.get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        flash("Customer not found.", "error")
        return redirect(url_for("customers_list"))
    has_history = (
        conn.execute("SELECT COUNT(*) c FROM sales WHERE customer_id=?", (customer_id,)).fetchone()["c"]
        or conn.execute("SELECT COUNT(*) c FROM orders WHERE customer_id=?", (customer_id,)).fetchone()["c"]
        or conn.execute("SELECT COUNT(*) c FROM payments WHERE customer_id=?", (customer_id,)).fetchone()["c"]
    )
    if has_history:
        conn.execute("UPDATE customers SET is_active=0 WHERE id=?", (customer_id,))
        conn.commit()
        flash(
            "This customer has sales/order history, so they've been archived rather than deleted \u2014 "
            "their records stay intact but they're hidden from your customer list and sale/order forms.", "success",
        )
        return redirect(url_for("customers_list"))
    conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    conn.commit()
    flash("Customer deleted.", "success")
    return redirect(url_for("customers_list"))


# ---------- sales ----------

@app.route("/sales")
@login_required
def sales_list():
    conn = dbm.get_db()
    rows = conn.execute(
        "SELECT s.*, c.name as customer_name FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
        "ORDER BY s.id DESC LIMIT 300"
    ).fetchall()
    return render_template("sales_list.html", sales=rows)


@app.route("/sales/new", methods=["GET", "POST"])
@sell_required
def sale_new():
    conn = dbm.get_db()

    def render_form(prefill=None):
        all_sellable = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
        customers = bounded_customer_list(conn, prefill.get("customer_id") if prefill else None)
        items_data = catalog_for_role(all_sellable)
        return render_template(
            "sale_form.html", items=all_sellable, customers=customers,
            items_json=json.dumps(items_data), prefill=prefill,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id") or None
        payment_status = request.form.get("payment_status", "paid")
        sale_date = request.form.get("sale_date") or date.today().isoformat()
        notes = request.form.get("notes", "").strip()
        paid_amount_raw = request.form.get("paid_amount", "")
        discount_amount = parse_float(request.form.get("discount_amount"), 0)
        items_json = request.form.get("items_json", "[]")
        try:
            lines = json.loads(items_json)
        except ValueError:
            lines = []

        prefill = {
            "customer_id": customer_id,
            "payment_status": payment_status,
            "sale_date": sale_date,
            "notes": notes,
            "paid_amount": paid_amount_raw,
            "discount_amount": request.form.get("discount_amount", ""),
            "lines": lines,
        }

        if not lines:
            flash("Add at least one item to the sale.", "error")
            return render_form(prefill)

        try:
            prepared, total_amount, total_cost, discount_amount = compute_sale_lines(conn, lines, discount_amount)
            if not prepared:
                flash("Add at least one valid item to the sale.", "error")
                return render_form(prefill)
            # discount_amount may have been capped inside compute_sale_lines to not exceed
            # the subtotal - that's already reflected in total_amount above.
            total_profit = total_amount - total_cost

            if payment_status == "paid":
                paid_amount = total_amount
            elif payment_status == "credit":
                paid_amount = 0.0
            else:
                paid_amount = parse_float(request.form.get("paid_amount"), 0)
                paid_amount = max(0.0, min(paid_amount, total_amount))

            due = total_amount - paid_amount
            if due > 0.005 and not customer_id:
                flash("Select a customer to record a credit / partial sale.", "error")
                return render_form(prefill)

            cur = conn.execute(
                "INSERT INTO sales (customer_id, sale_date, total_amount, discount_amount, total_cost, "
                "total_profit, paid_amount, payment_status, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                (customer_id, sale_date, total_amount, discount_amount, total_cost, total_profit,
                 paid_amount, payment_status, notes),
            )
            sale_id = cur.lastrowid

            for item_id, qty, price, unit_cost, line_total, line_profit in prepared:
                conn.execute(
                    "INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, unit_cost, "
                    "line_total, line_profit) VALUES (?,?,?,?,?,?,?)",
                    (sale_id, item_id, qty, price, unit_cost, line_total, line_profit),
                )
                update_stock_remove(conn, item_id, qty)

            if due > 0.005 and customer_id:
                conn.execute(
                    "UPDATE customers SET credit_balance = credit_balance + ? WHERE id=?", (due, customer_id)
                )

            negative_stock_warning(conn, prepared)
            conn.commit()
            msg = f"Sale recorded. Profit on this sale: {inr(total_profit)}"
            if discount_amount > 0.005:
                msg += f" (after a {inr(discount_amount)} discount)"
            log_action("recorded a sale", "sale", sale_id, inr(total_amount))
            flash(msg, "success")
            return redirect(url_for("sale_detail", sale_id=sale_id))
        except Exception as e:
            conn.rollback()
            flash(f"Could not save sale: {e}", "error")
            return render_form(prefill)

    return render_form()


@app.route("/sales/<int:sale_id>")
@login_required
def sale_detail(sale_id):
    conn = dbm.get_db()
    sale = conn.execute(
        "SELECT s.*, c.name as customer_name, c.phone as customer_phone FROM sales s "
        "LEFT JOIN customers c ON c.id = s.customer_id WHERE s.id=?", (sale_id,)
    ).fetchone()
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_list"))
    lines = conn.execute(
        "SELECT si.*, i.name as item_name, i.unit as item_unit FROM sale_items si "
        "JOIN items i ON i.id = si.item_id WHERE si.sale_id=?", (sale_id,)
    ).fetchall()
    return render_template("sale_detail.html", sale=sale, lines=lines)


@app.route("/sales/<int:sale_id>/edit", methods=["GET", "POST"])
@sell_required
@change_required
def sale_edit(sale_id):
    conn = dbm.get_db()
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_list"))

    def render_form(prefill):
        all_sellable = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
        customers = bounded_customer_list(conn, prefill.get("customer_id") if prefill else None)
        items_data = catalog_for_role(all_sellable)
        return render_template(
            "sale_form.html", items=all_sellable, customers=customers,
            items_json=json.dumps(items_data), prefill=prefill, edit_sale_id=sale_id,
        )

    if request.method == "GET":
        existing_lines = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
        prefill = {
            "customer_id": str(sale["customer_id"]) if sale["customer_id"] else None,
            "payment_status": sale["payment_status"],
            "sale_date": sale["sale_date"],
            "notes": sale["notes"] or "",
            "paid_amount": sale["paid_amount"],
            "discount_amount": sale["discount_amount"],
            "lines": [
                {"item_id": r["item_id"], "quantity": r["quantity"], "unit_price": r["unit_price"]}
                for r in existing_lines
            ],
        }
        return render_form(prefill)

    customer_id = request.form.get("customer_id") or None
    payment_status = request.form.get("payment_status", "paid")
    sale_date = request.form.get("sale_date") or date.today().isoformat()
    notes = request.form.get("notes", "").strip()
    paid_amount_raw = request.form.get("paid_amount", "")
    discount_amount = parse_float(request.form.get("discount_amount"), 0)
    items_json = request.form.get("items_json", "[]")
    try:
        lines = json.loads(items_json)
    except ValueError:
        lines = []

    prefill = {
        "customer_id": customer_id, "payment_status": payment_status, "sale_date": sale_date,
        "notes": notes, "paid_amount": paid_amount_raw,
        "discount_amount": request.form.get("discount_amount", ""), "lines": lines,
    }

    if not lines:
        flash("Add at least one item to the sale.", "error")
        return render_form(prefill)

    try:
        prepared, total_amount, total_cost, discount_amount = compute_sale_lines(conn, lines, discount_amount)
        if not prepared:
            flash("Add at least one valid item to the sale.", "error")
            return render_form(prefill)

        total_profit = total_amount - total_cost
        if payment_status == "paid":
            paid_amount = total_amount
        elif payment_status == "credit":
            paid_amount = 0.0
        else:
            paid_amount = parse_float(request.form.get("paid_amount"), 0)
            paid_amount = max(0.0, min(paid_amount, total_amount))

        due = total_amount - paid_amount
        if due > 0.005 and not customer_id:
            flash("Select a customer to record a credit / partial sale.", "error")
            return render_form(prefill)

        # Undo what the previous version of this sale did (restores stock, reverses old
        # credit), then apply the edited version fresh - same sale id, updated numbers.
        reverse_sale(conn, sale_id)

        conn.execute(
            "UPDATE sales SET customer_id=?, sale_date=?, total_amount=?, discount_amount=?, total_cost=?, "
            "total_profit=?, paid_amount=?, payment_status=?, notes=? WHERE id=?",
            (customer_id, sale_date, total_amount, discount_amount, total_cost, total_profit,
             paid_amount, payment_status, notes, sale_id),
        )
        for item_id, qty, price, unit_cost, line_total, line_profit in prepared:
            conn.execute(
                "INSERT INTO sale_items (sale_id, item_id, quantity, unit_price, unit_cost, "
                "line_total, line_profit) VALUES (?,?,?,?,?,?,?)",
                (sale_id, item_id, qty, price, unit_cost, line_total, line_profit),
            )
            update_stock_remove(conn, item_id, qty)

        if due > 0.005 and customer_id:
            conn.execute("UPDATE customers SET credit_balance = credit_balance + ? WHERE id=?", (due, customer_id))

        conn.commit()
        log_action("edited a sale", "sale", sale_id, inr(total_amount))
        flash(f"Sale #{sale_id} updated. Profit: {inr(total_profit)}", "success")
        return redirect(url_for("sale_detail", sale_id=sale_id))
    except Exception as e:
        conn.rollback()
        flash(f"Could not update sale: {e}", "error")
        return render_form(prefill)


@app.route("/sales/<int:sale_id>/delete", methods=["POST"])
@sell_required
@change_required
def sale_delete(sale_id):
    conn = dbm.get_db()
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_list"))
    try:
        reverse_sale(conn, sale_id)
        conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        conn.commit()
        log_action("deleted a sale", "sale", sale_id)
        flash(f"Sale #{sale_id} deleted. Stock and any credit balance have been reversed.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not delete sale: {e}", "error")
        return redirect(url_for("sale_detail", sale_id=sale_id))
    return redirect(url_for("sales_list"))


@app.route("/sales/<int:sale_id>/invoice")
@login_required
def sale_invoice(sale_id):
    conn = dbm.get_db()
    sale = conn.execute(
        "SELECT s.*, c.name as customer_name, c.phone as customer_phone, c.address as customer_address "
        "FROM sales s LEFT JOIN customers c ON c.id = s.customer_id WHERE s.id=?", (sale_id,)
    ).fetchone()
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_list"))
    lines = conn.execute(
        "SELECT si.*, i.name as item_name, i.unit as item_unit FROM sale_items si "
        "JOIN items i ON i.id = si.item_id WHERE si.sale_id=?", (sale_id,)
    ).fetchall()
    company = get_company(conn)
    return render_template("invoice.html", sale=sale, lines=lines, company=company,
                           invoice_paper=get_setting(conn, "invoice_paper", "a4"))


# ---------- expenses ----------

@app.route("/expenses")
@admin_required
def expenses_list():
    conn = dbm.get_db()
    today = date.today()
    sow, som, soy = period_bounds(today)

    preset = request.args.get("preset", "month")
    if preset == "week":
        start, end = sow, today
    elif preset == "year":
        start, end = soy, today
    elif preset == "all":
        start, end = None, today
    elif preset == "custom":
        try:
            start = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
        except ValueError:
            start = som
        try:
            end = datetime.strptime(request.args.get("to", ""), "%Y-%m-%d").date()
        except ValueError:
            end = today
    else:
        preset, start, end = "month", som, today

    lo = start.isoformat() if start else "0000-01-01"
    hi = end.isoformat()
    cat_filter = request.args.get("category", "")

    # Where the money actually went, biggest first - the whole point of the page.
    summary = conn.execute(
        # Group on a tidied name so "tea", "Tea" and " Tea " count as one category
        # instead of splitting the list three ways.
        "SELECT COALESCE(NULLIF(TRIM(category),''),'Uncategorised') cat, "
        "COUNT(*) n, SUM(amount) total, MAX(amount) biggest "
        "FROM expenses WHERE id != 0 AND expense_date BETWEEN ? AND ? "
        "GROUP BY LOWER(TRIM(category)) ORDER BY total DESC", (lo, hi)).fetchall()
    grand = sum(r["total"] for r in summary)

    q = ("SELECT * FROM expenses WHERE id != 0 AND expense_date BETWEEN ? AND ?")
    params = [lo, hi]
    if cat_filter:
        q += " AND LOWER(TRIM(COALESCE(NULLIF(TRIM(category),''),'Uncategorised'))) = LOWER(?)"
        params.append(cat_filter)
    q += " ORDER BY expense_date DESC, id DESC LIMIT 400"
    rows = conn.execute(q, params).fetchall()

    # Same categories a month earlier, so a jump is obvious rather than something you
    # only notice when the cash has gone.
    prev_totals = {}
    if start:
        span = (end - start).days + 1
        p_end = start - timedelta(days=1)
        p_start = p_end - timedelta(days=span - 1)
        for r in conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(category),''),'Uncategorised') cat, SUM(amount) total "
            "FROM expenses WHERE id != 0 AND expense_date BETWEEN ? AND ? GROUP BY cat",
                (p_start.isoformat(), p_end.isoformat())):
            prev_totals[r["cat"]] = r["total"]

    return render_template(
        "expenses_list.html", expenses=rows, summary=summary, grand=grand,
        prev_totals=prev_totals, preset=preset, start=start, end=end,
        cat_filter=cat_filter, today=today.isoformat())


@app.route("/expenses/new", methods=["GET", "POST"])
@admin_required
def expense_new():
    if request.method == "POST":
        conn = dbm.get_db()
        amount = parse_float(request.form.get("amount"))
        if amount <= 0:
            flash("Enter a valid amount.", "error")
            return redirect(url_for("expense_new"))
        conn.execute(
            "INSERT INTO expenses (category, amount, expense_date, notes) VALUES (?, ?, ?, ?)",
            (
                request.form.get("category", "").strip(),
                amount,
                request.form.get("expense_date") or date.today().isoformat(),
                request.form.get("notes", "").strip(),
            ),
        )
        conn.commit()
        flash("Expense recorded.", "success")
        return redirect(url_for("expenses_list"))
    conn = dbm.get_db()
    known = [r["c"] for r in conn.execute(
        "SELECT DISTINCT TRIM(category) c FROM expenses WHERE id != 0 "
        "AND TRIM(COALESCE(category,'')) != '' ORDER BY c")]
    return render_template("expense_form.html", known_categories=known)


# ---------- orders (advance-payment / custom jobs) ----------

@app.route("/orders")
@login_required
def orders_list():
    conn = dbm.get_db()
    status_filter = request.args.get("status", "")
    q = (
        "SELECT o.*, c.name as customer_name FROM orders o "
        "JOIN customers c ON c.id = o.customer_id"
    )
    params = []
    if status_filter == "late":
        # Still open, and the promised date has gone by.
        q += (" WHERE o.status IN ('pending','design','printing','ready') "
              "AND o.expected_ready_date IS NOT NULL AND o.expected_ready_date < ?")
        params.append(date.today().isoformat())
    elif status_filter in OPEN_STATUSES + ("completed", "cancelled"):
        q += " WHERE o.status=?"
        params.append(status_filter)

    if status_filter in OPEN_STATUSES + ("late", ""):
        # Open orders are most useful in due-date order - the ones you are about to be
        # late on first, rather than whichever was booked most recently.
        q += (" ORDER BY CASE WHEN o.status IN ('pending','design','printing','ready') "
              "THEN 0 ELSE 1 END, o.expected_ready_date IS NULL, o.expected_ready_date, o.id DESC")
    else:
        q += " ORDER BY o.id DESC"
    all_rows = conn.execute(q, params).fetchall()
    # Years of orders in one page would be enormous - 200,000 rows is roughly 90 MB of
    # HTML, which no browser will render. Show a page at a time.
    PER_PAGE = 200
    total_rows = len(all_rows)
    pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), pages))
    except ValueError:
        page = 1
    orders = all_rows[(page - 1) * PER_PAGE: page * PER_PAGE]
    return render_template("orders_list.html", orders=orders, status_filter=status_filter,
                           counts=open_order_counts(conn),
                           page=page, pages=pages, total_rows=total_rows)


@app.route("/orders/new", methods=["GET", "POST"])
@sell_required
def order_new():
    conn = dbm.get_db()

    def render_form(prefill=None):
        items = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
        customers = bounded_customer_list(conn, prefill.get("customer_id") if prefill else None)
        items_data = catalog_for_role(items)
        return render_template(
            "order_form.html", items=items, customers=customers,
            items_json=json.dumps(items_data), prefill=prefill,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id") or None
        order_date = request.form.get("order_date") or date.today().isoformat()
        expected_ready_date = request.form.get("expected_ready_date") or None
        advance_amount = parse_float(request.form.get("advance_amount"), 0)
        notes = request.form.get("notes", "").strip()
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except ValueError:
            lines = []

        prefill = {
            "customer_id": customer_id,
            "order_date": order_date,
            "expected_ready_date": expected_ready_date,
            "advance_amount": request.form.get("advance_amount", ""),
            "notes": notes,
            "lines": lines,
        }

        if not customer_id:
            flash("Select a customer for this order \u2014 needed since you're taking an advance.", "error")
            return render_form(prefill)
        if not lines:
            flash("Add at least one item or custom line to the order.", "error")
            return render_form(prefill)

        prepared = []
        agreed_total = 0.0
        for line in lines:
            qty = parse_float(line.get("quantity"), 1) or 1
            price = parse_float(line.get("unit_price"), 0)
            item_id = line.get("item_id") or None
            description = (line.get("description") or "").strip()
            if item_id:
                item_id = int(item_id)
            line_total = qty * price
            if line_total <= 0 and not description:
                continue
            agreed_total += line_total
            prepared.append((item_id, description, qty, price, line_total))

        if not prepared:
            flash("Add at least one valid item or custom line.", "error")
            return render_form(prefill)

        if advance_amount > agreed_total + 0.005:
            flash("Advance can't be more than the agreed total.", "error")
            return render_form(prefill)

        try:
            cur = conn.execute(
                "INSERT INTO orders (customer_id, order_date, expected_ready_date, status, agreed_total, "
                "advance_amount, advance_date, notes) VALUES (?,?,?,?,?,?,?,?)",
                (
                    customer_id, order_date, expected_ready_date, "pending", agreed_total,
                    advance_amount, order_date if advance_amount > 0 else None, notes,
                ),
            )
            order_id = cur.lastrowid
            for item_id, description, qty, price, line_total in prepared:
                conn.execute(
                    "INSERT INTO order_items (order_id, item_id, description, quantity, unit_price, line_total) "
                    "VALUES (?,?,?,?,?,?)",
                    (order_id, item_id, description, qty, price, line_total),
                )
            conn.commit()
            flash(f"Order #{order_id} booked. Advance recorded: {inr(advance_amount)}.", "success")
            return redirect(url_for("order_detail", order_id=order_id))
        except Exception as e:
            conn.rollback()
            flash(f"Could not save order: {e}", "error")
            return render_form(prefill)

    return render_form()


@app.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    conn = dbm.get_db()
    order = conn.execute(
        "SELECT o.*, c.name as customer_name, c.phone as customer_phone FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.id=?", (order_id,)
    ).fetchone()
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("orders_list"))
    items = conn.execute(
        "SELECT oi.*, i.name as item_name, i.unit as item_unit FROM order_items oi "
        "LEFT JOIN items i ON i.id = oi.item_id WHERE oi.order_id=?", (order_id,)
    ).fetchall()
    consumption = conn.execute(
        "SELECT oc.*, i.name as item_name, i.unit as item_unit FROM order_consumption oc "
        "JOIN items i ON i.id = oc.raw_item_id WHERE oc.order_id=?", (order_id,)
    ).fetchall()
    balance_due = order["agreed_total"] - order["advance_amount"]
    outsourced = conn.execute(
        "SELECT * FROM order_outsourced WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    images = conn.execute(
        "SELECT * FROM order_images WHERE order_id=? ORDER BY kind, id", (order_id,)).fetchall()
    return render_template(
        "order_detail.html", order=order, items=items, consumption=consumption,
        balance_due=balance_due, outsourced=outsourced, images=images
    )


@app.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@sell_required
@change_required
def order_edit(order_id):
    """Change an order that hasn't been handed over yet. Completed and cancelled orders are
    locked because their stock, costs and customer credit have already been posted -
    reopening those would need those movements unwound, which is riskier than it's worth."""
    conn = dbm.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("orders_list"))
    if order["status"] not in OPEN_STATUSES:
        flash(
            "Only orders still pending or ready for pickup can be edited. "
            "This one is already " + order["status"] + ".", "error",
        )
        return redirect(url_for("order_detail", order_id=order_id))

    def render_form(prefill=None):
        items = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
        customers = bounded_customer_list(conn, (prefill or {}).get("customer_id") or order["customer_id"])
        if prefill is None:
            existing = conn.execute(
                "SELECT item_id, description, quantity, unit_price FROM order_items WHERE order_id=?",
                (order_id,),
            ).fetchall()
            prefill = {
                "customer_id": str(order["customer_id"]),
                "order_date": order["order_date"],
                "expected_ready_date": order["expected_ready_date"] or "",
                "advance_amount": order["advance_amount"],
                "notes": order["notes"] or "",
                "lines": [dict(r) for r in existing],
            }
        return render_template(
            "order_form.html", items=items, customers=customers,
            items_json=json.dumps([dict(i) for i in items]),
            prefill=prefill, order=order,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id") or None
        order_date = request.form.get("order_date") or order["order_date"]
        expected_ready_date = request.form.get("expected_ready_date") or None
        advance_amount = parse_float(request.form.get("advance_amount"), 0)
        notes = request.form.get("notes", "").strip()
        try:
            lines = json.loads(request.form.get("lines_json", "[]"))
        except ValueError:
            lines = []

        prefill = {
            "customer_id": customer_id,
            "order_date": order_date,
            "expected_ready_date": expected_ready_date,
            "advance_amount": request.form.get("advance_amount", ""),
            "notes": notes,
            "lines": lines,
        }

        if not customer_id:
            flash("Select a customer for this order.", "error")
            return render_form(prefill)

        prepared = []
        agreed_total = 0.0
        for line in lines:
            qty = parse_float(line.get("quantity"), 1) or 1
            price = parse_float(line.get("unit_price"), 0)
            item_id = line.get("item_id") or None
            description = (line.get("description") or "").strip()
            if item_id:
                item_id = int(item_id)
            line_total = qty * price
            if line_total <= 0 and not description:
                continue
            agreed_total += line_total
            prepared.append((item_id, description, qty, price, line_total))

        if not prepared:
            flash("Add at least one valid item or custom line.", "error")
            return render_form(prefill)
        if advance_amount > agreed_total + 0.005:
            flash("Advance can't be more than the agreed total.", "error")
            return render_form(prefill)

        try:
            conn.execute(
                "UPDATE orders SET customer_id=?, order_date=?, expected_ready_date=?, "
                "agreed_total=?, advance_amount=?, advance_date=?, notes=? WHERE id=?",
                (
                    customer_id, order_date, expected_ready_date, agreed_total, advance_amount,
                    (order["advance_date"] or order_date) if advance_amount > 0 else None,
                    notes, order_id,
                ),
            )
            conn.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
            for item_id, description, qty, price, line_total in prepared:
                conn.execute(
                    "INSERT INTO order_items (order_id, item_id, description, quantity, unit_price, line_total) "
                    "VALUES (?,?,?,?,?,?)",
                    (order_id, item_id, description, qty, price, line_total),
                )
            conn.commit()
            flash(f"Order #{order_id} updated. Agreed total is now {inr(agreed_total)}.", "success")
            return redirect(url_for("order_detail", order_id=order_id))
        except Exception as e:
            conn.rollback()
            flash(f"Could not update order: {e}", "error")
            return render_form(prefill)

    return render_form()


@app.route("/orders/<int:order_id>/mark-ready", methods=["POST"])
@sell_required
def order_mark_ready(order_id):
    """Move an order to the next stage, or to a stage named in the form.

    Kept at the old address so any bookmark or older page still works - it just means
    "move this order forward" now rather than specifically "ready".
    """
    conn = dbm.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("orders_list"))
    if order["status"] not in OPEN_STATUSES:
        flash("This order is already " + order["status"] + ".", "error")
        return redirect(url_for("order_detail", order_id=order_id))

    target = request.form.get("stage") or next_stage(order["status"])
    if target not in OPEN_STATUSES:
        flash("That is not a valid stage.", "error")
        return redirect(url_for("order_detail", order_id=order_id))

    conn.execute("UPDATE orders SET status=? WHERE id=?", (target, order_id))
    conn.commit()
    flash(f"Order #{order_id} moved to \u201c{ORDER_LABELS[target]}\u201d.", "success")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<int:order_id>/cancel", methods=["GET", "POST"])
@sell_required
@change_required
def order_cancel(order_id):
    conn = dbm.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] not in OPEN_STATUSES + ("cancelled",):
        flash("This order can't be cancelled.", "error")
        return redirect(url_for("order_detail", order_id=order_id))

    if request.method == "POST":
        refund_amount = parse_float(request.form.get("refund_amount"), 0)
        refund_amount = max(0.0, min(refund_amount, order["advance_amount"]))
        cancelled_date = request.form.get("cancelled_date") or date.today().isoformat()
        conn.execute(
            "UPDATE orders SET status='cancelled', refunded_amount=?, cancelled_date=? WHERE id=?",
            (refund_amount, cancelled_date, order_id),
        )
        conn.commit()
        if refund_amount > 0.005:
            flash(
                f"Order cancelled. {inr(refund_amount)} refund recorded \u2014 "
                "it'll be subtracted from cash received on the refund date.", "success",
            )
        else:
            flash("Order cancelled. Advance kept, so it still counts as cash received.", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    return render_template("order_cancel_form.html", order=order)


@app.route("/orders/<int:order_id>/jobcard")
@login_required
def order_jobcard(order_id):
    """A 58mm slip that travels with the job.

    Deliberately sparse: job number, what to make, how many. No prices - it goes to the
    printer and around the workshop, and the customer's rate is nobody else's business.
    """
    conn = dbm.get_db()
    order = conn.execute(
        "SELECT o.*, c.name cname, c.phone cphone FROM orders o "
        "JOIN customers c ON c.id = o.customer_id WHERE o.id=?", (order_id,)).fetchone()
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("orders_list"))
    lines = conn.execute(
        "SELECT oi.*, i.name item_name, i.unit item_unit FROM order_items oi "
        "LEFT JOIN items i ON i.id = oi.item_id WHERE oi.order_id=?", (order_id,)).fetchall()
    images = conn.execute(
        "SELECT * FROM order_images WHERE order_id=? ORDER BY kind, id", (order_id,)).fetchall()
    return render_template(
        "order_jobcard.html", order=order, lines=lines, images=images,
        company=get_setting(conn, "company_name", ""),
        company_phone=get_setting(conn, "company_phone", ""),
        printed=datetime.now().strftime("%d %b %Y, %I:%M %p"))


@app.route("/orders/<int:order_id>/complete", methods=["GET", "POST"])
@sell_required
def order_complete(order_id):
    conn = dbm.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] in ("completed", "cancelled"):
        flash("This order can't be completed.", "error")
        return redirect(url_for("order_detail", order_id=order_id))
    order_items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    balance_expected = order["agreed_total"] - order["advance_amount"]

    if request.method == "POST":
        labor_cost = parse_float(request.form.get("labor_cost"), 0)
        other_cost = parse_float(request.form.get("other_cost"), 0)
        is_reprint = 1 if request.form.get("is_reprint") == "yes" else 0
        # Work bought in for this job - printing, lamination, binding. Charged to the job's
        # cost AND put on the supplier's payable, because it is a purchase, not overhead.
        try:
            outsourced = json.loads(request.form.get("outsourced_json", "[]"))
        except ValueError:
            outsourced = []
        balance_received = parse_float(request.form.get("balance_received"), balance_expected)
        completed_date = request.form.get("completed_date") or date.today().isoformat()
        raw_ids = request.form.getlist("raw_item_id[]")
        raw_qtys = request.form.getlist("raw_qty[]")
        raw_rolls = request.form.getlist("raw_roll_id[]")   # optional: which roll it came off

        try:
            material_cost = 0.0
            for oi in order_items:
                if oi["item_id"]:
                    item = conn.execute("SELECT * FROM items WHERE id=?", (oi["item_id"],)).fetchone()
                    material_cost += item["avg_cost"] * oi["quantity"]
                    update_stock_remove(conn, oi["item_id"], oi["quantity"])

            rolls_used = []
            for idx, (rid, rqty) in enumerate(zip(raw_ids, raw_qtys)):
                if not rid:
                    continue
                rid = int(rid)
                rqty = parse_float(rqty)
                roll_id = raw_rolls[idx] if idx < len(raw_rolls) else ""
                if roll_id:
                    # A roll was picked from the register - use its actual remaining weight,
                    # so you never have to work the kg out yourself.
                    roll = conn.execute(
                        "SELECT * FROM material_rolls WHERE id=?", (roll_id,)).fetchone()
                    if roll:
                        if rqty <= 0:
                            rqty = roll["remaining_kg"]
                        rqty = min(rqty, roll["remaining_kg"])
                        rolls_used.append((roll["id"], rqty))
                if rqty <= 0:
                    continue
                raw_item = conn.execute("SELECT * FROM items WHERE id=?", (rid,)).fetchone()
                line_cost = rqty * raw_item["avg_cost"]
                conn.execute(
                    "INSERT INTO order_consumption (order_id, raw_item_id, qty_consumed, unit_cost_at_time, line_cost) "
                    "VALUES (?,?,?,?,?)",
                    (order_id, rid, rqty, raw_item["avg_cost"], line_cost),
                )
                update_stock_remove(conn, rid, rqty)
                material_cost += line_cost

            prepared_out = []
            outsourced_total = 0.0
            for row in outsourced:
                amount = parse_float(row.get("amount"), 0)
                if amount <= 0:
                    continue
                sup_id = row.get("supplier_id") or None
                sup_name = (row.get("supplier_name") or "").strip()
                if sup_id:
                    srow = conn.execute("SELECT name FROM suppliers WHERE id=?", (sup_id,)).fetchone()
                    if srow:
                        sup_name = srow["name"]
                status = row.get("payment_status", "credit")
                paid = amount if status == "paid" else 0.0
                prepared_out.append((sup_id, sup_name, (row.get("description") or "").strip(),
                                     amount, paid, "paid" if paid >= amount - 0.005 else "credit"))
                outsourced_total += amount

            total_cost = material_cost + labor_cost + other_cost + outsourced_total
            profit = order["agreed_total"] - total_cost

            shortfall = balance_expected - balance_received
            if shortfall > 0.005:
                conn.execute(
                    "UPDATE customers SET credit_balance = credit_balance + ? WHERE id=?",
                    (shortfall, order["customer_id"]),
                )

            conn.execute(
                "UPDATE orders SET status='completed', material_cost=?, labor_cost=?, other_cost=?, "
                "total_cost=?, profit=?, balance_received=?, completed_date=?, is_reprint=? WHERE id=?",
                (material_cost, labor_cost, other_cost, total_cost, profit, balance_received,
                 completed_date, is_reprint, order_id),
            )

            # Replace any previous outsourced lines for this order, then re-apply.
            for old in conn.execute(
                    "SELECT * FROM order_outsourced WHERE order_id=?", (order_id,)).fetchall():
                owed_back = old["amount"] - (old["paid_amount"] or 0)
                if owed_back > 0.005 and old["supplier_id"]:
                    conn.execute(
                        "UPDATE suppliers SET payable_balance = payable_balance - ? WHERE id=?",
                        (owed_back, old["supplier_id"]))
            conn.execute("DELETE FROM order_outsourced WHERE order_id=?", (order_id,))

            for sup_id, sup_name, desc, amount, paid, status in prepared_out:
                conn.execute(
                    "INSERT INTO order_outsourced (order_id, supplier_id, supplier_name, "
                    "description, amount, paid_amount, payment_status) VALUES (?,?,?,?,?,?,?)",
                    (order_id, sup_id, sup_name, desc, amount, paid, status))
                owed = amount - paid
                if owed > 0.005 and sup_id:
                    conn.execute(
                        "UPDATE suppliers SET payable_balance = payable_balance + ? WHERE id=?",
                        (owed, sup_id))
            conn.commit()
            flash(f"Order #{order_id} completed. Profit: {inr(profit)}", "success")
            return redirect(url_for("order_detail", order_id=order_id))
        except Exception as e:
            conn.rollback()
            flash(f"Could not complete order: {e}", "error")
            return redirect(url_for("order_complete", order_id=order_id))

    items = conn.execute("SELECT * FROM items WHERE is_active=1 ORDER BY name").fetchall()
    raw_items = [dict(i) for i in items if i["item_type"] == "raw_material"]
    outsourced_rows = conn.execute(
        "SELECT * FROM order_outsourced WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    return render_template(
        "order_complete_form.html",
        outsourced_json=json.dumps([dict(r) for r in outsourced_rows]),
        suppliers=bounded_supplier_list(conn), order=order, order_items=order_items, items=items,
        balance_expected=balance_expected, raw_items_json=json.dumps(raw_items),
    )


# ---------- reports ----------

@app.route("/reports")
@admin_required
def reports():
    conn = dbm.get_db()
    today = date.today()
    sow, som, soy = period_bounds(today)

    sales, all_orders, orders_completed = load_revenue_sources(conn)

    def agg(since=None, until=None):
        return period_stats(sales, orders_completed, since, until)

    def cash_agg(since=None, until=None):
        return period_cash(conn, sales, all_orders, orders_completed, since, until)

    stats = {
        "today": agg(today, today),
        "week": agg(sow, today),
        "month": agg(som, today),
        "year": agg(soy, today),
        "all_time": agg(),
    }
    cash_stats = {
        "today": cash_agg(today, today)["net"],
        "week": cash_agg(sow, today)["net"],
        "month": cash_agg(som, today)["net"],
        "year": cash_agg(soy, today)["net"],
    }

    expenses_month = cash_agg(som, today)["expenses"]

    # best sellers this month by profit - regular sales plus catalog lines on completed
    # custom orders, so a printing charge sold through an order shows up here too
    top_items = product_breakdown(conn, som, today, limit=10, order_by="profit")

    monthly_trend = []
    for i in range(5, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        start = date(y, m, 1)
        end = date(y + (1 if m == 12 else 0), (1 if m == 12 else m + 1), 1) - timedelta(days=1)
        a = agg(start, end)
        monthly_trend.append({"label": start.strftime("%b %Y"), **a})
    max_month_rev = max([m["revenue"] for m in monthly_trend] + [1])

    return render_template(
        "reports.html",
        stats=stats,
        cash_stats=cash_stats,
        expenses_month=expenses_month,
        top_items=top_items,
        monthly_trend=monthly_trend,
        max_month_rev=max_month_rev,
    )


# ---------- employees ----------

@app.route("/employees")
@admin_required
def employees_list():
    conn = dbm.get_db()
    today = date.today()
    month_str = request.args.get("month") or today.strftime("%Y-%m")
    y, m = map(int, month_str.split("-"))
    employees = conn.execute("SELECT * FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    rows = [(e, employee_month_summary(conn, e, y, m)) for e in employees]
    month_label = date(y, m, 1).strftime("%B %Y")
    return render_template("employees_list.html", rows=rows, month_str=month_str, month_label=month_label)


@app.route("/employees/new", methods=["GET", "POST"])
@admin_required
def employee_new():
    conn = dbm.get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return render_template("employee_form.html", employee=None)
        conn.execute(
            "INSERT INTO employees (name, phone, monthly_salary) VALUES (?, ?, ?)",
            (name, request.form.get("phone", "").strip(), parse_float(request.form.get("monthly_salary"), 0)),
        )
        conn.commit()
        flash("Employee added.", "success")
        return redirect(url_for("employees_list"))
    return render_template("employee_form.html", employee=None)


@app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@admin_required
@change_required
def employee_edit(employee_id):
    conn = dbm.get_db()
    employee = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("employees_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return render_template("employee_form.html", employee=employee)
        conn.execute(
            "UPDATE employees SET name=?, phone=?, monthly_salary=? WHERE id=?",
            (
                name, request.form.get("phone", "").strip(),
                parse_float(request.form.get("monthly_salary"), 0), employee_id,
            ),
        )
        conn.commit()
        flash("Employee updated.", "success")
        return redirect(url_for("employee_detail", employee_id=employee_id))
    return render_template("employee_form.html", employee=employee)


@app.route("/employees/<int:employee_id>")
@admin_required
def employee_detail(employee_id):
    conn = dbm.get_db()
    employee = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("employees_list"))

    today = date.today()
    month_str = request.args.get("month") or today.strftime("%Y-%m")
    y, m = map(int, month_str.split("-"))
    start, end = month_bounds_ym(y, m)
    summary = employee_month_summary(conn, employee, y, m)

    attendance = conn.execute(
        "SELECT * FROM employee_attendance WHERE employee_id=? AND date BETWEEN ? AND ? ORDER BY date DESC",
        (employee_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    advances = conn.execute(
        "SELECT * FROM employee_advances WHERE employee_id=? AND advance_date BETWEEN ? AND ? ORDER BY advance_date DESC",
        (employee_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    month_label = date(y, m, 1).strftime("%B %Y")
    return render_template(
        "employee_detail.html", employee=employee, summary=summary, attendance=attendance,
        advances=advances, month_str=month_str, month_label=month_label,
    )


@app.route("/employees/<int:employee_id>/attendance", methods=["POST"])
@login_required
def employee_attendance_add(employee_id):
    conn = dbm.get_db()
    att_date = request.form.get("att_date") or date.today().isoformat()
    status = request.form.get("status", "present")
    notes = request.form.get("notes", "").strip()
    conn.execute(
        "INSERT INTO employee_attendance (employee_id, date, status, notes) VALUES (?,?,?,?) "
        "ON CONFLICT(employee_id, date) DO UPDATE SET status=excluded.status, notes=excluded.notes",
        (employee_id, att_date, status, notes),
    )
    conn.commit()
    flash("Attendance recorded.", "success")
    return redirect(url_for("employee_detail", employee_id=employee_id, month=att_date[:7]))


@app.route("/employees/<int:employee_id>/advance", methods=["POST"])
@admin_required
def employee_advance_add(employee_id):
    conn = dbm.get_db()
    employee = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("employees_list"))
    amount = parse_float(request.form.get("amount"), 0)
    if amount <= 0:
        flash("Enter a valid advance amount.", "error")
        return redirect(url_for("employee_detail", employee_id=employee_id))
    adv_date = request.form.get("advance_date") or date.today().isoformat()
    notes = request.form.get("notes", "").strip()

    # An advance is real cash leaving the drawer, so it's booked as an expense straight away.
    # That keeps Cash in hand honest on the day it's paid. At month end you record only the
    # Net payable (salary minus deductions minus advances) as the salary expense - the advance
    # portion is already booked here, so the same rupee is never counted twice.
    cur = conn.execute(
        "INSERT INTO expenses (category, amount, expense_date, notes) VALUES (?,?,?,?)",
        (
            "Salary advance",
            amount,
            adv_date,
            f"Advance to {employee['name']}" + (f" \u2014 {notes}" if notes else ""),
        ),
    )
    expense_id = cur.lastrowid
    conn.execute(
        "INSERT INTO employee_advances (employee_id, amount, advance_date, expense_id, notes) "
        "VALUES (?,?,?,?,?)",
        (employee_id, amount, adv_date, expense_id, notes),
    )
    conn.commit()
    flash(
        f"Advance of {inr(amount)} recorded and deducted from cash in hand. "
        "At month end, record only the Net payable as the salary expense.", "success",
    )
    return redirect(url_for("employee_detail", employee_id=employee_id, month=adv_date[:7]))


@app.route("/employees/<int:employee_id>/advance/<int:advance_id>/delete", methods=["POST"])
@admin_required
@change_required
def employee_advance_delete(employee_id, advance_id):
    conn = dbm.get_db()
    adv = conn.execute(
        "SELECT * FROM employee_advances WHERE id=? AND employee_id=?", (advance_id, employee_id)
    ).fetchone()
    if not adv:
        flash("Advance not found.", "error")
        return redirect(url_for("employee_detail", employee_id=employee_id))
    # Delete the advance row first: it holds a foreign key to the expense, so removing the
    # expense while the advance still points at it fails the constraint.
    expense_id = adv["expense_id"]
    conn.execute("DELETE FROM employee_advances WHERE id=?", (advance_id,))
    if expense_id:
        conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    flash("Advance removed and the cash put back.", "success")
    return redirect(url_for("employee_detail", employee_id=employee_id, month=adv["advance_date"][:7]))


@app.route("/employees/<int:employee_id>/delete", methods=["POST"])
@admin_required
@change_required
def employee_delete(employee_id):
    conn = dbm.get_db()
    employee = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("employees_list"))
    has_history = (
        conn.execute(
            "SELECT COUNT(*) c FROM employee_attendance WHERE employee_id=?", (employee_id,)
        ).fetchone()["c"]
        or conn.execute(
            "SELECT COUNT(*) c FROM employee_advances WHERE employee_id=?", (employee_id,)
        ).fetchone()["c"]
    )
    if has_history:
        conn.execute("UPDATE employees SET is_active=0 WHERE id=?", (employee_id,))
        conn.commit()
        flash(
            "This employee has attendance/advance history, so they've been archived "
            "rather than deleted \u2014 their records stay intact.", "success",
        )
    else:
        conn.execute("DELETE FROM employees WHERE id=?", (employee_id,))
        conn.commit()
        flash("Employee deleted.", "success")
    return redirect(url_for("employees_list"))


@app.route("/employees/attendance", methods=["GET", "POST"])
@login_required
def attendance_bulk():
    conn = dbm.get_db()
    employees = conn.execute("SELECT * FROM employees WHERE is_active=1 ORDER BY name").fetchall()

    if request.method == "POST":
        att_date = request.form.get("att_date") or date.today().isoformat()
        for e in employees:
            status = request.form.get(f"status_{e['id']}")
            if status:
                conn.execute(
                    "INSERT INTO employee_attendance (employee_id, date, status) VALUES (?,?,?) "
                    "ON CONFLICT(employee_id, date) DO UPDATE SET status=excluded.status",
                    (e["id"], att_date, status),
                )
        conn.commit()
        flash(f"Attendance saved for {att_date}.", "success")
        return redirect(url_for("attendance_bulk", date=att_date))

    att_date = request.args.get("date") or date.today().isoformat()
    existing = {
        r["employee_id"]: r["status"] for r in conn.execute(
            "SELECT employee_id, status FROM employee_attendance WHERE date=?", (att_date,)
        ).fetchall()
    }
    return render_template("attendance_bulk.html", employees=employees, att_date=att_date, existing=existing)


# ---------- analytics ----------

@app.route("/analytics")
@admin_required
def analytics():
    conn = dbm.get_db()
    today = date.today()

    # On a large database the rows are never loaded - the grouped queries below do the
    # work in SQL instead. Pulling two million rows here took about twenty seconds.
    if is_big(conn):
        sales = all_orders = orders_completed = None
    else:
        sales = conn.execute(
            "SELECT sale_date, total_amount, total_profit FROM sales").fetchall()
        all_orders = conn.execute(
            "SELECT status, completed_date, agreed_total, profit FROM orders").fetchall()
        orders_completed = [o for o in all_orders
                            if o["status"] == "completed" and o["completed_date"]]

    # daily revenue & profit, last 30 days
    daily_trend = []
    big = sales is None
    day_map = {}
    if big:
        # One grouped query for the whole window rather than 30 separate ones.
        lo = (today - timedelta(days=29)).isoformat()
        hi = today.isoformat()
        for r in conn.execute(
            "SELECT sale_date d, SUM(total_amount) rev, SUM(total_profit) pro FROM sales "
            "WHERE sale_date BETWEEN ? AND ? GROUP BY sale_date", (lo, hi)):
            day_map[r["d"]] = [r["rev"] or 0, r["pro"] or 0]
        for r in conn.execute(
            "SELECT completed_date d, SUM(agreed_total) rev, SUM(profit) pro FROM orders "
            "WHERE status='completed' AND completed_date BETWEEN ? AND ? GROUP BY completed_date",
                (lo, hi)):
            e = day_map.setdefault(r["d"], [0, 0])
            e[0] += r["rev"] or 0; e[1] += r["pro"] or 0
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        dstr = d.isoformat()
        if big:
            rev, profit = day_map.get(dstr, [0, 0])
        else:
            rev = sum(s["total_amount"] for s in sales if s["sale_date"] == dstr)
            rev += sum(o["agreed_total"] for o in orders_completed if o["completed_date"] == dstr)
            profit = sum(s["total_profit"] for s in sales if s["sale_date"] == dstr)
            profit += sum(o["profit"] for o in orders_completed if o["completed_date"] == dstr)
        daily_trend.append({"date": d.strftime("%d %b"), "revenue": rev, "profit": profit})

    # monthly revenue & profit, last 12 months
    monthly_trend = []
    month_map = {}
    if big:
        # One grouped query rather than twelve passes over every row in the table.
        for r in conn.execute(
            "SELECT substr(sale_date,1,7) m, SUM(total_amount) rev, SUM(total_profit) pro "
            "FROM sales GROUP BY m"):
            month_map[r["m"]] = [r["rev"] or 0, r["pro"] or 0]
        for r in conn.execute(
            "SELECT substr(completed_date,1,7) m, SUM(agreed_total) rev, SUM(profit) pro "
            "FROM orders WHERE status='completed' AND completed_date IS NOT NULL GROUP BY m"):
            e = month_map.setdefault(r["m"], [0, 0])
            e[0] += r["rev"] or 0; e[1] += r["pro"] or 0
    for i in range(11, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        start_d = date(y, m, 1)
        end_d = date(y + (1 if m == 12 else 0), (1 if m == 12 else m + 1), 1) - timedelta(days=1)
        if big:
            rev, profit = month_map.get(f"{y:04d}-{m:02d}", [0, 0])
        else:
            lo_, hi_ = start_d.isoformat(), end_d.isoformat()
            rev = sum(x["total_amount"] for x in sales if lo_ <= x["sale_date"] <= hi_)
            rev += sum(o["agreed_total"] for o in orders_completed if lo_ <= o["completed_date"] <= hi_)
            profit = sum(x["total_profit"] for x in sales if lo_ <= x["sale_date"] <= hi_)
            profit += sum(o["profit"] for o in orders_completed if lo_ <= o["completed_date"] <= hi_)
        monthly_trend.append({"label": start_d.strftime("%b %Y"), "revenue": rev, "profit": profit})

    # month picker: every month that has sale data, plus the current month
    if big:
        month_set = {r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(sale_date,1,7) m FROM sales") if r["m"]}
        month_set |= {r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(completed_date,1,7) m FROM orders "
            "WHERE status='completed' AND completed_date IS NOT NULL") if r["m"]}
    else:
        month_set = {s["sale_date"][:7] for s in sales}
        month_set |= {o["completed_date"][:7] for o in orders_completed}
    month_set.add(today.strftime("%Y-%m"))
    month_options = []
    for key in sorted(month_set, reverse=True):
        y, m = map(int, key.split("-"))
        month_options.append({"value": key, "label": date(y, m, 1).strftime("%B %Y")})

    selected_month = request.args.get("month") or today.strftime("%Y-%m")
    if selected_month not in month_set:
        selected_month = today.strftime("%Y-%m")
    sy, sm = map(int, selected_month.split("-"))
    mstart = date(sy, sm, 1)
    mend = date(sy + (1 if sm == 12 else 0), (1 if sm == 12 else sm + 1), 1) - timedelta(days=1)

    # product breakdown for the selected month - regular sales plus catalog lines from
    # custom orders completed that month, with one-off lines grouped as custom work
    # A chart with 20,000 slices is neither readable nor renderable. The top 25 carry
    # the story; the rest are grouped.
    product_rows = product_breakdown(conn, mstart, mend, limit=25, order_by="revenue")

    return render_template(
        "analytics.html",
        daily_trend=daily_trend,
        monthly_trend=monthly_trend,
        month_options=month_options,
        selected_month=selected_month,
        product_rows=product_rows,
    )


if __name__ == "__main__":
    with app.app_context():
        os.makedirs(os.path.join(APP_DIR, "instance"), exist_ok=True)
        # Safe to run every time: schema.sql only uses CREATE TABLE/INDEX IF NOT EXISTS,
        # so this adds any new tables from an app update without touching existing data.
        dbm.init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
