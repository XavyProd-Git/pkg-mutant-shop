#!/usr/bin/env python3
"""
DEPRECATED - the tile no longer needs this, and --apply has locked games before.
=================================================================================
The shipping tile is pms-tile.pkg, EMBEDDED IN THE SHOP ELF (tile_bundle.h) and installed by the
console's own installer on boot (tile_install() in server.c). Nothing edits app.db for it. If the
tile is missing, the safe route is one request to the running shop:

    GET http://<ps5>:8710/api/tile/install          (add ?force=1 to reinstall over a stale one)
    GET http://<ps5>:8710/api/tile/status           (is it registered?)

What this script does instead - rewriting app.db and appinfo.db by hand over FTP - is the action
the project's notes record as LOCKING THE USER'S GAMES and putting the console into "recover the
database". "SAFE BY DESIGN" below describes the dry-run and the backups; it does not make the
write safe. --apply therefore refuses unless --i-accept-app-db-rewrite is also given, and the
dry-run stays available as a way to LOOK at the rows.

Register the PKG MUTANT SHOP dashboard tile by CLONING Payload Manager's rows.
============================================================================
A PS5 dashboard tile is a "deeplink app": no eboot, just database rows whose
pprDeeplinkUri is a URL ShellCore opens when the tile is tapped. Payload Manager
(PLDM00001, Games section) is the perfect template. We clone its rows across both
system databases, substituting our title id / name / shop URL:

    app.db      tbl_contentinfo, tbl_conceptmetadata,
                tbl_iconinfo_<uid>, tbl_concepticoninfo_<uid>   (4 rows)
    appinfo.db  tbl_appinfo (72 k/v), tbl_conceptinfo (27 k/v)  (~99 rows)

SAFE BY DESIGN:
  * --dry-run (default) builds the modified DBs locally, runs PRAGMA integrity_check,
    prints exactly what would change, and uploads NOTHING.
  * --apply backs up BOTH databases on-console (app.db.mutantbak / appinfo.db.mutantbak)
    AND locally, then writes them back with an atomic rename (no torn reads).
  * --restore puts the on-console backups back.
  * --remove deletes only our PKGM00001 rows (clean uninstall).

The tile persists (both DBs are persistent) and appears at the next dashboard load
(i.e. next boot) - or immediately if a live ShellCore refresh is available.

Usage:
  python register_tile.py                 # dry-run (safe preview)
  python register_tile.py --apply
  python register_tile.py --restore
  python register_tile.py --remove
  python register_tile.py --apply --ip 10.0.0.99 --shop-url http://10.0.0.76:8710/
"""
import argparse, ftplib, io, json, os, shutil, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
MMS = "/system_data/priv/mms"
TEMPLATE_TID = "PLDM00001"          # Payload Manager - our template (Games section)
OUR_TID = "PKGM00001"
OUR_NAME = "PKG MUTANT SHOP"
TEMPLATE_NAME = "Payload Manager"
TEMPLATE_URL = "http://127.0.0.1:8084/"
# applicationCategoryType for a web/deeplink app. ShellCore reads the app's own
# sce_sys/param.json as the AUTHORITATIVE source and rebuilds app.db from it; without a
# declared deeplinkUri it treats the tile as a game (deeplink -> psgm:play) and crashes on
# launch. PM & CheatRunner both use 65536 + a declared deeplinkUri. That's the real fix.
CAT_DEEPLINK = 65536


def our_param_json(shop_url):
    """The authoritative app metadata ShellCore reads - MUST declare deeplinkUri (like PM's)."""
    return json.dumps({
        "titleId": OUR_TID,
        "applicationCategoryType": CAT_DEEPLINK,
        "deeplinkUri": shop_url,
        "localizedParameters": {
            "defaultLanguage": "en-US",
            "en-US": {"titleName": OUR_NAME},
        },
    }, indent=4)


def load_cfg():
    ip, shop = "10.0.0.99", None
    try:
        c = json.load(open(os.path.join(HERE, "..", "..", "companion", "config.json")))
        ip = c.get("ps5_ip", ip)
        comp = c.get("companion", {})
        host = comp.get("advertise_host") or comp.get("host")
        port = comp.get("port", 8710)
        if host and host not in ("0.0.0.0", "127.0.0.1", "localhost"):
            shop = "http://%s:%d/" % (host, port)
    except Exception:
        pass
    return ip, shop


