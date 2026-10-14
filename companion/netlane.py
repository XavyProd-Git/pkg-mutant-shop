# -*- coding: utf-8 -*-
"""netlane - the one road this app takes to the internet.

WHY THIS EXISTS. Every HTTPS call this app made was a bare urllib.request.urlopen(), and that has
exactly one idea of who to trust: the certificates ALREADY SITTING in the Windows store. Windows
does not keep its trusted roots there - it fetches each one the first time one of its OWN clients
asks for it - and Python never asks. On the PC this was measured on, after seven months of daily
use, the store held 46 of the 562 roots Windows is willing to trust. So on a PC where nothing has
asked yet for the two roots GitHub and its download host hang off, every call failed its
certificate check, and all the panel could say was "Could not reach GitHub (URLError)" - the same
word it printed for a dead DNS, a refused connection, a timeout, a proxy and a wrong clock.

  open_url(url, headers, timeout)                      -> NetResponse (status, headers, body, lane)
  download(url, dest, progress_cb, resume, ...)        -> dict(bytes, total, lane, resumed_from)
  both raise NetError(kind, detail) and nothing else for a network reason.

THREE LANES, IN THIS ORDER, EACH ONLY TRIED WHEN THE ONE BEFORE COULD NOT GET AN ANSWER:
  "py"       this process: Windows' stores AND the bundled Mozilla list, in one context
  "winhttp"  Windows' own HTTP stack, in this process - its trust, its proxy settings
  "curl"     Windows' own curl.exe (System32, never PATH) - the same trust, a separate process
An HTTP status is an ANSWER: 404, 403 and a rate limit are returned from the first lane that gets
one and no other lane is asked the same question.

ONLY FOR THE INTERNET. Everything this app says to a console or to another PC on the LAN stays on
urllib exactly as it was: those are plain http:// to a dotted address, and none of this applies.

Every lane, every failure kind and every fallback step in here was run before it was written
down - against the real hosts, a lab certificate authority, and one forced failure per step -
frozen and unfrozen.
"""
import ctypes
import email.utils
import http.client
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

NET_UA = "PKG-MUTANT-SHOP"
NET_LANES = ("py", "winhttp", "curl")
# What a failure IS, as a code the page can translate. "http-<code>" is the eighth, built on the fly.
NET_KINDS = ("dns", "connect", "timeout", "tls-trust", "tls-other", "clock", "rate-limited")
_REDIRECTS = (301, 302, 303, 307, 308)
_now = time.time                     # the wall clock, in one place so a test can move it


class NetError(Exception):
    """A network failure with a reason somebody can act on.

    kind    one of NET_KINDS, or "http-<code>"
    detail  one technical line for the log and the tooltip - never the sentence the owner reads
    lane    the lane that produced it;  tried = [(lane, kind, detail), ...] for every lane asked
    status / headers / body   set when a server answered (body: first 2 KB)
    retry_at  epoch seconds a rate limit lifts at, when the server said
    """

    def __init__(self, kind, detail="", lane="", status=0, headers=None, body=b"", retry_at=0):
        Exception.__init__(self, "%s (%s)" % (kind, detail) if detail else kind)
        self.kind, self.detail, self.lane = kind, detail, lane
        self.status, self.headers, self.body = status, headers or {}, body
        self.retry_at = retry_at
        self.tried = []


class NetResponse(object):
    def __init__(self, status, headers, body, url, lane):
        self.status, self.headers, self.body, self.url, self.lane = status, headers, body, url, lane


# ---- trust ------------------------------------------------------------------------------------- #
_ctx_lock = threading.Lock()
_ctx_cache = {"ctx": None, "at": 0.0, "stats": ""}
_CTX_TTL_S = 900        # the Windows store GROWS while we run (that is the whole problem), so re-read


def certifi_path():
    """The bundled Mozilla root list, or "" when this build carries none."""
    try:
        import certifi
        p = certifi.where()
        return p if os.path.isfile(p) else ""
    except Exception:
        return ""


