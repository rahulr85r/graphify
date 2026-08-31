"""
Getting a database into a shape we can interrogate.

Everything lands in SQLite -- either because it already was SQLite, or because
we replayed a dump into one. That gives the inference a single way to ask
questions (SQL) regardless of where the data came from, and it means value
overlap between two tables is a query rather than a Python loop over rows.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile


class Source:
    """A SQLite handle plus the schema facts we read straight off it."""

    def __init__(self, db: sqlite3.Connection, name: str, path: str | None = None):
        self.db = db
        self.name = name
        self.path = path
        self.tables = self._read_tables()

    def _read_tables(self) -> dict:
        names = [r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in names:
            cols = [(r[1], (r[2] or "").upper()) for r in self.db.execute(f'PRAGMA table_info("{t}")')]
            if not cols:
                continue
            rows = self.db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            declared_pk = [r[1] for r in self.db.execute(f'PRAGMA table_info("{t}")') if r[5]]
            fks = [{"column": r[3], "table": r[2], "to": r[4]}
                   for r in self.db.execute(f'PRAGMA foreign_key_list("{t}")')]
            out[t] = {"columns": cols, "rows": rows,
                      "declared_pk": declared_pk[0] if len(declared_pk) == 1 else None,
                      "declared_fks": [f for f in fks if f["column"] and f["table"]]}
        return {k: v for k, v in out.items() if v["rows"] > 0}

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# dumps
# --------------------------------------------------------------------------

# Rewrites that happen INSIDE a statement. Statement-level noise (SET, GRANT,
# CREATE INDEX and friends) is dropped by looking at each statement's first
# word instead -- a regex that substitutes away a leading keyword leaves the
# rest of the line behind, which then glues itself onto whatever follows.
_ENGINE_TAIL = re.compile(r"\)\s*(ENGINE|DEFAULT CHARSET|AUTO_INCREMENT|COLLATE|TABLESPACE)[^;]*;",
                          re.I)
_TYPES = [
    (re.compile(r"\b(character varying|varchar2|nvarchar2|nvarchar|nchar)\b", re.I), "VARCHAR"),
    (re.compile(r"\b(timestamp[a-z ()]*|datetime2?|date)\b", re.I), "TIMESTAMP"),
    (re.compile(r"\b(number|numeric|decimal|money|double precision|float8|real)\b", re.I), "DECIMAL"),
    (re.compile(r"\b(bigserial|serial|int8|int4|int2|smallint|bigint|tinyint)\b", re.I), "INTEGER"),
    (re.compile(r"\benum\s*\([^)]*\)", re.I), "VARCHAR"),
    (re.compile(r"\b(bool|boolean)\b", re.I), "INTEGER"),
    (re.compile(r"::\w+(\[\])?", re.I), ""),          # postgres casts
    (re.compile(r"\bAUTO_INCREMENT\b|\bAUTOINCREMENT\b|\bUNSIGNED\b|\bZEROFILL\b", re.I), ""),
    (re.compile(r"\bDEFAULT\s+nextval\([^)]*\)", re.I), ""),
]
_BACKTICK = re.compile(r"`([^`]*)`")
_BRACKET = re.compile(r"\[([A-Za-z_][\w ]*)\]")
_SCHEMA_QUAL = re.compile(r"\b(?:public|dbo)\.", re.I)
# MySQL declares its indexes inside the CREATE TABLE body. SQLite rejects the
# whole statement over one of them, so a single KEY line costs you the table.
# Dropping them is safe: an index changes how a query runs, never what is true.
_INDEX_LINE = re.compile(r"^\s*(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?(?:KEY|INDEX)\s+[^\n]*$",
                         re.I | re.M)
_DANGLING = re.compile(r",(\s*\))")


def _clean_create(stmt: str) -> str:
    stmt = _INDEX_LINE.sub("", stmt)
    stmt = re.sub(r"\n\s*\n", "\n", stmt)
    return _DANGLING.sub(r"\1", stmt)


def _normalise(sql: str) -> str:
    sql = _BACKTICK.sub(r'"\1"', sql)
    sql = _BRACKET.sub(r'"\1"', sql)
    sql = _SCHEMA_QUAL.sub("", sql)
    sql = _ENGINE_TAIL.sub(");", sql)
    for rx, rep in _TYPES:
        sql = rx.sub(rep, sql)
    return sql


def _statements(sql: str):
    """Split on semicolons that are not inside a string or a comment."""
    buf, quote, i, n = [], None, 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == "'":
                if i + 1 < n:
                    buf.append(sql[i + 1]); i += 2; continue
            elif ch == quote:
                if quote == "'" and i + 1 < n and sql[i + 1] == "'":
                    buf.append(sql[i + 1]); i += 2; continue
                quote = None
            i += 1
            continue
        if ch == "-" and sql[i:i + 2] == "--":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and sql[i:i + 2] == "/*":
            i = sql.find("*/", i)
            i = n if i < 0 else i + 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch); i += 1; continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                yield stmt
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def from_dump(path: str, workdir: str | None = None) -> Source:
    """
    Replay a .sql dump into SQLite. Statements that SQLite will not accept are
    skipped rather than fatal: a dump is usually 5% schema, 95% rows, and one
    unsupported index definition should not cost you the whole file.
    """
    text = _normalise(open(path, "r", errors="replace").read())
    store = os.path.join(workdir or tempfile.mkdtemp(prefix="graphify-"), "graphify.db")
    # The web server answers each request on its own thread, and a SQLite
    # connection is bound to the thread that made it unless told otherwise.
    db = sqlite3.connect(store, check_same_thread=False)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")

    ok = skipped = 0
    for stmt in _statements(text):
        head = " ".join(stmt.split()[:2]).upper()
        if not (head.startswith("CREATE TABLE") or head.startswith("INSERT")
                or head.startswith("ALTER TABLE") or head.startswith("REPLACE")):
            continue
        if head.startswith("CREATE TABLE"):
            stmt = _clean_create(stmt)
        try:
            db.execute(stmt)
            ok += 1
        except sqlite3.Error:
            skipped += 1
    db.commit()

    src = Source(db, os.path.basename(path), store)
    src.stats = {"applied": ok, "skipped": skipped}
    if not src.tables:
        raise ValueError(
            f"Nothing loaded from {os.path.basename(path)}. "
            f"{ok} statements applied, {skipped} skipped. "
            "Graphify needs CREATE TABLE plus INSERT statements with rows in them.")
    return src


def from_sqlite(path: str) -> Source:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    src = Source(db, os.path.basename(path), path)
    src.stats = {"applied": 0, "skipped": 0}
    if not src.tables:
        raise ValueError(f"{os.path.basename(path)} has no tables with rows in them.")
    return src


def open_source(path: str, workdir: str | None = None) -> Source:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as fh:
        magic = fh.read(16)
    if magic.startswith(b"SQLite format 3"):
        return from_sqlite(path)
    return from_dump(path, workdir)
