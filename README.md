# Ledger — a simple ERP for your shop

Built for a business that both **makes** things (cutting nonwoven and kraft
rolls into bags) and **resells** things (bought-in covers, finished bags) —
tracks stock, cost, sales, credit, and profit in one place, running entirely
on your own computer. No internet needed after the one-time install, no
monthly fees, your data never leaves your machine.

## What it does

- **Items & Inventory** — track raw materials (rolls, kg/meter/etc.) and
  finished goods (bags, covers, pcs) with live stock levels and low-stock
  alerts.
- **Purchases** — record what you buy and from whom; stock and average cost
  update automatically.
- **Production** — record a "cutting run": pick the raw material(s) consumed
  and how much finished product came out, add labor/overhead cost, and the
  app works out the exact cost per bag produced.
- **Sales** — a simple point-of-sale style form: add items, set the price,
  choose Paid / Partial / Credit. Profit per sale is calculated automatically
  (selling price − cost price, using your item's running average cost).
- **Customers & Credit** — every customer's outstanding balance, with a
  payment history and a one-click way to record money coming in.
- **Reports** — revenue and profit for today, this week, this month, this
  year, plus a monthly trend and your best sellers by profit.
- **Staff logins** — the admin account can create extra logins for staff if
  more than one person needs to use it.

## One-time setup

You need Python installed (3.9 or newer). If you don't have it:
- Windows: download from https://python.org/downloads — during install,
  tick **"Add python.exe to PATH"**.
- Mac: Python usually comes pre-installed; if not, https://python.org/downloads
- Linux: `sudo apt install python3 python3-venv` (Ubuntu/Debian) or your
  distro's equivalent.

Unzip this folder anywhere on your computer, e.g. `Documents\Ledger`.

## Running it

**Windows:** double-click `run.bat`.
**Mac / Linux:** open Terminal in this folder and run `./run.sh`
(first time only, you may need `chmod +x run.sh`).

Either script creates a private Python environment, installs the one
dependency (Flask), and starts the app. Leave that window open while you
use it — closing it stops the app.

Then open your browser to:

    http://127.0.0.1:5000

The first time, you'll be asked to create your admin username and password —
that's your login going forward. There's no separate account recovery, so
remember it (or write it down somewhere safe).

## Your data

All your data lives in `instance/erp.db` — a single SQLite database file in
this folder. To back it up, just copy that file somewhere safe (e.g. a USB
drive or cloud-synced folder) periodically. To move the app to another
computer, copy the whole folder including `instance/erp.db`.

## A few notes on how the numbers work

- **Average cost**: each item has a running *weighted average cost*. Every
  purchase blends into it; every sale uses the current average cost as the
  cost price for that sale's profit calculation. This is standard, simple,
  and works well for a business like this — it doesn't require you to track
  which exact roll or batch a bag came from.
- **Production cost**: when you cut a roll into bags, the cost per bag =
  (raw material consumed at its cost) + labor + other overhead, divided by
  the quantity produced. That becomes the new average cost for the bag.
- **Credit**: if a sale isn't paid in full, the difference is added to that
  customer's outstanding balance. Recording a payment reduces it.
- **Expenses** (rent, salaries, electricity...) are tracked separately from
  cost of goods, since they're overhead rather than tied to a specific sale.
  The Reports page shows profit both before and after this month's expenses.

## If something goes wrong

- "python not found" → Python isn't installed or not on PATH — reinstall
  and tick "Add to PATH" (Windows).
- Port 5000 already in use → close any other copy of the app that's running,
  or edit the last line of `app.py` and change `port=5000` to `port=5050`
  (then browse to that port instead).
- Forgot the admin password → stop the app, delete `instance/erp.db`, and
  restart it — this wipes all data and lets you set up again, so only do
  this if you don't mind losing existing records. To reset just the password
  without losing data, ask whoever set this up for you to help edit the
  database directly.
# Ledger-ERP
