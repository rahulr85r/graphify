# Graphify

**Turn a relational database into a graph you can actually read — including
the ones with no declared foreign keys.**

```bash
pip install graphify-db
graphify open extract.sql
```

That parses the dump, works out the graph hiding in it, writes down what it
decided, and opens a browser.

No dependencies. No server. No network. Nothing is uploaded, and nothing leaves
the machine — for the people most likely to want this, that is not a feature,
it is the precondition.

---

## The problem

A relational database is already a graph. The foreign keys are the edges. What's
missing is a way to walk it outward from one row you care about, bounded, so it
stays readable.

And half the databases worth looking at don't declare their keys at all. The
constraint was dropped for load speed in 2013 and never came back; the column is
called `OWNR_REF` and the thing it points at is called `CST_NO`.

Point Graphify at one of those and it still works:

```
reading legacy_core_banking.sql
  loaded — 5 tables, 23 rows
  profiling columns — 5 tables
  reading declared keys
  testing value overlap — names ignored
  sorting things from events — 3 relationships

  3 entities, 1 event tables
  0 declared relationships, 3 inferred from value overlap
  1 one-to-one satellite table(s) folded into their master
```

## How it works out the joins

Three tiers, cheapest first. Only the first two ever run.

**1. Declared.** Read the constraints. Free and certain.

**2. Statistical.** For every ordered pair of columns, ask SQL one question: is
every value on this side present on that side? It never consults a column
*name*, which is exactly why it works on schemas where nothing is named
consistently.

**3. Semantic.** *(not implemented)* A language model, shown the schema and
never the rows, for naming things and ranking which of four hundred tables
matter. Optional, and never used to decide a join.

Value overlap is the load-bearing idea, and it lies to you in four specific
ways. Each of these was a real bug, and each has a test:

| It says | Because | So |
|---|---|---|
| `emails.email_id → devices.device_id` | both are counters starting at 1 | a real key **repeats**; an identity does not |
| `transfers ≡ accounts` | 347 ids sit inside 354, and 354 is 98% inside 347 | a one-to-one has *every* value on both sides, and the same number of them |
| `from_account_id → cards.account_id` | that column is unique and contains it | a key points at a table's **identity**, not at somebody else's key — and the column names the parent's table |
| `posted_at` is a label | the dump typed it `VARCHAR`, and it has one distinct value per row | sniff the values; a timestamp is a timestamp whatever the column was declared as |

## The model file

Inference writes `extract.graphify.yaml`. Nobody types it — you only ever read
it and either accept a line or correct one.

```yaml
entities:
  CSTMSTR:
    key: CST_NO
    label: CST_NM
    display_name: Customer          # you, or tier 3
    extended_by: [CSTPREF]
    # one-to-one with this table: the same thing split across tables,
    # folded in so it is one node rather than two joined by nothing.
    stop: false                     # drawn, but the walk passes through

relationships:
  - from: ACCTMST.OWNR_REF
    to: CSTMSTR.CST_NO
    tier: statistical
    confidence: 0.99
    evidence: 100% of values in OWNR_REF exist in CST_NO; child repeats; names agree
    verified: false
```

Set `verified: true` on anything you have checked and re-running inference will
leave it alone. Rename anything. Check it into git — **the model lives in the
repo even though the database does not**, so the next person gets your decisions
rather than a fresh guess.

## What it decides, and why those are the decisions

| Question | The call |
|---|---|
| Which tables are *things*, which are *events*? | Two or more keys and no identity of its own means its rows are edges, not nodes. Getting this backwards is what produces the hairball everyone associates with graph views of a database. |
| What identifies a row? | Whatever other tables point at. That beats every naming rule, because it is the definition of a key rather than a guess about one. |
| What do you call it on screen? | A column that is text, readable, and mostly distinct. |
| Which types are *places*, not people? | Measured: mean links per node against the median across types. Merchants sit at 12×; everything else is under 2×. Those are drawn but not travelled through. |

## Reading the picture

| Channel | Means |
|---|---|
| Node colour and shape | entity type — six validated hues × six shapes |
| Node size | how many links that row has in the whole dataset |
| Edge width | how many records are behind that link |
| `+N` chip | how big the pile you have not opened is |

Two brakes, not one: **depth** is the ceiling, the **node budget** is what
actually stops the walk. Anything a brake cut off becomes a `+N` chip rather
than silently vanishing — an unexpanded pile is information, not an omission.

## Commands

```bash
graphify open extract.sql          # infer, then open the browser
graphify serve extract.sql -p 9000 # same, without opening a browser
graphify infer extract.sql         # write the model file and stop
graphify open bank.sqlite          # SQLite files work directly
graphify open extract.sql --fresh  # ignore an existing model and start over
```

Reads `.sql` dumps from pg_dump, mysqldump and `sqlite3 .dump`, and `.sqlite` /
`.db` files directly. Statements SQLite cannot swallow are skipped rather than
fatal: a dump is usually 5% schema and 95% rows, and one unsupported index
definition should not cost you the file.

## Where it stops

Honest limits, not a roadmap:

- The browser holds the dataset, so this is comfortable to a few hundred
  thousand rows. Past that the walk has to become SQL — each hop a query
  against DuckDB or the live connection — which is the main piece of work left.
- Composite keys are not detected. Single-column only.
- Live database connections (`postgres://`) are stubbed, not built.
- Tier 3 is a design, not code.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

## Licence

MIT.
