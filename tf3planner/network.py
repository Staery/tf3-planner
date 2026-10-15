"""Existing street/track network of the map (export v6, section ``network``).

The mod exports every BASE_EDGE entity (town/country roads, highways, player roads and
tracks, construction-internal edges of stations/depots/industries) as flat arrays, see
mod/tf3_planner_1/content/tf3_planner/network.lua. This module turns them into graphs
(numpy/scipy only):

* ``TransportNetwork.from_export(export)`` / ``for_game(game)`` - None for exports older than v6.
* edge classification: ``kind`` road / rail / tram / other (tram-only tracks, airport
  runways/taxiways are not usable by road vehicles), ``owner`` country / town / player,
  ``struct`` normal / bridge / tunnel, ``speed`` = max lane speed of the street template
  (from the exported streetTemplates, else the lane speed sampled in the game, else a default).
* ``graph(kind, weight)`` - undirected scipy CSR graph (parallel edges reduced to the
  cheapest one), ``components(kind)``, ``nearest_node``, ``shortest_path``,
  ``path_matrix`` (shortest existing-network paths between arbitrary points, e.g. towns).
* ``grid_segments`` - rasterization of the existing edges onto a routing grid (pairs of
  8-adjacent grid cells covered by an existing way, with the way speed and optionally the
  edge index). routing.py uses it to make existing roads/tracks free to build.
* ``snap_point`` / ``path_between`` - nearest point on the real (curved) geometry and the
  shortest way between two such points along the network, as oriented per-edge polylines.
  routing.py uses them to draw route parts on existing ways exactly over the road / track
  instead of along grid cell centres.

Geometry: edges are cubic Hermite curves p0 -> p1 with the exported tangents when they look
sane (|T| within 0.3..3 x chord, as TF uses tangent length ~ edge length), straight otherwise.
One-way streets are treated as two-way (direction is not exported).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

__all__ = ["KINDS", "OWNERS", "STRUCTS", "Template", "NetPath", "TransportNetwork", "for_game"]

KINDS = ("road", "rail", "tram", "other")
ROAD, RAIL, TRAM, OTHER = range(4)
OWNERS = ("country", "town", "player")
COUNTRY, TOWN, PLAYER = range(3)
STRUCTS = ("normal", "bridge", "tunnel")
DEFAULT_SPEED = {ROAD: 13.89, RAIL: 27.78, TRAM: 11.11, OTHER: 8.33}


def _kind_code(kind) -> int:
    if isinstance(kind, (int, np.integer)):
        return int(kind)
    return KINDS.index(kind)


@dataclass
class Template:
    name: str
    id: int | None
    kind: int
    speed: float           # m/s, max lane speed
    town: bool             # town street template (town-owned when not player-owned)


@dataclass
class NetPath:
    kind: str
    nodes: np.ndarray       # network node indices
    edges: np.ndarray       # network edge indices
    length_m: float
    time_s: float
    polyline: list[tuple[float, float]]
    snap_a_m: float         # distance from the query point to the first node
    snap_b_m: float


@dataclass
class TransportNetwork:
    node_ids: np.ndarray            # (N,) entity ids
    xyz: np.ndarray                 # (N, 3)
    edge_ids: np.ndarray            # (M,)
    edges: np.ndarray               # (M, 2) node indices
    template: np.ndarray            # (M,) index into templates
    kind: np.ndarray                # (M,) int8, KINDS
    owner: np.ndarray               # (M,) int8, OWNERS
    struct: np.ndarray              # (M,) int8, STRUCTS
    type_index: np.ndarray          # (M,) bridge/tunnel type index (-1 none)
    length: np.ndarray              # (M,) curve length, m
    speed: np.ndarray               # (M,) m/s
    tangents: np.ndarray | None     # (M, 2, 3)
    templates: list[Template]
    failed_edges: int = 0
    export_seconds: float | None = None
    _cache: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_export(cls, export: dict) -> "TransportNetwork | None":
        """Build from an export dict (data/export.json). None if it has no v6 network graph."""
        n = (export or {}).get("network") or {}
        if not isinstance(n, dict) or not n.get("edgeNodes") or not n.get("nodeIds"):
            return None
        node_ids = np.asarray(n["nodeIds"], dtype=np.int64)
        xyz = np.asarray(n["nodePos"], dtype=np.float64).reshape(-1, 3)
        edges = np.asarray(n["edgeNodes"], dtype=np.int64).reshape(-1, 2)
        M = len(edges)

        def arr(key, default, dtype):
            v = n.get(key)
            if not isinstance(v, list) or len(v) != M:
                return np.full(M, default, dtype=dtype)
            return np.asarray([default if x is None or isinstance(x, str) else x for x in v], dtype=dtype)

        edge_ids = arr("edgeIds", -1, np.int64)
        tmpl_idx = arr("edgeTemplate", -1, np.int64)
        road_type = arr("edgeRoadType", -1, np.int64)
        struct_raw = arr("edgeStruct", 0, np.int64)
        type_index = arr("edgeTypeIndex", -1, np.int64)
        length = arr("edgeLength", -1.0, np.float64)
        owned = arr("edgeOwned", 0, np.int64) > 0
        tg = n.get("edgeTangents")
        tangents = np.asarray(tg, dtype=np.float64).reshape(M, 2, 3) if isinstance(tg, list) and len(tg) == 6 * M else None

        # export-wide street templates (cost/speed source of model.Way): name -> (roadType, max lane speed)
        known: dict[str, tuple[int | None, float]] = {}
        track_value = None
        for s in export.get("streetTemplates") or []:
            d = s.get("data") or {}
            lanes = d.get("laneConfigs") or []
            sp = max([float(l.get("speed") or 0) for l in lanes if isinstance(l, dict)] or [0.0])
            known[str(s.get("name"))] = (d.get("roadType"), sp)
            if "/track/" in str(s.get("name")) and isinstance(d.get("roadType"), (int, float)):
                track_value = int(d["roadType"])
        rtv = n.get("roadTypeValues") or {}
        if isinstance(rtv.get("TRACK"), (int, float)):
            track_value = int(rtv["TRACK"])
        if track_value is None:
            track_value = 0

        templates: list[Template] = []
        for t in n.get("templates") or []:
            name = str(t.get("name") or "")
            low = name.lower()
            rt = t.get("roadType")
            k_rt, k_sp = known.get(name, (None, 0.0))
            if rt is None:
                rt = k_rt
            if "/street/tram/" in low:
                kind = TRAM
            elif "/street/airport/" in low or "runway" in low or "taxiway" in low:
                kind = OTHER
            elif isinstance(rt, (int, float)):
                kind = RAIL if int(rt) == track_value else ROAD
            else:
                kind = RAIL if ("/track/" in low or "/depots/rail" in low or "industry_track" in low) else ROAD
            speed = k_sp or float(t.get("speed") or 0) or DEFAULT_SPEED[kind]
            templates.append(Template(name, t.get("id"), kind, float(speed), "/street/town/" in low))
        if not templates:
            templates.append(Template("?", None, ROAD, DEFAULT_SPEED[ROAD], False))

        tmpl_ok = (tmpl_idx >= 0) & (tmpl_idx < len(templates))
        ti = np.where(tmpl_ok, tmpl_idx, 0)
        t_kind = np.array([t.kind for t in templates], dtype=np.int8)
        t_speed = np.array([t.speed for t in templates], dtype=np.float64)
        t_town = np.array([t.town for t in templates], dtype=bool)
        kind = t_kind[ti]
        no_tmpl = ~tmpl_ok
        if no_tmpl.any():   # fall back to the raw roadType of the edge
            kind[no_tmpl] = np.where(road_type[no_tmpl] == track_value, RAIL, ROAD)
        speed = np.where(tmpl_ok, t_speed[ti], [DEFAULT_SPEED[int(k)] for k in kind] if M else [])
        owner = np.where(owned, PLAYER, np.where(t_town[ti] & tmpl_ok, TOWN, COUNTRY)).astype(np.int8)

        sv = n.get("structValues") or {}
        struct = np.zeros(M, np.int8)
        bval = sv.get("BRIDGE") if isinstance(sv.get("BRIDGE"), (int, float)) else 1
        tval = sv.get("TUNNEL") if isinstance(sv.get("TUNNEL"), (int, float)) else 2
        struct[struct_raw == bval] = 1
        struct[struct_raw == tval] = 2

        p0, p1 = xyz[edges[:, 0]], xyz[edges[:, 1]]
        chord = np.linalg.norm(p1 - p0, axis=1)
        length = np.where(length > 0, np.maximum(length, chord), chord)
        return cls(node_ids, xyz, edge_ids, edges, ti.astype(np.int32), kind.astype(np.int8), owner, struct,
                   type_index, length, np.asarray(speed, np.float64), tangents, templates,
                   int(n.get("failedEdges") or 0), n.get("seconds"))

    # ------------------------------------------------------------------ basics
    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def n_edges(self) -> int:
        return len(self.edges)

    def hash(self) -> str:
        if "hash" not in self._cache:
            h = hashlib.sha1()
            for a in (self.node_ids, np.round(self.xyz, 1), self.edges, self.kind, self.owner,
                      np.round(self.speed, 3), np.round(self.length, 1)):
                h.update(np.ascontiguousarray(a).tobytes())
            self._cache["hash"] = h.hexdigest()[:16]
        return self._cache["hash"]

    def mask(self, kind=None, owners=None) -> np.ndarray:
        m = np.ones(self.n_edges, bool)
        if kind is not None:
            ks = [kind] if isinstance(kind, (str, int, np.integer)) else list(kind)
            m &= np.isin(self.kind, [_kind_code(k) for k in ks])
        if owners is not None:
            os_ = [owners] if isinstance(owners, (str, int, np.integer)) else list(owners)
            m &= np.isin(self.owner, [o if isinstance(o, (int, np.integer)) else OWNERS.index(o) for o in os_])
        return m

    def edge_time(self) -> np.ndarray:
        return self.length / np.maximum(self.speed, 0.1)

    def summary(self) -> dict:
        out = {"nodes": self.n_nodes, "edges": self.n_edges, "failed_edges": self.failed_edges,
               "export_seconds": self.export_seconds, "hash": self.hash(), "km": {}}
        for k, kn in enumerate(KINDS):
            m = self.kind == k
            if not m.any():
                continue
            out["km"][kn] = {OWNERS[o]: round(float(self.length[m & (self.owner == o)].sum()) / 1000, 1)
                             for o in range(3) if (m & (self.owner == o)).any()}
            out["km"][kn]["bridges"] = round(float(self.length[m & (self.struct == 1)].sum()) / 1000, 2)
            out["km"][kn]["tunnels"] = round(float(self.length[m & (self.struct == 2)].sum()) / 1000, 2)
            lab = self.components(kn)
            sizes = np.bincount(lab[lab >= 0]) if (lab >= 0).any() else np.zeros(0)
            out["km"][kn]["components"] = int(len(sizes))
            out["km"][kn]["largest_component_nodes"] = int(sizes.max()) if len(sizes) else 0
        return out

    # ------------------------------------------------------------------ graphs
    def graph(self, kind="road", weight: str = "time", owners=None) -> tuple[sparse.csr_matrix, sparse.csr_matrix]:
        """(weights, edge index + 1) CSR matrices of the undirected graph of the given kind(s).
        weight: 'time' (s) or 'length' (m). Parallel edges keep the cheapest one."""
        key = ("graph", str(kind), weight, str(owners))
        if key in self._cache:
            return self._cache[key]
        m = self.mask(kind, owners)
        idx = np.nonzero(m & (self.edges[:, 0] != self.edges[:, 1]))[0]
        w = (self.edge_time() if weight == "time" else self.length)[idx]
        w = np.maximum(w, 1e-6)
        u, v = self.edges[idx, 0], self.edges[idx, 1]
        rows, cols = np.concatenate([u, v]), np.concatenate([v, u])
        ww, ee = np.concatenate([w, w]), np.concatenate([idx, idx])
        order = np.lexsort((ww, cols, rows))
        rows, cols, ww, ee = rows[order], cols[order], ww[order], ee[order]
        first = np.ones(len(rows), bool)
        if len(rows) > 1:
            first[1:] = (rows[1:] != rows[:-1]) | (cols[1:] != cols[:-1])
        rows, cols, ww, ee = rows[first], cols[first], ww[first], ee[first]
        N = self.n_nodes
        G = sparse.csr_matrix((ww, (rows, cols)), shape=(N, N))
        E = sparse.csr_matrix((ee + 1, (rows, cols)), shape=(N, N))
        self._cache[key] = (G, E)
        return G, E

    def kind_nodes(self, kind="road") -> np.ndarray:
        """Indices of nodes touched by at least one edge of the kind."""
        key = ("nodes", str(kind))
        if key not in self._cache:
            m = self.mask(kind)
            self._cache[key] = np.unique(self.edges[m].ravel())
        return self._cache[key]

    def components(self, kind="road") -> np.ndarray:
        """Connected component label per node (-1 for nodes without an edge of the kind)."""
        key = ("comp", str(kind))
        if key not in self._cache:
            G, _ = self.graph(kind, "length")
            _, lab = csgraph.connected_components(G, directed=False)
            out = np.full(self.n_nodes, -1, np.int64)
            kn = self.kind_nodes(kind)
            if len(kn):
                # relabel 0..k-1 by decreasing size, only over nodes of the kind
                uniq, inv, cnt = np.unique(lab[kn], return_inverse=True, return_counts=True)
                rank = np.empty(len(uniq), np.int64)
                rank[np.argsort(-cnt, kind="stable")] = np.arange(len(uniq))
                out[kn] = rank[inv]
            self._cache[key] = out
        return self._cache[key]

    def nearest_node(self, x: float, y: float, kind="road", max_dist: float = math.inf) -> tuple[int, float]:
        """(node index, distance) of the nearest node having an edge of the kind; (-1, inf) if none."""
        key = ("tree", str(kind))
        if key not in self._cache:
            kn = self.kind_nodes(kind)
            self._cache[key] = (cKDTree(self.xyz[kn, :2]) if len(kn) else None, kn)
        tree, kn = self._cache[key]
        if tree is None:
            return -1, math.inf
        d, k = tree.query([x, y], distance_upper_bound=max_dist if math.isfinite(max_dist) else np.inf)
        if not np.isfinite(d):
            return -1, math.inf
        return int(kn[k]), float(d)

    def nearest_nodes(self, pts: np.ndarray, kind="road") -> tuple[np.ndarray, np.ndarray]:
        self.nearest_node(0.0, 0.0, kind)
        tree, kn = self._cache[("tree", str(kind))]
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        if tree is None:
            return np.full(len(pts), -1, np.int64), np.full(len(pts), np.inf)
        d, k = tree.query(pts)
        return kn[k].astype(np.int64), d

    def _resolve_point(self, p, kind) -> tuple[int, float, tuple[float, float]]:
        if isinstance(p, (int, np.integer)):
            return int(p), 0.0, (float(self.xyz[p, 0]), float(self.xyz[p, 1]))
        if hasattr(p, "x") and hasattr(p, "y"):
            p = (p.x, p.y)
        x, y = float(p[0]), float(p[1])
        k, d = self.nearest_node(x, y, kind)
        return k, d, (x, y)

    def shortest_path(self, a, b, kind="road", weight: str = "time") -> NetPath | None:
        """Shortest path on the existing network between a and b (node index, (x, y) or an object
        with .x/.y - snapped to the nearest node of the kind). None if not connected."""
        ka, da, _ = self._resolve_point(a, kind)
        kb, db, _ = self._resolve_point(b, kind)
        if ka < 0 or kb < 0:
            return None
        G, E = self.graph(kind, weight)
        dist, pred = csgraph.dijkstra(G, directed=False, indices=ka, return_predecessors=True)
        if not np.isfinite(dist[kb]):
            return None
        nodes = [kb]
        while nodes[-1] != ka:
            nodes.append(int(pred[nodes[-1]]))
        nodes = np.array(nodes[::-1], np.int64)
        eidx = np.array([int(E[u, v]) - 1 for u, v in zip(nodes[:-1], nodes[1:])], np.int64)
        L = float(self.length[eidx].sum()) if len(eidx) else 0.0
        T = float(self.edge_time()[eidx].sum()) if len(eidx) else 0.0
        poly = [(float(self.xyz[k, 0]), float(self.xyz[k, 1])) for k in nodes]
        return NetPath(KINDS[_kind_code(kind)], nodes, eidx, L, T, poly, da, db)

    def path_matrix(self, points, kind="road", weight: str = "time", max_snap: float = math.inf):
        """Shortest existing-network paths between all points (towns, industries, (x, y)).

        Returns dict with ``length`` and ``time`` matrices (inf = not connected or snap farther than
        max_snap), ``snap_node``, ``snap_dist`` and ``component`` per point."""
        pts = np.array([(p.x, p.y) if hasattr(p, "x") else (p[0], p[1]) for p in points], dtype=np.float64)
        n = len(pts)
        nodes, sd = self.nearest_nodes(pts, kind)
        ok = (nodes >= 0) & (sd <= max_snap)
        L = np.full((n, n), np.inf)
        T = np.full((n, n), np.inf)
        np.fill_diagonal(L, 0.0)
        np.fill_diagonal(T, 0.0)
        comp = np.full(n, -1, np.int64)
        if ok.any():
            G, E = self.graph(kind, weight)
            src = np.unique(nodes[ok])
            pos = {int(s): r for r, s in enumerate(src)}
            dist, pred = csgraph.dijkstra(G, directed=False, indices=src, return_predecessors=True)
            other = self.length if weight == "time" else self.edge_time()
            lab = self.components(kind)
            comp[ok] = lab[nodes[ok]]
            N = self.n_nodes
            okj = np.nonzero(ok)[0]
            for i in okj:
                r = pos[int(nodes[i])]
                # the other metric along the chosen paths: Dijkstra on the shortest-path tree
                reach = np.nonzero(pred[r] >= 0)[0]
                pv = pred[r, reach]
                eid = np.asarray(E[pv, reach]).ravel().astype(np.int64) - 1
                tree = sparse.csr_matrix((np.maximum(other[eid], 1e-9), (pv, reach)), shape=(N, N))
                acc = csgraph.dijkstra(tree, directed=True, indices=int(nodes[i]))
                for j in okj:
                    if i == j or not np.isfinite(dist[r, nodes[j]]):
                        continue
                    w, a = float(dist[r, nodes[j]]), float(acc[nodes[j]])
                    if weight == "time":
                        T[i, j], L[i, j] = w, a
                    else:
                        L[i, j], T[i, j] = w, a
        return {"length": L, "time": T, "snap_node": nodes, "snap_dist": sd, "component": comp}

    # ------------------------------------------------------------------ geometry
    def edge_samples(self, mask: np.ndarray | None = None, spacing: float = 16.0):
        """Points along edges: (edge index per sample, x, y, sample order within the edge).
        Samples include both end points; Hermite curves where the tangents look sane."""
        idx = np.nonzero(mask)[0] if mask is not None else np.arange(self.n_edges)
        if len(idx) == 0:
            z = np.zeros(0)
            return z.astype(np.int64), z, z
        p0 = self.xyz[self.edges[idx, 0], :2]
        p1 = self.xyz[self.edges[idx, 1], :2]
        chord = np.linalg.norm(p1 - p0, axis=1)
        L = np.maximum(self.length[idx], chord)
        ns = np.maximum(2, np.ceil(L / spacing).astype(np.int64) + 1)
        rep = np.repeat(np.arange(len(idx)), ns)
        start = np.concatenate([[0], np.cumsum(ns)[:-1]])
        t = (np.arange(int(ns.sum())) - start[rep]) / (ns[rep] - 1)
        a, b = p0[rep], p1[rep]
        x = a + (b - a) * t[:, None]
        if self.tangents is not None:
            T0 = self.tangents[idx, 0, :2]
            T1 = self.tangents[idx, 1, :2]
            n0, n1 = np.linalg.norm(T0, axis=1), np.linalg.norm(T1, axis=1)
            sane = (chord > 1.0) & (n0 > 0.3 * chord) & (n0 < 3 * chord) & (n1 > 0.3 * chord) & (n1 < 3 * chord)
            sr = sane[rep]
            if sr.any():
                tt = t[sr][:, None]
                h00, h10 = 2 * tt ** 3 - 3 * tt ** 2 + 1, tt ** 3 - 2 * tt ** 2 + tt
                h01, h11 = -2 * tt ** 3 + 3 * tt ** 2, tt ** 3 - tt ** 2
                x[sr] = h00 * a[sr] + h10 * T0[rep][sr] + h01 * b[sr] + h11 * T1[rep][sr]
        return idx[rep], x[:, 0], x[:, 1]

    def grid_segments(self, kind, x0: float, y0: float, cell: float, nx: int, ny: int,
                      owners=None, return_edges: bool = False):
        """Rasterize existing edges of the kind onto a grid (node (ix, iy) at x0 + ix*cell, y0 + iy*cell).

        Returns (u, v, speed): unique pairs of 8-adjacent grid cells (flat iy*nx+ix, u < v) that an
        existing way passes through consecutively, with the fastest such way's speed (m/s).
        ``return_edges``: also the index of that fastest network edge per pair (4th array)."""
        m = self.mask(kind, owners)
        if not m.any():
            z = np.zeros(0, np.int64)
            return (z, z, np.zeros(0), z) if return_edges else (z, z, np.zeros(0))
        e, x, y = self.edge_samples(m, spacing=cell / 4.0)
        ix = np.clip(np.rint((x - x0) / cell), 0, nx - 1).astype(np.int64)
        iy = np.clip(np.rint((y - y0) / cell), 0, ny - 1).astype(np.int64)
        c = iy * nx + ix
        same_edge = e[1:] == e[:-1]
        u, v = c[:-1][same_edge], c[1:][same_edge]
        ee = e[1:][same_edge]
        sp = self.speed[ee]
        keep = u != v
        u, v, sp, ee = u[keep], v[keep], sp[keep], ee[keep]
        dy = np.abs(v // nx - u // nx)
        dx = np.abs(v % nx - u % nx)
        adj = (dy <= 1) & (dx <= 1)
        u, v, sp, ee = u[adj], v[adj], sp[adj], ee[adj]
        lo, hi = np.minimum(u, v), np.maximum(u, v)
        if len(lo) == 0:
            return (lo, hi, sp, ee) if return_edges else (lo, hi, sp)
        key = lo * (nx * ny) + hi
        order = np.lexsort((-sp, key))
        key, lo, hi, sp, ee = key[order], lo[order], hi[order], sp[order], ee[order]
        first = np.ones(len(key), bool)
        first[1:] = key[1:] != key[:-1]
        if return_edges:
            return lo[first], hi[first], sp[first], ee[first].astype(np.int64)
        return lo[first], hi[first], sp[first]

    # ------------------------------------------------------------------ snapping to real geometry
    def _samples(self, kind, spacing: float = 8.0) -> dict:
        """Dense curve samples of all edges of the kind, grouped per edge in node0 -> node1 order:
        edge per sample, xy, arc length from the edge start, per-edge [start, end) offsets."""
        key = ("samples", str(kind), spacing)
        if key not in self._cache:
            e, x, y = self.edge_samples(self.mask(kind), spacing=spacing)
            xy = np.column_stack([x, y]) if len(e) else np.zeros((0, 2))
            ar = np.arange(self.n_edges)
            start = np.searchsorted(e, ar, "left")
            end = np.searchsorted(e, ar, "right")
            d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])) if len(e) > 1 else np.zeros(0)
            d = np.where(e[1:] == e[:-1], d, 0.0) if len(e) > 1 else d
            cs = np.concatenate([[0.0], np.cumsum(d)]) if len(e) else np.zeros(0)
            s = cs - cs[start[e]] if len(e) else cs
            self._cache[key] = {"e": e, "xy": xy, "s": s, "start": start, "end": end,
                                "tree": cKDTree(xy) if len(e) else None}
        return self._cache[key]

    def snap_point(self, x: float, y: float, kind="road", edges=None, spacing: float = 8.0):
        """Nearest point on the curve geometry of the existing ways of the kind (optionally only
        on the given edge indices). Returns (edge, global sample index, distance) or None."""
        S = self._samples(kind, spacing)
        if S["tree"] is None:
            return None
        if edges is not None:
            ids = [int(k) for k in np.unique(np.asarray(edges, np.int64)) if 0 <= k < self.n_edges]
            rng = [np.arange(S["start"][k], S["end"][k]) for k in ids if S["end"][k] > S["start"][k]]
            if rng:
                g = np.concatenate(rng)
                d = np.hypot(S["xy"][g, 0] - x, S["xy"][g, 1] - y)
                k = int(np.argmin(d))
                return int(S["e"][g[k]]), int(g[k]), float(d[k])
        d, g = S["tree"].query([x, y])
        return int(S["e"][g]), int(g), float(d)

    def _piece(self, S: dict, e: int, g_from: int, g_to: int) -> np.ndarray:
        """Samples of edge e between two global sample indices (inclusive, either direction)."""
        if g_from <= g_to:
            return S["xy"][g_from:g_to + 1]
        return S["xy"][g_to:g_from + 1][::-1]

    def path_between(self, a, b, kind="road", max_length: float = math.inf, spacing: float = 8.0):
        """Shortest (by length) way along the existing network between two snapped points
        ``(edge, global sample index)`` from :meth:`snap_point`, following the real curve geometry.

        Returns ``(pieces, length_m)`` with pieces ``[(edge index, (k, 2) xy array), ...]`` in
        travel order (the first / last piece are partial edges), or None if not connected within
        max_length."""
        S = self._samples(kind, spacing)
        ea, ga = int(a[0]), int(a[1])
        eb, gb = int(b[0]), int(b[1])
        sa, sb = float(S["s"][ga]), float(S["s"][gb])
        La = float(S["s"][S["end"][ea] - 1])
        Lb = float(S["s"][S["end"][eb] - 1])
        best = None
        if ea == eb:
            best = (abs(sb - sa), [(ea, self._piece(S, ea, ga, gb))])
        ua, va = (int(q) for q in self.edges[ea])
        ub, vb = (int(q) for q in self.edges[eb])
        G, E = self.graph(kind, "length")
        lim = max_length if math.isfinite(max_length) else np.inf
        dist, pred = csgraph.dijkstra(G, directed=False, indices=[ua, va], return_predecessors=True, limit=lim)
        offa = {ua: sa, va: La - sa}
        offb = {ub: sb, vb: Lb - sb}
        combo = None
        for r, na in enumerate((ua, va)):
            for nb in (ub, vb):
                tot = offa[na] + float(dist[r, nb]) + offb[nb]
                if np.isfinite(tot) and (combo is None or tot < combo[0]):
                    combo = (tot, r, na, nb)
        if combo is not None and (best is None or combo[0] < best[0] - 1e-9):
            tot, r, na, nb = combo
            nodes = [nb]
            while nodes[-1] != na:
                nodes.append(int(pred[r, nodes[-1]]))
            nodes.reverse()
            ga_end = S["start"][ea] if na == ua else S["end"][ea] - 1
            pieces = [(ea, self._piece(S, ea, ga, int(ga_end)))]
            eids = (np.asarray(E[nodes[:-1], nodes[1:]]).ravel().astype(np.int64) - 1).tolist() if len(nodes) > 1 else []
            for p, q, e in zip(nodes[:-1], nodes[1:], eids):
                g0, g1 = int(S["start"][e]), int(S["end"][e]) - 1
                pieces.append((e, self._piece(S, e, g0, g1) if int(self.edges[e, 0]) == p else self._piece(S, e, g1, g0)))
            gb_start = S["start"][eb] if nb == ub else S["end"][eb] - 1
            pieces.append((eb, self._piece(S, eb, int(gb_start), gb)))
            best = (tot, pieces)
        if best is None or best[0] > max_length:
            return None
        pieces = [(e, pts) for e, pts in best[1] if len(pts) >= 2 or len(best[1]) == 1]
        return pieces, float(best[0])

    def template_key(self, e: int) -> str:
        """Short template name of an edge, e.g. 'country_old_small' (matches model.Way.key)."""
        name = self.templates[int(self.template[e])].name
        return name.rsplit("/", 1)[-1].split(".", 1)[0]


# --------------------------------------------------------------------------- per-game cache

_BY_GAME: dict[int, tuple[object, TransportNetwork | None]] = {}


def for_game(game) -> TransportNetwork | None:
    """Network of a model.Game (cached; None if the export has no v6 network)."""
    raw = getattr(game, "raw", None)
    key = id(game)
    hit = _BY_GAME.get(key)
    if hit is not None and hit[0] is raw:
        return hit[1]
    net = TransportNetwork.from_export(raw) if isinstance(raw, dict) else None
    if len(_BY_GAME) > 4:
        _BY_GAME.pop(next(iter(_BY_GAME)))
    _BY_GAME[key] = (raw, net)
    return net


if __name__ == "__main__":
    import json
    import sys
    import time

    from .routing import load_game, network_nodes

    g = load_game()
    net = for_game(g)
    if net is None:
        print(f"export v{g.raw.get('version')}: no street/track network (needs export v6 - restart the game and export)")
        sys.exit(0)
    print(json.dumps(net.summary(), ensure_ascii=False, indent=1))
    towns = [n for n in network_nodes(g) if n.kind == "town"]
    t0 = time.perf_counter()
    pm = net.path_matrix(towns, "road")
    print(f"town road matrix: {time.perf_counter() - t0:.2f} s; snap distance median "
          f"{np.median(pm['snap_dist']):.0f} m, max {pm['snap_dist'].max():.0f} m")
    comps: dict[int, list[str]] = {}
    for t, c in zip(towns, pm["component"].tolist()):
        comps.setdefault(c, []).append(t.name)
    print("road components of towns:", {k: len(v) for k, v in comps.items()})
    L, T = pm["length"], pm["time"]
    for i in range(min(len(towns), 6)):
        j = int(np.argmin(np.where(np.arange(len(towns)) == i, np.inf,
                                   np.hypot([t.x - towns[i].x for t in towns], [t.y - towns[i].y for t in towns]))))
        print(f"  {towns[i].name} -> {towns[j].name}: straight "
              f"{math.hypot(towns[i].x - towns[j].x, towns[i].y - towns[j].y) / 1000:.1f} km, road "
              f"{L[i, j] / 1000:.1f} km, {T[i, j] / 60:.1f} min")
