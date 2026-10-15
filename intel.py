"""The intelligence layer: connections, anomalies, and root causes.

Everything here is read-only and derived from tables that already exist - nothing new
is stored. It looks at the same rows the rest of Ledger uses and surfaces the shape of
them: what is connected to what, what does not fit the pattern, and where a problem
actually starts rather than where it is merely visible.

Kept in its own module, the same way metrics.py is, so app.py does not have to grow
further and so this can be tested on its own.
"""
import math
from datetime import date, timedelta


def _trunc(s, n=22):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "\u2026"


# ================================================================== entity graphs
#
# A graph here means: one centre, up to three rings of real relationships around it,
# each ring only ever built from a join that already exists in the schema. Nothing is
# inferred that the data cannot actually support - a customer connects to suppliers
# only via order_outsourced, because that is the one table that genuinely links them.

def customer_graph(conn, customer_id, limit=8):
    cust = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        return None
    nodes = [{"id": "c", "kind": "center", "ring": 0,
              "label": _trunc(cust["name"], 26), "sub": cust["customer_type"] or "",
              "url": f"/customers/{cust['id']}"}]
    edges = []

    sales = conn.execute(
        "SELECT * FROM sales WHERE customer_id=? ORDER BY sale_date DESC LIMIT ?",
        (customer_id, limit)).fetchall()
    orders = conn.execute(
        "SELECT * FROM orders WHERE customer_id=? ORDER BY order_date DESC LIMIT ?",
        (customer_id, limit)).fetchall()

    seen_items, seen_suppliers = {}, {}
    for s in sales:
        nid = f"s{s['id']}"
        nodes.append({"id": nid, "kind": "sale", "ring": 1,
                      "label": f"Sale #{s['id']}", "sub": s["sale_date"],
                      "url": f"/sales/{s['id']}"})
        edges.append(("c", nid))
        for li in conn.execute(
                "SELECT item_id FROM sale_items WHERE sale_id=?", (s["id"],)):
            iid = li["item_id"]
            if iid is None:
                continue
            item_nid = f"i{iid}"
            if item_nid not in seen_items:
                it = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
                if not it:
                    continue
                nodes.append({"id": item_nid, "kind": "item", "ring": 2,
                              "label": _trunc(it["name"]), "sub": it["unit"],
                              "url": f"/items/{iid}/edit"})
                seen_items[item_nid] = True
            edges.append((nid, item_nid))

    for o in orders:
        nid = f"o{o['id']}"
        nodes.append({"id": nid, "kind": "order", "ring": 1,
                      "label": f"Order #{o['id']}", "sub": o["status"],
                      "url": f"/orders/{o['id']}"})
        edges.append(("c", nid))
        for oo in conn.execute(
                "SELECT supplier_id FROM order_outsourced "
                "WHERE order_id=? AND supplier_id IS NOT NULL", (o["id"],)):
            sid = oo["supplier_id"]
            sup_nid = f"sup{sid}"
            if sup_nid not in seen_suppliers:
                sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
                if not sup:
                    continue
                nodes.append({"id": sup_nid, "kind": "supplier", "ring": 3,
                              "label": _trunc(sup["name"]), "sub": "outsourced work",
                              "url": f"/suppliers/{sid}"})
                seen_suppliers[sup_nid] = True
            edges.append((nid, sup_nid))

    return {"nodes": nodes, "edges": edges, "center_kind": "customer",
            "counts": {"sales": len(sales), "orders": len(orders),
                      "items": len(seen_items), "suppliers": len(seen_suppliers)}}


