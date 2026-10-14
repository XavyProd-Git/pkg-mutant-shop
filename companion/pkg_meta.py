#!/usr/bin/env python3
"""
Real PS4 PKG / param.sfo reader.  No external deps.

parse_pkg(path) -> dict with title_id, content_id, title, kind, app_ver, version,
region, size  (or None if the file can't be read as a PKG).
Optionally extracts icon0.png (real box art) to a cache path.

Verified against retail fpkgs (e.g. DARK SOULS: REMASTERED CUSA08692).
PKG header is big-endian; param.sfo is little-endian. param.sfo (entry id 0x1000)
and icon0.png (entry id 0x1200) are stored plaintext in scene fpkgs.
"""
import os
import struct

PKG_MAGIC = 0x7F434E54          # \x7FCNT  - PS4 fPKG
PKG_MAGIC_PS5 = 0x7F464948      # \x7FFIH  - PS5 package: a DIFFERENT
                                #            container, not a broken PS4 one
SFO_MAGIC = b"\x00PSF"
ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0 = 0x1200
PNG_MAGIC = b"\x89PNG"

REGION_BY_PREFIX = {"U": "US", "E": "EU", "J": "JP", "H": "ASIA", "K": "KR", "I": "INT"}


def _find_entries(f, wanted):
    """Return {entry_id: (offset, size)} for the wanted entry ids."""
    f.seek(0)
    head = f.read(0x30)
    if len(head) < 0x30 or struct.unpack(">I", head[:4])[0] != PKG_MAGIC:
        return None
    entry_count = struct.unpack(">I", head[0x10:0x14])[0]
    table_off = struct.unpack(">I", head[0x18:0x1C])[0]
    if entry_count > 100000:
        return None
    f.seek(table_off)
    table = f.read(entry_count * 32)
    out = {}
    for i in range(entry_count):
        e = table[i * 32:(i + 1) * 32]
        if len(e) < 24:
            break
        eid, _fn, _f1, _f2, off, size = struct.unpack(">IIIIII", e[:24])
        if eid in wanted:
            out[eid] = (off, size)
    return out


def parse_sfo(blob):
    """param.sfo bytes -> {KEY: value}."""
    if blob[:4] != SFO_MAGIC:
        return {}
    key_tbl = struct.unpack("<I", blob[8:12])[0]
    data_tbl = struct.unpack("<I", blob[12:16])[0]
    n = struct.unpack("<I", blob[16:20])[0]
    result = {}
    for i in range(n):
        base = 0x14 + i * 16
        rec = blob[base:base + 16]
        if len(rec) < 16:
            break
        ko, fmt, ln, _mx, do = struct.unpack("<HHIII", rec)
        try:
            key_end = blob.index(b"\x00", key_tbl + ko)
            key = blob[key_tbl + ko:key_end].decode("utf-8", "replace")
        except ValueError:
            continue
        raw = blob[data_tbl + do:data_tbl + do + ln]
        if fmt == 0x0404:                       # int32
            val = struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else 0
        else:                                   # utf-8 string
            val = clean_text(raw.split(b"\x00")[0].decode("utf-8", "replace"))
        result[key] = val
    return result


def clean_text(s):
    # drop the U+FFFD replacement char and control chars, collapse whitespace
    s = "".join(ch for ch in s if (ch == "\n" or ord(ch) >= 0x20) and ord(ch) != 0xFFFD)
    return " ".join(s.split()).strip()


def kind_from_category(cat):
    cat = (cat or "").lower()
    if cat.startswith("gp"):
        return "update"
    if cat.startswith("ac"):
        return "dlc"
    return "base"


def region_from_content_id(cid):
    if cid and len(cid) >= 1:
        return REGION_BY_PREFIX.get(cid[0].upper(), "—")
    return "—"


