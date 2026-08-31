"""
Working out the graph hiding in a relational schema.

Three tiers, cheapest first, and only the first two run offline:

  declared      the schema states a constraint. Free and certain.
  statistical   value overlap. For every ordered pair of columns, ask SQL one
                question: is every value on this side present on that side? It
                never consults a column NAME, which is exactly why it works on
                schemas where nothing is named consistently.
  semantic      a language model, shown the schema and never the rows, for
                naming and ranking. Optional, and never used to decide a join.

The output is a GraphModel: a file a person reads, argues with, and checks in.
"""

from __future__ import annotations

import re
from itertools import product

from .model import Entity, Event, GraphModel, Relationship
from .profile import profile

# A parent key must be unique, so a child column is contained in it. Below this
# fraction we are looking at coincidence rather than a key.
CONTAINMENT_FLOOR = 0.95
# Comparing every column against every other is quadratic in columns. Fine for
# hundreds; this caps the pathological case rather than the normal one.
MAX_PAIRS = 400_000

TOKEN = re.compile(r"[a-z]+")
DATEISH = re.compile(r"(_at|_on|_dt|_ts|date|time|stamp|created|updated|posted|"
                     r"occurred|opened|started|effective)", re.I)
STARTISH = re.compile(r"(open|start|since|effective|valid_from|joined|issued|"
                      r"activated|enrolled|signup|signed_up)", re.I)
MEASUREISH = re.compile(r"(amount|amt|total|value|balance|sum|price|cost|fee|charge|"
                        r"revenue|spend|notional|qty|quantity|units|duration|minutes)", re.I)
VERBS = [
    (re.compile(r"transfer|remittance|wire|settlement|payment_out", re.I), "money moved by"),
    (re.compile(r"transaction|txn|payment|charge|auth|posting|invoice", re.I), "paid via"),
    (re.compile(r"purchase|order|sale|basket|booking", re.I), "bought via"),
    (re.compile(r"session|login|logon|signin|visit|event|click|impression", re.I), "seen in"),
    (re.compile(r"message|mail|call|contact|interaction", re.I), "contacted via"),
    (re.compile(r"holding|position|subscription|enrolment|enrollment", re.I), "holds"),
]


def _stems(name: str) -> set:
    return {w[:3] for w in TOKEN.findall(name.lower()) if len(w) > 2}


# --------------------------------------------------------------------------
# tier 2: value containment
# --------------------------------------------------------------------------

def _candidates(source, stats) -> list[dict]:
    cols = [(t, c) for t, m in source.tables.items() for c, _ in m["columns"]]
    if len(cols) ** 2 > MAX_PAIRS:
        # Narrow to plausible key columns before pairing: short-ish, not free
        # text, not a measure. Keeps a 400-table schema tractable.
        cols = [(t, c) for (t, c) in cols
                if stats[t][c]["type"] in ("int", "text")
                and stats[t][c]["len"][1] <= 64
                and not MEASUREISH.search(c)]

    out = []
    for (ct, cc), (pt, pc) in product(cols, cols):
        if ct == pt:
            continue
        cs, ps = stats[ct][cc], stats[pt][pc]
        if cs["distinct"] < 2 or not ps["unique"]:
            continue                                    # a parent key is unique
        if cs["type"] != ps["type"] or cs["type"] in ("time", "num"):
            continue                                    # dates and amounts are not keys
        if MEASUREISH.search(cc) or MEASUREISH.search(pc):
            continue
        if cs["type"] == "text" and abs(cs["len"][1] - ps["len"][1]) > 2:
            continue                                    # a 14-char account is not a 4-char code

        orphans = source.db.execute(
            f'SELECT COUNT(*) FROM (SELECT DISTINCT "{cc}" v FROM "{ct}" WHERE "{cc}" IS NOT NULL) '
            f'WHERE v NOT IN (SELECT "{pc}" FROM "{pt}" WHERE "{pc}" IS NOT NULL)'
        ).fetchone()[0]
        containment = 1 - orphans / cs["distinct"]
        if containment < CONTAINMENT_FLOOR:
            continue
        out.append({
            "child": (ct, cc), "parent": (pt, pc),
            "containment": containment,
            "coverage": cs["distinct"] / max(1, ps["distinct"]),
            "child_unique": cs["unique"],
            "name_hint": bool(_stems(cc) & _stems(pc)),
            # `from_account_id` sits inside cards.account_id as happily as it
            # sits inside accounts.account_id -- both are unique and both
            # contain it. What separates them is the one thing a person would
            # look at: the child column names the parent's TABLE.
            "table_hint": bool(_stems(cc) & _stems(pt)),
        })
    return out