def supplier_graph(conn, supplier_id, limit=8):
    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not sup:
        return None
    nodes = [{"id": "c", "kind": "center", "ring": 0,
              "label": _trunc(sup["name"], 26), "sub": "supplier",
              "url": f"/suppliers/{sup['id']}"}]
    edges = []

    purchases = conn.execute(
        "SELECT p.*, i.name item_name FROM purchases p JOIN items i ON i.id = p.item_id "
        "WHERE p.supplier_id=? ORDER BY p.purchase_date DESC LIMIT ?",
        (supplier_id, limit)).fetchall()
    outsourced = conn.execute(
        "SELECT oo.*, o.id oid, o.customer_id FROM order_outsourced oo "
        "JOIN orders o ON o.id = oo.order_id WHERE oo.supplier_id=? "
        "ORDER BY oo.id DESC LIMIT ?", (supplier_id, limit)).fetchall()

    seen_items, seen_custs = {}, {}
    for p in purchases:
        nid = f"p{p['id']}"
        nodes.append({"id": nid, "kind": "purchase", "ring": 1,
                      "label": f"Purchase #{p['id']}", "sub": p["purchase_date"],
                      "url": "/purchases"})
        edges.append(("c", nid))
        item_nid = f"i{p['item_id']}"
        if item_nid not in seen_items:
            nodes.append({"id": item_nid, "kind": "item", "ring": 2,
                          "label": _trunc(p["item_name"]), "sub": "",
                          "url": f"/items/{p['item_id']}/edit"})
            seen_items[item_nid] = True
        edges.append((nid, item_nid))

    for oo in outsourced:
        nid = f"j{oo['id']}"
        nodes.append({"id": nid, "kind": "order", "ring": 1,
                      "label": f"Order #{oo['oid']}", "sub": _trunc(oo["description"], 18),
                      "url": f"/orders/{oo['oid']}"})
        edges.append(("c", nid))
        if oo["customer_id"]:
            cid = oo["customer_id"]
            cust_nid = f"cu{cid}"
            if cust_nid not in seen_custs:
                cu = conn.execute("SELECT name FROM customers WHERE id=?", (cid,)).fetchone()
                if cu:
                    nodes.append({"id": cust_nid, "kind": "customer", "ring": 3,
                                  "label": _trunc(cu["name"]), "sub": "their job",
                                  "url": f"/customers/{cid}"})
                    seen_custs[cust_nid] = True
            edges.append((nid, cust_nid))

    return {"nodes": nodes, "edges": edges, "center_kind": "supplier",
            "counts": {"purchases": len(purchases), "outsourced": len(outsourced),
                      "items": len(seen_items), "customers": len(seen_custs)}}


def item_graph(conn, item_id, limit=8):
    it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        return None
    nodes = [{"id": "c", "kind": "center", "ring": 0,
              "label": _trunc(it["name"], 26), "sub": it["item_type"],
              "url": f"/items/{it['id']}/edit"}]
    edges = []

    sales = conn.execute(
        "SELECT s.id sid, s.sale_date, s.customer_id, c.name cname FROM sale_items si "
        "JOIN sales s ON s.id = si.sale_id LEFT JOIN customers c ON c.id = s.customer_id "
        "WHERE si.item_id=? ORDER BY s.sale_date DESC LIMIT ?", (item_id, limit)).fetchall()
    purchases = conn.execute(
        "SELECT p.*, s.name sname FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id "
        "WHERE p.item_id=? ORDER BY p.purchase_date DESC LIMIT ?", (item_id, limit)).fetchall()

    seen_custs = {}
    for s in sales:
        nid = f"s{s['sid']}"
        nodes.append({"id": nid, "kind": "sale", "ring": 1,
                      "label": f"Sale #{s['sid']}", "sub": s["sale_date"],
                      "url": f"/sales/{s['sid']}"})
        edges.append(("c", nid))
        if s["customer_id"]:
            cust_nid = f"cu{s['customer_id']}"
            if cust_nid not in seen_custs:
                nodes.append({"id": cust_nid, "kind": "customer", "ring": 2,
                              "label": _trunc(s["cname"]), "sub": "buyer",
                              "url": f"/customers/{s['customer_id']}"})
                seen_custs[cust_nid] = True
            edges.append((nid, cust_nid))

    seen_sups = {}
    for p in purchases:
        nid = f"p{p['id']}"
        nodes.append({"id": nid, "kind": "purchase", "ring": 1,
                      "label": f"Purchase #{p['id']}", "sub": p["purchase_date"],
                      "url": "/purchases"})
        edges.append(("c", nid))
        if p["supplier_id"]:
            sup_nid = f"sup{p['supplier_id']}"
            if sup_nid not in seen_sups:
                nodes.append({"id": sup_nid, "kind": "supplier", "ring": 2,
                              "label": _trunc(p["sname"]), "sub": "source",
                              "url": f"/suppliers/{p['supplier_id']}"})
                seen_sups[sup_nid] = True
            edges.append((nid, sup_nid))

    return {"nodes": nodes, "edges": edges, "center_kind": "item",
            "counts": {"sales": len(sales), "purchases": len(purchases),
                      "customers": len(seen_custs), "suppliers": len(seen_sups)}}