def connect(ip):
    ftp = ftplib.FTP()
    ftp.connect(ip, 2121, timeout=20)
    ftp.login()
    try:
        ftp.sendcmd("MTRW")          # make the system partition writable (harmless if already)
    except Exception:
        pass
    return ftp


def retr(ftp, remote, local):
    with open(local, "wb") as f:
        ftp.retrbinary("RETR " + remote, f.write)


def exists(ftp, path):
    try:
        ftp.size(path)
        return True
    except Exception:
        try:
            ftp.sendcmd("MLST " + path)
            return True
        except Exception:
            return False


def atomic_put(ftp, local, remote):
    """Upload then atomically rename over the target (POSIX rename = no torn reads)."""
    tmp = remote + ".new"
    with open(local, "rb") as f:
        ftp.storbinary("STOR " + tmp, f)
    try:
        ftp.rename(tmp, remote)                     # RNFR/RNTO - atomic replace
    except Exception:
        # fall back: unlink + rename (tiny window)
        try:
            ftp.delete(remote)
        except Exception:
            pass
        ftp.rename(tmp, remote)


def user_tables(con):
    names = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    icon = next((n for n in names if n.startswith("tbl_iconinfo_")), None)
    cicon = next((n for n in names if n.startswith("tbl_concepticoninfo_")), None)
    return icon, cicon


def sub(v, shop_url):
    if not isinstance(v, str):
        return v
    v = v.replace(TEMPLATE_TID, OUR_TID)
    v = v.replace(TEMPLATE_NAME, OUR_NAME)
    v = v.replace(TEMPLATE_URL, shop_url)
    return v


TS_COLS = {"lastAccessTime", "installTime", "promoteTime", "mTime", "lastInteractedTime",
           "install_time", "last_access_date", "promote_time",
           "recentActivityDatePlayedOrInstalled", "installedDate"}


def _imax(con, sql):
    try:
        r = con.execute(sql).fetchone()[0]
        return int(r) if r is not None else 0
    except Exception:
        return 0


def _ver(con, cat):
    """Read a global counter from tbl_version (returns None if absent/non-numeric)."""
    try:
        r = con.execute("select status from tbl_version where category=?", (cat,)).fetchone()
        if r is None:
            return None
        return int(r[0])
    except Exception:
        return None


def compute_counters(app, appinfo):
    """ShellCore keeps global 'next index' counters in tbl_version (appinfo_sync_index,
    conceptinfo_sync_index, access_index). A row whose sync_index EXCEEDS the counter is
    impossible in ShellCore's model, so it flags the whole database corrupt on the next sync
    (which any tile tap triggers) - THAT is what crashed tests #1-#3. The correct move is to
    assign our tile the CURRENT counter value and bump the counter, exactly like ShellCore
    registering a real app. We stay <= counter, never ahead of it."""
    def pick(cat, *cons):
        for c in cons:
            v = _ver(c, cat)
            if v is not None:
                return v
        return 0
    return {
        "appinfo_sync": pick("appinfo_sync_index", appinfo, app),
        "concept_sync": pick("conceptinfo_sync_index", appinfo, app),
        "access": pick("access_index", appinfo, app),
    }


def patch_flat_json(s, shop_url, sync, access):
    """AppInfoJson (flat dict): rewrite strings (title/paths/uri) + set counter fields."""
    try:
        d = json.loads(s)
    except Exception:
        return sub(s, shop_url)
    for k in list(d.keys()):
        if isinstance(d[k], str):
            d[k] = sub(d[k], shop_url)
        lk = k.lower()
        if "sync_index" in lk:
            d[k] = sync                       # <= appinfo_sync_index counter (safe)
        elif k == "CATEGORY_TYPE":
            d[k] = CAT_DEEPLINK
        # NB: leave #_access_index as PM's real value - the app.db access scale differs from
        # appinfo.db's, and overshooting it would re-trigger the corruption we just fixed.
    return json.dumps(d, separators=(",", ":"))


