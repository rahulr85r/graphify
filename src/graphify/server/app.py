"""
A local web server, on the standard library only.

The browser does the drawing; this hands it the data and the model. Nothing
listens on anything but localhost, and nothing is ever sent anywhere -- for the
people most likely to want this, that is not a feature, it is the precondition.
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..infer import dumps as model_dumps

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# The browser holds the dataset in memory. Past this we send a slice and say so,
# rather than freezing the tab and pretending everything is there.
BROWSER_ROW_CAP = 400_000


class _State:
    lock = threading.Lock()
    source = None
    model = None
    model_path = ""
    dataset = None


STATE = _State()


def _dataset(source, model):
    """
    The shape the front end already understands: tables with their rows, and
    every relationship the inference settled on expressed as an explicit key.
    The browser therefore never has to guess at anything the server has already
    worked out.
    """
    fks = {}
    for r in model.relationships:
        if not r.enabled or r.to_table not in model.entities:
            continue
        fks.setdefault(r.from_table, []).append(
            {"column": r.from_column, "table": r.to_table, "to": r.to_column})

    tables, total, truncated = {}, 0, []
    for t, meta in source.tables.items():
        cols = [c for c, _ in meta["columns"]]
        budget = max(0, BROWSER_ROW_CAP - total)
        cur = source.db.execute(f'SELECT * FROM "{t}" LIMIT {budget}')
        rows = [dict(zip(cols, r)) for r in cur]
        total += len(rows)
        if len(rows) < meta["rows"]:
            truncated.append({"table": t, "sent": len(rows), "of": meta["rows"]})
        ent = model.entities.get(t)
        tables[t] = {
            "columns": cols,
            "pk": ent.key if ent else None,
            "fks": fks.get(t, []),
            "rows": rows,
            "display_name": ent.display_name if ent else None,
            "stop": bool(ent.stop) if ent else False,
        }
    return {"name": model.source or source.name, "tables": tables,
            "truncated": truncated,
            "counts": {t: m["rows"] for t, m in source.tables.items()}}


class Handler(BaseHTTPRequestHandler):
    server_version = "graphify"

    def log_message(self, *_):
        pass                                   # a local tool should be quiet

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            self._route(self.path.split("?")[0])
        except Exception:
            # A local tool should say what went wrong rather than drop the
            # socket and leave the tab spinning.
            self._send(500, json.dumps({"error": traceback.format_exc(limit=3)}))

    def _route(self, path):
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/dataset":
            with STATE.lock:
                if STATE.dataset is None:
                    STATE.dataset = _dataset(STATE.source, STATE.model)
            return self._send(200, json.dumps(STATE.dataset, default=str))
        if path == "/api/model":
            return self._send(200, json.dumps({
                "path": STATE.model_path,
                "yaml": model_dumps(STATE.model),
                "summary": _summary(STATE.model),
            }))
        if path.startswith("/static/"):
            return self._file(os.path.basename(path), _ctype(path))
        return self._send(404, json.dumps({"error": "not found"}))

    def _file(self, name, ctype):
        full = os.path.join(WEB, name)
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _ctype(path):
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def _summary(model):
    rel = model.relationships
    return {
        "entities": len(model.entities),
        "events": len(model.events),
        "relationships": len(rel),
        "declared": sum(1 for r in rel if r.tier == "declared"),
        "inferred": sum(1 for r in rel if r.tier == "statistical"),
        "needs_review": sum(1 for r in rel if not r.verified and r.confidence < 0.8),
        "folded": sum(len(e.extended_by) for e in model.entities.values()),
    }


def serve(source, model, model_path="", host="127.0.0.1", port=8000, open_browser=True):
    STATE.source, STATE.model, STATE.model_path = source, model, model_path
    STATE.dataset = None

    for attempt in range(20):
        try:
            httpd = ThreadingHTTPServer((host, port + attempt), Handler)
            break
        except OSError:
            continue
    else:
        raise OSError(f"no free port between {port} and {port + 19}")

    url = f"http://{host}:{httpd.server_address[1]}/"
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return httpd, url