def tls_context(fresh=False):
    """ONE context holding BOTH trust stores: everything Windows has, plus the bundled list.

    BOTH, NOT EITHER. The Windows stores alone are what failed on a new PC. The bundled list alone
    fails on a PC behind a local HTTPS inspector, whose own root exists only in the Windows store.
    A chain that either store would accept is accepted here, in one handshake.

    ONE CERTIFICATE AT A TIME. ssl's own loader hands the whole store to OpenSSL as one blob, and a
    single unreadable certificate in it raises - which fails every HTTPS call the process makes.
    """
    with _ctx_lock:
        c = _ctx_cache["ctx"]
        if c is not None and not fresh and (time.time() - _ctx_cache["at"]) < _CTX_TTL_S:
            return c
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # CERT_REQUIRED and the hostname check
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    n_win = n_bad = 0
    if sys.platform == "win32":
        for store in ("ROOT", "CA"):
            try:
                certs = ssl.enum_certificates(store)
            except Exception:
                certs = []
            for der, enc, trust in certs:
                if enc != "x509_asn":
                    continue
                if not (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
                    continue
                try:
                    ctx.load_verify_locations(cadata=der)
                    n_win += 1
                except ssl.SSLError:
                    n_bad += 1
    else:
        try:
            ctx.load_default_certs()
        except Exception:
            pass
    p = certifi_path()
    if p:
        try:
            ctx.load_verify_locations(cafile=p)
        except Exception:
            p = ""
    with _ctx_lock:
        _ctx_cache.update(ctx=ctx, at=time.time(),
                          stats="system=%d unreadable=%d bundled=%s total=%d" % (
                              n_win, n_bad, "yes" if p else "NO", ctx.cert_store_stats().get("x509_ca", 0)))
    return ctx


# ---- the clock ----------------------------------------------------------------------------------- #
def clock_skew(host, timeout=4.0):
    """How far this PC's clock is from `host`'s, in seconds (+ = this PC is ahead), or None.

    Asked over PLAIN HTTP on purpose: it is only ever asked when TLS has just refused a certificate
    for its dates, and the Date header of any answer - a redirect will do - needs no certificate.
    """
    try:
        c = http.client.HTTPConnection(host, 80, timeout=timeout)
        try:
            c.request("HEAD", "/", headers={"User-Agent": NET_UA, "Host": host})
            d = c.getresponse().getheader("Date")
        finally:
            c.close()
        if not d:
            return None
        return _now() - email.utils.parsedate_to_datetime(d).timestamp()
    except Exception:
        return None


def _dates_verdict(host, not_yet, lane, detail):
    """A certificate refused for its dates: is it the certificate, or this PC's clock?"""
    skew = clock_skew(host)
    if skew is not None and abs(skew) > 12 * 3600:
        return NetError("clock", "this PC's clock is %s by %.1f days (%s)" % (
            "ahead" if skew > 0 else "behind", abs(skew) / 86400.0, detail), lane)
    if skew is None and not_yet:
        # Nobody serves a certificate before it is issued. With no second opinion available,
        # "not valid yet" is this PC's clock running behind.
        return NetError("clock", "the certificate is not valid yet by this PC's clock (%s)" % detail, lane)
    return NetError("tls-other", "certificate dates refused and the clock agrees with the server (%s)"
                    % detail, lane)


# ---- lane "py": this process ------------------------------------------------------------------- #
_slow_lock = threading.Lock()
_slow_family = {}        # host -> (family, until): an address family that just timed out goes last
_CONNECT_S = 5.0         # per ADDRESS, not per call - see _tcp_connect


def _tcp_connect(host, port, timeout):
    """Connect, giving each address a SHORT turn.

    socket.create_connection() gives every address the caller's whole timeout, one after another.
    With a 180 s download timeout and an IPv6 route that swallows packets, that is three minutes of
    nothing before IPv4 is even tried. Here an address gets _CONNECT_S, the families alternate, and
    a family that timed out for this host goes to the back for ten minutes.
    """
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise NetError("dns", "%s: %s" % (host, e), "py")
    except UnicodeError as e:
        raise NetError("dns", "%s: %s" % (host, e), "py")
    with _slow_lock:
        slow = _slow_family.get(host)
        if slow and slow[1] < time.time():
            slow = None
    fams, by = [], {}
    for inf in infos:
        if inf[0] not in by:
            fams.append(inf[0])
            by[inf[0]] = []
        by[inf[0]].append(inf)
    if slow and slow[0] in fams and len(fams) > 1:
        fams.remove(slow[0])
        fams.append(slow[0])
    order = []
    while any(by[f] for f in fams):                      # v6, v4, v6, v4 ... in the resolver's order
        for f in fams:
            if by[f]:
                order.append(by[f].pop(0))
    deadline = time.monotonic() + max(1.0, timeout)
    last = None
    for fam, typ, proto, _cn, sa in order[:6]:
        left = deadline - time.monotonic()
        if left <= 0.05:
            break
        s = socket.socket(fam, typ, proto)
        s.settimeout(min(_CONNECT_S, left))
        try:
            s.connect(sa)
            s.settimeout(timeout)
            return s
        except socket.timeout:
            last = NetError("timeout", "no answer from %s port %d within %.0f s" % (sa[0], port, min(_CONNECT_S, left)), "py")
            with _slow_lock:
                _slow_family[host] = (fam, time.time() + 600)
        except OSError as e:
            if getattr(e, "winerror", 0) == 10013:
                # WSAEACCES: not the network. Something on THIS PC decided this app may not connect.
                last = NetError("connect", "this PC refused to let the app open a connection to %s "
                                "(10013) - a firewall rule for this program" % sa[0], "py")
            else:
                last = NetError("connect", "%s port %d: %s" % (sa[0], port, e.strerror or e), "py")
        try:
            s.close()
        except OSError:
            pass
    raise last or NetError("timeout", "no address of %s answered" % host, "py")


def _proxy_for(scheme, host, use_proxy):
    """(proxy_host, proxy_port, auth_header) from the Windows settings urllib already reads, or None."""
    if not use_proxy:
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        p = urllib.request.getproxies().get(scheme)
    except Exception:
        return None
    if not p:
        return None
    u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
    if not u.hostname:
        return None
    auth = ""
    if u.username:
        import base64
        raw = "%s:%s" % (urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ""))
        auth = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return u.hostname, u.port or 80, auth


def _tls_verify_error(e, host, lane):
    code = getattr(e, "verify_code", 0)
    msg = getattr(e, "verify_message", "") or str(e)
    detail = "%s: %s" % (host, msg)
    if code in (9, 10):                  # not yet valid / has expired
        return _dates_verdict(host, code == 9, lane, detail)
    if code in (2, 18, 19, 20, 21, 27):  # no issuer / self-signed / nothing we trust signed this
        return NetError("tls-trust", detail, lane)
    return NetError("tls-other", detail, lane)       # 62 = hostname mismatch, and the rest


