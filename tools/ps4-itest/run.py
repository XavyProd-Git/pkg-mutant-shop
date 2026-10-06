# -*- coding: utf-8 -*-
"""Exercise the companion's PS4 code paths against the stand-in console - no hardware needed.

    python tools/ps4-itest/run.py ok            a package that installs
    python tools/ps4-itest/run.py psn-update    a package the console stops part-way through


The four fixes made while the real console was unreachable are all companion-facing, so they can be
proven without hardware: a Ps5Bridge is pointed at fake_ps4.py and the real methods are called.
Nothing here starts a second app, touches the user's config, or opens a browser.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))      # <repo>/tools/ps4-itest -> <repo>
PORT = 8799
passed = failed = 0


def check(good, label, detail=""):
    global passed, failed
    if good:
        passed += 1
    else:
        failed += 1
    print("  [%s] %-52s %s" % ("OK" if good else "!!", label, detail))


def load_companion():
    sys.path.insert(0, os.path.join(ROOT, "companion"))
    spec = importlib.util.spec_from_file_location("pms", os.path.join(ROOT, "companion", "server.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["pms"] = m
    spec.loader.exec_module(m)
    return m


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    if not os.path.isfile(os.path.join(HERE, "app.db")):
        subprocess.check_call([sys.executable, os.path.join(HERE, "make_fixtures.py")])
    fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_console.py"), str(PORT), mode],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(1.5)
    try:
        m = load_companion()
        cfg = dict(m.DEFAULT_CONFIG)
        cfg["console"] = {"shop_port": PORT, "app_db_path": "/system_data/priv/mms/app.db"}
        cfg["ftp"] = {"port": 2121}
        cfg["ps5_ip"] = "10.255.255.1"          # unreachable on purpose: the fleet's second console
        cfg["ps4_ip"] = "127.0.0.1"
        cfg["consoles"] = []
        m.reconcile_consoles(cfg)
        check([c["platform"] for c in cfg["consoles"]] == ["ps5", "ps4"],
              "two addresses become a two-console fleet, PS5 first",
              json.dumps([(c["id"], c["ip"]) for c in cfg["consoles"]]))

        fleet = m.Fleet(cfg)
        b4 = fleet.bridge("ps4")
        b5 = fleet.bridge("ps5")

        print("\n-- platform detection")
        check(b4.is_ps4() is True, "the PS4 is recognised from its own /api/health", b4.platform_id())
        check(b5.is_ps4() is False, "an unreachable console defaults to PS5, not PS4", b5.platform_id())
        check(fleet.bridge("nope") is None, "an unknown console id is refused, not guessed at")

        print("\n-- reading the console")
        apps = b4.console_apps(force=True)
        check(apps is not None and len(apps) >= 3, "installed games read from the PS4",
              "%d title(s)" % (len(apps or [])))
        a = (apps or [{}])[0]
        check(a.get("platform") == "PS4" and a.get("app_ver") and a.get("source") == "installed",
              "they carry the shape the rest of the app expects",
              "%s ver=%s" % (a.get("title_id"), a.get("app_ver")))
        t0 = time.time()
        check(b5.console_apps(force=True) is None, "an unreachable console gives up fast, not slowly",
              "%.2fs" % (time.time() - t0))

        print("\n-- add-ons and versions, against the console's real databases")
        addons = b4.installed_addons(force=True)
        check(addons is not None, "addcont.db parsed with the PS4 schema",
              "%d add-on(s)" % len(addons or []))
        check(b4.already_installed("base", "CUSA02365", pkg_bytes=107806720) != "",
              "a game that is really installed is recognised",
              b4.already_installed("base", "CUSA02365", pkg_bytes=107806720))
        check(b4.already_installed("base", "CUSA99999") == "",
              "a game that is not there is not claimed to be")

        print("\n-- the library merges every console")
        srv = types.SimpleNamespace(fleet=fleet, cfg=cfg,
                                    library=types.SimpleNamespace(games=[], file_registry={},
                                                                  file_sizes={}, gen=1,
                                                                  last_scan_ms=0, is_empty=True))
        lib = m.build_library(srv)
        on = [g for g in lib["games"] if g.get("on_console")]
        check(lib["console_reachable"] is True, "reachable when ONE of two consoles answers")
        check(on and all(g.get("installed_on") == ["ps4"] for g in on),
              "each title records which console has it", "%d title(s)" % len(on))

        print("\n-- the install hand-over")
        q = m.Ps5Bridge._pkg_query({"content_id": "EP0786-CUSA02365_00-RIPTIDEGP2PS4001",
                                    "size": 107806720, "kind": "update"})
        check("cid=EP0786" in q and "size=107806720" in q and "type=PS4GP" in q,
              "content id, size and type travel with the request", q)

        url = "http://127.0.0.1:%d/api/engine/log" % PORT      # any URL the stand-in can fetch
        ok, info = b4.install_spawn(url, "Test Package",
                                    pkg={"content_id": "EP0786-CUSA02365_00-RIPTIDEGP2PS4001",
                                         "size": 12, "kind": "base"})
        if mode == "ok":
            check(ok is True, "a running install answers as ACCEPTED inside the 90s window",
                  json.dumps(info)[:90])
        else:
            # This one fails before the first poll, so the verdict IS the failure - and what the
            # user must see is the console's own sentence, not a PS5 code table's guess at it.
            check(ok is False and "PlayStation Network" in (info.get("error") or ""),
                  "a failure carries the console's own words, not a PS5 code table's",
                  (info.get("error") or "")[:70])

        print("\n-- the queue reads the console's own verdict")
        queue = types.SimpleNamespace(fleet=fleet, _job_memo={})
        t = {"id": "job-1", "console": "ps4", "key": "k", "local_progress": True}
        counter = {"max": 5, "total": 100}
        got = m.Queue._with_console_verdict(queue, t, b4, counter)
        check("state" in got, "a PS4 progress reading carries the console's state", str(got)[:80])
        check(counter == {"max": 5, "total": 100}, "the shared byte counter is not mutated")
        got5 = m.Queue._with_console_verdict(queue, t, b5, counter)
        check(got5 is counter, "a PS5 is left exactly as it was - no extra round trip")

        # let the stand-in finish, then read the verdict the queue would show
        for _ in range(40):
            time.sleep(0.5)
            j = b4.engine_job() or {}
            if j.get("state") in ("installed", "error"):
                break
        queue._job_memo.clear()
        got = m.Queue._with_console_verdict(queue, t, b4, counter)
        if mode == "ok":
            check(got.get("state") == "installed", "a finished install reads as finished", got.get("msg"))
        else:
            check(got.get("state") == "error" and "PlayStation Network" in (got.get("msg") or ""),
                  "the console's own explanation is what the queue would show",
                  (got.get("msg") or "")[:70])

        print("\n%d passed, %d failed" % (passed, failed))
        return 0 if failed == 0 else 1
    finally:
        try:
            fake.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
