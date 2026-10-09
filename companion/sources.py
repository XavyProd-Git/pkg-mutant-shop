#!/usr/bin/env python3
"""
Source engine - intelligent, multi-mirror selection.
=====================================================
Given several places a PKG could live (local disk / LAN box / another PC / a tunneled home box /
your own cloud bucket), probe them, rank by reachability + latency, resolve the best URL for a file,
and fail over when one dies.  Stdlib only.

A Source = {
    "name": "home-box",
    "type": "lan" | "http" | "tunnel" | "cloud" | "local",
    "base_url": "http://host:8710/library/",   # files are hosted by basename under here
    "priority": 10,                            # lower = preferred on ties
    "auth": {"header": "X-Token", "value": "..."}   # optional, for remote sources
}
"""
import os
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import quote


def _file_url(source, filename):
    base = source.get("base_url", "")
    if not base:
        return None
    return base.rstrip("/") + "/" + quote(filename)


def is_companion(source):
    """A source that runs the PKG MUTANT SHOP companion (so it exposes /api/served for live progress)."""
    return source.get("name") == "companion-lan" or bool(source.get("companion"))


def served_url(source, key):
    """Derive a companion source's /api/served/<key> endpoint from its /library/ base_url."""
    base = source.get("base_url", "")
    if not base:
        return None
    root = base.rstrip("/")
    if root.endswith("/library"):
        root = root[:-len("/library")]
    return root + "/api/served/" + key


def _request(url, source, timeout):
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    auth = source.get("auth") or {}
    if auth.get("header"):
        req.add_header(auth["header"], auth.get("value", ""))
    return req


def probe_source(source, filename=None, timeout=2.5):
    """Health/latency of a source. If filename given, probe that exact file (confirms it's present)."""
    name, typ = source.get("name", "?"), source.get("type", "http")
    if typ == "local":
        base = source.get("base_url", "")
        ok = (not base) or os.path.isdir(base) or os.path.isfile(base)
        return {"name": name, "type": typ, "ok": ok, "latency_ms": 0,
                "has_file": None, "error": None if ok else "path missing"}

    url = _file_url(source, filename) if filename else source.get("base_url", "")
    if not url:
        return {"name": name, "type": typ, "ok": False, "latency_ms": None, "has_file": None, "error": "no base_url"}

    start = time.time()
    try:
        with urllib.request.urlopen(_request(url, source, timeout), timeout=timeout) as r:
            r.read(1)
            code = r.status
        lat = round((time.time() - start) * 1000, 1)
        return {"name": name, "type": typ, "ok": True, "latency_ms": lat,
                "has_file": True if filename else None, "error": None}
    except urllib.error.HTTPError as e:
        lat = round((time.time() - start) * 1000, 1)
        # host is up. 404/416 on a specific file means "not here"; on the base path it's just no listing.
        present = None if filename is None else (e.code not in (404, 410))
        return {"name": name, "type": typ, "ok": True, "latency_ms": lat,
                "has_file": present, "error": "http %d" % e.code}
    except Exception as e:
        return {"name": name, "type": typ, "ok": False, "latency_ms": None,
                "has_file": False if filename else None, "error": type(e).__name__}


def _rank_key(p, sources):
    pri = next((s.get("priority", 100) for s in sources if s.get("name") == p["name"]), 100)
    type_rank = 0 if p["type"] == "local" else 1
    lat = p["latency_ms"] if (p["ok"] and p["latency_ms"] is not None) else 9e9
    return (0 if p["ok"] else 1, type_rank, lat, pri)


class SourceEngine:
    def __init__(self, sources=None, cache_ttl=30):
        self.sources = list(sources or [])
        self.ttl = cache_ttl
        self._cache = {}          # (name, filename) -> (probe, ts)
        self._lock = threading.Lock()

    def set_sources(self, sources):
        self.sources = list(sources or [])

    def _cached(self, key):
        with self._lock:
            v = self._cache.get(key)
            if v and time.time() - v[1] < self.ttl:
                return v[0]
        return None

    # A BOUND, because this is keyed by (source, FILENAME) and nothing ever removed an entry.
    # One per source per package probed, kept for the life of the process: on a library of a few
    # hundred titles across several sources it is a slow leak of dictionary entries that are
    # already stale - the TTL is 30 seconds and they were being held for hours.
    _CACHE_MAX = 4096

    def _store(self, key, probe):
        now = time.time()
        with self._lock:
            self._cache[key] = (probe, now)
            if len(self._cache) <= self._CACHE_MAX:
                return
            # Drop what the TTL has already expired; that is almost always everything that needs to
            # go, and it costs one pass over a dictionary that only gets this big by leaking.
            for k in [k for k, v in self._cache.items() if now - v[1] >= self.ttl]:
                self._cache.pop(k, None)
            # Still over? Then the entries are genuinely live and the cap is the backstop: drop the
            # oldest until it fits, so a pathological library cannot grow this without limit.
            if len(self._cache) > self._CACHE_MAX:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][1])[
                        :len(self._cache) - self._CACHE_MAX]:
                    self._cache.pop(k, None)

    def probe_all(self, filename=None, force=False):
        """Probe every source concurrently (cached). Returns list of probe dicts."""
        out = {}

        def work(s):
            key = (s.get("name"), filename)
            cached = None if force else self._cached(key)
            probe = cached or probe_source(s, filename)
            self._store(key, probe)
            out[s.get("name")] = probe

        threads = [threading.Thread(target=work, args=(s,)) for s in self.sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=4)
        return [out[s["name"]] for s in self.sources if s.get("name") in out]

    def rank(self, filename=None, force=False):
        probes = self.probe_all(filename, force)
        return sorted(probes, key=lambda p: _rank_key(p, self.sources))

    def best(self, filename=None, force=False):
        for p in self.rank(filename, force):
            if p["ok"] and (p.get("has_file") in (True, None)):
                return p
        return None

    def resolve(self, filename, force=False):
        """Return (url, source_name, ranking) for the best mirror that has `filename`, else (None, None, ranking)."""
        ranking = self.rank(filename, force)
        for p in ranking:
            if not p["ok"] or p.get("has_file") is False:
                continue
            src = next((s for s in self.sources if s.get("name") == p["name"]), None)
            if src and src.get("type") != "local":
                url = _file_url(src, filename)
                if url:
                    return url, p["name"], ranking
        return None, None, ranking


if __name__ == "__main__":
    import json
    demo = [
        {"name": "companion-lan", "type": "lan", "base_url": "http://127.0.0.1:8710/library/", "priority": 10},
        {"name": "dead-cloud", "type": "cloud", "base_url": "http://10.255.255.1:9/library/", "priority": 50},
    ]
    eng = SourceEngine(demo)
    print("rank (host health):")
    print(json.dumps(eng.rank(force=True), indent=2))
    print("\nbest:", (eng.best() or {}).get("name"))