def patch_concept_json(s, shop_url, sync, access):
    """ConceptInfoJson (typed field_list): rewrite strings keeping 'size' correct + counters."""
    try:
        d = json.loads(s)
    except Exception:
        return sub(s, shop_url)
    for f in d.get("field_list", []):
        key = str(f.get("key", "")).lower()
        if isinstance(f.get("data"), str):
            nd = sub(f["data"], shop_url)
            f["data"] = nd
            if isinstance(f.get("size"), int):
                f["size"] = len(nd)
        if "sync_index" in key:
            f["data"] = sync
        # leave last_access_index as PM's real value (see note in patch_flat_json)
    return json.dumps(d, separators=(",", ":"))


def clone_row(con, table, where_sql, args, shop_url, now, C, col_overrides=None):
    """Read the template row(s), rewrite values (unique counters + our strings), INSERT."""
    cur = con.execute("select * from %s where %s" % (table, where_sql), args)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    out = 0
    # sync counter is per-domain: concept tables use conceptinfo_sync_index, the rest appinfo_sync_index
    sync = C["concept_sync"] if table in ("tbl_conceptinfo", "tbl_conceptmetadata") else C["appinfo_sync"]
    access = C["access"]
    for row in rows:
        vals = []
        for c, v in zip(cols, row):
            if c == "AppInfoJson" and isinstance(v, str):
                nv = patch_flat_json(v, shop_url, sync, access)
            elif c == "ConceptInfoJson" and isinstance(v, str):
                nv = patch_concept_json(v, shop_url, sync, access)
            else:
                nv = sub(v, shop_url)
                if c in TS_COLS and isinstance(v, str) and len(v) > 10:
                    nv = now
                if col_overrides and c in col_overrides:
                    nv = col_overrides[c]
            vals.append(nv)
        # key-value tables (appinfo.db): rewrite counter fields + timestamps in the 'val' column
        if table in ("tbl_appinfo", "tbl_conceptinfo"):
            d = dict(zip(cols, vals))
            k = str(d.get("key", ""))
            lk = k.lower()
            if "sync_index" in lk:
                d["val"] = sync
            elif "_access_index" in lk or lk == "last_access_index":
                d["val"] = access
            elif k == "CATEGORY_TYPE":
                d["val"] = CAT_DEEPLINK
            elif any(t in k for t in ("_time", "access_date", "install_time", "promote")):
                if isinstance(d.get("val"), str) and len(str(d["val"])) > 10 and "-" in str(d["val"]):
                    d["val"] = now
            vals = [d[c] for c in cols]
        con.execute("insert or replace into %s (%s) values (%s)" %
                    (table, ",".join(cols), ",".join("?" * len(cols))), vals)
        out += 1
    return out


def remove_rows(con, shop_url):
    icon, cicon = user_tables(con)
    n = 0
    plan = [("tbl_contentinfo", "titleId=?", (OUR_TID,)),
            ("tbl_conceptmetadata", "localConceptId=?", ("cid:local:" + OUR_TID,))]
    if icon:
        plan.append((icon, "titleId=?", (OUR_TID,)))
    if cicon:
        plan.append((cicon, "localConceptId=?", ("cid:local:" + OUR_TID,)))
    for t, w, a in plan:
        try:
            n += con.execute("delete from %s where %s" % (t, w), a).rowcount
        except Exception:
            pass
    return n


def bump_version(con, C):
    """Advance ShellCore's global counters past the values our tile consumed, so the counter
    stays > every row's index (exactly what ShellCore does after registering an app)."""
    for cat, base in (("appinfo_sync_index", C["appinfo_sync"]),
                      ("conceptinfo_sync_index", C["concept_sync"]),
                      ("access_index", C["access"])):
        try:
            con.execute("update tbl_version set status=? where category=? "
                        "and cast(status as integer) <= ?", (base + 1, cat, base))
        except Exception:
            pass


