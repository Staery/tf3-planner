"""Process-parallel candidate generation for planner.run (Windows "spawn"-safe).

The three heavy generators (passengers, multimodal chains, tours) take ~90 % of a plan run and
are re-run every January with read-only inputs. Their outer loops are independent per key
(town / town pair / cargo / producer node; every cache inside them is keyed by that key or is a
pure function of the static map), so the keys are split between worker processes and the parts
are merged back in the serial order -> the merged candidate lists equal the serial ones.

Workers: N single-process executors (fixed affinity: worker k always gets shard k, so its
caches - routing trees, tour orders, CVRP partitions - stay warm from year to year). Each worker
receives a pickled copy of the Planner once (initializer) and every January a small state blob
(free supply/demand, built keys/stations, project volumes, town growth state).

January, two round trips:
  A  passengers intra-town (by town) + intercity (by pair), multimodal (by cargo),
     tours milk runs (by cargo) + circuits (by producer node)
  B  passenger bundles (by pair; need all town loops from A), tours route geometry of the
     selected lines (round robin)
The planner dispatches A before the MILP target design and the direct-link candidates, which
run in the main process meanwhile.

Settings (environment, from data/settings.json via the control center):
  TF3P_WORKERS  "auto" (default): logical cores - 1, capped by free RAM (~0.75 GB per worker);
                "0" / "1"... : exact number of worker processes ("0" = serial, as before).
"""
from __future__ import annotations

import os
import pickle
import sys
import time
import traceback
import zlib
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace

ENV_WORKERS = "TF3P_WORKERS"
WORKER_MB = 750            # working set of one worker after a few Januaries (measured ~0.6-0.7 GB)
RESERVE_MB = 1500          # RAM left to the OS / game / main process when sizing "auto"
OFFLOAD = ("gen_passengers", "gen_multimodal", "gen_tours")
_PLANNER_SKIP = ("generators", "_eval_cache", "_eval_cache_year", "_rank_fn", "_pool", "_pending")


# --------------------------------------------------------------------------- sizing

