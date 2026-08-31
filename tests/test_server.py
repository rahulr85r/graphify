"""The server hands the browser a dataset and a model, and nothing else."""

import json
import os
import threading
import urllib.request

from graphify.infer import infer
from graphify.ingest import open_source
from graphify.server import serve

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def running():
    src = open_source(os.path.join(EXAMPLES, "card_platform.sql"))
    model = infer(src)
    httpd, url = serve(src, model, "", port=8911, open_browser=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return src, model, httpd, url


def get(url, path):
    with urllib.request.urlopen(url + path, timeout=20) as r:
        return r.status, r.read()


def test_it_serves_the_page_the_model_and_the_data():
    src, model, httpd, url = running()
    try:
        status, body = get(url, "")
        assert status == 200 and b"<title>" in body

        status, body = get(url, "api/model")
        payload = json.loads(body)
        assert payload["summary"]["entities"] == len(model.entities)
        assert "relationships:" in payload["yaml"]

        status, body = get(url, "api/dataset")
        data = json.loads(body)
        assert set(data["tables"]) == set(src.tables)
        # Every relationship the inference settled on is handed over as an
        # explicit key, so the browser never has to guess at anything the
        # server already worked out.
        wired = {(t, f["column"], f["table"])
                 for t, spec in data["tables"].items() for f in spec["fks"]}
        for r in model.relationships:
            if r.enabled and r.to_table in model.entities:
                assert (r.from_table, r.from_column, r.to_table) in wired
    finally:
        httpd.shutdown(); httpd.server_close(); src.close()


def test_a_thread_per_request_does_not_break_sqlite():
    """Each request lands on its own thread; the connection has to allow it."""
    src, model, httpd, url = running()
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(get(url, "api/dataset")[0]))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert results == [200] * 6
    finally:
        httpd.shutdown(); httpd.server_close(); src.close()


def test_an_unknown_path_is_a_clean_404():
    src, model, httpd, url = running()
    try:
        try:
            get(url, "api/nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown(); httpd.server_close(); src.close()


def test_the_bundled_example_can_be_found():
    """`graphify demo` has to work without the user finding a file first."""
    from graphify.cli import _bundled_example
    assert os.path.exists(_bundled_example())
