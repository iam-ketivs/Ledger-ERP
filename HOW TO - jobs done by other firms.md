# Jobs where someone else does the work

For invitations, visiting cards, laminating — anything where you take the
order, hand it to another firm, and keep the difference.

---

## The example

Customer asks for wedding invitations. You quote **₹6,000**.
The 4-colour press charges you **₹4,000**, on credit.
You made **₹2,000**.

---

## How to record it — four steps

**1. Add the printer as a supplier, once**
Suppliers & Payables → + Add Supplier → *4 Colour Press*.
You only ever do this once.

**2. Book the order**
New Order (Advance) → customer → a custom line:
`Wedding invitation — design & print`, qty 1, price **6,000**.
Take an advance if they gave one.

**3. Complete it when you hand the invitations over**
Complete & collect balance. On that screen, under
**"Work bought in from another firm"**:

| Who did it | What they did | They charged | Paid or credit |
|---|---|---|---|
| 4 Colour Press | printing 500 invitations | 4000 | On credit |

Leave raw material blank — you used none of your own stock.

**4. Pay the printer later**
Suppliers & Payables → 4 Colour Press → Pay them → ₹4,000.

---

## What happens to your numbers

**When you complete the order:**

| | |
|---|---|
| Revenue | +₹6,000 |
| Profit | **+₹2,000** |
| Cash in hand | +₹6,000 |
| You owe 4 Colour Press | **+₹4,000** |

**When you pay the printer:**

| | |
|---|---|
| Cash in hand | −₹4,000 |
| You owe them | −₹4,000 |
| **Profit** | **no change** |

Profit doesn't move, because the ₹4,000 was already taken off when you
completed the job. Counting it again would show your profit as ₹2,000
lower than it really was.

---

## Why not just record it as an Expense?

An expense is overhead — rent, power, tea. It belongs to no particular
job and nobody is waiting to be paid.

The printer's ₹4,000 is different on both counts. It is the **cost of one
specific job**, and it is **owed to a specific supplier**.

Put it in Expenses and you lose both: you can't tell which job it
belonged to, and the printer never appears in what you owe. Your cash
would look ₹4,000 healthier than it is.

This is how ERPNext, Tally and SAP all handle it — as a purchase against
the job, not as overhead.

---

## If you pay the printer straight away

Choose **Paid now** instead of On credit. Then:

- Cash goes up ₹6,000 and down ₹4,000 the same day → net **+₹1,200** on a
  ₹3,000 job costing ₹1,800
- Nothing is added to what you owe
- It shows in the Cash Book as "Outsourced work — 4 Colour Press"

---

## Rules worth remembering

**Credit always needs a real supplier.** If you pick "not on my supplier
list" and choose On credit, Ledger stops you — there would be nowhere to
record the debt, and it would quietly disappear.

**More than one firm on one job is fine.** Add a row each — a printer and
a laminator, for example. All of it counts against that job's cost.

**You can reopen a completed order and fix the figures.** The old lines
are replaced, not added to, and what you owe adjusts to match.

---

## Where to see it afterwards

- **The order page** — a "Work bought in for this job" table
- **Suppliers & Payables** — the printer, with what you owe
- **Cash Book** — shows when you actually paid
- **Reports** — the job's true profit, not the invoice value
