# How to run Kalyani on Ledger — the whole thing, plainly

You only ever do **four things**. Everything else is a report.

---

## The idea in one line

You buy **kg of roll** → you cut it into **bags** → you sell **bags**.
Ledger's only job is to follow the money through that, so it can tell you
what a bag actually costs and what you really made.

---

## ① When a roll arrives

**Roll Register → + Log rolls**

- Pick the size (or add it there if it's new)
- Type the weight written on **each** roll
- Enter the rate per kg **or** the lot total — whichever the bill shows
- Choose paid / credit

Stock goes up in kg, and Ledger records what that kg cost you.

> **First time only:** create one item per size, with the unit set to **kg**.
> Example: `Kraft 53.5cm 60 GSM`. Leave opening stock and cost at 0.

---

## ② When you cut a roll into bags

**+ New Production**

This is the step that confused you, so here it is exactly:

You are telling Ledger *"I destroyed this much roll, and made these bags."*

| Field | What to put |
|---|---|
| Finished good produced | the bag — e.g. 26×39 |
| Quantity produced | how many bags came out — e.g. 300 |
| **Which roll?** | pick the roll off the rack — **it fills in the kg for you** |
| Quantity used | only if you didn't pick a roll — this is **KG**, not rolls, not bags |
| Labour cost | what you paid the cutter and stitcher for this run |

Ledger then works out:

```
material (kg × rate) + labour
--------------------------------  =  cost per bag
        bags produced
```

**That cost per bag is the whole point.** It's what every future sale of
that bag measures its profit against.

**What went wrong before:** you typed `1` in Quantity used. That meant
1 kg — enough for about 60 bags, not 300. So the material cost came out
near zero, and that near-zero cost got written onto the bag. That's why
the price in Items & Inventory changed by itself.

**Now:** pick the roll instead and you never type a kg figure at all. And
if the cost still comes out impossible, Ledger says so on screen and
refuses to save rather than quietly corrupting the item.

---

## ③ When you sell

**+ New Sale** — pick customer, pick bags, quantity, price.

Profit is worked out for you: selling price − the cost per bag from step ②.

Money not collected becomes credit against that customer.

---

## ④ When money moves

- Customer pays later → **Customers & Credit → Record a payment**
- You pay a supplier → **Suppliers & Payables → Pay them**
- Rent, power, tea, salary → **Expenses**

---

# That's it. Everything below is just reading the results.

| Question | Where to look |
|---|---|
| What did I make today? | Dashboard |
| Where did every rupee go? | Cash Book |
| Why is my cash less than my profit? | Money Position |
| Who owes me, and how long? | Credit Aging |
| Which bag earns most? | Analytics |
| What's left on the rack? | Roll Register |

---

## The two rules that keep the numbers honest

**1. Always record production before selling the bags.**
If you sell a bag Ledger has never seen made, it has no cost for it —
so the profit will be wrong and stock goes negative.

**2. Quantity used is in the raw material's own unit.**
Rolls are held in **kg**, so that box means kg. Pick the roll from the
dropdown and you don't have to think about it at all.

---

## A worked example — one real day

Morning: a lot arrives, 6 rolls of kraft, 239 kg total, ₹92.50/kg on credit.
→ *Roll Register → Log rolls.* Stock: 239 kg. You owe the supplier ₹22,107.50.

Midday: you cut roll R-103 (39.8 kg) into 1,700 bags, paying ₹400 labour.
→ *New Production*, pick R-103 from the dropdown.
Ledger says: **1,700 pcs at ₹2.37 each.** Roll R-103 is now marked finished.

Afternoon: you sell 500 of those bags at ₹6 each, customer pays half.
→ *New Sale.* Revenue ₹3,000, cost ₹1,185, **profit ₹1,815.**
₹1,500 goes onto that customer's credit.

Evening: you pay ₹5,000 to the supplier and ₹150 for tea.
→ *Suppliers → Pay them*, and *Expenses*.

Everything else — dashboard, cash book, reports, aging — fills itself in
from those four entries.

---

## If a number ever looks wrong

Ask which of the four steps was missed. It's nearly always one of:

- Bags sold that were never recorded as produced → no cost, wrong profit
- kg typed where a roll should have been picked → cost per bag wrong
- A supplier paid but not recorded → cash looks higher than it is

The Cash Book shows every single movement, so you can always trace back
to the entry that caused it.
