"""Business metrics, worked out from what is already in Ledger.

Every figure here is derived - nothing is entered by hand, nothing is stored. That
matters: a metric you have to maintain is a metric that goes stale and lies to you.

Where the data cannot honestly support a metric yet, the function returns a "ready"
flag of False and a plain reason. A confident wrong number is worse than a blank.
"""
from datetime import date, timedelta


def _fdate(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _months_of_data(conn):
    """How many distinct months have any sales in them."""
    rows = conn.execute(
        "SELECT DISTINCT substr(sale_date,1,7) m FROM sales "
        "UNION SELECT DISTINCT substr(completed_date,1,7) FROM orders "
        "WHERE status='completed' AND completed_date IS NOT NULL").fetchall()
    return len([r for r in rows if r[0]])


# ------------------------------------------------------------------ profitability

def break_even(app, conn, months=3):
    """How much you must sell each month to cover overheads before earning anything.

    Uses your real gross margin, so it answers in rupees of sales rather than units -
    which is what you actually plan against.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)
    revenue, profit = st["revenue"], st["profit"]
    margin = (profit / revenue) if revenue > 0 else 0

    exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE expense_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchone()["t"]
    monthly_exp = exp / months if months else 0

    if margin <= 0 or monthly_exp <= 0:
        return {"ready": False,
                "why": "Needs some sales and some expenses recorded before this means anything."}

    needed = monthly_exp / margin
    # Where you actually are, this month so far
    som = end.replace(day=1)
    this_month = app.period_stats(sales, comp, som, end)["revenue"]
    day_of_month = end.day
    pace = (this_month / day_of_month * 30) if day_of_month else 0

    return {
        "ready": True,
        "margin": margin * 100,
        "monthly_expenses": monthly_exp,
        "needed": needed,
        "per_day": needed / 26,
        "this_month": this_month,
        "projected": pace,
        "cover": (pace / needed) if needed else 0,
        "safety": pace - needed,
        "months": months,
    }


def owner_salary(app, conn, months=3):
    """What the business can afford to pay you, month after month.

    Deliberately conservative. It takes the AVERAGE month rather than the best one,
    because a seasonal business that pays itself from a peak month runs dry in the
    quiet one. It then holds back a reserve for the cash already tied up in stock and
    customer credit, since that money is earned but not in your hand.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)

    exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE expense_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchone()["t"]

    gross_pm = st["profit"] / months
    exp_pm = exp / months
    net_pm = gross_pm - exp_pm

    if net_pm <= 0:
        return {"ready": False,
                "why": "The business is not yet earning more than it spends over this "
                       "period, so there is nothing to draw safely."}

    receivable = conn.execute(
        "SELECT COALESCE(SUM(credit_balance),0) t FROM customers WHERE credit_balance>0"
    ).fetchone()["t"]
    payable = conn.execute(
        "SELECT COALESCE(SUM(payable_balance),0) t FROM suppliers WHERE payable_balance>0"
    ).fetchone()["t"]
    cash = app.money_position(conn, sales, all_orders, comp)["cash_in_hand"]

    # Three tiers. Safe is what you can take without thinking; stretch assumes the
    # month behaves and the collections come in.
    safe = net_pm * 0.35
    moderate = net_pm * 0.50
    stretch = net_pm * 0.65

    # Reasons to hold back, stated plainly rather than buried in the arithmetic.
    cautions = []
    if cash < exp_pm:
        cautions.append("Cash in hand is less than one month of expenses. Build that "
                        "buffer before drawing at the top of this range.")
    if receivable > st["revenue"] / months:
        cautions.append("Customers owe you more than a full month of sales. Much of your "
                        "profit is in their pockets, not yours.")
    if payable > cash * 3 and cash > 0:
        cautions.append("What you owe suppliers is well ahead of your cash. Keep enough "
                        "back to meet those bills.")
    if _months_of_data(conn) < 3:
        cautions.append("Only a few months of data so far - treat this as a first "
                        "estimate and revisit after a full season.")

    return {
        "ready": True, "months": months,
        "gross_pm": gross_pm, "exp_pm": exp_pm, "net_pm": net_pm,
        "safe": safe, "moderate": moderate, "stretch": stretch,
        "cash": cash, "receivable": receivable, "payable": payable,
        "cautions": cautions,
        "annual_safe": safe * 12,
    }


# ------------------------------------------------------------------ customers