def stats_distinct(source, table, column) -> int:
    return source.db.execute(
        f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"').fetchone()[0]


def _extensions(cands, source) -> dict:
    """
    Two columns that contain each other, both unique, means the tables are
    one-to-one -- which almost always means they are the same entity split
    across tables. Legacy cores do this constantly: a master plus satellites for
    preferences, KYC and limits, all keyed by the same number. Folding them is
    what turns four hundred tables into forty things.
    """
    ext = {}
    for c in cands:
        ct, cc = c["child"]
        pt, pc = c["parent"]
        back = next((x for x in cands if x["child"] == (pt, pc) and x["parent"] == (ct, cc)), None)
        if back is None or not c["child_unique"]:
            continue
        # Overlapping is not the same as being the same thing. Two counters,
        # one running to 347 and one to 354, each sit almost entirely inside
        # the other -- and folding those together would delete a whole table.
        # A genuine one-to-one has every value on both sides and the same
        # number of them.
        if c["containment"] < 1.0 or back["containment"] < 1.0:
            continue
        if stats_distinct(source, ct, cc) != stats_distinct(source, pt, pc):
            continue
        rank = lambda t: (len(source.tables[t]["columns"]), source.tables[t]["rows"], t)
        rep, sat = (ct, pt) if rank(ct) > rank(pt) else (pt, ct)
        sat_col = cc if sat == ct else pc
        rep_col = pc if sat == ct else cc
        if rep in ext:                                  # never chain satellites
            continue
        ext[sat] = {"of": rep, "sat_col": sat_col, "rep_col": rep_col}
    return {s: e for s, e in ext.items() if e["of"] not in ext}


def _resolve(cands, ext) -> list[dict]:
    """
    Direction first: whichever side repeats is the child. Then fold the
    satellites away, so a key pointing at one is re-pointed at the table it
    extends. Then keep the strongest parent for each child column.
    """
    best = {}
    for c in cands:
        ct, cc = c["child"]
        pt, pc = c["parent"]
        if ct in ext or (pt in ext and c["child_unique"]):
            continue                                    # this pair IS the 1:1 join
        if pt in ext:
            e = ext[pt]
            if pc == e["sat_col"]:
                pt, pc = e["of"], e["rep_col"]
        symmetric = any(x["child"] == (pt, pc) and x["parent"] == (ct, cc) for x in cands)
        if c["child_unique"] and symmetric:
            continue                                    # still ambiguous: not a key

        # The trap in this whole technique: two unrelated tables numbered from
        # one will always contain each other. `emails.email_id` sits inside
        # `devices.device_id` for no reason beyond both being dense counters.
        # A real foreign key REPEATS -- many rows point at one parent -- so a
        # child that never repeats is its own table's identity, not a reference
        # to somebody else's. Where the names agree we keep it and ask, because
        # a genuine one-to-one link does exist; where they do not, it is noise.
        review = None
        if c["child_unique"]:
            if not c["name_hint"]:
                continue
            review = "child never repeats: could be a one-to-one link, or two counters that happen to overlap"

        conf = min(0.99, 0.55 * c["containment"] + 0.25 * min(1, c["coverage"])
                   + (0.12 if not c["child_unique"] else 0.0)
                   + (0.10 if c["name_hint"] else 0.0)
                   + (0.15 if c["table_hint"] else 0.0))
        if c["child_unique"]:
            conf = min(conf, 0.7)
        row = dict(c, parent=(pt, pc), confidence=round(conf, 2), review=review)
        prev = best.get(row["child"])
        if prev is None or row["confidence"] > prev["confidence"]:
            best[row["child"]] = row
    return sorted(best.values(), key=lambda x: (-x["confidence"], x["child"]))


# --------------------------------------------------------------------------
# things, events, names
# --------------------------------------------------------------------------

def _pick_key(table, cols, stats, keyed, referenced):
    """
    A key is a column other tables point at. That beats every naming rule,
    because it is the definition of a key rather than a guess about one.
    """
    declared = cols.get("declared_pk")
    if declared and stats[declared]["unique"]:
        return declared
    uniq = [c for c in stats if stats[c]["unique"] and c not in keyed]
    if not uniq:
        return None
    return sorted(uniq, key=lambda c: (
        -referenced.get((table, c), 0),
        stats[c]["type"] == "text" and stats[c]["len"][1] > 24,   # prose is not a key
        list(stats).index(c),
    ))[0]


def _pick_label(stats, free):
    """A label must tell rows apart and be readable: text, mostly distinct."""
    best, score = None, 0.0
    for c in free:
        s = stats[c]
        if s["type"] != "text" or s["scanned"] == 0 or s["len"][1] <= 3:
            continue
        ratio = s["distinct"] / s["scanned"]
        if ratio > score and ratio >= 0.6:
            best, score = c, ratio
    return best


def _verb(table):
    for rx, word in VERBS:
        if rx.search(table):
            return word
    return "linked by"


def _humanise(table: str) -> str:
    """A guess at what people call this. Tier 3 does better; this is the floor."""
    # An all-caps token from a legacy schema carries no word boundaries we can
    # find -- "ACCTMST" is not "Acctmst". Leave those alone and let tier 3 or a
    # person name them. Ordinary lowercase names are safe to tidy.
    if table.isupper() and not re.search(r"[_\s]", table):
        return table
    words = re.split(r"[_\s]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", table))
    words = [w for w in words if w and w.lower() not in
             ("tbl", "table", "mst", "mstr", "master", "hist", "dim", "fact", "t")]
    out = " ".join(w.capitalize() for w in words) or table
    if out.lower().endswith("ies"):
        out = out[:-3] + "y"
    elif re.search(r"(ses|xes|zes|ches|shes)$", out, re.I):
        out = out[:-2]
    elif re.search(r"[^s]s$", out):
        out = out[:-1]
    return out


