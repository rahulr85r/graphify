"""
The graph model: what the inference decided, in a file a person can read.

A database states tables, columns and types. It does not state the four things
a graph needs -- which tables are things and which are events, what identifies
a row, what to call it on screen, and which columns actually join. Those live
here, each with the tier that proposed it and the evidence behind it, so review
is "is this true?" rather than "trust me".

Written as YAML because people read it. Parsed by hand because the subset we
emit is small, and a tool that turns a database into a picture should not need
a dependency to read its own output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any


# --------------------------------------------------------------------------
# a very small YAML subset: nested maps, lists of maps, lists of scalars
# --------------------------------------------------------------------------

def _scalar(text: str) -> Any:
    t = text.strip()
    if t.startswith(("'", '"')) and t.endswith(("'", '"')) and len(t) > 1:
        return t[1:-1]
    if t in ("null", "~", ""):
        return None
    if t == "true":
        return True
    if t == "false":
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d*\.\d+", t):
        return float(t)
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    return t


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_dump_scalar(x) for x in v) + "]"
    s = str(v)
    if s == "" or re.search(r'^[\s\-?:,\[\]{}#&*!|>%@`"\']|: |#| $', s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, but not a # that lives inside a quoted value."""
    out, quote = [], None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i:i + 2]); i += 2; continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _lines(text: str) -> list[tuple[int, str]]:
    out = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        out.append((len(line) - len(line.lstrip()), line.strip()))
    return out


def _parse_block(rows: list[tuple[int, str]], i: int, indent: int):
    """A block is a list if its first line starts with '- ', otherwise a map."""
    if i >= len(rows) or rows[i][0] < indent:
        return None, i
    return (_parse_list if rows[i][1].startswith("- ") else _parse_map)(rows, i, indent)


def _parse_map(rows, i, indent):
    out: dict = {}
    while i < len(rows):
        ind, body = rows[i]
        if ind < indent or body.startswith("- "):
            break
        if ":" not in body:
            i += 1
            continue
        key, _, value = body.partition(":")
        key, value = key.strip(), value.strip()
        i += 1
        if value == "":
            child, i = _parse_block(rows, i, ind + 1)
            out[key] = child if child is not None else {}
        else:
            out[key] = _scalar(value)
    return out, i


def _parse_list(rows, i, indent):
    out: list = []
    while i < len(rows):
        ind, body = rows[i]
        if ind < indent or not body.startswith("- "):
            break
        item_text = body[2:].strip()
        i += 1
        if ":" in item_text and not item_text.startswith(("'", '"')):
            key, _, value = item_text.partition(":")
            item = {key.strip(): _scalar(value.strip())}
            # continuation lines of this list item sit deeper than the dash
            rest, i = _parse_map(rows, i, ind + 1)
            item.update(rest)
            out.append(item)
        else:
            out.append(_scalar(item_text))
    return out, i


def loads(text: str) -> dict:
    """Parse the subset this module writes. Comments and blank lines ignored."""
    rows = _lines(text)
    value, _ = _parse_block(rows, 0, 0)
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# the model itself
# --------------------------------------------------------------------------

@dataclass
class Entity:
    table: str
    key: str | None = None
    label: str | None = None
    rows: int = 0
    display_name: str | None = None
    extends: str | None = None          # this table is a 1:1 satellite of another
    extended_by: list[str] = field(default_factory=list)
    include: bool = True
    stop: bool = False                  # drawn, but the walk does not pass through


@dataclass
class Event:
    table: str
    rows: int = 0
    timestamp: str | None = None
    measure: str | None = None
    verb: str | None = None


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    tier: str = "statistical"           # declared | statistical | semantic | manual
    confidence: float = 0.0
    evidence: str = ""
    review: str | None = None
    verified: bool = False
    enabled: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.from_table, self.from_column)


@dataclass
class GraphModel:
    source: str = ""
    entities: dict[str, Entity] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)

    # -- merging -----------------------------------------------------------
    def adopt_decisions_from(self, old: "GraphModel") -> None:
        """
        Re-running inference must never quietly undo a person's work. Anything
        marked verified, and any display name they typed, survives.
        """
        for name, ent in self.entities.items():
            prev = old.entities.get(name)
            if not prev:
                continue
            if prev.display_name:
                ent.display_name = prev.display_name
            ent.include, ent.stop = prev.include, prev.stop

        kept = {r.key: r for r in old.relationships if r.verified}
        for rel in self.relationships:
            prev = kept.pop(rel.key, None)
            if prev:
                rel.to_table, rel.to_column = prev.to_table, prev.to_column
                rel.verified, rel.enabled = True, prev.enabled
                rel.tier, rel.confidence = prev.tier, prev.confidence
                rel.evidence = prev.evidence + " (kept: you verified this)"
        # verified relationships the new pass did not rediscover are still theirs
        self.relationships.extend(kept.values())