def customer_metrics(app, conn, months=6):
    """Concentration, repeat rate and value per customer."""
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    lo, hi = start.isoformat(), end.isoformat()

    # Group the sales first, then join to names. A subquery per customer means scanning
    # the sales table once for every customer - fine at 30 customers, ruinous at 50,000.
    rows = conn.execute(
        "WITH spend AS ("
        "  SELECT customer_id cid, SUM(total_amount) amt, COUNT(*) n FROM sales "
        "  WHERE customer_id IS NOT NULL AND sale_date BETWEEN ? AND ? GROUP BY customer_id"
        "  UNION ALL "
        "  SELECT customer_id cid, SUM(agreed_total) amt, COUNT(*) n FROM orders "
        "  WHERE status='completed' AND completed_date BETWEEN ? AND ? GROUP BY customer_id"
        ") "
        "SELECT c.id, c.name, SUM(s.amt) spend, SUM(s.n) visits "
        "FROM spend s JOIN customers c ON c.id = s.cid "
        "WHERE c.is_active=1 GROUP BY c.id ORDER BY spend DESC",
        (lo, hi, lo, hi)).fetchall()

    buyers = [dict(r) for r in rows if (r["spend"] or 0) > 0]
    if not buyers:
        return {"ready": False, "why": "No customer sales recorded in this period yet."}

    total = sum(b["spend"] for b in buyers)
    repeat = [b for b in buyers if b["visits"] > 1]

    top1 = buyers[0]["spend"] / total * 100 if total else 0
    top5 = sum(b["spend"] for b in buyers[:5]) / total * 100 if total else 0

    # Walk-in trade has no customer attached, which is fine - but it should not be
    # counted as concentration risk, so it is reported separately.
    walkin = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) t FROM sales "
        "WHERE customer_id IS NULL AND sale_date BETWEEN ? AND ?", (lo, hi)).fetchone()["t"]

    if top5 >= 70:
        risk, risk_note = "high", ("Most of your named-customer revenue sits with five "
                                   "buyers. Losing one would hurt.")
    elif top5 >= 45:
        risk, risk_note = "medium", ("A sizeable share rests on a few buyers. Worth "
                                     "widening the base when you have room.")
    else:
        risk, risk_note = "low", "Your revenue is well spread. That is a resilient position."

    return {
        "ready": True, "months": months,
        "total": total, "walkin": walkin,
        "buyers": len(buyers), "repeat": len(repeat),
        "repeat_rate": len(repeat) / len(buyers) * 100,
        "avg_spend": total / len(buyers),
        "avg_visits": sum(b["visits"] for b in buyers) / len(buyers),
        "top1": top1, "top5": top5, "risk": risk, "risk_note": risk_note,
        "top_list": buyers[:8],
    }


# ------------------------------------------------------------------ production

def production_metrics(app, conn, months=3):
    """Output, cost per unit and material yield from recorded production runs."""
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    runs = conn.execute(
        "SELECT pr.*, i.name iname, i.unit FROM production_runs pr "
        "JOIN items i ON i.id = pr.output_item_id "
        "WHERE pr.production_date BETWEEN ? AND ? ORDER BY pr.production_date",
        (start.isoformat(), end.isoformat())).fetchall()

    if not runs:
        return {"ready": False,
                "why": "No production runs recorded in this period. Record them under "
                       "New Production and this fills in by itself."}

    total_out = sum(r["output_qty"] or 0 for r in runs)
    material = sum(r["total_material_cost"] or 0 for r in runs)
    labour = sum((r["labor_cost"] or 0) + (r["other_cost"] or 0) for r in runs)
    total_cost = material + labour

    by_item = {}
    for r in runs:
        k = r["iname"]
        d = by_item.setdefault(k, {"name": k, "unit": r["unit"], "qty": 0.0, "cost": 0.0, "runs": 0})
        d["qty"] += r["output_qty"] or 0
        d["cost"] += (r["total_material_cost"] or 0) + (r["labor_cost"] or 0) + (r["other_cost"] or 0)
        d["runs"] += 1
    for d in by_item.values():
        d["per_unit"] = d["cost"] / d["qty"] if d["qty"] else 0
    items = sorted(by_item.values(), key=lambda d: -d["qty"])

    # Staff cost against output, which is the honest version of "productivity" for a
    # workshop this size - no shift logs, no machine hours.
    staff = conn.execute(
        "SELECT COALESCE(SUM(monthly_salary),0) t FROM employees WHERE is_active=1"
    ).fetchone()["t"]
    staff_period = staff * months

    return {
        "ready": True, "months": months, "runs": len(runs),
        "total_out": total_out, "material": material, "labour": labour,
        "total_cost": total_cost,
        "cost_per_unit": total_cost / total_out if total_out else 0,
        "material_share": material / total_cost * 100 if total_cost else 0,
        "labour_share": labour / total_cost * 100 if total_cost else 0,
        "by_item": items[:8],   # not "items" - that collides with dict.items in templates
        "staff_cost": staff_period,
        "output_per_rupee": total_out / staff_period if staff_period else None,
        "runs_per_month": len(runs) / months,
    }