def radial_layout(graph, width=920, height=720):
    """Places every node on a ring around the centre and resolves each edge to real
    coordinates. Deterministic and dependency-free - no physics engine, so the same
    graph always draws the same way, which matters when you are trying to read it."""
    if not graph:
        return None
    cx, cy = width / 2, height / 2
    radius = {0: 0, 1: 165, 2: 290, 3: 410}

    by_ring = {}
    for n in graph["nodes"]:
        by_ring.setdefault(n["ring"], []).append(n)

    pos = {}
    for ring, ns in by_ring.items():
        r = radius.get(ring, 410)
        count = len(ns)
        for i, node in enumerate(ns):
            if ring == 0:
                x, y = cx, cy
            else:
                angle = (i / count) * 2 * math.pi - math.pi / 2
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
            node["x"], node["y"] = round(x, 1), round(y, 1)
            pos[node["id"]] = (node["x"], node["y"])

    edges = []
    for a, b in graph["edges"]:
        if a in pos and b in pos:
            x1, y1 = pos[a]
            x2, y2 = pos[b]
            edges.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    return {"nodes": graph["nodes"], "edges": edges, "width": width, "height": height,
            "center_kind": graph["center_kind"], "counts": graph["counts"]}


# ================================================================== anomalies
#
# Every check compares a transaction to ITS OWN trailing history - the same item, the
# same kind of thing - never to a global average. A thin-margin product is not an
# anomaly just for being thin-margin; it is only flagged if IT, specifically, just did
# something unlike its own recent pattern. Needs a minimum history before it will flag
# anything, so a brand new item with two sales never gets a false alarm.

def sale_margin_anomalies(conn, days=30, min_history=5, threshold_pts=15, cap=400):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT s.id sid, s.sale_date, c.name cname, si.item_id, i.name iname, "
        "si.line_total, si.line_profit FROM sale_items si "
        "JOIN sales s ON s.id = si.sale_id JOIN items i ON i.id = si.item_id "
        "LEFT JOIN customers c ON c.id = s.customer_id "
        "WHERE s.sale_date >= ? AND si.line_total > 0.5 "
        "ORDER BY s.sale_date DESC LIMIT ?", (cutoff, cap)).fetchall()

    flagged = []
    for r in rows:
        hist = conn.execute(
            "SELECT si.line_total, si.line_profit FROM sale_items si "
            "JOIN sales s ON s.id = si.sale_id "
            "WHERE si.item_id=? AND s.sale_date < ? AND si.line_total > 0.5 "
            "ORDER BY s.sale_date DESC LIMIT 20", (r["item_id"], r["sale_date"])).fetchall()
        if len(hist) < min_history:
            continue
        base_rev = sum(h["line_total"] for h in hist)
        if base_rev <= 0:
            continue
        baseline = sum(h["line_profit"] for h in hist) / base_rev * 100
        actual = r["line_profit"] / r["line_total"] * 100
        gap = baseline - actual
        if gap >= threshold_pts:
            flagged.append({
                "kind": "sale", "id": r["sid"], "date": r["sale_date"],
                "who": r["cname"] or "Walk-in", "what": r["iname"],
                "actual": actual, "baseline": baseline, "gap": gap,
                "url": f"/sales/{r['sid']}",
            })
    flagged.sort(key=lambda f: -f["gap"])
    return flagged[:15]


def purchase_cost_anomalies(conn, days=60, min_history=3, threshold_pct=15, cap=400):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT p.id, p.purchase_date, p.item_id, i.name iname, p.unit_cost, "
        "COALESCE(p.supplier_name, s.name) sname FROM purchases p "
        "JOIN items i ON i.id = p.item_id LEFT JOIN suppliers s ON s.id = p.supplier_id "
        "WHERE p.purchase_date >= ? ORDER BY p.purchase_date DESC LIMIT ?",
        (cutoff, cap)).fetchall()

    flagged = []
    for r in rows:
        hist = conn.execute(
            "SELECT unit_cost FROM purchases WHERE item_id=? AND purchase_date < ? "
            "ORDER BY purchase_date DESC LIMIT 10", (r["item_id"], r["purchase_date"])).fetchall()
        if len(hist) < min_history:
            continue
        baseline = sum(h["unit_cost"] for h in hist) / len(hist)
        if baseline <= 0:
            continue
        pct = (r["unit_cost"] - baseline) / baseline * 100
        if pct >= threshold_pct:
            flagged.append({
                "kind": "purchase", "id": r["id"], "date": r["purchase_date"],
                "who": r["sname"] or "\u2014", "what": r["iname"],
                "actual": r["unit_cost"], "baseline": baseline, "gap": pct,
                "url": "/purchases",
            })
    flagged.sort(key=lambda f: -f["gap"])
    return flagged[:15]


