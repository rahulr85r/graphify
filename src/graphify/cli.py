"""
graphify -- command line.

    graphify open extract.sql        infer, then open the browser
    graphify infer extract.sql       write the model file and stop
    graphify serve extract.sql       run the server without opening a browser
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

from . import __version__
from .graph import build_index
from .infer import infer as run_inference
from .infer import model as model_io
from .ingest import open_source
from .server import serve as run_server


def _default_model_path(dump: str) -> str:
    base = os.path.splitext(os.path.basename(dump))[0]
    return os.path.join(os.path.dirname(os.path.abspath(dump)), f"{base}.graphify.yaml")


def _step(label, detail=""):
    print(f"  {label}{(' — ' + detail) if detail else ''}", flush=True)


def _bundled_example() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for candidate in (os.path.join(here, "examples", "card_platform.sql"),
                      os.path.join(os.path.dirname(here), "examples", "card_platform.sql")):
        if os.path.exists(candidate):
            return candidate
    sys.exit("graphify: the bundled example is not installed alongside this copy; "
             "pass a .sql file instead")


def _load(args):
    if not os.path.exists(args.source):
        sys.exit(f"graphify: no such file: {args.source}")

    started = time.time()
    print(f"graphify {__version__}")
    print(f"reading {os.path.basename(args.source)}")
    source = open_source(args.source)
    stats = getattr(source, "stats", {})
    rows = sum(m["rows"] for m in source.tables.values())
    _step("loaded", f"{len(source.tables)} tables, {rows:,} rows"
          + (f", {stats['skipped']} statements skipped" if stats.get("skipped") else ""))

    model_path = args.model or _default_model_path(args.source)
    model = run_inference(source, on_progress=_step)

    if os.path.exists(model_path) and not args.fresh:
        try:
            model.adopt_decisions_from(model_io.load(model_path))
            _step("kept your decisions", os.path.basename(model_path))
        except Exception as exc:
            _step("could not read the existing model", str(exc))

    model_io.save(model, model_path)

    declared = sum(1 for r in model.relationships if r.tier == "declared")
    inferred = sum(1 for r in model.relationships if r.tier == "statistical")
    review = [r for r in model.relationships if not r.verified and r.confidence < 0.8]
    folded = sum(len(e.extended_by) for e in model.entities.values())

    print()
    print(f"  {len(model.entities)} entities, {len(model.events)} event tables")
    print(f"  {declared} declared relationships, {inferred} inferred from value overlap")
    if folded:
        print(f"  {folded} one-to-one satellite table(s) folded into their master")
    if review:
        print(f"  {len(review)} need a look:")
        for r in review[:6]:
            print(f"      {r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}"
                  f"   confidence {r.confidence}")
    print(f"  model written to {model_path}")
    print(f"  {time.time() - started:.1f}s")
    return source, model, model_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="graphify",
        description="Turn a relational database into a graph you can actually read.")
    p.add_argument("--version", action="version", version=f"graphify {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    demo = sub.add_parser("demo", help="open the bundled example, no file needed")
    demo.add_argument("-p", "--port", type=int, default=8000)
    demo.add_argument("--host", default="127.0.0.1")

    for name, help_text in [("open", "infer and open the browser"),
                            ("serve", "infer and serve, without opening a browser"),
                            ("infer", "write the model file and stop")]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("source", help="a .sql dump or a .sqlite/.db file")
        s.add_argument("-m", "--model", help="path to the model file "
                                             "(default: alongside the source)")
        s.add_argument("--fresh", action="store_true",
                       help="ignore an existing model file and infer from scratch")
        if name != "infer":
            s.add_argument("-p", "--port", type=int, default=8000)
            s.add_argument("--host", default="127.0.0.1")

    args = p.parse_args(argv)
    if args.cmd == "demo":
        # Nothing to install, nothing to find: the point is that somebody can
        # see what this does before deciding whether to point it at their data.
        args.source = _bundled_example()
        args.model = os.path.join(tempfile.gettempdir(), "graphify-demo.yaml")
        args.fresh = True
    source, model, model_path = _load(args)

    if args.cmd == "infer":
        return 0

    index = build_index(source, model)
    print(f"  graph: {len(index.nodes):,} nodes, {len(index.edges):,} links, "
          f"{len(index.edge_types)} relationship types")

    httpd, url = run_server(source, model, model_path, args.host, args.port,
                            open_browser=(args.cmd in ("open", "demo")))
    print(f"\n  {url}\n  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