def build_appdb(path, shop_url, now, C, remove=False):
    con = sqlite3.connect(path)
    icon, cicon = user_tables(con)
    if remove:
        n = remove_rows(con, shop_url)
        con.commit()
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        return n, ok
    n = 0
    # The row is cloned from Payload Manager, whose metadata lives in /user/app/PLDM00001/sce_sys
    # with contentLocation=2. Inheriting that pointed our row at a path nothing ever wrote, so the
    # tile never rendered and the console began asking to recover the database. Set these
    # explicitly to the shape the two WORKING homebrew tiles use (Elf Arsenal, Homebrew Launcher):
    # metadata in /user/appmeta/<TID>, contentLocation 0.
    _meta = "/user/appmeta/%s" % OUR_TID
    n += clone_row(con, "tbl_contentinfo", "titleId=?", (TEMPLATE_TID,), shop_url, now, C,
                   col_overrides={"categoryType": CAT_DEEPLINK,
                                  "metaDataPath": _meta,
                                  "icon0Info": "%s/icon0.png?ts=%d" % (_meta, int(time.time())),
                                  "contentLocation": 0})
    n += clone_row(con, "tbl_conceptmetadata", "localConceptId=?",
                   ("cid:local:" + TEMPLATE_TID,), shop_url, now, C)
    if icon:
        n += clone_row(con, icon, "titleId=?", (TEMPLATE_TID,), shop_url, now, C)
    if cicon:
        n += clone_row(con, cicon, "localConceptId=?", ("cid:local:" + TEMPLATE_TID,), shop_url, now, C)
    bump_version(con, C)
    con.commit()
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return n, ok


