"""
Turning the model plus the data into something walkable.

Two decisions carry the whole thing. Rows of an entity table become nodes.
Rows of an event table become EDGES between the entities they reference -- a
transaction is not a thing that exists, it is a link between a card and a shop
that happened at a time. Getting that backwards is what produces the hairball
everyone associates with graph views of a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEP = "\x1f"


@dataclass
class EdgeType:
    id: int
    label: str
    detail: str
    kind: str                     # key | event | proposed
    a_table: str
    b_table: str
    via: str
    timed: bool = False
    money: bool = False
    default_on: bool = True
    count: int = 0


@dataclass
class GraphIndex:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    edge_types: list = field(default_factory=list)
    adj: dict = field(default_factory=dict)
    by_table: dict = field(default_factory=dict)
    t_min: float = 0.0
    t_max: float = 0.0


def _key(table, value):
    return f"{table}{SEP}{value}"


def _rows(source, table, columns):
    cols = ", ".join(f'"{c}"' for c in columns)
    return source.db.execute(f'SELECT {cols} FROM "{table}"')


def build_index(source, model, row_cap: int = 250_000) -> GraphIndex:
    ix = GraphIndex()
    ent = {t: e for t, e in model.entities.items() if e.include and e.key}

    # -- nodes -------------------------------------------------------------
    for t, e in ent.items():
        cols = [c for c, _ in source.tables[t]["columns"]]
        ix.by_table[t] = []
        wanted = [e.key] + ([e.label] if e.label and e.label != e.key else [])
        for i, row in enumerate(_rows(source, t, cols)):
            if i >= row_cap:
                break
            data = dict(zip(cols, row))
            ident = data.get(e.key)
            if ident is None:
                continue
            k = _key(t, ident)
            if k in ix.nodes:
                continue
            label = data.get(e.label) if e.label else None
            node = {"key": k, "table": t, "id": ident, "row": i,
                    "label": str(label) if label not in (None, "") else str(ident),
                    "deg": 0, "data": data}
            ix.nodes[k] = node
            ix.by_table[t].append(node)
            ix.adj[k] = []

    seen_edge: dict = {}

    def add_type(**kw) -> EdgeType:
        et = EdgeType(id=len(ix.edge_types), **kw)
        ix.edge_types.append(et)
        return et

    def link(a, b, et, ts, amount):
        if a == b or a not in ix.nodes or b not in ix.nodes:
            return
        x, y = (a, b) if a < b else (b, a)
        ident = f"{x}{SEP}{y}{SEP}{et.id}"
        idx = seen_edge.get(ident)
        if idx is None:
            idx = len(ix.edges)
            seen_edge[ident] = idx
            ix.edges.append({"i": idx, "a": x, "b": y, "et": et.id, "n": 0,
                             "amount": 0.0, "ts": [], "amt": []})
            ix.adj[x].append(idx)
            ix.adj[y].append(idx)
        e = ix.edges[idx]
        e["n"] += 1
        if amount:
            e["amount"] += amount
        if ts is not None:
            e["ts"].append(ts)
            e["amt"].append(amount or 0.0)
        et.count += 1

    # -- key edges: entity pointing straight at entity ----------------------
    live = [r for r in model.relationships if r.enabled]
    for r in live:
        if r.from_table not in ent or r.to_table not in ent:
            continue
        et = add_type(label=f"{_name(model, r.from_table)} → {_name(model, r.to_table)}",
                      detail=f"{r.tier} key · {r.from_table}.{r.from_column}",
                      kind="key", a_table=r.from_table, b_table=r.to_table,
                      via=r.from_table, timed=False)
        src_key = ent[r.from_table].key
        for row in _rows(source, r.from_table, [src_key, r.from_column]):
            if row[0] is None or row[1] is None:
                continue
            # A key exists for as long as both rows do, so it carries no
            # timestamp and survives every date filter.
            link(_key(r.from_table, row[0]), _key(r.to_table, row[1]), et, None, 0)

    # -- event edges: every pair of entities a fact row touches --------------
    for t, ev in model.events.items():
        dims = [r for r in live if r.from_table == t and r.to_table in ent]
        if len(dims) < 2:
            continue
        primary = max(dims, key=lambda r: sum(1 for x in live if x.to_table == r.to_table))
        cols = [d.from_column for d in dims]
        extra = [c for c in (ev.timestamp, ev.measure) if c]
        pairs = [(i, j) for i in range(len(dims)) for j in range(i + 1, len(dims))]
        types = {}
        for i, j in pairs:
            a, b = dims[i], dims[j]
            through = len(dims) < 3 or primary.to_table in (a.to_table, b.to_table)
            types[(i, j)] = add_type(
                label=f"{_name(model, a.to_table)} ↔ {_name(model, b.to_table)}",
                detail=f"{ev.verb or 'linked by'} · {t}" +
                       ("" if through else f" · via {_name(model, primary.to_table)}"),
                kind="event", a_table=a.to_table, b_table=b.to_table, via=t,
                timed=bool(ev.timestamp), money=bool(ev.measure),
                default_on=through)
        for row in _rows(source, t, cols + extra):
            vals = row[:len(cols)]
            ts = _to_ts(row[len(cols)]) if ev.timestamp else None
            amt = _to_num(row[len(cols) + (1 if ev.timestamp else 0)]) if ev.measure else 0.0
            for (i, j), et in types.items():
                if vals[i] is None or vals[j] is None:
                    continue
                link(_key(dims[i].to_table, vals[i]), _key(dims[j].to_table, vals[j]), et, ts, amt)

    # -- degrees and the activity window ------------------------------------
    lo, hi = float("inf"), float("-inf")
    for e in ix.edges:
        pairs = sorted(zip(e["ts"], e["amt"]))
        e["ts"] = [p[0] for p in pairs]
        e["amt"] = [p[1] for p in pairs]
        cum, run = [0.0], 0.0
        for a in e["amt"]:
            run += a
            cum.append(run)
        e["cum"] = cum
        ix.nodes[e["a"]]["deg"] += 1
        ix.nodes[e["b"]]["deg"] += 1
        if e["ts"]:
            lo = min(lo, e["ts"][0])
            hi = max(hi, e["ts"][-1])
    ix.t_min, ix.t_max = (lo, hi) if lo != float("inf") else (0.0, 1.0)
    for t in ix.by_table:
        ix.by_table[t].sort(key=lambda n: -n["deg"])
    return ix


def _name(model, table):
    e = model.entities.get(table)
    return (e.display_name or table) if e else table


def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_ts(v):
    """Seconds since epoch from whatever the column happens to hold."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if 1e8 < n < 4e9:
            return n
        if 1e11 < n < 4e12:
            return n / 1000.0
        return None
    s = str(v).strip().replace("T", " ")
    if len(s) < 8:
        return None
    import calendar
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            import time as _t
            return calendar.timegm(_t.strptime(s[:len(fmt) + 6].strip(), fmt))
        except (ValueError, TypeError):
            continue
    return None
