"""
Walking outward from one row, bounded so it stays readable.

Two brakes, not one. Depth is the ceiling; the node budget is what actually
stops it. Anything a brake cut off becomes a "+N" chip rather than silently
vanishing -- an unexpanded pile is information, not an omission.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

# The first ring around what you asked about deserves more room than the fifth.
GROUP_CAP = [14, 14, 7, 5, 4, 4, 3, 3, 3, 3, 3]
MAX_CHIPS = 40


def _span(edge, frm, to):
    ts = edge["ts"]
    return bisect_left(ts, frm), bisect_right(ts, to)


def weight_in(edge, frm, to):
    if not edge["ts"] or frm is None:
        return edge["n"]                 # untimed links never filter out
    a, b = _span(edge, frm, to)
    return b - a


def amount_in(edge, frm, to):
    if not edge["ts"] or frm is None:
        return edge["amount"]
    a, b = _span(edge, frm, to)
    return edge["cum"][b] - edge["cum"][a]


def expand(ix, seed, depth=2, budget=250, enabled=None, frm=None, to=None,
           extra=None, exclude=None, terminal=None):
    if seed not in ix.nodes:
        return None
    enabled = enabled if enabled is not None else [t.default_on for t in ix.edge_types]
    extra = extra or {}
    exclude = exclude or set()
    terminal = terminal or set()

    hop = {seed: 0}
    keep = {seed}
    used = set()
    supers = []
    frontier = [seed]
    budget_hit = False

    for d in range(1, depth + 1):
        if not frontier:
            break
        nxt, hop_supers = [], []
        for key in frontier:
            # A "stop" type is drawn but never travelled through. The seed is
            # always expanded: you asked about it, so stopping there draws a dot.
            if key != seed and ix.nodes[key]["table"] in terminal:
                continue
            groups = {}
            for ei in ix.adj[key]:
                e = ix.edges[ei]
                if not enabled[e["et"]]:
                    continue
                w = weight_in(e, frm, to)
                if w <= 0:
                    continue
                other = e["b"] if e["a"] == key else e["a"]
                on = ix.nodes[other]
                if on["table"] in exclude:
                    continue
                gk = f'{e["et"]}\x1f{on["table"]}'
                groups.setdefault(gk, {"et": e["et"], "table": on["table"], "items": []})
                groups[gk]["items"].append({"ei": ei, "other": other, "w": w, "deg": on["deg"]})

            for gk, g in groups.items():
                g["items"].sort(key=lambda it: (-it["w"], -it["deg"], it["other"]))
                super_key = f"{key}\x1f{gk}"
                cap = max(GROUP_CAP[min(d, len(GROUP_CAP) - 1)], extra.get(super_key, 0))
                shown, held = 0, []
                for it in g["items"]:
                    if it["other"] in keep:
                        used.add(it["ei"])
                        continue
                    if shown >= cap:
                        held.append(it); continue
                    if len(keep) >= budget:
                        held.append(it); budget_hit = True; continue
                    keep.add(it["other"]); hop[it["other"]] = d
                    used.add(it["ei"]); nxt.append(it["other"]); shown += 1
                if held:
                    sup = {"key": super_key, "parent": key, "hop": d, "et": g["et"],
                           "table": g["table"], "hidden": len(held),
                           "total": len(g["items"]), "held": held[:400],
                           "weight": sum(x["w"] for x in held)}
                    supers.append(sup)
                    hop_supers.append(sup)

        # The caps exist to stop one node's five hundred neighbours from eating
        # the screen. When the budget is nowhere near spent they are hiding
        # things for nothing, so finish off the groups that fit, smallest first,
        # and put what they admit into the next frontier -- otherwise a node
        # arrives on the canvas without any of its own neighbours.
        room = (budget - len(keep)) if d == depth else (budget - len(keep)) // 2
        spent = 0
        for sup in sorted((s for s in hop_supers if len(s["held"]) == s["hidden"]),
                          key=lambda s: s["hidden"]):
            if spent + sup["hidden"] > room:
                continue
            for it in sup["held"]:
                if it["other"] in keep:
                    continue
                keep.add(it["other"]); hop[it["other"]] = d
                used.add(it["ei"]); nxt.append(it["other"]); spent += 1
            sup["hidden"] = 0; sup["held"] = []
        frontier = nxt

    supers = [s for s in supers if s["hidden"] > 0]

    # Close the induced subgraph: an edge whose two ends both survived belongs
    # on the canvas even if the walk did not use it to get there. Without this
    # the picture looks like a tree when the data is a mesh.
    for key in keep:
        for ei in ix.adj[key]:
            e = ix.edges[ei]
            if ei in used or not enabled[e["et"]]:
                continue
            if e["a"] in keep and e["b"] in keep and weight_in(e, frm, to) > 0:
                used.add(ei)

    held_total = sum(s["hidden"] for s in supers)
    chips = supers
    if len(supers) > MAX_CHIPS:
        own = [s for s in supers if s["parent"] == seed]
        rest = sorted((s for s in supers if s["parent"] != seed),
                      key=lambda s: -s["hidden"])
        chips = own + rest[:max(0, MAX_CHIPS - len(own))]

    return {
        "seed": seed, "depth": depth, "budget": budget,
        "hop": hop, "keep": keep, "budget_hit": budget_hit,
        "held_total": held_total,
        "supers": [{k: v for k, v in s.items() if k != "held"} for s in chips],
        "nodes": [ix.nodes[k] for k in keep],
        "edges": [dict(ix.edges[i], w=weight_in(ix.edges[i], frm, to),
                       amt=amount_in(ix.edges[i], frm, to)) for i in used],
    }


def shortest_path(ix, a, b, enabled=None, frm=None, to=None):
    if a not in ix.nodes or b not in ix.nodes:
        return None
    enabled = enabled if enabled is not None else [t.default_on for t in ix.edge_types]
    prev = {a: None}
    frontier = [a]
    for _ in range(12):
        if not frontier:
            break
        nxt = []
        for key in frontier:
            for ei in ix.adj[key]:
                e = ix.edges[ei]
                if not enabled[e["et"]] or weight_in(e, frm, to) <= 0:
                    continue
                other = e["b"] if e["a"] == key else e["a"]
                if other in prev:
                    continue
                prev[other] = (key, ei)
                if other == b:
                    nodes, edges, cur = [], [], b
                    while cur != a:
                        p, ei2 = prev[cur]
                        nodes.append(cur); edges.append(ei2); cur = p
                    nodes.append(a)
                    return {"nodes": nodes[::-1], "edges": edges[::-1]}
                nxt.append(other)
        frontier = nxt
    return None