def _py_hop(method, url, headers, timeout, on_headers, use_proxy=True):
    """ONE request, no redirects. on_headers(status, headers) returns a writer(chunk) or None."""
    u = urllib.parse.urlsplit(url)
    https = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if https else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", NET_UA)
    hdrs.setdefault("Accept", "*/*")
    hdrs["Host"] = u.netloc.rsplit("@", 1)[-1]
    hdrs["Connection"] = "close"
    px = _proxy_for(u.scheme, host, use_proxy)
    lane = "py"
    if https:
        if px:
            conn = http.client.HTTPSConnection(px[0], px[1], timeout=timeout, context=tls_context())
            conn.set_tunnel(host, port, headers=({"Proxy-Authorization": px[2]} if px[2] else {}))
        else:
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=tls_context())
    else:
        if px:
            conn = http.client.HTTPConnection(px[0], px[1], timeout=timeout)
            path = url
            if px[2]:
                hdrs["Proxy-Authorization"] = px[2]
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn._create_connection = lambda addr, t=None, src=None: _tcp_connect(addr[0], addr[1], timeout)
    try:
        # THE TWO HALVES OF connect(), SEPARATELY, so a failure knows which half it was.
        try:
            http.client.HTTPConnection.connect(conn)          # TCP, and CONNECT through a proxy
        except NetError as e:
            if px:
                e.detail = "proxy %s:%d - %s" % (px[0], px[1], e.detail)
            raise
        except socket.timeout:
            raise NetError("timeout", "the proxy %s:%d did not open a tunnel" % (px[0], px[1]) if px else "connect", lane)
        except OSError as e:
            raise NetError("connect", "proxy %s:%d - %s" % (px[0], px[1], e) if px else str(e), lane)
        if https:
            try:
                conn.sock = tls_context().wrap_socket(conn.sock, server_hostname=host)
            except ssl.SSLCertVerificationError as e:
                raise _tls_verify_error(e, host, lane)
            except socket.timeout:
                raise NetError("timeout", "%s: the TLS handshake did not finish" % host, lane)
            except ssl.SSLError as e:
                raise NetError("tls-other", "%s: %s" % (host, getattr(e, "reason", None) or e), lane)
            except OSError as e:
                raise NetError("tls-other", "%s: the connection was closed during the TLS handshake (%s)"
                               % (host, e.strerror or e), lane)
        try:
            conn.request(method, path, headers=hdrs)
            r = conn.getresponse()
        except socket.timeout:
            raise NetError("timeout", "%s: connected, then no answer within %.0f s" % (host, timeout), lane)
        except (OSError, http.client.HTTPException) as e:
            raise NetError("connect", "%s: the connection dropped before an answer (%s)"
                           % (host, e.__class__.__name__), lane)
        rh = {}
        for k, v in r.getheaders():
            k = k.lower()
            rh[k] = (rh[k] + ", " + v) if k in rh else v
        sink = on_headers(r.status, rh)
        got, small = 0, b""
        try:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if sink is not None:
                    sink(chunk)
                elif len(small) < 4096:
                    small += chunk[:4096 - len(small)]
                else:
                    break                                   # an error page we do not need the rest of
        except socket.timeout:
            raise NetError("timeout", "%s: no data for %.0f s after %d bytes" % (host, timeout, got), lane)
        except (OSError, http.client.HTTPException) as e:
            raise NetError("connect", "%s: the connection dropped after %d bytes (%s)"
                           % (host, got, e.__class__.__name__), lane)
        return r.status, rh, small
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---- lane "winhttp": Windows' own stack, in this process ------------------------------------------ #
_wh = {"dll": None, "session": None, "cb": None, "failed": ""}
_wh_lock = threading.Lock()
_wh_tls = threading.local()
_WH_ERR = {12007: ("dns", "the name could not be resolved"), 12029: ("connect", "could not connect"),
           12002: ("timeout", "timed out"), 12030: ("connect", "the connection was closed"),
           12152: ("connect", "the answer was not HTTP"), 12157: ("tls-other", "secure channel error"),
           12044: ("tls-other", "the server asked for a client certificate"),
           12180: ("connect", "proxy auto-detection failed"), 12167: ("connect", "the proxy script could not be read"),
           12009: ("connect", "invalid option"), 12005: ("connect", "invalid address"), 12006: ("connect", "unknown scheme")}