def _mem_mb() -> tuple[float, float]:
    """(available physical RAM, available commit) in MB; (inf, inf) when unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullAvailPhys / 2 ** 20, m.ullAvailPageFile / 2 ** 20
        else:
            pages = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return pages / 2 ** 20, float("inf")
    except Exception:
        pass
    return float("inf"), float("inf")


def worker_count(env: str | None = None) -> int:
    """Worker processes for this run: TF3P_WORKERS (int) or auto = cores - 1 capped by free RAM."""
    v = (os.environ.get(ENV_WORKERS, "") if env is None else env).strip().lower()
    if v not in ("", "auto"):
        try:
            return max(0, int(v))
        except ValueError:
            pass
    n = max(0, (os.cpu_count() or 1) - 1)
    phys, commit = _mem_mb()
    n_mem = int((phys - RESERVE_MB) // WORKER_MB) if phys != float("inf") else n
    # Windows commit charge: each worker commits ~ its working set (BLAS threads are pinned to 1)
    n_commit = int((commit - RESERVE_MB) // WORKER_MB) if commit != float("inf") else n
    return max(0, min(n, n_mem, n_commit))


def shard_of(key, n: int) -> int:
    """Stable shard of a key (same worker every year -> warm caches)."""
    return zlib.crc32(repr(key).encode("utf-8")) % n


# --------------------------------------------------------------------------- state shipped to workers

def planner_payload(pl, flags: dict) -> bytes:
    state = {k: v for k, v in pl.__dict__.items() if k not in _PLANNER_SKIP}
    return pickle.dumps((type(pl), state, flags), protocol=pickle.HIGHEST_PROTOCOL)


def _towns_state(pl):
    tm = getattr(pl, "towns", None)
    if tm is None:
        return None
    return {k: v for k, v in tm.__dict__.items() if k != "game"}


def state_blob(pl, sim, ver: int) -> bytes:
    """What the offloaded generators read from the simulation (see module docstring)."""
    projects = [SimpleNamespace(cand=SimpleNamespace(p=pr.cand.p, d=pr.cand.d, cargo=pr.cand.cargo,
                                                     mode=pr.cand.mode),
                                volume=pr.volume, income=pr.income, running=pr.running)
                for pr in sim.projects]
    generic = [SimpleNamespace(uses_supply=dict(g.uses_supply), kind=g.kind, nodes=list(g.nodes),
                               meta={"chain_output": (g.meta or {}).get("chain_output") or {}})
               for g in sim.generic]
    lite = {"month": sim.month, "supply": sim.supply, "demand": sim.demand, "delivered": sim.delivered,
            "built_pairs": sim.built_pairs, "stations": sim.stations, "projects": projects, "generic": generic}
    for k in ("levels", "cap_mult", "boosted", "grow_acc"):
        if hasattr(sim, k):
            lite[k] = getattr(sim, k)
    towns = (_towns_state(pl), bool(getattr(pl, "_towns_applied", False)))
    return pickle.dumps((ver, lite, towns), protocol=pickle.HIGHEST_PROTOCOL)


# --------------------------------------------------------------------------- worker side

_W: dict = {}


def _init(payload: bytes, n: int, k: int):
    cls, state, flags = pickle.loads(payload)
    pl = cls.__new__(cls)
    pl.__dict__.update(state)
    pl.generators = []
    _W.update(pl=pl, n=n, k=k, flags=flags, ver=None, towns=None)
    # structures the serial run creates at the first January call from the initial game state
    # (towns grown later must not leak into them): build them now, before any town update
    if flags.get("passengers", True):
        from . import passengers as pax
        p = pax.PassengerParams()
        if p.use_streets:
            pax.street_planner(pl.game, pax.town_infos(pl.game, p))
    if flags.get("multimodal", True):
        from . import multimodal as mm
        mm._context(pl, pl.game)


def _ping():
    return os.getpid()


def _sync(blob: bytes):
    ver, lite, (towns, applied) = pickle.loads(blob)
    if _W["ver"] == ver:
        return
    pl = _W["pl"]
    from .planner import Sim
    sim = Sim(pl.game, pl.vehicles, pl.nodes)
    for k, v in lite.items():
        setattr(sim, k, v)
    if towns is not None and pl.towns is not None and towns != _W["towns"]:
        pl.towns.__dict__.update(towns)
        if applied:
            pl.towns.apply_to_game(pl.game, town_objects=True)
        _W["towns"] = towns
    _W.update(ver=ver, sim=sim, pax_ctx=None, pax_inter={})


def _guard(fn):
    t = time.perf_counter()
    try:
        return {"ok": fn(), "s": time.perf_counter() - t}
    except Exception as e:                      # reported like a failing serial generator
        return {"error": f"{e}", "trace": traceback.format_exc(limit=8), "s": time.perf_counter() - t}


def _phase_a(blob: bytes, keys: dict) -> dict:
    t0 = time.perf_counter()
    _sync(blob)
    pl, sim = _W["pl"], _W["sim"]
    year = sim.year
    out = {}
    if "pax" in keys:
        from . import passengers as pax

        def run_pax():
            ctx = pax.make_context(pl.game, pl.vehicles, pl, year, set(sim.built_pairs), set(sim.stations),
                                   boost_value=lambda node, factor: pl.boost_value(sim, node, factor))
            _W["pax_ctx"] = ctx
            towns, pairs = keys["pax"]
            intra = [(t, pax.intra_town_candidates(ctx, towns=[t])) for t in towns]
            inter = [(pr, pax.intercity_candidates(ctx, pairs=[pr])) for pr in pairs]
            _W["pax_inter"] = dict(inter)
            return intra, inter
        out["pax"] = _guard(run_pax)
    n, k = _W["n"], _W["k"]
    if "mm" in keys:
        from . import multimodal as mm
        out["mm"] = _guard(lambda: mm.chain_parts(pl.game, pl.vehicles, pl, year, dict(sim.supply),
                                                  dict(sim.demand), set(sim.built_pairs), set(sim.stations),
                                                  own=lambda key: shard_of(("mm",) + key, n) == k)[0])
    if "tours" in keys:
        from . import tours as tr

        def run_tours():
            bk = set(sim.built_pairs) | set(sim.stations)
            sup, dem = dict(sim.supply), dict(sim.demand)
            return (tr.milk_run_parts(pl, year, sup, dem, bk, own=lambda key: shard_of(("milk",) + key, n) == k),
                    tr.circuit_parts(pl, year, sup, dem, bk, own=lambda key: shard_of(("circ",) + key, n) == k))
        out["tours"] = _guard(run_tours)
    out["_s"] = time.perf_counter() - t0
    return out


def _phase_b(intra_all, pairs, geoms) -> dict:
    t0 = time.perf_counter()
    pl = _W["pl"]
    out = {}
    if pairs:
        from . import passengers as pax

        def run_bundles():
            ctx = _W["pax_ctx"]
            return [(pr, pax.bundle_candidates(ctx, intra_all, _W["pax_inter"].get(pr, []))) for pr in pairs]
        out["pax"] = _guard(run_bundles)
    if geoms:
        from . import tours as tr
        out["geom"] = _guard(lambda: [(i, tr.line_geometry(pl, ev)) for i, ev in geoms])
    out["_s"] = time.perf_counter() - t0
    return out


def _leg_jobs(jobs: list) -> tuple[list, float]:
    from .upgrades import run_leg_job
    t = time.perf_counter()
    return [run_leg_job(j) for j in jobs], time.perf_counter() - t


# --------------------------------------------------------------------------- main side

class _Env:
    """Temporarily set environment variables (inherited by spawned workers)."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class GenPool:
    """N single-process executors with fixed shard affinity."""

    def __init__(self, pl, n: int, flags: dict):
        import multiprocessing as mp
        self.n = n
        self.flags = flags
        self.ver = 0
        self.stats: dict[str, float] = {}
        payload = planner_payload(pl, flags)
        ctx = mp.get_context("spawn")
        # one BLAS/OpenMP thread per worker: the workers already fill the cores, and every extra
        # OpenBLAS thread commits its own buffers (~60 MB each on Windows)
        with _Env(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                  NUMEXPR_NUM_THREADS="1", TF3P_WORKER="1"):
            self.ex = [ProcessPoolExecutor(1, mp_context=ctx, initializer=_init, initargs=(payload, n, k))
                       for k in range(n)]
            for e in self.ex:               # start all processes now (init runs while main works)
                e.submit(_ping)

    def close(self):
        """Stop the workers now (a stuck worker must never keep the planner from exiting)."""
        for e in self.ex:
            procs = list((getattr(e, "_processes", None) or {}).values())
            try:
                e.shutdown(wait=False, cancel_futures=True)
            finally:
                for pr in procs:
                    try:
                        if pr.is_alive():
                            pr.terminate()
                    except Exception:
                        pass

    # ---------------- keys
    def _keys(self, pl, sim) -> list[dict]:
        n = self.n
        keys = [dict() for _ in range(n)]
        if self.flags.get("passengers", True):
            from . import passengers as pax
            # pairs depend on town positions only (static)
            if not hasattr(self, "_pax_pairs"):
                ctx = SimpleNamespace(towns=pax.town_infos(pl.game, pax.PassengerParams()),
                                      p=pax.PassengerParams())
                self._pax_pairs = pax.intercity_pairs(ctx)
                self._pax_towns = [t.index for t in ctx.towns]
            for k in range(n):
                keys[k]["pax"] = ([t for t in self._pax_towns if shard_of(("t", t), n) == k],
                                  [p for p in self._pax_pairs if shard_of(("p", p), n) == k])
        for k in range(n):                  # multimodal / tours: the workers pick their own parts
            if self.flags.get("multimodal", True):
                keys[k]["mm"] = True
            if self.flags.get("tours", True):
                keys[k]["tours"] = True
        return keys

    # ---------------- January
    def dispatch(self, pl, sim) -> dict:
        """Start phase A for the current state; returns a handle for collect()."""
        self.ver += 1
        blob = state_blob(pl, sim, self.ver)
        keys = self._keys(pl, sim)
        futs = [self.ex[k].submit(_phase_a, blob, keys[k]) for k in range(self.n)]
        return {"month": sim.month, "futs": futs, "keys": keys, "t0": time.perf_counter()}

    def collect(self, pl, handle) -> dict:
        """{generator name: list of candidates | Exception} in serial order."""
        t0 = time.perf_counter()
        res = [f.result() for f in handle["futs"]]
        self._acc("phase_a_wait", time.perf_counter() - t0)
        self._acc("phase_a_max_worker", max(r.get("_s", 0.0) for r in res))
        self._acc("phase_a_sum_workers", sum(r.get("_s", 0.0) for r in res))
        if os.environ.get("TF3P_PARALLEL_DEBUG"):
            print("phase A " + " | ".join(" ".join(f"{k}:{v['s']:.1f}" for k, v in r.items() if isinstance(v, dict))
                                          + f" ={r.get('_s', 0):.1f}" for r in res), flush=True)
        out: dict = {}
        errors = {}
        for name, key in (("gen_passengers", "pax"), ("gen_multimodal", "mm"), ("gen_tours", "tours")):
            errs = [r[key]["error"] for r in res if key in r and "error" in r[key]]
            if errs:
                errors[key] = errs[0]
                out[name] = RuntimeError(errs[0])
        # ---- multimodal
        if "mm" in handle["keys"][0] and "mm" not in errors:
            from . import multimodal as mm
            parts = [p for r in res for p in r["mm"]["ok"]]
            out["gen_multimodal"] = mm.merge_parts(mm.select_chains(mm._context(pl, pl.game), pl.game, parts))
        # ---- tours: select in serial order, geometry of the selected lines in phase B
        sel_milk = sel_circ = None
        if "tours" in handle["keys"][0] and "tours" not in errors:
            from . import tours as tr
            milk = sorted((p for r in res for p in r["tours"]["ok"][0]), key=lambda x: x[0])
            circ = sorted((p for r in res for p in r["tours"]["ok"][1]), key=lambda x: x[0])
            sel_milk = tr.select(pl, [x for _c, lst in milk for x in lst], 60)
            sel_circ = tr.select(pl, [x for _k, lst in circ for x in lst], 60)
        # ---- passengers: merge intra / intercity, bundles in phase B
        intra = inter = None
        if "pax" in handle["keys"][0] and "pax" not in errors:
            intra = [c for _t, lst in sorted((p for r in res for p in r["pax"]["ok"][0]), key=lambda x: x[0])
                     for c in lst]
            inter_parts = sorted((p for r in res for p in r["pax"]["ok"][1]), key=lambda x: x[0])
            inter = [c for _p, lst in inter_parts for c in lst]
        # ---- phase B
        geoms = [[] for _ in range(self.n)]
        flat = (sel_milk or []) + (sel_circ or [])
        for i, (_c, ev) in enumerate(flat):      # stable worker per line: its geometry memo hits
            geoms[shard_of((ev.mode, ev.topology, tuple(ev.visits)), self.n)].append((i, ev))
        t1 = time.perf_counter()
        futs = []
        for k in range(self.n):
            pairs = handle["keys"][k]["pax"][1] if intra is not None else []
            if pairs or geoms[k]:
                futs.append(self.ex[k].submit(_phase_b, intra, pairs, geoms[k]))
        resb = [f.result() for f in futs]
        self._acc("phase_b_wait", time.perf_counter() - t1)
        errb = [r[key]["error"] for r in resb for key in ("pax", "geom") if key in r and "error" in r[key]]
        if sel_milk is not None or sel_circ is not None:
            gerr = [r["geom"]["error"] for r in resb if "geom" in r and "error" in r["geom"]]
            if gerr:
                out["gen_tours"] = RuntimeError(gerr[0])
            else:
                from . import tours as tr
                g = dict(x for r in resb if "geom" in r for x in r["geom"]["ok"])
                for i, (c, _ev) in enumerate(flat):
                    tr.set_geometry(c, g[i])
                out["gen_tours"] = [c for c, _ in sel_milk or []] + [c for c, _ in sel_circ or []]
        if intra is not None:
            perr = [r["pax"]["error"] for r in resb if "pax" in r and "error" in r["pax"]]
            if perr:
                out["gen_passengers"] = RuntimeError(perr[0])
            else:
                from . import passengers as pax
                bparts = sorted((p for r in resb if "pax" in r for p in r["pax"]["ok"]), key=lambda x: x[0])
                bundles = [c for _p, lst in bparts for c in lst]
                ctx = SimpleNamespace(p=pax.PassengerParams())
                allc = pax.finish(ctx, intra + inter + bundles)
                out["gen_passengers"] = [c for c in allc if c.kind != "workers"]
        del errb
        self._acc("collect_total", time.perf_counter() - t0)
        self._acc("januaries", 1)
        return out

    def leg_runs(self, jobs: list) -> list:
        """upgrades' dynamics simulations (pure jobs) spread over the workers, results in order.
        Jobs are dealt longest-route-first round robin so the chunks take similar time."""
        t0 = time.perf_counter()
        if not jobs:
            return []

        def size(j):
            prof = j[2]
            return float(prof) if isinstance(prof, (int, float)) else float(prof[-1][0]) if len(prof) else 0.0
        order = sorted(range(len(jobs)), key=lambda i: -size(jobs[i]))
        chunks = [[] for _ in range(self.n)]
        for r, i in enumerate(order):
            chunks[r % self.n].append(i)
        futs = [(c, self.ex[k].submit(_leg_jobs, [jobs[i] for i in c])) for k, c in enumerate(chunks) if c]
        out = [None] * len(jobs)
        cpu = 0.0
        for c, f in futs:
            res, secs = f.result()
            cpu += secs
            for i, r in zip(c, res):
                out[i] = r
        self.last_sims_cpu = cpu
        self._acc("upgrade_sims_cpu", cpu)
        self._acc("upgrade_sims", len(jobs))
        self._acc("upgrade_sims_wait", time.perf_counter() - t0)
        return out

    def _acc(self, k, v):
        self.stats[k] = self.stats.get(k, 0.0) + v


def flags_of(pl) -> dict:
    """Which offloadable generators the planner has registered (by closure name)."""
    names = {getattr(g, "__name__", "") for g in pl.generators}
    return {"passengers": "gen_passengers" in names, "multimodal": "gen_multimodal" in names,
            "tours": "gen_tours" in names}


def make_pool(pl, n: int | None = None) -> GenPool | None:
    """GenPool for a planner, or None (serial) when disabled / nothing to offload / start failed."""
    if os.environ.get("TF3P_WORKER"):
        return None                         # never nest pools inside a worker
    n = worker_count() if n is None else n
    flags = flags_of(pl)
    if n <= 0 or not any(flags.values()):
        return None
    try:
        return GenPool(pl, n, flags)
    except Exception as e:                  # pragma: no cover - fall back to the serial path
        print(f"parallel: workers not started ({e}); serial generators", flush=True)
        return None