def machine_metrics(app, conn):
    """Machine utilisation and OEE.

    Ledger does not record machine run hours, stoppages or reject counts, and there is
    no honest way to derive them from sales and purchases. Rather than invent a figure,
    this says what would have to be recorded first.
    """
    return {
        "ready": False,
        "why": "OEE needs three things logged on the floor: hours a machine actually "
               "ran against hours available, speed against rated speed, and good pieces "
               "against total pieces. Ledger records none of these, and they cannot be "
               "worked out from sales or purchases. It would need a short entry after "
               "each shift.",
        "would_need": [
            "Hours each machine ran, and hours it was available",
            "Pieces produced against what it should produce in that time",
            "Rejects and rework, counted separately from good output",
        ],
    }


# ------------------------------------------------------------------ forecasting

def forecast(app, conn):
    """Revenue outlook.

    Honest about what one month of data can and cannot tell you. With a few months it
    reports a trend; with a year or more it can also account for the season.
    """
    months_have = _months_of_data(conn)
    end = date.today()
    sales, all_orders, comp = app.load_revenue_sources(conn)

    monthly = []
    for back in range(11, -1, -1):
        m_end = (end.replace(day=1) - timedelta(days=1)) if back == 0 else None
        first = end.replace(day=1)
        for _ in range(back):
            first = (first - timedelta(days=1)).replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        last = nxt - timedelta(days=1)
        if first > end:
            continue
        r = app.period_stats(sales, comp, first, min(last, end))["revenue"]
        monthly.append({"month": first.strftime("%b %Y"), "start": first,
                        "revenue": r, "partial": last > end})

    real = [m for m in monthly if m["revenue"] > 0]
    if months_have < 2 or len(real) < 2:
        return {
            "ready": False, "months_have": months_have,
            "why": "A forecast needs at least three complete months to mean anything, "
                   "and a full year before it can allow for your seasons. With less "
                   "than that it is guesswork dressed up as a number.",
            "monthly": monthly,
        }

    # Simple, explainable: average of complete months, and the direction of travel.
    complete = [m for m in real if not m["partial"]]
    base = complete[-3:] if len(complete) >= 3 else complete
    avg = sum(m["revenue"] for m in base) / len(base) if base else 0

    trend = None
    if len(complete) >= 2:
        first_half = complete[:max(1, len(complete)//2)]
        second_half = complete[max(1, len(complete)//2):]
        a = sum(m["revenue"] for m in first_half)/len(first_half)
        b = sum(m["revenue"] for m in second_half)/len(second_half)
        trend = (b - a) / a * 100 if a else None

    # This month, projected at the current daily pace.
    som = end.replace(day=1)
    mtd = app.period_stats(sales, comp, som, end)["revenue"]
    pace = mtd / end.day * 30 if end.day else 0

    return {
        "ready": True, "months_have": months_have,
        "monthly": monthly, "avg": avg, "trend": trend,
        "mtd": mtd, "pace": pace,
        "confidence": ("low" if months_have < 4 else
                       "fair" if months_have < 12 else "good"),
        "seasonal_ready": months_have >= 12,
    }


# ------------------------------------------------------------------ the whole picture

def financial_model(app, conn, months=3):
    """A single view of the business: what it earns, what it holds, what it owes.

    Everything derived from the same figures the rest of Ledger uses, so nothing here
    can disagree with the Money Position or the Reports page.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)
    pos = app.money_position(conn, sales, all_orders, comp)

    exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE expense_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchone()["t"]

    revenue = st["revenue"]
    gross = st["profit"]
    net = gross - exp

    worth = pos["business_worth"]
    # Return on what is tied up in the business - the closest honest equivalent to the
    # ratios a large company would quote.
    annual_net = (net / months) * 12
    roce = (annual_net / worth * 100) if worth > 0 else None

    return {
        "months": months,
        "revenue": revenue, "cogs": st["cost"], "gross": gross, "expenses": exp, "net": net,
        "gross_margin": gross / revenue * 100 if revenue else 0,
        "net_margin": net / revenue * 100 if revenue else 0,
        "revenue_pm": revenue / months, "net_pm": net / months,
        "annual_run_rate": revenue / months * 12,
        "cash": pos["cash_in_hand"], "receivable": pos["receivable"],
        "stock": pos["stock_value"], "payable": pos["payable"], "worth": worth,
        "roce": roce,
        "expense_ratio": exp / revenue * 100 if revenue else 0,
    }


# ================================================================== growth & efficiency
#
# Company-level KPIs beyond the Overview tab. Every one of these is computed fresh from
# the same rows the rest of Ledger uses - nothing stored, nothing that can go stale.
#
# Three of them - machine utilisation, waste, reprints - genuinely cannot be worked out
# from sales and purchases. Rather than fake a number, each checks whether it has been
# logged at all and says so plainly when it has not. A confident wrong number is worse
# than an honest blank.

def expense_classification(app, conn):
    """Which expense categories the owner has marked fixed (rent, standing salary) vs
    variable (things that move with how much you sell or make).

    Contribution margin needs this split and Ledger cannot guess it - "Salary" is fixed
    for a monthly-paid tailor and variable for day-rate labour, and only the owner knows
    which this is. Defaults to a reasonable guess on first use; the owner can correct it
    once in Settings and it is remembered from then on.
    """
    raw = app.get_setting(conn, "fixed_expense_categories", "")
    if raw:
        fixed = {c.strip().lower() for c in raw.split(",") if c.strip()}
    else:
        fixed = {"rent", "salary", "salaries", "wages"}
    return fixed


def contribution_margin(app, conn, months=3):
    """Revenue minus material cost minus VARIABLE overhead - what is left to cover
    fixed costs and profit. Different from gross margin only in that fixed overhead
    (rent, standing salary) is left out rather than lumped in with material cost.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)

    fixed_cats = expense_classification(app, conn)
    rows = conn.execute(
        "SELECT category, SUM(amount) t FROM expenses WHERE expense_date BETWEEN ? AND ? "
        "GROUP BY lower(trim(category))", (start.isoformat(), end.isoformat())).fetchall()
    fixed_total = sum(r["t"] for r in rows if (r["category"] or "").strip().lower() in fixed_cats)
    variable_total = sum(r["t"] for r in rows if (r["category"] or "").strip().lower() not in fixed_cats)

    revenue = st["revenue"]
    cm = revenue - st["cost"] - variable_total
    return {
        "ready": revenue > 0, "months": months,
        "revenue": revenue, "material": st["cost"],
        "variable_expenses": variable_total, "fixed_expenses": fixed_total,
        "contribution_margin": cm,
        "contribution_pct": (cm / revenue * 100) if revenue else 0,
        "net_after_fixed": cm - fixed_total,
    }


def customer_acquisition_cost(app, conn, months=3):
    """What it cost, in tagged spend, to bring in each new customer this period.

    Ledger has no separate marketing budget line by default - this only produces a
    number once the owner has actually tagged some expense categories as acquisition
    spend (adverts, referral commission, a stall at a trade fair). Until then it says
    plainly that nothing is tagged, rather than quietly reporting zero as if that were
    the true cost of winning a customer.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    tagged = app.get_setting(conn, "acquisition_expense_categories", "")
    cats = {c.strip().lower() for c in tagged.split(",") if c.strip()}

    new_customers = conn.execute(
        "SELECT COUNT(*) n FROM customers WHERE created_at >= ? AND created_at <= ?",
        (start.isoformat(), (end + timedelta(days=1)).isoformat())).fetchone()["n"]

    if not cats:
        return {"ready": False, "tagged": False, "new_customers": new_customers,
                "why": "No expense category is tagged as customer acquisition spend "
                       "(advertising, referral fees, a stall at a fair). Tag one in "
                       "Settings and this will compute a real cost per new customer."}

    rows = conn.execute(
        "SELECT category, SUM(amount) t FROM expenses WHERE expense_date BETWEEN ? AND ? "
        "GROUP BY lower(trim(category))", (start.isoformat(), end.isoformat())).fetchall()
    spend = sum(r["t"] for r in rows if (r["category"] or "").strip().lower() in cats)

    if new_customers == 0:
        return {"ready": False, "tagged": True, "spend": spend, "new_customers": 0,
                "why": f"{spend:,.0f} tagged as acquisition spend, but no new customers "
                       "were added in this period, so a cost-per-customer cannot be worked out."}

    return {"ready": True, "tagged": True, "spend": spend, "new_customers": new_customers,
            "cac": spend / new_customers, "months": months}


def average_order_value(app, conn, months=3):
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)
    if st["count"] == 0:
        return {"ready": False, "why": "No completed transactions in this period."}
    return {"ready": True, "months": months, "revenue": st["revenue"],
            "count": st["count"], "aov": st["revenue"] / st["count"]}


def on_time_delivery(app, conn, months=3):
    """Of the custom orders that had a promised date, how many actually went out on
    or before it. Free to compute - both dates are already on every completed order -
    and it is the most direct measure of whether the shop keeps its word.
    """
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    rows = conn.execute(
        "SELECT expected_ready_date, completed_date FROM orders "
        "WHERE status='completed' AND completed_date BETWEEN ? AND ? "
        "AND expected_ready_date IS NOT NULL",
        (start.isoformat(), end.isoformat())).fetchall()
    if not rows:
        return {"ready": False,
                "why": "No completed orders with a promised date in this period."}
    on_time = sum(1 for r in rows if r["completed_date"] <= r["expected_ready_date"])
    avg_slip = sum(
        (date.fromisoformat(r["completed_date"]) - date.fromisoformat(r["expected_ready_date"])).days
        for r in rows) / len(rows)
    return {"ready": True, "months": months, "total": len(rows), "on_time": on_time,
            "pct": on_time / len(rows) * 100, "avg_slip_days": avg_slip}


def revenue_per_employee(app, conn, months=3):
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    sales, all_orders, comp = app.load_revenue_sources(conn)
    st = app.period_stats(sales, comp, start, end)
    n = conn.execute("SELECT COUNT(*) n FROM employees WHERE is_active=1").fetchone()["n"]
    if n == 0:
        return {"ready": False, "why": "No active employees recorded."}
    return {"ready": True, "months": months, "revenue": st["revenue"], "employees": n,
            "per_employee": st["revenue"] / n / months}


def reprint_rate(app, conn, months=3):
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    rows = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(is_reprint),0) r FROM orders "
        "WHERE status='completed' AND completed_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchone()
    if rows["n"] == 0:
        return {"ready": False, "why": "No completed orders in this period."}
    return {"ready": True, "months": months, "total": rows["n"], "reprints": rows["r"],
            "pct": rows["r"] / rows["n"] * 100}


def waste_rate(app, conn, months=3):
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    rows = conn.execute(
        "SELECT waste_qty, output_qty FROM production_runs "
        "WHERE production_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchall()
    logged = [r for r in rows if r["waste_qty"] is not None]
    if not logged:
        return {"ready": False, "total_runs": len(rows),
                "why": "Waste is not logged on any production run yet. Add it as an "
                       "optional field when you record production and this fills in."}
    total_waste = sum(r["waste_qty"] for r in logged)
    total_output = sum(r["output_qty"] for r in logged)
    denom = total_waste + total_output
    return {"ready": True, "months": months, "logged_runs": len(logged),
            "total_runs": len(rows), "waste": total_waste, "output": total_output,
            "pct": (total_waste / denom * 100) if denom else 0}


def machine_utilisation(conn, months=3):
    end = date.today()
    start = end - timedelta(days=months * 30 - 1)
    rows = conn.execute(
        "SELECT * FROM machine_logs WHERE log_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchall()
    if not rows:
        return {"ready": False,
                "why": "No shift logs recorded yet. Machine utilisation needs hours run "
                       "against hours available, logged after each shift - Ledger cannot "
                       "work this out from sales or purchases."}
    hrun = sum(r["hours_run"] for r in rows)
    havail = sum(r["hours_available"] for r in rows)
    good = sum(r["good_units"] for r in rows)
    reject = sum(r["reject_units"] for r in rows)
    total_units = good + reject
    return {
        "ready": True, "months": months, "logs": len(rows),
        "hours_run": hrun, "hours_available": havail,
        "utilisation_pct": (hrun / havail * 100) if havail else 0,
        "quality_pct": (good / total_units * 100) if total_units else None,
    }


# ================================================================== customer growth
#
# One row per customer, all-time. This is the table that answers "which customers are
# actually carrying the company" - sorted by lifetime revenue, it is a straight Pareto
# list. Every figure is derived from sales, orders and payments already on file.

def customer_growth_table(app, conn, sort="revenue", direction="desc", search=""):
    today = date.today()
    where = ""
    params = []
    if search:
        where = "WHERE c.name LIKE ? OR c.phone LIKE ?"
        params = [f"%{search}%", f"%{search}%"]

    rows = conn.execute(
        f"SELECT c.id, c.name, c.phone, c.customer_type, c.credit_balance, c.created_at, "
        f"COALESCE((SELECT SUM(s.total_amount) FROM sales s WHERE s.customer_id=c.id),0) "
        f" + COALESCE((SELECT SUM(o.agreed_total) FROM orders o WHERE o.customer_id=c.id "
        f"    AND o.status='completed'),0) AS revenue, "
        f"COALESCE((SELECT SUM(s.total_profit) FROM sales s WHERE s.customer_id=c.id),0) "
        f" + COALESCE((SELECT SUM(o.profit) FROM orders o WHERE o.customer_id=c.id "
        f"    AND o.status='completed'),0) AS profit, "
        f"COALESCE((SELECT COUNT(*) FROM sales s WHERE s.customer_id=c.id),0) "
        f" + COALESCE((SELECT COUNT(*) FROM orders o WHERE o.customer_id=c.id "
        f"    AND o.status='completed'),0) AS tx_count, "
        f"(SELECT MAX(x) FROM (SELECT MAX(sale_date) x FROM sales WHERE customer_id=c.id "
        f"   UNION SELECT MAX(completed_date) FROM orders WHERE customer_id=c.id "
        f"   AND status='completed')) AS last_order "
        f"FROM customers c {where} ORDER BY c.id", params).fetchall()

    out = []
    for r in rows:
        if r["tx_count"] == 0:
            continue  # a customer record with no transaction yet has nothing to rank
        try:
            first = date.fromisoformat((r["created_at"] or "")[:10])
        except ValueError:
            first = today
        years_active = max((today - first).days / 365.25, 1 / 12)  # floor at one month
        last_order = r["last_order"]
        days_since = (today - date.fromisoformat(last_order)).days if last_order else None
        out.append({
            "id": r["id"], "name": r["name"], "phone": r["phone"],
            "type": r["customer_type"], "revenue": r["revenue"], "profit": r["profit"],
            "margin_pct": (r["profit"] / r["revenue"] * 100) if r["revenue"] else 0,
            "tx_count": r["tx_count"],
            "orders_per_year": r["tx_count"] / years_active,
            "aov": r["revenue"] / r["tx_count"] if r["tx_count"] else 0,
            "last_order": last_order, "days_since": days_since,
            "outstanding": r["credit_balance"] or 0,
        })

    key_map = {
        "revenue": lambda x: x["revenue"], "profit": lambda x: x["profit"],
        "margin": lambda x: x["margin_pct"], "orders": lambda x: x["tx_count"],
        "opy": lambda x: x["orders_per_year"], "aov": lambda x: x["aov"],
        "recency": lambda x: x["days_since"] if x["days_since"] is not None else 999999,
        "outstanding": lambda x: x["outstanding"],
    }
    keyfn = key_map.get(sort, key_map["revenue"])
    out.sort(key=keyfn, reverse=(direction != "asc"))

    total_revenue = sum(c["revenue"] for c in out)
    running = 0.0
    for c in sorted(out, key=lambda x: -x["revenue"]):
        running += c["revenue"]
        c["cum_share_by_revenue"] = (running / total_revenue * 100) if total_revenue else 0
    # attach the cumulative share computed above back onto the requested sort order
    share_by_id = {c["id"]: c["cum_share_by_revenue"] for c in out}
    for c in out:
        c["cum_share_by_revenue"] = share_by_id[c["id"]]

    n = len(out)
    top20_n = max(1, round(n * 0.2))
    top20 = sorted(out, key=lambda x: -x["revenue"])[:top20_n]
    top20_share = (sum(c["revenue"] for c in top20) / total_revenue * 100) if total_revenue else 0

    return {
        "customers": out, "total_revenue": total_revenue, "count": n,
        "top20_n": top20_n, "top20_share": top20_share,
        "sort": sort, "direction": direction, "search": search,
    }