def _winhttp():
    """winhttp.dll with every prototype declared (a default-int return truncates a 64-bit handle)."""
    with _wh_lock:
        if _wh["dll"] is not None or _wh["failed"]:
            if _wh["failed"]:
                raise NetError("connect", _wh["failed"], "winhttp")
            return _wh["dll"]
        try:
            if sys.platform != "win32":
                raise OSError("not Windows")
            from ctypes import wintypes as wt
            w = ctypes.WinDLL("winhttp", use_last_error=True)
            H, D, P = ctypes.c_void_p, wt.DWORD, ctypes.POINTER
            w.WinHttpOpen.restype, w.WinHttpOpen.argtypes = H, [wt.LPCWSTR, D, wt.LPCWSTR, wt.LPCWSTR, D]
            w.WinHttpConnect.restype, w.WinHttpConnect.argtypes = H, [H, wt.LPCWSTR, wt.WORD, D]
            w.WinHttpOpenRequest.restype = H
            w.WinHttpOpenRequest.argtypes = [H, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, D]
            w.WinHttpSetTimeouts.restype, w.WinHttpSetTimeouts.argtypes = wt.BOOL, [H, ctypes.c_int] + [ctypes.c_int] * 3
            w.WinHttpSetOption.restype, w.WinHttpSetOption.argtypes = wt.BOOL, [H, D, ctypes.c_void_p, D]
            w.WinHttpSendRequest.restype = wt.BOOL
            w.WinHttpSendRequest.argtypes = [H, wt.LPCWSTR, D, ctypes.c_void_p, D, D, ctypes.c_size_t]
            w.WinHttpReceiveResponse.restype, w.WinHttpReceiveResponse.argtypes = wt.BOOL, [H, ctypes.c_void_p]
            w.WinHttpQueryHeaders.restype = wt.BOOL
            w.WinHttpQueryHeaders.argtypes = [H, D, wt.LPCWSTR, ctypes.c_void_p, P(D), P(D)]
            w.WinHttpReadData.restype, w.WinHttpReadData.argtypes = wt.BOOL, [H, ctypes.c_void_p, D, P(D)]
            w.WinHttpCloseHandle.restype, w.WinHttpCloseHandle.argtypes = wt.BOOL, [H]
            CB = ctypes.WINFUNCTYPE(None, H, ctypes.c_size_t, D, ctypes.c_void_p, D)
            w.WinHttpSetStatusCallback.restype = ctypes.c_void_p
            w.WinHttpSetStatusCallback.argtypes = [H, CB, D, ctypes.c_size_t]

            def _cb(hnd, context, status, info, length):
                # WINHTTP_CALLBACK_STATUS_SECURE_FAILURE: WHY the certificate was refused.
                try:
                    if status == 0x00010000 and info and length >= 4:
                        _wh_tls.secure = ctypes.cast(info, P(D))[0]
                except Exception:
                    pass

            cb = CB(_cb)
            # 4 = WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY (Windows 8.1+): the PC's own proxy settings,
            # script and auto-detect included. 0 = the WinHTTP default, for anything older.
            s = w.WinHttpOpen(NET_UA, 4, None, None, 0) or w.WinHttpOpen(NET_UA, 0, None, None, 0)
            if not s:
                raise OSError("WinHttpOpen failed (%d)" % ctypes.get_last_error())
            w.WinHttpSetStatusCallback(s, cb, 0x00010000, 0)
            for protos in (0x00000800 | 0x00002000, 0x00000800):       # TLS 1.2 + 1.3, else 1.2
                v = D(protos)
                if w.WinHttpSetOption(s, 84, ctypes.byref(v), 4):
                    break
            _wh.update(dll=w, session=s, cb=cb)
            return w
        except Exception as e:
            _wh["failed"] = "Windows' HTTP stack is not usable here (%s)" % e
            raise NetError("connect", _wh["failed"], "winhttp")