def parse_pkg(path, extract_icon_to=None):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            wanted = {ENTRY_PARAM_SFO}
            if extract_icon_to:
                wanted.add(ENTRY_ICON0)
            entries = _find_entries(f, wanted)
            if not entries or ENTRY_PARAM_SFO not in entries:
                return None
            off, esize = entries[ENTRY_PARAM_SFO]
            f.seek(off)
            sfo = parse_sfo(f.read(min(esize, 1 << 20)))
            if not sfo:
                return None

            icon = None
            if extract_icon_to and ENTRY_ICON0 in entries:
                io, isz = entries[ENTRY_ICON0]
                f.seek(io)
                blob = f.read(min(isz, 4 << 20))
                if blob[:4] == PNG_MAGIC:
                    try:
                        os.makedirs(os.path.dirname(extract_icon_to), exist_ok=True)
                        with open(extract_icon_to, "wb") as ic:
                            ic.write(blob)
                        icon = extract_icon_to
                    except OSError:
                        icon = None

        cat = sfo.get("CATEGORY", "")
        cid = sfo.get("CONTENT_ID", "")
        return {
            "title_id": sfo.get("TITLE_ID") or (cid.split("-")[1].split("_")[0] if "-" in cid else None),
            "content_id": cid,
            "title": sfo.get("TITLE") or sfo.get("TITLE_00") or "",
            "category": cat,
            "kind": kind_from_category(cat),
            "app_ver": sfo.get("APP_VER") or sfo.get("VERSION") or "",
            "version": sfo.get("VERSION") or "",
            "region": region_from_content_id(cid),
            "size": size,
            "icon": icon,
            "sfo": sfo,
        }
    except (OSError, struct.error, ValueError):
        return None


def pkg_completeness(path):
    """Structural completeness check WITHOUT decrypting: PKG magic, a content_id, and the encrypted pfs
    image (offset @0x410, size @0x418) ending exactly at EOF, plus a high-entropy tail (truncation / zero-pad
    guard). Returns {complete, confident, reason, ...}. Only a *confident* False (bad magic / truncated /
    missing content_id) should ever block an install; anything inconclusive returns confident=False so the
    caller proceeds. Offsets verified live against DARK SOULS: REMASTERED (CUSA08692). [B7]"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(0x420)
            if size > 65536:
                f.seek(size - 65536)
                tail = f.read(65536)
            else:
                f.seek(0)
                tail = f.read(size)
    except OSError as e:
        return {"complete": False, "confident": False, "reason": "read error: %s" % e, "size": 0}
    magic = struct.unpack(">I", head[:4])[0] if len(head) >= 4 else 0
    if magic == PKG_MAGIC_PS5:
        # A PS5 package is a real, valid package - it simply is not a PS4 fPKG, so none of the
        # offsets below mean anything in it and we must not pretend to judge it. Saying
        # confident=True here labelled every PS5 update and DLC "bad PKG magic (not a .pkg)":
        # harmless while integrity.on_local_corrupt is "warn", but with it set to "block" it would
        # have refused to install any PS5 add-on at all. Inconclusive is the honest answer.
        return {"complete": True, "confident": False,
                "reason": "PS5 package (7F 46 49 48) - structure not parsed", "size": size}
    if len(head) < 0x420 or magic != PKG_MAGIC:
        return {"complete": False, "confident": True, "reason": "bad PKG magic (not a .pkg)", "size": size}
    content_id = head[0x40:0x40 + 36].split(b"\x00")[0].decode("ascii", "replace")
    if not content_id or "-" not in content_id:
        return {"complete": False, "confident": True, "reason": "missing content_id",
                "size": size, "content_id": content_id}
    pfs_off = struct.unpack(">Q", head[0x410:0x418])[0]
    pfs_size = struct.unpack(">Q", head[0x418:0x420])[0]
    info = {"size": size, "content_id": content_id, "pfs_offset": pfs_off,
            "pfs_size": pfs_size, "pfs_end": pfs_off + pfs_size}
    if pfs_off and pfs_size:
        if pfs_off + pfs_size > size:
            info.update(complete=False, confident=True,
                        reason="TRUNCATED: pfs image runs %d bytes past end of file" % (pfs_off + pfs_size - size))
            return info
        distinct = len(set(tail)) if tail else 0
        zeros = tail.count(0) if tail else 0
        if tail and (distinct < 16 or zeros > len(tail) * 0.9):
            info.update(complete=False, confident=True,
                        reason="tail looks padded/truncated (%d distinct bytes, %d%% zero)"
                               % (distinct, int(100 * zeros / len(tail))))
            return info
        info.update(complete=True, confident=True, reason="complete (pfs image fits + tail high-entropy)")
        return info
    info.update(complete=True, confident=False, reason="no pfs image fields — completeness inconclusive")
    return info


if __name__ == "__main__":
    import sys
    import json
    for arg in sys.argv[1:]:
        m = parse_pkg(arg)
        if m:
            m.pop("sfo", None)
            print(json.dumps(m, indent=2, ensure_ascii=False))
        else:
            print("could not parse:", arg)
