-- ERP database schema
-- Money stored as REAL (rupees, 2 decimal precision assumed at app layer)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',   -- 'admin' or 'staff'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Items = anything that has stock: raw materials, self-manufactured goods, or bought-in finished goods
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    item_type TEXT NOT NULL,          -- 'raw_material' or 'finished_good'
    category TEXT,                    -- e.g. 'Nonwoven Bags', 'Kraft Bags', 'Nonwoven Sheet'
    unit TEXT NOT NULL,               -- kg, roll, pcs, meter, bundle, etc.
    stock_qty REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0, -- weighted average cost per unit
    selling_price REAL NOT NULL DEFAULT 0, -- default selling price per unit (can override at sale time)
    reorder_level REAL NOT NULL DEFAULT 0,
    photo TEXT,                    -- filename in instance/uploads, for the catalogue
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Purchases: buying raw material or ready-made finished goods from a supplier
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    supplier_name TEXT,
    quantity REAL NOT NULL,
    unit_cost REAL NOT NULL,
    total_cost REAL NOT NULL,
    paid_amount REAL NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'paid',  -- paid, partial, credit (what we owe the supplier)
    purchase_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT,
    -- Several items bought together share one batch_ref, so a multi-item purchase can be
    -- shown and removed as a single bill while each item keeps its own row (which is what
    -- stock levels, average cost and supplier balances are all calculated from).
    batch_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Work bought in from another firm for a specific job - printing, lamination, binding.
-- This is NOT an expense: it is cost of goods for one job, usually bought on credit from
-- a named supplier. Keeping it here means the job's profit is right AND the supplier
-- shows up in payables until they are paid.
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

-- Who did what, and when.
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

-- Individual rolls of raw material.
-- Stock and costing live in kg on the item (one item per size, weighted average cost),
-- because that is how the mill bills you. This table is the physical roll register:
-- what is actually standing in the godown, with the weight written on each roll.
CREATE TABLE IF NOT EXISTS material_rolls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    purchase_id INTEGER REFERENCES purchases(id),
    roll_no TEXT,                                  -- number written on the roll, if any
    weight_kg REAL NOT NULL,                       -- as weighed on arrival
    remaining_kg REAL NOT NULL,                    -- what is left (same as weight until used)
    cost_per_kg REAL NOT NULL DEFAULT 0,           -- the lot rate this roll came in at
    status TEXT NOT NULL DEFAULT 'in_stock',       -- in_stock, part_used, used, scrapped
    received_date TEXT NOT NULL DEFAULT (date('now')),
    used_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rolls_item ON material_rolls(item_id, status);
CREATE INDEX IF NOT EXISTS idx_rolls_purchase ON material_rolls(purchase_id);

-- Suppliers you buy from regularly, with a running balance of what you owe them
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    payable_balance REAL NOT NULL DEFAULT 0,   -- amount WE owe this supplier right now
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Money we pay a supplier towards what we owe them
CREATE TABLE IF NOT EXISTS supplier_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    amount REAL NOT NULL,
    payment_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Production: converting raw material(s) into a finished good (e.g. cutting a roll into bags)
CREATE TABLE IF NOT EXISTS production_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_item_id INTEGER NOT NULL REFERENCES items(id),
    output_qty REAL NOT NULL,
    labor_cost REAL NOT NULL DEFAULT 0,
    other_cost REAL NOT NULL DEFAULT 0,
    total_material_cost REAL NOT NULL DEFAULT 0,
    cost_per_unit REAL NOT NULL DEFAULT 0,
    production_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS production_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_id INTEGER NOT NULL REFERENCES production_runs(id) ON DELETE CASCADE,
    raw_item_id INTEGER NOT NULL REFERENCES items(id),
    qty_consumed REAL NOT NULL,
    unit_cost_at_time REAL NOT NULL,
    line_cost REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    credit_balance REAL NOT NULL DEFAULT 0,   -- amount the customer currently owes us
    customer_type TEXT NOT NULL DEFAULT 'regular',  -- 'regular' (repeat/wholesale) or 'one_time'
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER REFERENCES customers(id),
    sale_date TEXT NOT NULL DEFAULT (date('now')),
    total_amount REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    total_profit REAL NOT NULL DEFAULT 0,
    paid_amount REAL NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'paid',  -- paid, partial, credit
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost REAL NOT NULL,      -- snapshot of avg_cost at sale time
    line_total REAL NOT NULL,
    line_profit REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount REAL NOT NULL,
    payment_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    expense_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_item ON sale_items(item_id);

-- Orders: for jobs booked in advance (e.g. custom printing) where payment happens in two
-- stages - an advance at booking, and the balance when the customer collects the finished goods.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL DEFAULT (date('now')),
    expected_ready_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, ready, completed, cancelled
    agreed_total REAL NOT NULL DEFAULT 0,
    advance_amount REAL NOT NULL DEFAULT 0,
    advance_date TEXT,
    balance_received REAL NOT NULL DEFAULT 0,
    material_cost REAL NOT NULL DEFAULT 0,
    labor_cost REAL NOT NULL DEFAULT 0,
    other_cost REAL NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    profit REAL NOT NULL DEFAULT 0,
    completed_date TEXT,
    refunded_amount REAL NOT NULL DEFAULT 0,
    cancelled_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Order line items: either a catalog item (item_id set) or a one-off custom line
-- (item_id NULL, described in free text) with a lump-sum agreed price.
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES items(id),
    description TEXT,
    quantity REAL NOT NULL DEFAULT 1,
    unit_price REAL NOT NULL DEFAULT 0,
    line_total REAL NOT NULL DEFAULT 0
);

-- Raw material actually consumed for a custom job, recorded when the order is completed
-- (mirrors production_consumption, but scoped to one customer's order).
CREATE TABLE IF NOT EXISTS order_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    raw_item_id INTEGER NOT NULL REFERENCES items(id),
    qty_consumed REAL NOT NULL,
    unit_cost_at_time REAL NOT NULL,
    line_cost REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Simple key/value store for business info shown on printed invoices
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Staff / employee management: salary, daily attendance, advance payments
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    monthly_salary REAL NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per employee per calendar day. status: present, leave, half_day
CREATE TABLE IF NOT EXISTS employee_attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(employee_id, date)
);

-- Money advanced to an employee ahead of payday, deducted from that month's salary
CREATE TABLE IF NOT EXISTS employee_advances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    amount REAL NOT NULL,
    advance_date TEXT NOT NULL DEFAULT (date('now')),
    expense_id INTEGER REFERENCES expenses(id),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attendance_employee_date ON employee_attendance(employee_id, date);
CREATE INDEX IF NOT EXISTS idx_advances_employee_date ON employee_advances(employee_id, advance_date);

-- Date indexes: the Cash Book, Dashboard, Reports and Analytics all scan by date range,
-- so these keep those pages fast as years of records build up.
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(payment_date);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date);
CREATE INDEX IF NOT EXISTS idx_supplier_payments_date ON supplier_payments(payment_date);
CREATE INDEX IF NOT EXISTS idx_orders_advance_date ON orders(advance_date);
CREATE INDEX IF NOT EXISTS idx_orders_completed_date ON orders(completed_date);
CREATE INDEX IF NOT EXISTS idx_orders_cancelled_date ON orders(cancelled_date);

-- Lookup indexes for the joins these pages do on every row.
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON purchases(supplier_id);
CREATE INDEX IF NOT EXISTS idx_customers_type_name ON customers(customer_type, name);