def _winhttp_hop(method, url, headers, timeout, on_headers, use_proxy=True):
    """ONE request through WinHTTP, no redirects (they are followed by _follow, with our rules)."""
    w = _winhttp()
    lane = "winhttp"
    u = urllib.parse.urlsplit(url)
    https = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if https else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    D = ctypes.c_ulong
    hc = hr = None
    _wh_tls.secure = 0

    def fail(step):
        err = ctypes.get_last_error()
        if err == 12175:                                    # ERROR_WINHTTP_SECURE_FAILURE
            f = getattr(_wh_tls, "secure", 0)
            detail = "%s: Windows refused the certificate (flags 0x%x)" % (host, f)
            if f & 0x20:                                    # CERT_DATE_INVALID
                return _dates_verdict(host, False, lane, detail)
            if f & 0x08:                                    # INVALID_CA
                return NetError("tls-trust", detail, lane)
            return NetError("tls-other", detail, lane)      # 0x10 name, 0x01/0x04 revocation, ...
        kind, text = _WH_ERR.get(err, ("connect", "error %d" % err))
        return NetError(kind, "%s: %s (%s, %d)" % (host, text, step, err), lane)

    try:
        hc = w.WinHttpConnect(_wh["session"], host, port, 0)
        if not hc:
            raise fail("connect")
        hr = w.WinHttpOpenRequest(hc, method, path, None, None, None, 0x00800000 if https else 0)
        if not hr:
            raise fail("open")
        ms = int(timeout * 1000)
        w.WinHttpSetTimeouts(hr, ms, min(ms, int(_CONNECT_S * 1000)), ms, ms)
        v = D(1)
        w.WinHttpSetOption(hr, 4, ctypes.byref(v), 4)       # WINHTTP_OPTION_CONNECT_RETRIES
        v = D(0x1 | 0x2)                  # no cookies, NO AUTOMATIC REDIRECTS (ours are stricter)
        w.WinHttpSetOption(hr, 63, ctypes.byref(v), 4)
        hdrs = dict(headers or {})
        hdrs.setdefault("Accept", "*/*")
        if hdrs.get("User-Agent", NET_UA) == NET_UA:
            hdrs.pop("User-Agent", None)                    # the session already says who we are
        raw = "".join("%s: %s\r\n" % (k, v2) for k, v2 in hdrs.items())
        if not w.WinHttpSendRequest(hr, raw, len(raw), None, 0, 0, 0):
            raise fail("send")
        if not w.WinHttpReceiveResponse(hr, None):
            raise fail("receive")
        st, n = D(0), D(4)
        if not w.WinHttpQueryHeaders(hr, 19 | 0x20000000, None, ctypes.byref(st), ctypes.byref(n), None):
            raise fail("status")
        n = D(0)
        w.WinHttpQueryHeaders(hr, 22, None, None, ctypes.byref(n), None)      # size of the raw headers
        rh = {}
        if n.value:
            buf = ctypes.create_unicode_buffer(n.value // 2 + 2)
            if w.WinHttpQueryHeaders(hr, 22, None, buf, ctypes.byref(n), None):
                for line in buf.value.split("\r\n")[1:]:
                    if ":" in line:
                        k, v2 = line.split(":", 1)
                        k = k.strip().lower()
                        rh[k] = (rh[k] + ", " + v2.strip()) if k in rh else v2.strip()
        sink = on_headers(st.value, rh)
        got, small = 0, b""
        buf = ctypes.create_string_buffer(1 << 16)
        while True:
            rd = D(0)
            if not w.WinHttpReadData(hr, buf, len(buf), ctypes.byref(rd)):
                e = fail("read")
                e.detail += " after %d bytes" % got
                raise e
            if not rd.value:
                break
            chunk = buf.raw[:rd.value]
            got += len(chunk)
            if sink is not None:
                sink(chunk)
            elif len(small) < 4096:
                small += chunk[:4096 - len(small)]
            else:
                break
        return st.value, rh, small
    finally:
        if hr:
            w.WinHttpCloseHandle(hr)
        if hc:
            w.WinHttpCloseHandle(hc)


# ---- redirects, for the two in-process lanes ------------------------------------------------------ #
def _follow(hop, lane, url, headers, timeout, on_headers, https_only=True, use_proxy=True):
    """Follow redirects with OUR rules, then hand the final answer to on_headers.

    * never from https to anything else;
    * Authorization goes to the host it was written for and no further - a release asset redirects
      to a signed address on another host, and a token has no business arriving there.
    """
    hdrs = dict(headers or {})
    first = urllib.parse.urlsplit(url)
    for _ in range(7):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "http") or (https_only and u.scheme != "https"):
            raise NetError("tls-other", "refused a redirect to a non-HTTPS address (%s://%s)" % (u.scheme, u.netloc), lane)
        box = {}

        def gate(status, rh, box=box):
            box["status"], box["rh"] = status, rh
            if status in _REDIRECTS and rh.get("location"):
                return None
            return on_headers(status, rh)

        status, rh, small = hop("GET", url, hdrs, timeout, gate, use_proxy)
        if status in _REDIRECTS and rh.get("location"):
            nxt = urllib.parse.urljoin(url, rh["location"])
            if urllib.parse.urlsplit(nxt).netloc.lower() != first.netloc.lower():
                for k in list(hdrs):
                    if k.lower() in ("authorization", "cookie", "proxy-authorization"):
                        del hdrs[k]
            url = nxt
            continue
        return status, rh, small, url
    raise NetError("connect", "too many redirects", lane)


# ---- lane "curl": Windows' own curl.exe ----------------------------------------------------------- #
def curl_exe():
    """System32\\curl.exe (Windows 10 1803 and later carry it) or "". NEVER whatever PATH offers."""
    if sys.platform != "win32":
        return ""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    for sub in ("System32", "Sysnative"):
        p = os.path.join(root, sub, "curl.exe")
        if os.path.isfile(p):
            return p
    return ""


def _curl_q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "") + '"'


_CURL_RC = {5: "dns", 6: "dns", 7: "connect", 28: "timeout", 52: "connect", 56: "connect", 18: "connect",
            55: "connect", 16: "connect", 92: "connect", 47: "connect", 97: "connect", 35: "tls-other",
            60: "tls-trust", 51: "tls-other", 53: "tls-other", 58: "tls-other", 59: "tls-other", 77: "tls-trust",
            83: "tls-other", 90: "tls-other", 91: "tls-other"}