def dumps(model: GraphModel) -> str:
    out: list[str] = []
    w = out.append
    w("# graphify.model.yaml")
    w("#")
    w("# Generated. Nothing here was typed by hand, and nothing here is final.")
    w("# Every line records which tier proposed it and the evidence behind it.")
    w("#")
    w("#   declared      the schema said so")
    w("#   statistical   value overlap said so, without consulting any name")
    w("#   semantic      a language model suggested it, from the schema only")
    w("#   manual        you said so")
    w("#")
    w("# Set `verified: true` on anything you have checked. Re-running inference")
    w("# will leave it alone.")
    w("")
    w(f"source: {_dump_scalar(model.source)}")
    w("")

    w("entities:")
    if not model.entities:
        w("  {}")
    for name, e in model.entities.items():
        w(f"  {name}:")
        w(f"    key: {_dump_scalar(e.key)}")
        w(f"    label: {_dump_scalar(e.label)}")
        w(f"    rows: {e.rows}")
        w(f"    display_name: {_dump_scalar(e.display_name)}")
        w(f"    include: {_dump_scalar(e.include)}")
        w(f"    stop: {_dump_scalar(e.stop)}")
        if e.extended_by:
            w(f"    extended_by: {_dump_scalar(e.extended_by)}")
            w("    # one-to-one with this table: the same thing split across tables,")
            w("    # folded in so it is one node rather than two joined by nothing.")
    w("")

    w("events:")
    if not model.events:
        w("  {}")
    for name, ev in model.events.items():
        w(f"  {name}:")
        w(f"    rows: {ev.rows}")
        w(f"    timestamp: {_dump_scalar(ev.timestamp)}")
        w(f"    measure: {_dump_scalar(ev.measure)}")
        w(f"    verb: {_dump_scalar(ev.verb)}")
    w("")

    w("relationships:")
    if not model.relationships:
        w("  []")
    for r in model.relationships:
        w(f"  - from: {r.from_table}.{r.from_column}")
        w(f"    to: {r.to_table}.{r.to_column}")
        w(f"    tier: {r.tier}")
        w(f"    confidence: {r.confidence}")
        w(f"    evidence: {_dump_scalar(r.evidence)}")
        if r.review:
            w(f"    review: {_dump_scalar(r.review)}")
        w(f"    verified: {_dump_scalar(r.verified)}")
        w(f"    enabled: {_dump_scalar(r.enabled)}")
    w("")
    return "\n".join(out)


def load(path: str) -> GraphModel:
    raw = loads(open(path).read())
    m = GraphModel(source=raw.get("source") or "")
    for name, body in (raw.get("entities") or {}).items():
        body = body or {}
        m.entities[name] = Entity(
            table=name, key=body.get("key"), label=body.get("label"),
            rows=body.get("rows") or 0, display_name=body.get("display_name"),
            extended_by=list(body.get("extended_by") or []),
            include=body.get("include", True), stop=body.get("stop", False),
        )
    for name, body in (raw.get("events") or {}).items():
        body = body or {}
        m.events[name] = Event(table=name, rows=body.get("rows") or 0,
                               timestamp=body.get("timestamp"),
                               measure=body.get("measure"), verb=body.get("verb"))
    for r in (raw.get("relationships") or []):
        if not isinstance(r, dict) or "from" not in r:
            continue
        ft, _, fc = str(r["from"]).partition(".")
        tt, _, tc = str(r.get("to", "")).partition(".")
        m.relationships.append(Relationship(
            from_table=ft, from_column=fc, to_table=tt, to_column=tc,
            tier=r.get("tier") or "manual", confidence=float(r.get("confidence") or 0),
            evidence=r.get("evidence") or "", review=r.get("review"),
            verified=bool(r.get("verified")), enabled=r.get("enabled", True),
        ))
    return m


def save(model: GraphModel, path: str, merge: bool = False) -> str:
    """
    Writing is writing. Merging an earlier model in here as well meant --fresh
    could not actually be fresh, because the caller's decision was undone one
    layer down. Callers that want the merge ask for it.
    """
    if merge and os.path.exists(path):
        try:
            model.adopt_decisions_from(load(path))
        except Exception:
            pass                        # a corrupt model must not block a rebuild
    text = dumps(model)
    with open(path, "w") as fh:
        fh.write(text)
    return text