def build_appinfo(path, shop_url, now, C, remove=False):
    con = sqlite3.connect(path)
    if remove:
        n = 0
        for t, w, a in (("tbl_appinfo", "titleId=?", (OUR_TID,)),
                        ("tbl_conceptinfo", "local_concept_id=?", ("cid:local:" + OUR_TID,))):
            try:
                n += con.execute("delete from %s where %s" % (t, w), a).rowcount
            except Exception:
                pass
        con.commit()
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        return n, ok
    n = 0
    n += clone_row(con, "tbl_appinfo", "titleId=?", (TEMPLATE_TID,), shop_url, now, C)
    n += clone_row(con, "tbl_conceptinfo", "local_concept_id=?",
                   ("cid:local:" + TEMPLATE_TID,), shop_url, now, C)
    bump_version(con, C)
    con.commit()
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return n, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--ip", default=None)
    ap.add_argument("--shop-url", default=None)
    ap.add_argument("--i-accept-app-db-rewrite", action="store_true",
                    help="required with --apply: you accept that hand-editing app.db can lock "
                         "your games and force a database recovery")
    a = ap.parse_args()

    cfg_ip, cfg_shop = load_cfg()
    ip = a.ip or cfg_ip

    # The safer route first, every time - it is the one that ships. The database rewrite below
    # stays reachable for research, behind a flag that says what it can cost.
    print("The tile is embedded in the shop ELF and installs itself. To put it back safely:")
    print("    GET http://%s:8710/api/tile/install     (force=1 to reinstall over a stale one)" % ip)
    print("    GET http://%s:8710/api/tile/status" % ip)
    print("")
    if a.apply and not a.i_accept_app_db_rewrite:
        print("REFUSED: --apply rewrites app.db and appinfo.db by hand. On this console that has")
        print("locked every installed game and forced a 'recover the database' pass. Use the")
        print("/api/tile/install route above. If you really want the rewrite, add")
        print("--i-accept-app-db-rewrite. Nothing was changed.")
        sys.exit(2)
    shop_url = a.shop_url or cfg_shop or "http://10.0.0.76:8710/"
    work = os.path.join(HERE, "_appdb_work")
    os.makedirs(work, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S.000")
    ftp = connect(ip)

    if a.restore:
        for db in ("app.db", "appinfo.db"):
            bak = "%s/%s.mutantbak" % (MMS, db)
            if exists(ftp, bak):
                tmp = os.path.join(work, db + ".restore")
                retr(ftp, bak, tmp)
                atomic_put(ftp, tmp, "%s/%s" % (MMS, db))
                print("restored %s from %s" % (db, bak))
            else:
                print("no backup found:", bak)
        ftp.quit()
        print("\nRestore done. Tile changes reverted at next dashboard load.")
        return

    # pull current DBs, snapshotting the pristine originals BEFORE we modify them
    la = os.path.join(work, "app.db")
    li = os.path.join(work, "appinfo.db")
    retr(ftp, MMS + "/app.db", la)
    retr(ftp, MMS + "/appinfo.db", li)
    shutil.copy(la, la + ".orig")
    shutil.copy(li, li + ".orig")
    print("shop URL for tile deeplink: %s" % shop_url)

    # read ShellCore's global counters from tbl_version (shared by both DBs). Our tile gets the
    # current counter value (never above it) and we advance the counter - like a real install.
    _ac = sqlite3.connect(la)
    _ai = sqlite3.connect(li)
    C = compute_counters(_ac, _ai)
    _ac.close()
    _ai.close()
    if not a.remove:
        print("ShellCore counters -> appinfo_sync=%d  concept_sync=%d  access=%d  (tile uses these, then +1)"
              % (C["appinfo_sync"], C["concept_sync"], C["access"]))

    na, oka = build_appdb(la, shop_url, now, C, remove=a.remove)
    ni, oki = build_appinfo(li, shop_url, now, C, remove=a.remove)
    verb = "removed" if a.remove else "cloned"
    print("app.db:     %s %d row(s)   integrity=%s" % (verb, na, oka))
    print("appinfo.db: %s %d row(s)   integrity=%s" % (verb, ni, oki))
    if oka != "ok" or oki != "ok":
        print("!! integrity check FAILED - not uploading. Nothing changed on console.")
        ftp.quit()
        sys.exit(1)

    if not (a.apply or a.remove):
        print("\n[dry-run] Built + integrity-checked locally. Uploaded nothing.")
        print("          Files: %s , %s" % (la, li))
        print("          Re-run with --apply to write the tile (backs up first).")
        ftp.quit()
        return

    # Metadata goes in /user/appmeta/<TID>, NOT /user/app/<TID>/sce_sys.
    #
    # This is the whole reason the tile stopped appearing. Both homebrew tiles that DO work on
    # this console (Elf Arsenal PSPS69691, Homebrew Launcher FAKE00000) keep their param.json and
    # icon0.png in /user/appmeta/<TID>, and their app.db row's metaDataPath points there. Writing
    # to /user/app/<TID>/sce_sys left the database row pointing at a path ShellCore does not read,
    # so the tile never rendered - and a registered title whose metadata cannot be found is
    # exactly what makes the console start asking to "recover the database".
    if not a.remove:
        meta = "/user/appmeta/%s" % OUR_TID
        try:
            ftp.mkd(meta)
        except Exception:
            pass
        icon_remote = meta + "/icon0.png"
        if not exists(ftp, icon_remote):
            src = os.path.join(HERE, "PKGM00001", "icon0.png")
            if not os.path.isfile(src):
                src = os.path.join(HERE, "..", "..", "web", "assets", "icon0.png")
            with open(src, "rb") as f:
                ftp.storbinary("STOR " + icon_remote, f)
            print("uploaded tile icon -> %s" % icon_remote)
        pj = our_param_json(shop_url).encode("utf-8")
        ftp.storbinary("STOR " + meta + "/param.json", io.BytesIO(pj))
        print("wrote deeplink param.json -> %s/param.json  (deeplinkUri=%s)" % (meta, shop_url))

    # back up BOTH dbs on-console (pristine originals). Keep the FIRST backup only
    # (so re-runs never overwrite the known-good pre-tile snapshot).
    for db, local in (("app.db", la), ("appinfo.db", li)):
        bak = "%s/%s.mutantbak" % (MMS, db)
        if exists(ftp, bak):
            print("backup already exists (keeping pristine): %s" % bak)
            continue
        with open(local + ".orig", "rb") as f:
            ftp.storbinary("STOR " + bak, f)
        print("backup -> %s" % bak)

    # write the modified DBs atomically
    atomic_put(ftp, la, MMS + "/app.db")
    atomic_put(ftp, li, MMS + "/appinfo.db")
    print("wrote app.db + appinfo.db (atomic).")

    # verify our rows are present on-console
    vpath = os.path.join(work, "app.db.verify")
    retr(ftp, MMS + "/app.db", vpath)
    vcon = sqlite3.connect(vpath)
    cnt = vcon.execute("select count(*) from tbl_contentinfo where titleId=?", (OUR_TID,)).fetchone()[0]
    vcon.close()
    ftp.quit()
    if a.remove:
        print("\nRemoved. The PKG MUTANT SHOP tile is gone (contentinfo rows for %s: %d)." % (OUR_TID, cnt))
    else:
        print("\nVERIFIED on-console: tbl_contentinfo rows for %s = %d" % (OUR_TID, cnt))
        print("The tile is registered. It appears on the dashboard at the next boot")
        print("(both DBs are persistent). Deeplink opens: %s" % shop_url)
        print("If anything looks wrong:  python register_tile.py --restore")


if __name__ == "__main__":
    main()
