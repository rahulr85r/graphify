"""
One pass over every column: how many distinct values, how many nulls, how long,
what kind of thing it holds. Everything downstream is decided from these numbers
rather than from what a column happens to be called.
"""

from __future__ import annotations

import re

INT = re.compile(r"INT|SERIAL")
NUM = re.compile(r"DEC|NUM|REAL|FLOA|DOUB|MONEY")
TIME = re.compile(r"DATE|TIME")

# Dumps routinely type every column as VARCHAR, so a timestamp arrives looking
# like free text -- and free text with one distinct value per row is exactly
# what the label picker wants. Sniff the values instead of trusting the type.
LOOKS_DATE = re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]|$)|^\s*\d{1,2}[-/]\d{1,2}[-/]\d{4}")

# Columns that hold an identity rather than an attribute. Used only to propose
# links nobody declared, and those arrive switched off.
IDENTITYISH = re.compile(
    r"(email|e_mail|phone|mobile|msisdn|address|addr|postcode|zip|fingerprint|"
    r"device|imei|ssn|tax|passport|iban|swift|account_?no|acct_?no|nric|aadhaar)", re.I)


def family(decl: str) -> str:
    d = (decl or "").upper()
    if TIME.search(d):
        return "time"
    if INT.search(d):
        return "int"
    if NUM.search(d):
        return "num"
    return "text"


def profile(source, sample: int = 50_000) -> dict:
    """
    Column statistics. `sample` caps the work on very large tables: distinctness
    measured on a bounded slice is still a reliable signal for whether something
    is a key, and it keeps profiling linear in schema size rather than row count.
    """
    stats = {}
    for t, meta in source.tables.items():
        rows = meta["rows"]
        scan = min(rows, sample)
        scoped = f'(SELECT * FROM "{t}" LIMIT {scan})' if rows > sample else f'"{t}"'
        cols = {}
        for c, decl in meta["columns"]:
            distinct, nulls, lo, hi = source.db.execute(
                f'SELECT COUNT(DISTINCT "{c}"), SUM("{c}" IS NULL), '
                f'MIN(LENGTH("{c}")), MAX(LENGTH("{c}")) FROM {scoped}'
            ).fetchone()
            kind = family(decl)
            if kind == "text" and (hi or 0) >= 8:
                probe = [r[0] for r in source.db.execute(
                    f'SELECT "{c}" FROM {scoped} WHERE "{c}" IS NOT NULL LIMIT 200')]
                if probe and sum(1 for v in probe if LOOKS_DATE.match(str(v))) >= len(probe) * 0.8:
                    kind = "time"
            cols[c] = {
                "distinct": distinct or 0,
                "nulls": nulls or 0,
                "scanned": scan,
                "rows": rows,
                # "unique" means unique across what we looked at. On a sampled
                # table that is a candidate, not a proof, and the inclusion test
                # below is what actually earns it.
                "unique": scan > 0 and (distinct or 0) == scan and not (nulls or 0),
                "type": kind,
                "len": (lo or 0, hi or 0),
                "identityish": bool(IDENTITYISH.search(c)),
                "sampled": rows > sample,
            }
        stats[t] = cols
    return stats
