# -*- coding: utf-8 -*-
"""Build the two console databases the stand-in serves.

THE SCHEMA IS THE REAL ONE, taken from a PS4 on 13.52; the CONTENT is invented. A real console's
app.db carries the owner's library, user ids, entitlement ids and purchase dates, and none of that
belongs in a repository - so the fixtures are generated here instead of being copied off a console.

What is reproduced faithfully, because it is what the code under test depends on:
  * per-user `tbl_appbrowse_<userid>` tables - TWO of them, as a console with two users really has
  * the columns the payload reads: titleId, contentId, titleName, category, contentSize
  * category 'gd' for a real game and 'gdi' for a system stub, which must be filtered out
  * the key/value `tbl_appinfo` holding APP_VER
  * `addcont` with a content_id column, which is what the companion's DLC proof reads
"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))

BROWSE_COLS = ("titleId TEXT, contentId TEXT, titleName TEXT, metaDataPath TEXT, "
               "lastAccessTime TEXT, contentStatus INTEGER, onDisc INTEGER, parentalLevel INTEGER, "
               "visible INTEGER, sortPriority INTEGER, pathInfo INTEGER, lastAccessIndex INTEGER, "
               "dispLocation TEXT, canRemove INTEGER, category TEXT, contentType INTEGER, "
               "contentSize INTEGER, installDate TEXT, platform TEXT, uiCategory TEXT")

GAMES = [
    ("CUSA02365", "EP0786-CUSA02365_00-RIPTIDEGP2PS4001", "Riptide GP2", "gd", 107806720, "01.00"),
    ("CUSA11740", "EP0576-CUSA11740_00-METALSLUGXX00001", "METAL SLUG XX", "gd", 532217856, "01.02"),
    ("CUSA00050", "EP1018-CUSA00050_00-DYINGLIGHT000000", "Dying Light", "gd", 31351111680, "01.09"),
    ("NPXS20001", "", "System Stub", "gdi", 0, ""),
]
ADDONS = [
    (1, "CUSA00050", "BETHEZOMBIE00000", "EP1018-CUSA00050_00-BETHEZOMBIE00000", "Be the Zombie"),
    (2, "CUSA00050", "THEFOLLOWING0000", "EP1018-CUSA00050_00-THEFOLLOWING0000", "The Following"),
]


def build():
    app = os.path.join(HERE, "app.db")
    add = os.path.join(HERE, "addcont.db")
    for p in (app, add):
        if os.path.exists(p):
            os.remove(p)

    con = sqlite3.connect(app)
    for uid in ("0421682697", "0421682698"):      # two users, as a real console has
        t = "tbl_appbrowse_%s" % uid
        con.execute("CREATE TABLE %s (%s)" % (t, BROWSE_COLS))
        for tid, cid, name, cat, size, _v in GAMES:
            con.execute("INSERT INTO %s (titleId,contentId,titleName,category,contentSize,visible,"
                        "dispLocation,canRemove) VALUES (?,?,?,?,?,1,'0',1)" % t,
                        (tid, cid, name, cat, size))
    con.execute("CREATE TABLE tbl_appinfo (titleId TEXT, key TEXT, val TEXT)")
    for tid, _c, _n, _cat, _s, ver in GAMES:
        if ver:
            con.execute("INSERT INTO tbl_appinfo VALUES (?,?,?)", (tid, "APP_VER", ver))
    con.commit()
    con.close()

    con = sqlite3.connect(add)
    con.execute("CREATE TABLE addcont (id INTEGER PRIMARY KEY, title_id TEXT, dir_name TEXT, "
                "content_id TEXT, title TEXT, version TEXT, attribute INTEGER, status INTEGER)")
    for row in ADDONS:
        con.execute("INSERT INTO addcont (id,title_id,dir_name,content_id,title,version,attribute,"
                    "status) VALUES (?,?,?,?,?, '01.00', 0, 0)", row)
    con.commit()
    con.close()
    print("wrote %s and %s" % (os.path.basename(app), os.path.basename(add)))


if __name__ == "__main__":
    build()
