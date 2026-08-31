"""The walk: things become nodes, events become edges, and both brakes work."""

import os

from graphify.graph import build_index, expand, shortest_path
from graphify.infer import infer
from graphify.ingest import open_source

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def load():
    src = open_source(os.path.join(EXAMPLES, "card_platform.sql"))
    model = infer(src)
    return src, model, build_index(src, model)


def by_table(view):
    out = {}
    for n in view["nodes"]:
        out[n["table"]] = out.get(n["table"], 0) + 1
    return out


def test_events_become_edges_and_never_nodes():
    src, model, ix = load()
    assert set(ix.by_table) == set(model.entities)
    assert "transactions" not in ix.by_table
    assert any(t.via == "transactions" for t in ix.edge_types)
    src.close()


def test_a_walk_is_bounded_by_its_budget():
    src, model, ix = load()
    seed = ix.by_table["customers"][0]["key"]
    for budget in (25, 60, 150):
        v = expand(ix, seed, depth=4, budget=budget)
        assert len(v["nodes"]) <= budget
    src.close()


def test_depth_one_reaches_only_neighbours():
    src, model, ix = load()
    seed = ix.by_table["customers"][0]["key"]
    v = expand(ix, seed, depth=1, budget=500)
    assert set(v["hop"].values()) <= {0, 1}
    src.close()


def test_nothing_is_hidden_without_being_counted():
    src, model, ix = load()
    seed = ix.by_table["merchants"][0]["key"]
    v = expand(ix, seed, depth=2, budget=40)
    assert v["held_total"] == 0 or v["supers"]
    assert v["held_total"] >= sum(s["hidden"] for s in v["supers"])
    src.close()


def test_a_stop_type_is_drawn_but_not_travelled_through():
    src, model, ix = load()
    seed = next(n for n in ix.by_table["customers"] if n["label"] == "Kyle Gilbert")["key"]
    through = expand(ix, seed["key"] if isinstance(seed, dict) else seed,
                     depth=2, budget=400)
    stopped = expand(ix, seed, depth=2, budget=400, terminal={"merchants"})
    assert by_table(stopped).get("merchants"), "still drawn"
    assert len(stopped["nodes"]) < len(through["nodes"]), "but not walked through"
    src.close()


def test_excluding_a_type_removes_it_from_the_walk_entirely():
    src, model, ix = load()
    seed = ix.by_table["customers"][0]["key"]
    v = expand(ix, seed, depth=3, budget=300, exclude={"merchants", "emails"})
    assert "merchants" not in by_table(v)
    assert "emails" not in by_table(v)
    src.close()


def test_a_date_window_filters_events_but_never_keys():
    src, model, ix = load()
    seed = next(n for n in ix.by_table["customers"] if n["label"] == "Kyle Gilbert")["key"]
    day = 86400
    week = expand(ix, seed, depth=2, budget=300, frm=ix.t_max - 7 * day, to=ix.t_max,
                  terminal={"merchants"})
    everything = expand(ix, seed, depth=2, budget=300, terminal={"merchants"})
    assert len(week["nodes"]) < len(everything["nodes"])
    # A key exists for as long as both rows do. Narrowing the dates must not
    # take somebody's account or email address away from them.
    assert by_table(week).get("accounts"), "keys survive the window"
    assert by_table(week).get("emails"), "keys survive the window"
    src.close()


def test_a_path_between_two_people_can_be_walked():
    src, model, ix = load()
    a = ix.by_table["customers"][0]["key"]
    b = ix.by_table["customers"][5]["key"]
    p = shortest_path(ix, a, b)
    assert p and p["nodes"][0] == a and p["nodes"][-1] == b
    assert len(p["edges"]) == len(p["nodes"]) - 1
    src.close()


def test_the_household_holds_together():
    """The demo persona is the thing anybody looks at first."""
    src, model, ix = load()
    seed = next(n for n in ix.by_table["customers"] if n["label"] == "Kyle Gilbert")["key"]
    v = expand(ix, seed, depth=2, budget=300, terminal={"merchants"})
    people = sorted(n["label"] for n in v["nodes"] if n["table"] == "customers")
    assert people == ["Dana Gilbert", "Kyle Gilbert", "Ryan Gilbert"]
    assert by_table(v)["devices"] == 2
    assert by_table(v)["cards"] == 1
    src.close()