def production_cost_anomalies(conn, days=90, min_history=3, threshold_pct=15, cap=200):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT pr.id, pr.production_date, pr.output_item_id, i.name iname, pr.cost_per_unit "
        "FROM production_runs pr JOIN items i ON i.id = pr.output_item_id "
        "WHERE pr.production_date >= ? AND pr.cost_per_unit > 0 "
        "ORDER BY pr.production_date DESC LIMIT ?", (cutoff, cap)).fetchall()

    flagged = []
    for r in rows:
        hist = conn.execute(
            "SELECT cost_per_unit FROM production_runs WHERE output_item_id=? "
            "AND production_date < ? AND cost_per_unit > 0 "
            "ORDER BY production_date DESC LIMIT 10", (r["output_item_id"], r["production_date"])).fetchall()
        if len(hist) < min_history:
            continue
        baseline = sum(h["cost_per_unit"] for h in hist) / len(hist)
        if baseline <= 0:
            continue
        pct = (r["cost_per_unit"] - baseline) / baseline * 100
        if pct >= threshold_pct:
            flagged.append({
                "kind": "production", "id": r["id"], "date": r["production_date"],
                "who": "\u2014", "what": r["iname"],
                "actual": r["cost_per_unit"], "baseline": baseline, "gap": pct,
                "url": "/production",
            })
    flagged.sort(key=lambda f: -f["gap"])
    return flagged[:15]


def all_anomalies(conn):
    sales = sale_margin_anomalies(conn)
    purch = purchase_cost_anomalies(conn)
    prod = production_cost_anomalies(conn)
    combined = sorted(sales + purch + prod, key=lambda f: -f["gap"])
    return {"sales": sales, "purchases": purch, "production": prod,
            "combined": combined, "total": len(sales) + len(purch) + len(prod)}


# ================================================================== root cause: late orders
#
# "Late" already exists elsewhere in Ledger as a count. This answers the next question -
# late WHERE. Grouped by the stage it is stuck in, which customer it belongs to, and
# which outsourced supplier (if any) is on the job - each a real, joinable relationship,
# not a guess.

def late_order_analysis(conn):
    today = date.today()
    late = conn.execute(
        "SELECT o.*, c.name cname FROM orders o LEFT JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status IN ('pending','design','printing','ready') "
        "AND o.expected_ready_date IS NOT NULL AND o.expected_ready_date < ?",
        (today.isoformat(),)).fetchall()

    by_stage, by_customer, by_supplier = {}, {}, {}
    total_value = 0.0
    for o in late:
        try:
            days_late = (today - date.fromisoformat(o["expected_ready_date"])).days
        except ValueError:
            continue
        total_value += o["agreed_total"] or 0

        st = by_stage.setdefault(o["status"], {"n": 0, "days": 0, "value": 0.0})
        st["n"] += 1; st["days"] += days_late; st["value"] += o["agreed_total"] or 0

        who = o["cname"] or "Walk-in"
        cu = by_customer.setdefault(who, {"n": 0, "days": 0})
        cu["n"] += 1; cu["days"] += days_late

        for oo in conn.execute(
                "SELECT supplier_id FROM order_outsourced "
                "WHERE order_id=? AND supplier_id IS NOT NULL", (o["id"],)):
            sup = conn.execute("SELECT name FROM suppliers WHERE id=?",
                               (oo["supplier_id"],)).fetchone()
            if sup:
                sp = by_supplier.setdefault(sup["name"], {"n": 0, "days": 0})
                sp["n"] += 1; sp["days"] += days_late

    for grp in (by_stage, by_customer, by_supplier):
        for v in grp.values():
            v["avg_days"] = v["days"] / v["n"] if v["n"] else 0

    return {
        "total": len(late), "total_value": total_value,
        "by_stage": sorted(by_stage.items(), key=lambda x: -x[1]["n"]),
        "by_customer": sorted(by_customer.items(), key=lambda x: -x[1]["n"])[:10],
        "by_supplier": sorted(by_supplier.items(), key=lambda x: -x[1]["n"])[:10],
    }