def _curl_run(url, headers, timeout, out_path, hdr_path, extra, https_only, on_tick=None, best_effort=True):
    exe = curl_exe()
    if not exe:
        raise NetError("connect", "this Windows has no curl.exe", "curl")
    cfg = ["url = " + _curl_q(url), "silent", "show-error", "location", "max-redirs = 6",
           "proto = " + _curl_q("=https" if https_only else "=https,http"),
           "proto-redir = " + _curl_q("=https" if https_only else "=https,http"),
           "connect-timeout = %d" % max(2, min(int(timeout), 15)),
           "output = " + _curl_q(out_path), "dump-header = " + _curl_q(hdr_path),
           "user-agent = " + _curl_q((headers or {}).get("User-Agent") or NET_UA),
           "write-out = " + _curl_q("%{http_code} %{size_download}")]
    if best_effort:
        # Schannel refuses a certificate whose revocation list it cannot FETCH. That is a second
        # network dependency, to a third party, on the lane that exists for when things are broken.
        cfg.append("ssl-revoke-best-effort")
    for k, v in (headers or {}).items():
        if k.lower() != "user-agent":
            cfg.append("header = " + _curl_q("%s: %s" % (k, v)))
    cfg += extra
    # -q FIRST: no curlrc of the owner's gets a say. The URL, the headers and any token go in on
    # STDIN, so nothing private is ever on a command line. CREATE_NO_WINDOW: this is a windowed app,
    # and a console child without it flashes a black window. All three handles are given, because a
    # windowed PyInstaller build has no valid ones to inherit.
    p = subprocess.Popen([exe, "-q", "--config", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, creationflags=0x08000000)
    box = {}

    def feed():
        try:
            box["out"], box["err"] = p.communicate(("\n".join(cfg) + "\n").encode("utf-8"))
        except Exception as e:
            box["out"], box["err"] = b"", str(e).encode()

    th = threading.Thread(target=feed, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.25)
            if on_tick:
                on_tick()
    except BaseException:
        try:
            p.kill()
        except Exception:
            pass
        raise
    rc = p.returncode
    out = (box.get("out") or b"").decode("ascii", "replace").strip()
    err = (box.get("err") or b"").decode("utf-8", "replace").strip()
    if rc == 2 and best_effort and "ssl-revoke-best-effort" in err:
        # A curl older than 7.70 (the one Windows 10 1803 shipped). Ask again without it.
        return _curl_run(url, headers, timeout, out_path, hdr_path, extra, https_only, on_tick, False)
    status = 0
    try:
        status = int(out.split()[0])
    except Exception:
        pass
    rh = {}
    try:
        with open(hdr_path, "rb") as f:
            blocks = [b for b in f.read().decode("latin-1").replace("\r\n", "\n").split("\n\n") if b.strip()]
        for line in (blocks[-1].split("\n")[1:] if blocks else []):
            if ":" in line:
                k, v = line.split(":", 1)
                rh[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return rc, status, rh, err


def _curl_error(rc, status, err, host):
    line = (err.splitlines() or [""])[0][:200]
    if rc in (0, 22):
        return None                                        # an HTTP answer; the caller classifies it
    if "SEC_E_CERT_EXPIRED" in err or "CERT_E_EXPIRED" in err:
        return _dates_verdict(host, False, "curl", line)
    if "SEC_E_UNTRUSTED_ROOT" in err or "CERT_E_UNTRUSTEDROOT" in err or "CERT_E_CHAINING" in err:
        return NetError("tls-trust", line, "curl")
    if "SEC_E_WRONG_PRINCIPAL" in err or "CERT_E_CN_NO_MATCH" in err:
        return NetError("tls-other", line, "curl")
    return NetError(_CURL_RC.get(rc, "connect"), line or "curl exit %d" % rc, "curl")


# ---- answers ------------------------------------------------------------------------------------- #
def _http_error(status, rh, body, lane):
    """A status that is not success, as a NetError. A rate limit is told apart from a refusal."""
    retry_at = 0
    limited = status == 429
    if status in (403, 429):
        if str(rh.get("x-ratelimit-remaining", "")) == "0":
            limited = True
            try:
                retry_at = int(rh.get("x-ratelimit-reset") or 0)
            except ValueError:
                pass
        if rh.get("retry-after"):
            limited = True
            try:
                retry_at = int(_now()) + int(rh["retry-after"])
            except ValueError:
                pass
        if b"rate limit" in (body or b"").lower():
            limited = True
    e = NetError("rate-limited" if limited else "http-%d" % status, "HTTP %d" % status, lane,
                 status=status, headers=rh, body=(body or b"")[:2048], retry_at=retry_at)
    return e


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


_sticky_lock = threading.Lock()
_sticky = {}             # host -> (lane, until): the lane that last got an answer where an earlier one could not


def _lane_order(host, lanes):
    order = [l for l in (lanes or NET_LANES) if l in NET_LANES]
    with _sticky_lock:
        s = _sticky.get(host)
    if s and s[1] > time.time() and s[0] in order:
        order.remove(s[0])
        order.insert(0, s[0])
    return order


def _goes_on(err, lane):
    """After this failure on this lane, is another lane worth asking?"""
    k = err.kind
    if k == "http-407":
        return True                      # a proxy wants credentials this lane cannot give; Windows can
    if k.startswith("http-") or k in ("rate-limited", "clock"):
        return False                     # an ANSWER, or a cause no other lane escapes
    return True


def _checked_host(url, https_only):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or (https_only and u.scheme != "https"):
        raise NetError("tls-other", "refused an address that is not HTTPS (%s)" % url[:80])
    return u.hostname


_down_lock = threading.Lock()
_down = {}               # host -> (NetError, until): no lane could even reach it a moment ago
_DOWN_S = 30


def _run(host, lanes, attempt):
    """Ask each lane in turn. attempt(lane) returns the result or raises NetError."""
    tried, first = [], None
    order = _lane_order(host, lanes)
    # EIGHTEEN PROJECTS ARE CHECKED ONE AFTER ANOTHER. On a PC with no route out, each of them would
    # otherwise wait out every lane's timeout in turn. When no lane could reach a host, the next
    # question about that host inside half a minute gets the same answer at once.
    with _down_lock:
        d = _down.get(host)
        if d and d[1] > time.time() and lanes is None:
            e = NetError(d[0].kind, d[0].detail, d[0].lane)
            e.tried = list(d[0].tried)
            raise e
    for i, lane in enumerate(order):
        try:
            res = attempt(lane)
            if tried:
                with _sticky_lock:
                    _sticky[host] = (lane, time.time() + 1800)
            return res, tried
        except NetError as e:
            e.lane = e.lane or lane
            tried.append((lane, e.kind, e.detail))
            first = first or e
            if not _goes_on(e, lane):
                e.tried = tried
                raise e
            if lane == "winhttp" and _wh["dll"] is not None and e.kind in ("dns", "connect", "timeout"):
                # Windows' own stack, with the PC's own proxy settings, could not get there either.
                # curl.exe asks the same resolver over the same route: it is for a TLS disagreement,
                # or for a PC where the in-process stack would not load.
                break
    first.tried = tried
    if lanes is None and all(k in ("dns", "connect", "timeout") for _l, k, _d in tried):
        with _down_lock:
            _down[host] = (first, time.time() + _DOWN_S)
    raise first


def open_url(url, headers=None, timeout=10.0, max_bytes=8 << 20, lanes=None, https_only=True, use_proxy=True):
    """GET `url` into memory. Returns NetResponse for a 2xx; raises NetError for everything else."""
    host = _checked_host(url, https_only)

    def attempt(lane):
        if lane == "curl":
            d = tempfile.mkdtemp(prefix="pms-net-")
            body_p, hdr_p = os.path.join(d, "b"), os.path.join(d, "h")
            try:
                rc, status, rh, err = _curl_run(url, headers, timeout, body_p, hdr_p,
                                                ["max-time = %d" % max(3, int(timeout * 3))], https_only)
                e = _curl_error(rc, status, err, host)
                if e:
                    raise e
                body = b""
                try:
                    with open(body_p, "rb") as f:
                        body = f.read(max_bytes)
                except OSError:
                    pass
                if not (200 <= status < 300):
                    raise _http_error(status, rh, body, lane)
                return NetResponse(status, rh, body, url, lane)
            finally:
                for p in (body_p, hdr_p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        hop = _py_hop if lane == "py" else _winhttp_hop
        parts, size = [], [0]

        def on_headers(status, rh):
            if not (200 <= status < 300):
                return None

            def sink(chunk):
                if size[0] < max_bytes:
                    parts.append(chunk[:max_bytes - size[0]])
                    size[0] += len(chunk)
            return sink

        status, rh, small, final = _follow(hop, lane, url, headers, timeout, on_headers, https_only, use_proxy)
        if not (200 <= status < 300):
            raise _http_error(status, rh, small, lane)
        return NetResponse(status, rh, b"".join(parts), final, lane)

    res, tried = _run(host, lanes, attempt)
    res.tried = tried
    return res


def download(url, dest, progress_cb=None, resume=True, headers=None, timeout=30.0, expect_size=0,
             lanes=None, https_only=True, use_proxy=True, resume_key=""):
    """Fetch `url` into the file `dest`, appending to what is already there when resume is on.

    `dest` IS THE WORKING FILE - the caller's "<name>.part". Nothing is renamed here, so the
    caller's own checks (size, marker, magic bytes) still run before anything live is replaced.

    `timeout` IS A STALL TIMEOUT, not a total: a 200 MB file on a slow line is allowed to take as
    long as it takes, and a transfer that delivers nothing for `timeout` seconds is over.

    RESUME, WITHIN ONE CALL: a dropped connection is picked up where it stopped (Range), up to four
    times a lane, and the next lane carries on from the same byte. A server that answers 200 to a
    Range starts the file again from zero; 416 with a matching length means it was already whole.

    RESUME, ACROSS CALLS, ONLY WITH A KEY. Half of yesterday's file and half of today's is the
    right length and is not a file. Several projects publish every release under ONE name, so the
    name proves nothing: a part left by an earlier call is continued only when `resume_key` - the
    caller's word for "this exact asset", e.g. the release's digest, or id + size + updated_at -
    is the key that part was started under (kept beside it as <dest>.key). No key, no carry-over.

    progress_cb(done, total) is called a few times a second; total is 0 when the server did not say.
    Raising from it cancels the transfer.

    Returns {"bytes": n, "total": n, "lane": "py", "resumed_from": n, "tried": [...]}.
    """
    host = _checked_host(url, https_only)
    keyf = dest + ".key"
    old_key = ""
    try:
        with open(keyf, "r", encoding="utf-8") as f:
            old_key = f.read(4096)
    except (OSError, UnicodeError):
        pass
    if not (resume and resume_key and old_key == resume_key):
        _rm(dest)
    _rm(keyf)
    if resume and resume_key:
        try:
            with open(keyf, "w", encoding="utf-8") as f:
                f.write(resume_key)
        except OSError:
            pass
    start0 = os.path.getsize(dest) if os.path.isfile(dest) else 0
    last_tick = [0.0]

    def tick(done, total, force=False):
        if progress_cb and (force or time.monotonic() - last_tick[0] >= 0.25):
            last_tick[0] = time.monotonic()
            progress_cb(done, total)

    def attempt(lane):
        drops = 0
        while True:
            have = os.path.getsize(dest) if os.path.isfile(dest) else 0
            if expect_size and have == expect_size:
                return {"bytes": have, "total": have, "lane": lane}
            if expect_size and have > expect_size:
                _rm(dest)
                have = 0
            hdrs = dict(headers or {})
            try:
                if lane == "curl":
                    d = tempfile.mkdtemp(prefix="pms-net-")
                    hdr_p = os.path.join(d, "h")
                    try:
                        extra = ["fail", "speed-limit = 1", "speed-time = %d" % max(5, int(timeout))]
                        if have:
                            extra.append("continue-at = %d" % have)
                        rc, status, rh, err = _curl_run(
                            url, hdrs, timeout, dest, hdr_p, extra, https_only,
                            on_tick=lambda: tick(os.path.getsize(dest) if os.path.isfile(dest) else 0, expect_size))
                    finally:
                        try:
                            os.remove(hdr_p)
                        except OSError:
                            pass
                        try:
                            os.rmdir(d)
                        except OSError:
                            pass
                    if rc == 33 or status == 416:            # cannot resume / nothing left to send
                        total = 0
                        try:
                            total = int(str(rh.get("content-range", "")).rsplit("/", 1)[-1])
                        except ValueError:
                            pass
                        if status == 416 and total and total == have:
                            return {"bytes": have, "total": total, "lane": lane}
                        _rm(dest)
                        continue
                    e = _curl_error(rc, status, err, host)
                    if e:
                        raise e
                    if not (200 <= status < 300):
                        raise _http_error(status, rh, b"", lane)
                    got = os.path.getsize(dest)
                    return {"bytes": got, "total": got, "lane": lane}
                hop = _py_hop if lane == "py" else _winhttp_hop
                if have:
                    hdrs["Range"] = "bytes=%d-" % have
                st = {"f": None, "done": have, "total": 0, "status": 0}

                def on_headers(status, rh, st=st, have=have):
                    st["status"] = status
                    if status == 206:
                        cr = str(rh.get("content-range", ""))          # bytes 100-299/300
                        try:
                            begin = int(cr.split()[1].split("-")[0])
                            st["total"] = int(cr.rsplit("/", 1)[-1])
                        except (ValueError, IndexError):
                            begin = -1
                        if begin != have:
                            return None                                 # not the bytes we asked for
                        st["f"] = open(dest, "ab")
                    elif status == 200:
                        try:
                            st["total"] = int(rh.get("content-length") or 0)
                        except ValueError:
                            pass
                        st["done"] = 0
                        st["f"] = open(dest, "wb")                      # the server ignored Range
                    else:
                        return None

                    def sink(chunk):
                        st["f"].write(chunk)
                        st["done"] += len(chunk)
                        tick(st["done"], st["total"] or expect_size)
                    return sink

                try:
                    status, rh, small, _final = _follow(hop, lane, url, hdrs, timeout, on_headers,
                                                        https_only, use_proxy)
                finally:
                    if st["f"]:
                        st["f"].close()
                if status == 416:
                    total = 0
                    try:
                        total = int(str(rh.get("content-range", "")).rsplit("/", 1)[-1])
                    except ValueError:
                        pass
                    if total and total == have:
                        return {"bytes": have, "total": total, "lane": lane}
                    _rm(dest)
                    continue
                if status == 206 and st["f"] is None:
                    _rm(dest)                                     # a range we did not ask for
                    continue
                if not (200 <= status < 300):
                    raise _http_error(status, rh, small, lane)
                got = os.path.getsize(dest)
                total = st["total"] or got
                if got < total:
                    raise NetError("connect", "%s: the transfer ended at %d of %d bytes" % (host, got, total), lane)
                return {"bytes": got, "total": total, "lane": lane}
            except NetError as e:
                now_have = os.path.getsize(dest) if os.path.isfile(dest) else 0
                # A DROP THAT MADE PROGRESS IS NOT A FAILED LANE. Pick it up where it stopped.
                # A stall gets two more turns and a drop four; after that the lane has had its chance.
                if e.kind in ("connect", "timeout") and now_have > have and drops < (2 if e.kind == "timeout" else 4):
                    drops += 1
                    continue
                raise

    res, tried = _run(host, lanes, attempt)
    res["resumed_from"] = start0
    res["tried"] = tried
    if expect_size and res["bytes"] != expect_size:
        _rm(dest)                        # more than the release says: not a file a resume can mend
        raise NetError("connect", "the download was %d bytes and should be %d" % (res["bytes"], expect_size), res["lane"])
    _rm(keyf)
    tick(res["bytes"], res["total"], force=True)
    return res


def net_report():
    """This PC's road out, in one dict, for the log line at start and for /api/health.

    Touches no network. It is what lets a failure on somebody else's PC be read from here: how many
    certificates Windows had, whether the build carries its own list, what proxy would be used.
    """
    tls_context()
    try:
        px = urllib.request.getproxies()
    except Exception:
        px = {}
    return {"trust": _ctx_cache["stats"], "openssl": ssl.OPENSSL_VERSION,
            "bundled_roots": bool(certifi_path()), "curl": bool(curl_exe()),
            "winhttp": not _wh["failed"], "proxy": sorted(px.keys()),
            "sticky": dict((h, v[0]) for h, v in _sticky.items())}


# What the page prints for each code (English; the page owns the fifteen translations).
NET_SENTENCES = {
    "dns": "This PC could not look up the address of {host} - check the internet connection",
    "connect": "This PC could not connect to {host} - check the internet connection, or a firewall or proxy on this PC",
    "timeout": "{host} did not answer in time - try again",
    "tls-trust": "This PC does not trust the certificate {host} presented - something on this PC or network is reading secure connections",
    "tls-other": "A secure connection to {host} could not be set up",
    "clock": "This PC's date and time are wrong, so secure connections are refused - set the clock and try again",
    "rate-limited": "GitHub is rate-limiting these checks - try again in a few minutes",
    "http-404": "{host} has no such file - the project may have moved it",
    "http": "{host} answered with an error - try again later",
}


def sentence(kind, host=""):
    """The English sentence for a NetError kind, with the host it is about."""
    k = str(kind or "")
    text = NET_SENTENCES.get(k)
    if text is None:
        text = NET_SENTENCES["http"] if k.startswith("http-") else (
            "%s could not be reached" % (host or "That address"))
    return text.replace("{host}", host or "that address")


def host_of(url):
    try:
        return urllib.parse.urlsplit(str(url or "")).hostname or ""
    except Exception:
        return ""