# --------------------------------------------------------------------------

def infer(source, on_progress=None) -> GraphModel:
    say = on_progress or (lambda *_: None)

    say("profiling columns", f"{len(source.tables)} tables")
    stats = profile(source)

    say("reading declared keys", "")
    declared = []
    for t, meta in source.tables.items():
        for fk in meta["declared_fks"]:
            if fk["table"] in source.tables:
                declared.append({
                    "child": (t, fk["column"]),
                    "parent": (fk["table"], fk["to"] or source.tables[fk["table"]]["declared_pk"]),
                    "containment": 1.0, "coverage": 1.0, "child_unique": False,
                    "name_hint": True, "confidence": 1.0, "tier": "declared",
                })
    declared = [d for d in declared if d["parent"][1]]

    say("testing value overlap", "names ignored")
    cands = _candidates(source, stats)
    ext = _extensions(cands, source)
    declared_children = {d["child"] for d in declared}
    found = [c for c in _resolve(cands, ext) if c["child"] not in declared_children]
    for c in found:
        c["tier"] = "statistical"
    rels = declared + found

    say("sorting things from events", f"{len(rels)} relationships")
    referenced = {}
    for r in rels:
        referenced[r["parent"]] = referenced.get(r["parent"], 0) + 1

    model = GraphModel(source=source.name)
    out_fks = {t: [r for r in rels if r["child"][0] == t] for t in source.tables}

    for t, meta in source.tables.items():
        if t in ext:
            continue
        keyed = {r["child"][1] for r in out_fks[t]}
        key = _pick_key(t, meta, stats[t], keyed, referenced)
        free = [c for c in stats[t] if c != key and c not in keyed]
        label = _pick_label(stats[t], free)
        is_event = len(out_fks[t]) >= 2 and label is None

        if is_event:
            dated = [c for c in stats[t] if stats[t][c]["type"] == "time" and DATEISH.search(c)]
            events_first = [c for c in dated if not STARTISH.search(c)]
            # A name match is a preference, not a requirement: plenty of real
            # measures are called things nobody put in a regex. Any number that
            # is not a key will do, and the one with the most distinct values is
            # the one carrying information.
            numeric = [c for c in stats[t] if stats[t][c]["type"] == "num"
                       and c != key and c not in keyed]
            measure = next((c for c in numeric if MEASUREISH.search(c)), None)
            if measure is None and numeric:
                measure = max(numeric, key=lambda c: stats[t][c]["distinct"])
            model.events[t] = Event(table=t, rows=meta["rows"], verb=_verb(t),
                                    timestamp=(events_first or dated or [None])[0],
                                    measure=measure)
        else:
            sats = [s for s, e in ext.items() if e["of"] == t]
            model.entities[t] = Entity(table=t, key=key, label=label, rows=meta["rows"],
                                       display_name=_humanise(t), extended_by=sats)

    for r in rels:
        ct, cc = r["child"]
        pt, pc = r["parent"]
        if pt not in model.entities:
            continue                                     # points at an event: not a node link
        if r["tier"] == "declared":
            evidence = "declared FOREIGN KEY constraint"
        else:
            evidence = (f"{int(r['containment'] * 100)}% of values in {cc} exist in {pc}; "
                        f"{'child repeats' if not r['child_unique'] else 'child is unique'}"
                        f"{'; names agree' if r.get('table_hint') or r['name_hint'] else '; names share nothing'}")
        model.relationships.append(Relationship(
            from_table=ct, from_column=cc, to_table=pt, to_column=pc,
            tier=r["tier"], confidence=round(r["confidence"], 2), evidence=evidence,
            review=r.get("review") or (None if r["confidence"] >= 0.8
                                        else "low confidence: worth a look"),
            verified=(r["tier"] == "declared"),
        ))

    say("done", f"{len(model.entities)} entities, {len(model.events)} event tables")
    _mark_hubs(model, stats)
    return model


def _mark_hubs(model: GraphModel, stats) -> None:
    """
    Types that are places rather than people. Everything touches them, so walking
    onward through one collects the whole book -- draw them, do not travel
    through them. Measured, not guessed: how many links land on the average node
    of each type, against the median across types.
    """
    degree = {t: 0 for t in model.entities}
    for r in model.relationships:
        if r.to_table in degree:
            degree[r.to_table] += stats[r.from_table][r.from_column]["rows"]
    means = {t: degree[t] / max(1, e.rows) for t, e in model.entities.items()}
    if len(means) < 3:
        return
    ordered = sorted(means.values())
    median = ordered[len(ordered) // 2] or 1
    subject = max(model.entities, key=lambda t: sum(1 for r in model.relationships if r.to_table == t))
    for t, m in means.items():
        if t != subject and m >= median * 5:
            model.entities[t].stop = True
