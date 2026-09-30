# -*- coding: utf-8 -*-
"""
WMP & Zune Find Album Info (FAI) Metadata Server 2.0 (High Performance Edition)
10x Speed, Accuracy, Multi-Disc, CD TOC, Caching & Resilience
"""
import os
import sys
import uuid
import html
import ssl
import subprocess
import threading
import warnings
import hashlib
import time
import json
import re
import base64
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from flask import Flask, request, Response, redirect, url_for, render_template_string, jsonify
from urllib3.exceptions import InsecureRequestWarning

warnings.simplefilter('ignore', InsecureRequestWarning)

# ==========================================================
# CONFIGURATION & CONSTANTS
# ==========================================================
HOST = "0.0.0.0"
HTTP_PORT = 80
HTTPS_PORT = 443
BROWSER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MUSICBRAINZ_USER_AGENT = "WindowsMediaPlayerFAI/2.0 ( contact@wmpfai.local )"
MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2/"

APP_DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WMP_FAIServer')
os.makedirs(APP_DATA_DIR, exist_ok=True)
CERT_FILE = os.path.join(APP_DATA_DIR, 'cert.pem')
KEY_FILE = os.path.join(APP_DATA_DIR, 'key.pem')

# Persistent HTTP session with connection pooling
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=2)
session.mount('https://', adapter)
session.mount('http://', adapter)

# ---- MusicBrainz rate limiting -------------------------------------------------
# MusicBrainz allows ONE request per second per client and answers everything
# above that with '503 Service Temporarily Unavailable'. A single search here
# fires up to seven MusicBrainz calls at once (the release search, three exact
# counts, and the artist/recording view), so the burst was reliably throttled:
# every extra call came back 503, the 'status_code != 200' branches returned an
# empty list SILENTLY, and the dialog showed iTunes results with no MusicBrainz
# rows and no error anywhere. Verified: 'artist:"pink floyd" AND (the OR wall)'
# returns 1291 releases from MusicBrainz, but the dialog showed zero.
#
# Every MusicBrainz call now goes through _mb_get(), which serialises them behind
# a lock, keeps at least _MB_MIN_INTERVAL between calls, and retries 503 with a
# backoff. Throttling costs a few seconds on a cold query and is free on a warm
# one, because results are cached.
_MB_MIN_INTERVAL = 1.05      # seconds; the published limit is 1/sec
_MB_LOCK = threading.Lock()
_MB_LAST_CALL = [0.0]
_MB_THROTTLED = [0]          # how many times we have had to back off (diagnostics)
# Exact release totals, keyed by the Lucene query that produced them. The search
# response carries 'count' for free, so count_search_totals() reuses it instead
# of spending another rate-limited request on a number we already have.
_MB_RELEASE_TOTAL = {}


def _mb_wait_turn():
    """Block until it is this thread's turn to hit MusicBrainz."""
    with _MB_LOCK:
        now = time.time()
        gap = _MB_MIN_INTERVAL - (now - _MB_LAST_CALL[0])
        if gap > 0:
            time.sleep(gap)
        _MB_LAST_CALL[0] = time.time()


def _mb_get(url, params=None, timeout=8, attempts=3):
    """Rate-limited MusicBrainz GET that survives 503 throttling.

    Returns the response, or None if every attempt was throttled or failed.
    """
    delay = 1.6
    for attempt in range(1, attempts + 1):
        _mb_wait_turn()
        try:
            resp = session.get(url, params=params,
                               headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
                               timeout=timeout)
        except Exception as e:
            print(f"[MUSICBRAINZ] {attempt}/{attempts} transport error: {e}")
            time.sleep(delay)
            delay *= 1.6
            continue
        if resp.status_code == 200:
            return resp
        if resp.status_code == 503:
            _MB_THROTTLED[0] += 1
            print(f"[MUSICBRAINZ] throttled (503) on attempt {attempt}/{attempts}, "
                  f"retrying in {delay:.1f}s: {params.get('query') if params else url}")
            time.sleep(delay)
            delay *= 1.6
            continue
        print(f"[MUSICBRAINZ] HTTP {resp.status_code} for "
              f"{params.get('query') if params else url}")
        return None
    return None


app = Flask(__name__)

# ==========================================================
# THREAD-SAFE TTL CACHE
# ==========================================================
class TTLCache:
    def __init__(self, default_ttl=3600):
        self._cache = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl

    def get(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            val, exp = entry
            if time.time() > exp:
                del self._cache[key]
                return None
            return val

    def set(self, key, val, ttl=None):
        if ttl is None:
            ttl = self.default_ttl
        with self._lock:
            if len(self._cache) > 2000:
                now = time.time()
                expired = [k for k, (_, exp) in self._cache.items() if now > exp]
                for k in expired:
                    del self._cache[k]
                if len(self._cache) > 2000:
                    for k in list(self._cache.keys())[:500]:
                        del self._cache[k]
            self._cache[key] = (val, time.time() + ttl)

    def clear(self):
        with self._lock:
            self._cache.clear()

search_cache = TTLCache(default_ttl=7200)       # 2 hours
album_cache = TTLCache(default_ttl=86400)       # 24 hours
image_cache = TTLCache(default_ttl=86400)       # 24 hours

# ==========================================================
# FILE LOGGING (every request + key events -> fai_server.log)
# ==========================================================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fai_server.log")
# In a PyInstaller ONE-FILE build __file__ points into the self-extracting temp
# directory (%TEMP%\_MEIxxxxxx), which is deleted when the process exits - so a
# log written there is unfindable and gone. The frozen build therefore logs into
# APP_DATA_DIR, the same place the TLS certificate already lives, and that is
# the path the README tells users to look at. Unfrozen runs keep the historical
# behaviour of logging next to the source file.
if getattr(sys, "frozen", False):
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        LOG_FILE = os.path.join(APP_DATA_DIR, "fai_server.log")
    except OSError:
        pass
LOG_LOCK = threading.Lock()
LOG_MAX_BYTES = 5 * 1024 * 1024

def log_line(tag, msg):
    """Append one timestamped line to fai_server.log (self-rotating)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{tag}] {msg}\n"
    try:
        with LOG_LOCK:
            try:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass

def parse_wmp_toc(toc_string):
    """Decode WMP's 'cd=' TOC into real per-track lengths.

    WMP sends the disc table of contents as
        <drive>+<firsttrack>+<offset1>+...+<offsetN>+<leadout>
    with every offset a 4-6 digit HEX LBA. The first offset is the lead-in
    (0x96 = 150 frames = the standard 2-second pre-gap), and the last value is
    the lead-out, so a 10-track disc carries 11 offsets.

    This is the one piece of disc information that is available even when the
    MDQ is empty: track count and running time are properties of the PHYSICAL
    disc, read from its TOC, not tags. The 'Existing Information' panel showed
    "No existing information" for such a disc while holding enough data to say
    "10 audio tracks, 37:52 total" - which is exactly what the user sees in
    WMP behind the dialog.

    Returns a list of track durations in milliseconds, or [] if the TOC is not
    in the expected shape. Never raises: a malformed TOC must not break the
    dialog.
    """
    try:
        parts = [p for p in re.split(r"[+ ]", (toc_string or "").strip()) if p]
        # Need at least drive + first + one offset + lead-out.
        if len(parts) < 4:
            return []
        frames = [int(p, 16) for p in parts[1:]]
    except (ValueError, TypeError):
        return []
    if len(frames) < 2:
        return []
    # Frames must increase; a non-monotonic TOC is not a real disc layout.
    if any(frames[i] >= frames[i + 1] for i in range(len(frames) - 1)):
        return []
    out = []
    for i in range(len(frames) - 1):
        # Round to the nearest SECOND, not truncate: WMP displays whole
        # seconds, and a track whose length is 3:02.93 is shown as 3:03.
        # Truncating reported 3:02 for a track WMP calls 3:03, which reads as
        # a different disc. Verified: rounding reproduces all ten of the
        # Course of Nature track lengths exactly.
        out.append(int(round((frames[i + 1] - frames[i]) / 75.0)) * 1000)
    return out


def format_duration(ms):
    """Milliseconds -> 'M:SS' (or 'H:MM:SS' past an hour)."""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def raw_query_arg(name, default=""):
    """Read a query-string value without turning a literal '+' into a space.

    Werkzeug's request.args decodes '+' as space (form encoding), but WMP CD
    TOC signatures always start with a literal '+' (e.g. '+hAhAAQBAAMAA...').
    Using request.args on such a value corrupts it, and WriteNamesEx then
    receives a TOC that does not match the disc, so WMP silently ignores it.
    """
    try:
        qs = request.query_string.decode("utf-8", "replace")
    except Exception:
        return default
    for part in qs.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        if k == name:
            return requests.utils.unquote(v)
    return default


@app.after_request
def _log_request(response):
    """Log every request (raw query string preserved) to fai_server.log."""
    try:
        qs = request.query_string.decode("utf-8", "replace")
        ua = request.headers.get("User-Agent", "")
        log_line("REQ", f"{request.method} {request.path}"
                        f"{'?' + qs if qs else ''} -> {response.status_code}"
                        f" | UA={ua[:100]}")
    except Exception:
        pass
    return response


@app.route("/client_error", methods=["POST", "GET"])
def client_error():
    """Beacon endpoint: the dialog page reports JS/COM outcomes from inside WMP."""
    payload = request.get_json(silent=True)
    if not payload:
        payload = request.form.to_dict() if request.form else dict(request.args)
    log_line("CLIENT", json.dumps(payload, ensure_ascii=False)[:3000])
    return Response("OK", mimetype="text/plain")


# ---- Discogs (OPTIONAL third provider) ---------------------------------------
# Discogs needs a personal access token, so it is OFF unless one is configured.
# The token is read from the environment or from local_settings.py, which is
# git-ignored - it is never written into this file, which is committed to a
# PUBLIC repository. A token pasted into tracked source is a leaked credential.
#
#   PowerShell:  $env:DISCOGS_TOKEN = 'your-token'   (then start the server)
#   or create local_settings.py containing:  DISCOGS_TOKEN = 'your-token'
#
# Without a token every Discogs code path returns empty and the dialog behaves
# exactly as it did before Discogs existed - that is the whole point of gating
# it, so a missing credential can never break search.
DISCOGS_BASE_URL = "https://api.discogs.com/"
# Discogs asks for a descriptive User-Agent identifying the app and a contact
# URL. It deliberately does NOT reuse the MusicBrainz User-Agent constant: that
# constant is the guard for 'every MusicBrainz call goes through _mb_get()', and
# spending it on a non-MusicBrainz request would both misidentify the client and
# blur that guard.
DISCOGS_USER_AGENT = "WMPFaiServer/2.0 +https://github.com/jns630/wmp-fai-server"
DISCOGS_TOKEN = ""
try:
    import local_settings  # git-ignored
    DISCOGS_TOKEN = (getattr(local_settings, "DISCOGS_TOKEN", "") or "").strip()
except Exception:
    pass
DISCOGS_TOKEN = (os.environ.get("DISCOGS_TOKEN") or DISCOGS_TOKEN or "").strip()
# Authenticated Discogs allows 60 requests/minute. A single search here fires one
# Discogs call, so the limiter only matters across consecutive cold searches,
# but an unthrottled burst gets 429s and the provider silently disappears from
# the list - the same failure mode as the MusicBrainz 503s above.
_DG_MIN_INTERVAL = 1.1
_DG_LOCK = threading.Lock()
_DG_LAST_CALL = [0.0]


def _discogs_configured():
    return bool(DISCOGS_TOKEN)


def _discogs_get(path, params=None, timeout=8, attempts=3):
    """Rate-limited Discogs GET. Returns the response, or None.

    Returns None - never raises - when Discogs is unconfigured, throttled or
    failing. A provider that is absent must not be able to fail a search.
    """
    if not _discogs_configured():
        return None
    url = path if path.startswith("http") else DISCOGS_BASE_URL + path.lstrip("/")
    delay = 1.5
    for attempt in range(1, attempts + 1):
        with _DG_LOCK:
            gap = _DG_MIN_INTERVAL - (time.time() - _DG_LAST_CALL[0])
            if gap > 0:
                time.sleep(gap)
            _DG_LAST_CALL[0] = time.time()
        try:
            resp = session.get(
                url, params=params, timeout=timeout,
                headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}",
                         "User-Agent": DISCOGS_USER_AGENT,
                         "Accept": "application/json"})
        except Exception as e:
            print(f"[DISCOGS] {attempt}/{attempts} transport error: {e}")
            time.sleep(delay)
            delay *= 1.6
            continue
        if resp.status_code == 200:
            return resp
        if resp.status_code in (429, 503):
            print(f"[DISCOGS] throttled ({resp.status_code}) on attempt "
                  f"{attempt}/{attempts}, retrying in {delay:.1f}s")
            time.sleep(delay)
            delay *= 1.6
            continue
        # 401 means the token is bad or revoked. Retrying cannot fix that, and
        # saying so once is far more useful than silently returning no results.
        if resp.status_code == 401:
            print("[DISCOGS] 401 Unauthorized - DISCOGS_TOKEN is invalid or "
                  "revoked; Discogs results are disabled until it is fixed")
        else:
            print(f"[DISCOGS] HTTP {resp.status_code} for {url}")
        return None
    return None


# ==========================================================
# GLOBAL STATE & NAVIGATION TRACKING
# ==========================================================
# Returned when no metadata has been staged for the disc/collection WMP is
# asking about. It carries no album and no tracks, so WMP leaves the album
# exactly as it is instead of applying whatever was staged most recently.
EMPTY_METADATA_XML = (
    '<METADATA>'
    '<version>5.0</version>'
    '<status>NOTFOUND</status>'
    '<MDR-CD>'
    '<version>5.0</version>'
    '</MDR-CD>'
    '</METADATA>'
)

LAST_XML = None   # last staged document (diagnostics only - never served blindly)
# Reentrant: store_staged_xml() holds this while calling _stage_request_xml(),
# which takes it again to record the pending document. A plain Lock would
# deadlock there and no XML would ever be staged.
XML_LOCK = threading.RLock()
# Staged XML keyed by WMP's requestid / CD TOC so the background delivery
# endpoint can echo the exact XML belonging to the current dialog session.
STAGED_REQUESTS = {}
# When each key above was staged, so any fallback can be time-bounded.
STAGED_AT = {}
# The most recently staged document. WMP's LIBRARY write is a separate request
# carrying a FRESHLY GENERATED request id, so it matches nothing in
# STAGED_REQUESTS; this is what answers it. Used only for that specific call
# shape - see _lookup_staged_xml().
PENDING_WRITE = {'xml': '', 'at': 0.0, 'wmid': ''}
# Fresh library-write ids we have answered, mapped to the document they were
# answered with. Kept separate from STAGED_REQUESTS because such a pairing is
# only valid for the document that was pending at the time: pinning one in
# STAGED_REQUESTS made a LATER, different album resolve as an exact match and
# serve the PREVIOUS album's tags. Cleared whenever a new document is staged.
FRESH_WRITES = {}
# How long a staged document may still answer a library write. Generous enough
# to cover WMP finishing the dialog and committing the write, short enough that
# a disc swapped much later cannot pick up the previous album.
_LIBRARY_WRITE_WINDOW = 180.0
LAST_TOC = ""   # most recent CD TOC seen (survives POST-only TOC submissions)
# WMP identifies a library collection by its WMID. WMP sends it as ?wmid=...
# (e.g. /cdinfo/GetMDRCD.aspx?...&wmid=5FA05D35-...) when it fetches metadata
# for an album it already knows. Reusing that exact GUID as the album's
# WMCollectionID is what makes the returned document line up with WMP's own
# library entry instead of an unrelated generated one.
LAST_WMID = ""

# Counts staged documents, so the artwork URL can be made unique per APPLY.
# See the token in build_wmp_xml(): WMP will not re-fetch a cover URL it has
# already seen for a collection, so a stable per-album token means "re-apply to
# fix the art" can never work. Logged 2026-09-29 15:52: the re-apply of
# Prospekt's March produced a byte-identical URL to 15:43's and there was no
# [IMAGE] line at all in that session.
_COVER_SEQ = 0

# How the artwork URL is presented to WMP: "proxy" (our own /cover/ endpoint) or
# "direct" (the upstream image URL, verbatim).
#
# This is THE artwork fix, and it was available long before it was made. Counting
# /cover/ requests by user agent over the whole log:
#
#   WMP cover fetches, all time
#     via our proxy : 62      attachments: 0
#     direct        :  0      (never tried)
#
# 62 fetches, 0 attachments, against an alternative that had never been attempted.
# That asymmetry is a conclusion, not a hypothesis. It was available at the point
# the counts were taken and three further commits went somewhere else first.
#
# Why the proxy failed is not established - only that it never once worked. What is
# known is that it put a loopback http:// URL inside a document WMP fetches over
# https from musicmatch-ssl.xboxlive.com, and that it existed only to work around a
# quote() bug (escaping the slashes, so the proxy received 'https%3A%2F%2F...' and
# 404'd) that had already been fixed. Nothing needed it afterwards.
#
# The proxy is kept as a fallback in case an upstream host is found to refuse WMP.
# It should not be reinstated as the default on the strength of a theory.
_ART_MODE = "direct"

def _remember_wmid(value):
    """Record the most recent wmid WMP asked us about."""
    global LAST_WMID
    val = str(value or "").strip().strip('{}')
    if val and re.match(r'^[0-9A-Fa-f-]{8,}$', val, re.IGNORECASE):
        if val != LAST_WMID:
            log_line("WMID", f"captured {val}")
        LAST_WMID = val
        return val
    return ""

def _request_names_a_disc():
    """True when WMP is asking about a specific physical disc.

    Used by the delivery path only to decide what to SAY in the log, and by the
    search page to tell a disc from a library update. It deliberately does NOT
    suppress retargeting: a request carrying both ?cd= and ?wmid= is WMP naming
    the collection it made for that disc, and the document must describe that
    collection or the album-level fields - including where the cover attaches -
    are keyed to one WMP is not tracking here.

    ============================ ARTWORK: WHAT IS ACTUALLY KNOWN =============
    Artwork for a re-ripped disc needed two separate fixes, neither of which
    was about the collection id:

      1. The cover was not being RE-FETCHED. WMP had the URL on file and the
         value never changed. A stable per-album token in the cover URL path
         fixed that - confirmed on the Prospekt disc, fetched again at 15:43:43
         and 15:43:45 (170405B) despite the collection already existing.
      2. For a period the whole document was not well-formed XML (a bare '&'
         from that token), so WMP rejected the response outright.

    First apply to a fresh disc fetched the cover twice. Re-applies did not
    fetch at all until the token was added.
    """
    for name in ('toc', 'TOC', 'mdq', 'cd', 'CD'):
        if raw_query_arg(name).strip():
            return True
    return False


def _stage_request_xml(xml, request_id="", toc="", wmid="", cd="",
                       claim_wmid=""):
    """Index staged XML by WMP request id / CD TOC / WMID / disc id (bounded FIFO).

    'cd' is WMP's real-CD-flow identifier (?cd=<hex>+<hex>...); 'wmid' is the
    library collection GUID. WMP looks the document up by whichever it knows.

    'wmid' and 'claim_wmid' are deliberately different. Indexing under a wmid
    only makes THIS document answer for that collection. Pre-claiming
    (PENDING_WRITE['wmid']) additionally stops any other collection from ever
    claiming the pending document, so it is reserved for a wmid WMP really put
    in the dialog's URL - never for a LAST_WMID fallback guess.
    """
    keys = [str(request_id).strip(), str(toc).strip(),
            str(wmid).strip(), str(cd).strip()]
    now = time.time()
    for k in keys:
        if k:
            STAGED_REQUESTS[k] = xml
            STAGED_AT[k] = now
    # Remember the document staged most recently, and when. WMP writes tags to
    # the library in a SEPARATE request carrying a freshly generated request id,
    # so this is the only thing that can answer it - see _lookup_staged_xml().
    # Callers may already hold XML_LOCK, which is why it is an RLock.
    prev_xml = PENDING_WRITE.get('xml')
    PENDING_WRITE['xml'] = xml
    PENDING_WRITE['at'] = now
    # Which collection this document is committed to. Empty means UNCLAIMED,
    # and the FIRST wmid that fetches this document claims it.
    #
    # Only an id the dialog itself was opened with pre-claims it
    # (claim_wmid). The write target is NOT enough: it may be the LAST_WMID
    # fallback, and pre-binding to a guess would stop a genuinely different
    # collection from ever claiming this document - which is the bug this whole
    # path exists to fix.
    if prev_xml is not xml:
        PENDING_WRITE['wmid'] = ''
    if claim_wmid:
        PENDING_WRITE['wmid'] = claim_wmid
    # A fresh library-write id we remembered was paired with the PREVIOUS
    # document, so it must not survive this staging.
    FRESH_WRITES.clear()
    if len(STAGED_REQUESTS) > 32:
        for old_key in list(STAGED_REQUESTS.keys())[: len(STAGED_REQUESTS) - 32]:
            STAGED_REQUESTS.pop(old_key, None)
            STAGED_AT.pop(old_key, None)

def _lookup_staged_xml():
    """Find staged XML matching the current requestid/TOC/wmid/cd.

    Two ways WMP asks, and they must both be answered:

    1. The BACKGROUND fetch, which echoes the dialog's own request id
       (?requestid=4E6825F4-...). Matches STAGED_REQUESTS directly.
    2. The LIBRARY write. WMP POSTs /cdinfo/GetMDRCD.aspx with a FRESHLY
       GENERATED request id that we have never seen - logged from a real
       session as staging under 23FDCD4D-... and then writing with
       E7A89714-.... It matched nothing, so the library was served
       <status>NOTFOUND</status> and the tracks kept their old tags.

    For (2) we fall back to the most recently staged document, but ONLY when
    the call has no disc identifier of its own. A request that names a ?cd=,
    ?toc= or ?wmid= is asking about a specific disc or collection, so an
    unmatched one still gets empty metadata and is left alone. That guard is
    what stops this reintroducing the bug where every disc was written with
    whichever album was applied last.
    """
    candidates = []
    for name in ('requestid', 'requestID'):
        val = request.args.get(name, '').strip()
        if val:
            candidates.append(val)
    # wmid is how WMP identifies the library collection it wants updated.
    # Keep the RAW value too: _remember_wmid() only accepts GUID-shaped values,
    # so gating the fallback on the validated one would let a wmid-bearing
    # request slip through as 'no wmid' whenever the value is not a GUID.
    wmid_raw = raw_query_arg('wmid').strip()
    wmid = _remember_wmid(wmid_raw)
    if wmid:
        candidates.append(wmid)
    # TOC/MDQ must be read raw: Werkzeug turns a literal '+' into a space and
    # WMP CD TOCs always start with '+'.
    disc_ids = []
    for name in ('toc', 'TOC', 'mdq'):
        val = raw_query_arg(name).strip()
        if val:
            candidates.append(val)
            disc_ids.append(val)
    # WMP's real CD flow fetches by ?cd=<hex>+<hex>... - the same disc id the
    # dialog was opened with, so staged XML resolves to it too.
    for name in ('cd', 'CD'):
        val = raw_query_arg(name).strip()
        if val:
            candidates.append(val)
            disc_ids.append(val)
    # ALWAYS scan the POST body, not just when the query had nothing. The query
    # carries WMP's fresh request id, but the body may repeat the original
    # mdqRequestID we did stage under, which is an exact match.
    if request.method == "POST":
        post_body = request.data.decode("utf-8", errors="ignore")
        for name in ('requestid', 'requestID', 'toc', 'mdq', 'cd', 'wmid'):
            match = re.search(name + r'=([^&"\s]+)', post_body, re.IGNORECASE)
            if match:
                candidates.append(requests.utils.unquote(match.group(1)))
        log_line("MDR-BODY", f"body={post_body[:200]!r} ids={candidates[:4]}")

    with XML_LOCK:
        for cand in candidates:
            xml = STAGED_REQUESTS.get(cand)
            if xml:
                return xml
        # A fresh library-write id we already answered, for the same document.
        for cand in candidates:
            xml = FRESH_WRITES.get(cand)
            if xml:
                return xml

        # No exact match, and the request names no DISC of its own.
        #
        # Two shapes need the pending document, and both were observed failing
        # against real WMP:
        #
        #  a) a library WRITE: POST /cdinfo/GetMDRCD.aspx with a freshly
        #     generated requestID, naming neither a disc nor a collection.
        #  b) WMP's own collection fetch: GET .../GetMDRCD.aspx?wmid=<GUID>.
        #     On the very first run of a session WMP reveals the collection GUID
        #     for the FIRST TIME in this request, moments after the dialog
        #     closed - so it could not have been known at staging time, the
        #     document was not bound to it, and the fetch was answered empty:
        #
        #       [STAGED] album='Clocks' req_id='4934E449-...'
        #       WRITE=WriteNamesEx-mdq-tagsonly-ok
        #       [WMID] captured F62C9D85-...
        #       [MDR-EMPTY] no metadata staged for 'F62C9D85-...'
        #
        # A wmid is claimed at most once by the pending document. Binding it
        # makes every later fetch for that collection an exact lookup, while a
        # DIFFERENT collection still gets nothing - which is the guard that
        # stops every disc being written with whichever album was last applied.
        if not disc_ids and 'getmdrcd' in request.path.lower():
            pending = PENDING_WRITE.get('xml') or ''
            age = time.time() - (PENDING_WRITE.get('at') or 0.0)
            bound = (PENDING_WRITE.get('wmid') or '').strip()
            # A collection fetch is a GET in every real session logged. A POST
            # that names a wmid is a different shape - it already carries its own
            # requestID and its body ids were scanned above - so it is left alone.
            claimable = (request.method == "GET" and wmid_raw
                         and (not bound or bound == wmid_raw))
            if pending and age <= _LIBRARY_WRITE_WINDOW and (
                    (request.method == "POST" and not wmid_raw) or claimable):
                if wmid_raw:
                    # Bind it, so repeats are exact rather than fallbacks.
                    PENDING_WRITE['wmid'] = wmid_raw
                    STAGED_REQUESTS[wmid_raw] = pending
                    STAGED_AT[wmid_raw] = time.time()
                    log_line("MDR-FALLBACK", f"bound pending doc to collection "
                                              f"{wmid_raw} (age={age:.1f}s)")
                    return pending
                log_line("MDR-FALLBACK", f"serving staged doc for fresh "
                                          f"library write (age={age:.1f}s)")
                for cand in candidates[:1]:
                    FRESH_WRITES[cand] = pending
                return pending
            if pending and age > _LIBRARY_WRITE_WINDOW:
                log_line("MDR-STALE", f"staged doc is {age:.0f}s old, "
                                      f"not serving it for a library write")

    if candidates:
        log_line("MDR-EMPTY", f"no metadata staged for {candidates[0][:40]!r} "
                              f"- returning empty (discs left untouched)")
    return None

FAI_NAVIGATION = {
    'current_step': {},
    'session_history': {},
    'dialog_state': {},
    'stats': {
        'total_searches': 0,
        'itunes_hits': 0,
        'musicbrainz_hits': 0,
        'xml_deliveries': 0,
        'images_served': 0
    }
}
NAV_LOCK = threading.Lock()

def esc(s):
    if not s:
        return ""
    return html.escape(str(s), quote=True)

def xesc(s):
    """Escape a value for use inside the WMP XML document.

    esc() is for HTML: it encodes an apostrophe as '&#x27;', which is a
    valid HTML entity but NOT one WMP understands in XML - it would tag the
    album literally as "Prospekt&#x27;s March". XML only recognises
    &amp; &lt; &gt; &quot; &apos;.
    """
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))

def guid(v):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(v))).upper()

def get_session_id():
    session_data = "{}_{}_{}_{}".format(
        request.args.get('requestid', ''),
        request.args.get('artist', ''),
        request.args.get('album', ''),
        int(time.time())
    )
    return hashlib.md5(session_data.encode()).hexdigest()[:16]

def track_fai_navigation(session_id, step, data=None):
    with NAV_LOCK:
        if session_id not in FAI_NAVIGATION['current_step']:
            FAI_NAVIGATION['current_step'][session_id] = 'start'
            FAI_NAVIGATION['session_history'][session_id] = []
            FAI_NAVIGATION['dialog_state'][session_id] = {}
        
        FAI_NAVIGATION['current_step'][session_id] = step
        FAI_NAVIGATION['session_history'][session_id].append(step)
        if data:
            FAI_NAVIGATION['dialog_state'][session_id].update(data)
    
    print(f"[FAI NAVIGATION] Session {session_id}: {step}")
    log_line("NAV", f"session={session_id} step={step} data={json.dumps(data, ensure_ascii=False) if data else ''}")
# ==========================================================
# METADATA PROVIDER CLIENTS (ITUNES & MUSICBRAINZ)
# ==========================================================
def search_itunes(query, limit=20):
    cache_key = f"itunes_search_{query.lower().strip()}_{limit}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    results = []
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": query,
            "entity": "album",
            "limit": limit,
            "country": "US"
        }
        resp = session.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            for item in resp.json().get("results", []):
                cid = item.get("collectionId")
                if not cid:
                    continue
                art100 = item.get("artworkUrl100", item.get("artworkUrl60", ""))
                art600 = art100.replace("100x100bb", "600x600bb") if art100 else ""
                results.append({
                    "id": str(cid),
                    "source": "itunes",
                    "title": item.get("collectionName", "Unknown Album"),
                    "artist": item.get("artistName", "Unknown Artist"),
                    "year": (item.get("releaseDate") or "")[:4],
                    "genre": item.get("primaryGenreName", "Rock"),
                    "track_count": item.get("trackCount", 0),
                    "art_thumb": art100 or item.get("artworkUrl60", ""),
                    "art_full": art600
                })
    except Exception as e:
        print(f"[ITUNES] Search error: {e}")

    search_cache.set(cache_key, results, ttl=3600)
    return results

def get_itunes_album_details(collection_id):
    cache_key = f"itunes_album_{collection_id}"
    cached = album_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        url = f"https://itunes.apple.com/lookup?id={collection_id}&entity=song"
        resp = session.get(url, timeout=6)
        if resp.status_code != 200:
            return None
        items = resp.json().get("results", [])
        if not items:
            return None

        album_info = items[0]
        album_artist = album_info.get("collectionArtistName") or album_info.get("artistName", "Unknown Artist")
        album_title = album_info.get("collectionName", "Unknown Album")
        album_genre = album_info.get("primaryGenreName", "Rock")
        album_year = (album_info.get("releaseDate") or "2000")[:4]

        art100 = album_info.get("artworkUrl100", "")
        art_hi = art100.replace("100x100bb", "600x600bb") if art100 else ""

        tracks = []
        for item in items[1:]:
            if item.get("wrapperType") == "track":
                t_artist = item.get("artistName", album_artist)
                # iTunes' composerName is present on the wire but empirically
                # always null (verified: 0 of 26 tracks on The Wall, and 0 on
                # every other album tried). It is deprecated in the storefront
                # API. Do NOT fall back to the artist - that writes a false
                # credit into the user's library. Leave it empty instead.
                t_composer = (item.get("composerName") or "").strip()
                tracks.append({
                    "id": str(item.get("trackId")),
                    "name": item.get("trackName", "Unknown Track"),
                    "number": item.get("trackNumber", 0),
                    "disc": item.get("discNumber", 1),
                    "artist": t_artist,
                    "performer": t_artist,
                    "composer": t_composer,
                    "duration_ms": item.get("trackTimeMillis", 0),
                    "genre": item.get("primaryGenreName", album_genre)
                })

        details = {
            "id": str(collection_id),
            "source": "itunes",
            "title": album_title,
            "artist": album_artist,
            "genre": album_genre,
            "year": album_year,
            "art_url": art_hi,
            "tracks": tracks
        }
        album_cache.set(cache_key, details, ttl=86400)
        return details
    except Exception as e:
        print(f"[ITUNES] Details error for {collection_id}: {e}")
        return None

def _build_lucene_query(query, artist_hint=None, album_hint=None):
    """Build the Lucene query used for BOTH the search and the count.

    These two MUST stay identical or the headline number is meaningless. A
    loose 'kind of blue miles' is an OR over every word and matches 523,001
    releases in MusicBrainz; the scoped 'artist:"kind of" AND release:"blue
    miles"' matches 0, and a realistic 'artist:"pink floyd" AND
    release:"the wall"' matches 154. Counting the loose form advertised over
    half a million albums for a single album search.
    """
    if artist_hint and album_hint:
        clean_art = re.sub(r'[\'"+]', ' ', artist_hint).strip()
        clean_alb = re.sub(r'[\'"+]', ' ', album_hint).strip()
        if clean_art and clean_alb:
            # The album half is matched as loose OR terms, not as one exact
            # phrase. A quoted phrase requires the release title to contain
            # that exact wording, so 'release:"ghost story"' missed Coldplay's
            # 'Ghost Stories' entirely and the whole scoped query returned 0 -
            # which is why no MusicBrainz albums surfaced at all. The artist
            # stays an exact phrase (it is the reliable half) and the album
            # terms are OR'd so singular/plural and minor wording differences
            # still match. Measured: 'coldplay ghost story' 0 -> 24 releases.
            terms = [t for t in clean_alb.split() if t]
            if terms:
                album_clause = "(" + " OR ".join(terms) + ")"
                return f'artist:"{clean_art}" AND {album_clause}'
            return f'artist:"{clean_art}" AND release:"{clean_alb}"'
    return re.sub(r'[\'"+]', ' ', query or "").strip()


def _build_entity_queries(query, artist_hint=None, album_hint=None):
    """Per-entity Lucene queries for the three counts.

    The album count must mirror the RELEASE search exactly (see
    _build_lucene_query). The artist and track counts cannot reuse that string:
    'artist:"X" AND release:"Y"' is a release-scoped clause, and asking the
    artist or recording endpoint with it returns 0, because those entities have
    no 'release' field. They are therefore scoped to the artist plus the album
    TERMS, which is the honest question: how many artists / recordings match
    the artist and title words of what you typed.
    """
    base = _build_lucene_query(query, artist_hint, album_hint)
    loose = re.sub(r'[\'"+]', ' ', query or "").strip()
    artist_q = loose
    rec_q = loose
    if artist_hint and album_hint:
        ca = re.sub(r'[\'"+]', ' ', artist_hint).strip()
        cb = re.sub(r'[\'"+]', ' ', album_hint).strip()
        terms = [t for t in cb.split() if t]
        if ca:
            artist_q = f'artist:"{ca}"'
            # Keep the album words for the recording search too. Scoping to the
            # artist alone returned Coldplay's entire 3,008-track catalogue
            # instead of the Ghost Stories material the user searched for.
            rec_q = artist_q + ((" AND (" + " OR ".join(terms) + ")") if terms else "")
    return {"release": base, "recording": rec_q, "artist": artist_q}


def _interleave_sources(groups):
    """Round-robin merge of any number of (source_name, rows) groups.

    The album list used to be 'all iTunes, then all MusicBrainz'. Once paging
    was added that was actively harmful: iTunes returned 40 rows for a broad
    query, filled the whole 40-row window, and MusicBrainz was pushed onto page
    2 or beyond - so a first page could contain no MusicBrainz rows at all
    even though albums_shown counted them.

    Round-robin the sources strictly, so all of them are visible immediately: a
    40/40 split alternates down page 1, and a 40/1 split still puts the single
    MusicBrainz row second rather than burying it at position 41. Order within
    each source is preserved, which is what the providers use for relevance
    ranking, and no row is ever dropped or repeated.

    Accepting a LIST of groups rather than a fixed pair is what lets Discogs
    join the same rotation without anyone having to re-derive the fairness rule.
    """
    lists = [(name, list(rows or [])) for name, rows in groups]
    out = []
    idx = [0] * len(lists)
    lens = [len(rows) for _, rows in lists]
    while any(idx[i] < lens[i] for i in range(len(lists))):
        for i, (name, rows) in enumerate(lists):
            if idx[i] < lens[i]:
                out.append((name, rows[idx[i]]))
                idx[i] += 1
    return out


def _interleave_providers(itunes_results, mb_results):
    """Two-provider wrapper kept for the iTunes/MusicBrainz contract.

    All the merging logic lives in _interleave_sources(); this preserves the
    original two-argument call so the existing behaviour - and the existing
    tests that pin it - are untouched.
    """
    return _interleave_sources([("itunes", itunes_results),
                                ("musicbrainz", mb_results)])


def count_search_totals(query, artist_hint=None, album_hint=None):
    """Return the REAL number of matches, not the number of rows we render.

    The dialog's lead-in used to read a hardcoded 'Found 500+ Album(s)', which
    lies on every search and says nothing about how many albums sit behind the
    ~25 rows actually shown.

    MusicBrainz returns an exact 'count' for a search regardless of 'limit'. The
    RELEASE search already hands us that number for free, so only the recording
    and artist counts cost an extra request here. iTunes exposes only
    'resultCount', which is capped by the limit we request (200 max) and is
    therefore NOT a total - iTunes is only ever reported as 'shown'.
    """
    queries = _build_entity_queries(query, artist_hint, album_hint)
    empty = {"albums": None, "tracks": None, "artists": None,
             "albums_shown": 0, "tracks_shown": 0}
    if not queries.get("release"):
        return empty

    def _count(kind):
        try:
            resp = _mb_get(MUSICBRAINZ_BASE_URL + kind,
                           params={"query": queries[kind], "fmt": "json",
                                   "limit": 0},
                           timeout=6)
            if resp is None:
                return None
            return int(resp.json().get("count", 0))
        except Exception:
            return None

    out = dict(empty)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = (("tracks", ex.submit(_count, "recording")),
                   ("artists", ex.submit(_count, "artist")))
        for key, fut in futures:
            try:
                out[key] = fut.result(timeout=9)
            except Exception:
                out[key] = None
    return out


def search_mb_entities(query, kind, artist_hint=None, album_hint=None, limit=15):
    """Search MusicBrainz for artists or recordings (the filter-strip views).

    The album view is served by search_musicbrainz(); this covers the other two
    tabs so Artists and Tracks return real rows instead of being decorative.
    Reuses the same scoped query builder as the album search and the counts.
    """
    cache_key = f"mb_{kind}_{query.lower().strip()}_{artist_hint}_{album_hint}_{limit}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    if kind == "artist":
        q = re.sub(r'[\'"+]', ' ', query or "").strip()
        if artist_hint:
            ca = re.sub(r'[\'"+]', ' ', artist_hint).strip()
            if ca:
                q = f'artist:"{ca}"'
    else:
        q = _build_entity_queries(query, artist_hint, album_hint).get("recording", "")

    out = []
    if not q:
        return out
    try:
        resp = _mb_get(MUSICBRAINZ_BASE_URL + kind,
                       params={"query": q, "fmt": "json", "limit": limit},
                       timeout=8)
        if resp is not None:
            data = resp.json()
            if kind == "artist":
                for a in data.get("artists", []):
                    name = a.get("name", "")
                    if not name:
                        continue
                    score = a.get("score") or 0
                    out.append({
                        "id": a.get("id", ""), "name": name,
                        "country": a.get("country", ""),
                        "type": a.get("type", "") or "",
                        "score": score,
                        "art_thumb": f"https://coverartarchive.org/release/{a.get('id','')}/front-250.jpg",
                    })
            else:
                for rec in data.get("recordings", []):
                    title = rec.get("title", "")
                    if not title:
                        continue
                    ac = rec.get("artist-credit") or []
                    r_artist = ac[0].get("name", "Unknown Artist") if ac else "Unknown Artist"
                    # 'releases' and 'media' are LISTS in the recording payload,
                    # and an entry may be missing 'media' entirely. Chaining
                    # .get() straight through them raised AttributeError
                    # ('list' object has no attribute 'get') and the whole
                    # Tracks view silently returned zero rows.
                    fmt = ""
                    rels = rec.get("releases") or []
                    if rels and isinstance(rels[0], dict):
                        media = rels[0].get("media") or []
                        if media and isinstance(media[0], dict):
                            fmt = media[0].get("format", "") or ""
                    out.append({
                        "id": rec.get("id", ""), "title": title,
                        "artist": r_artist,
                        "length_ms": (rec.get("length") or 0),
                        "format": fmt,
                    })
    except Exception as e:
        print(f"[MUSICBRAINZ] {kind} search error: {e}")
    search_cache.set(cache_key, out, ttl=3600)
    return out


def search_musicbrainz(query, artist_hint=None, album_hint=None, limit=10):
    cache_key = f"mb_search_{query.lower().strip()}_{artist_hint}_{album_hint}_{limit}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    results = []
    try:
        search_url = MUSICBRAINZ_BASE_URL + "release"
        # Same builder as count_search_totals, so the headline count always
        # describes the very set of results rendered underneath it.
        lucene_q = _build_lucene_query(query, artist_hint, album_hint)

        params = {
            "query": lucene_q,
            "fmt": "json",
            "limit": limit
        }
        resp = _mb_get(search_url, params=params, timeout=6)
        if resp is not None:
            data = resp.json()
            # The search response already carries the exact total, so remember
            # it. count_search_totals() used to spend a SECOND rate-limited
            # MusicBrainz request asking for a number we had just been handed;
            # under the 1 req/sec limiter that doubled the wait on every cold
            # search for no new information.
            try:
                _MB_RELEASE_TOTAL[lucene_q] = int(data.get("count", 0))
            except Exception:
                pass
            for rel in data.get("releases", []):
                rel_id = rel.get("id")
                if not rel_id:
                    continue
                r_artist = "Unknown Artist"
                if rel.get("artist-credit"):
                    r_artist = rel["artist-credit"][0].get("name", "Unknown Artist")
                r_title = rel.get("title", "Unknown Album")
                r_date = (rel.get("date") or "")[:4]
                r_country = rel.get("country", "")

                art_url = f"https://coverartarchive.org/release/{rel_id}/front-500.jpg"

                results.append({
                    "id": rel_id,
                    "source": "musicbrainz",
                    "title": r_title,
                    "artist": r_artist,
                    "year": r_date,
                    "country": r_country,
                    "art_thumb": art_url,
                    "art_full": art_url
                })
    except Exception as e:
        print(f"[MUSICBRAINZ] Search error: {e}")

    search_cache.set(cache_key, results, ttl=3600)
    return results


# ==========================================================
# ARTWORK REDIRECT RESOLUTION
# ==========================================================
# MusicBrainz is the only provider whose artwork URL is a REDIRECT. The other two
# hand out a direct image, which is why only MusicBrainz-sourced albums came back
# without artwork:
#
#   coverartarchive.org/release/<id>/front-500.jpg
#     -> 307  text/plain            archive.org/download/mbid-<id>/..._thumb500.jpg
#     -> 302  image/jpeg            dn710007.ca.archive.org/0/items/..._thumb500.jpg
#
#   iTunes : 200 image/jpeg          is1-ssl.mzstatic.com/.../600x600bb.jpg
#   Discogs: 200 image/jpeg          i.discogs.com/...
#
# The first MusicBrainz hop is not an image at all - it is a text/plain 307. WMP
# fetches largeCoverParams itself and was being handed that redirecting URL, so
# it did not end up with the JPEG that the other two providers were always
# giving it. Following the chain here gives WMP the final direct URL and puts
# MusicBrainz on the same footing as iTunes and Discogs.
#
# The resolution is deliberately forgiving: any failure - offline, a timeout, a
# release with no front cover at all (404, no image) - returns the original URL
# unchanged, which is exactly the behaviour this had before. A redirect that
# cannot be resolved must never cost the album its artwork entirely.
def _resolve_art_url(url):
    """Follow an artwork redirect chain down to a final, direct image URL.

    Returns the resolved URL, or `url` unchanged if it is already direct or if
    the chain could not be walked.
    """
    if not url or not url.startswith("http"):
        return url
    try:
        resp = requests.get(url, stream=True, timeout=15, allow_redirects=True)
        try:
            final = resp.url or url
            # Only accept the result if the walk actually ended somewhere else and
            # that somewhere is plausibly an image. `stream=True` keeps this from
            # pulling down the whole image body just to look at a URL.
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if final != url and ctype.startswith("image/"):
                return final
        finally:
            resp.close()
    except Exception as e:
        print(f"[ART] redirect resolve failed for {url}: {e}")
    return url


def get_musicbrainz_album_details(release_id):
    cache_key = f"mb_album_{release_id}"
    cached = album_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # Composer credits live on the WORK, not the recording, and MusicBrainz
        # only returns a work's own relations when 'work-rels' is requested.
        # Verified: same release, 5 tracks - 0 composers without work-rels,
        # 5 with it. Without these incs every composer came back as the artist.
        url = MUSICBRAINZ_BASE_URL + f"release/{release_id}"
        params = {
            "inc": ("recordings+release-groups+labels+artist-credits+media+genres"
                    "+recording-level-rels+work-level-rels+work-rels+artist-rels"),
            "fmt": "json"
        }
        resp = _mb_get(url, params=params, timeout=8)
        if resp is None:
            return None
        data = resp.json()

        album_artist = "Unknown Artist"
        if data.get("artist-credit"):
            album_artist = data["artist-credit"][0].get("name", "Unknown Artist")
        album_title = data.get("title", "Unknown Album")
        album_year = (data.get("date") or "2000")[:4]

        genres = [g.get("name") for g in data.get("genres", []) if g.get("name")]
        if not genres and data.get("release-group", {}).get("genres"):
            genres = [g.get("name") for g in data["release-group"]["genres"] if g.get("name")]
        primary_genre = genres[0].title() if genres else "Rock"

        tracks = []
        track_seq = 1
        for media in data.get("media", []):
            disc_num = media.get("position", 1)
            for t in media.get("tracks", []):
                t_name = t.get("title", f"Track {track_seq}")
                t_artist = album_artist
                if t.get("recording", {}).get("artist-credit"):
                    t_artist = t["recording"]["artist-credit"][0].get("name", album_artist)
                elif t.get("artist-credit"):
                    t_artist = t["artist-credit"][0].get("name", album_artist)

                # Composer credit: recording -> performance relation -> work
                # -> the work's own 'composer' relations. An empty string means
                # "MusicBrainz has no composer for this track", which is honest;
                # substituting the artist (the old behaviour) wrote a factually
                # wrong credit into the user's library.
                t_composer = ""
                for rel in t.get("recording", {}).get("relations", []) or []:
                    work = rel.get("work")
                    if not work:
                        continue
                    names = []
                    for wrel in work.get("relations", []) or []:
                        if wrel.get("type") == "composer":
                            wname = (wrel.get("artist") or {}).get("name")
                            if wname and wname not in names:
                                names.append(wname)
                    if names:
                        t_composer = "; ".join(names)
                        break

                tracks.append({
                    "id": t.get("id") or f"{release_id}_{disc_num}_{t.get('position', track_seq)}",
                    "name": t_name,
                    "number": t.get("position", track_seq),
                    "disc": disc_num,
                    "artist": t_artist,
                    "performer": t_artist,
                    "composer": t_composer,
                    "duration_ms": t.get("length", 0),
                    "genre": primary_genre
                })
                track_seq += 1

        # Resolve the coverartarchive redirect to a direct image URL. See
        # _resolve_art_url for the chain and why iTunes/Discogs never needed this.
        art_url = _resolve_art_url(
            f"https://coverartarchive.org/release/{release_id}/front-500.jpg")

        details = {
            "id": str(release_id),
            "source": "musicbrainz",
            "title": album_title,
            "artist": album_artist,
            "genre": primary_genre,
            "year": album_year,
            "art_url": art_url,
            "tracks": tracks
        }
        album_cache.set(cache_key, details, ttl=86400)
        return details
    except Exception as e:
        print(f"[MUSICBRAINZ] Details error for {release_id}: {e}")
        return None


# ==========================================================
# DISCOGS SEARCH & NORMALIZATION
# ==========================================================
# Discogs is the strongest of the three providers on release-level detail
# (accurate tracklists, positions, formats, runtimes, credits) but it is
# strictly OPTIONAL: it needs a token, so every entry point below short-circuits
# to empty when _discogs_configured() is False. With no token the Albums list is
# exactly the iTunes/MusicBrainz list it always was.

def _dg_split_artist_title(title):
    """Discogs packs 'Artist - Album' into one 'title' field.

    The search endpoint does not separate them, so split on the first
    ' - ' with a space on both sides, which is Discogs' own separator. A title
    with no separator keeps the whole string as the title rather than guessing.
    """
    raw = (title or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            artist, _, rest = raw.partition(sep)
            if artist.strip() and rest.strip():
                return artist.strip(), rest.strip()
    return "", raw


def _dg_duration_ms(duration):
    """'4:21' or '1:02:33' -> milliseconds. Discogs durations are strings."""
    if not duration:
        return 0
    try:
        parts = str(duration).strip().split(":")
        if not all(p.strip().isdigit() for p in parts):
            return 0
        ms = 0
        for n in parts:                      # right-fold: 4 -> 4, 4:21 -> 261
            ms = ms * 60 + int(n)
        return ms * 1000
    except Exception:
        return 0


def _dg_position(position):
    """Discogs 'position' ('A1', 'B2', '1', '2-7') -> (disc, number).

    The letters are Discogs' side markers, not disc numbers: on a 2-CD set it
    runs A1..D12, and reading that as disc 4 on a 2-disc album would invent
    discs WMP never had. Letters therefore all map to disc 1; a real multi-disc
    release comes back with '1-1' style positions, where the leading number IS
    the disc.
    """
    pos = str(position or "").strip()
    m = re.match(r"^([A-Za-z])\s*[-.]?\s*(\d+)$", pos)
    if m:
        return 1, int(m.group(2))
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", pos)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)$", pos)
    if m:
        return 1, int(m.group(1))
    return 1, 0


def search_discogs(query, limit=40, artist_hint=None, album_hint=None):
    """Search Discogs. Returns rows shaped exactly like the other providers.

    Search MASTERS, not releases: the release index returns the same album once
    per pressing, so 'Kind Of Blue' alone returns well over a hundred rows that
    are all the same five tracks. A master is the canonical work and points at
    the real tracklist.
    """
    if not _discogs_configured():
        return []
    cache_key = f"dg_search_{query}|{limit}|{artist_hint}|{album_hint}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    resp = _discogs_get("database/search",
                        params={"q": query, "type": "master",
                                "per_page": min(limit, 100)})
    if resp is None:
        return []
    try:
        results = resp.json().get("results", []) or []
    except Exception as e:
        print(f"[DISCOGS] search parse error: {e}")
        return []

    out = []
    for r in results:
        artist, album = _dg_split_artist_title(r.get("title", ""))
        if not album:
            continue
        master_id = r.get("master_id") or r.get("id")
        if not master_id:
            continue
        styles = r.get("style") or ([r["genre"]] if r.get("genre") else [])
        out.append({
            "id": str(master_id),
            "title": album,
            "artist": artist,
            "year": str(r.get("year") or ""),
            "genre": (styles[0] if styles else ""),
            "art_thumb": r.get("cover_image") or r.get("thumb") or "",
            "country": "",
            "format": " • ".join(r.get("format") or []),
        })
    search_cache.set(cache_key, out, ttl=7200)
    return out


def get_discogs_album_details(master_id):
    """Fetch a Discogs master tracklist, normalized to the shared shape.

    Returns the SAME dictionary the iTunes and MusicBrainz paths return, so the
    confirm page, track selection, build_wmp_xml() and the WMP write are all
    unchanged - a Discogs album simply arrives pre-normalized.
    """
    cache_key = f"dg_album_{master_id}"
    cached = album_cache.get(cache_key)
    if cached is not None:
        return cached

    resp = _discogs_get(f"masters/{master_id}")
    if resp is None:
        return None
    try:
        data = resp.json()
    except Exception as e:
        print(f"[DISCOGS] detail parse error for {master_id}: {e}")
        return None

    # A master can legitimately carry an empty tracklist: the work is catalogued
    # but no pressing has been entered. Its main release is where the real
    # tracklist (and often better images) actually live.
    if not data.get("tracklist") and data.get("main_release"):
        rel = _discogs_get(f"releases/{data['main_release']}")
        if rel is not None:
            try:
                release = rel.json()
                if release.get("tracklist"):
                    data["tracklist"] = release["tracklist"]
                if not data.get("images") and release.get("images"):
                    data["images"] = release["images"]
            except Exception:
                pass

    try:
        artists = [a.get("name", "").strip()
                   for a in (data.get("artists") or []) if a.get("name")]
        album_artist = ", ".join(artists) if artists else "Unknown Artist"
        album_title = data.get("title") or "Unknown Album"
        album_year = str(data.get("year") or "")[:4] or "2000"

        genres = data.get("genres") or []
        styles = data.get("styles") or []
        primary_genre = genres[0] if genres else (styles[0] if styles else "Rock")
        if isinstance(primary_genre, str):
            primary_genre = primary_genre.title()

        # Prefer the 'primary' image (the real front cover) over any 'secondary'
        # back/inner sleeve Discogs happened to list first.
        images = data.get("images") or []
        chosen = next((i for i in images if i.get("type") == "primary"),
                      images[0] if images else None)
        art_url = ""
        if chosen:
            art_url = chosen.get("uri") or chosen.get("resource_url") or ""

        tracks = []
        seen = 0
        for entry in (data.get("tracklist") or []):
            if entry.get("type_") == "heading":
                continue          # a side/medium heading is structure, not a track
            t_name = (entry.get("title") or "").strip()
            if not t_name:
                continue
            disc, number = _dg_position(entry.get("position"))
            seen += 1

            # Discogs carries per-track credits in 'extraartists' with a free-text
            # 'role'. Written-By / Composed By is the composer slot; Producer,
            # Engineer and Featuring are NOT composers and must never be written
            # into the user's library as one.
            #
            # The role is HYPHENATED in the wild - 'Written-By' is what Discogs
            # actually sends, not 'Written By'. Matching the spaced form only
            # found 'Composed By' and silently dropped 'Written-By' credits, so
            # normalise the separators before looking for the role.
            composer = ""
            for xa in (entry.get("extraartists") or []):
                role = re.sub(r"[-_]+", " ", (xa.get("role") or "")).lower()
                if "compos" in role or "written by" in role:
                    nm = (xa.get("name") or "").strip()
                    if nm and nm not in composer:
                        composer = (composer + "; " + nm) if composer else nm

            t_artist = album_artist
            for xa in (entry.get("artists") or []):
                nm = (xa.get("name") or "").strip()
                if nm:
                    t_artist = nm
                    break

            tracks.append({
                "id": f"dg_{master_id}_{disc}_{number or seen}",
                "name": t_name,
                "number": number or seen,
                "disc": disc,
                "artist": t_artist,
                "performer": t_artist,
                "composer": composer,
                "duration_ms": _dg_duration_ms(entry.get("duration")),
                "genre": primary_genre,
            })

        details = {
            # This 'id' feeds guid() to make the WMP collection GUID. The 'dg'
            # prefix stops a Discogs master ever colliding with an iTunes
            # collection id or a MusicBrainz release id - all three are bare
            # integers, and a collision would apply the wrong album's tags.
            "id": f"dg{master_id}",
            "source": "discogs",
            "title": album_title,
            "artist": album_artist,
            "genre": primary_genre,
            "year": album_year,
            "art_url": art_url,
            "tracks": tracks,
        }
        album_cache.set(cache_key, details, ttl=86400)
        return details
    except Exception as e:
        print(f"[DISCOGS] Details error for {master_id}: {e}")
        return None


def lookup_by_discid_or_toc(toc_string):
    cache_key = f"toc_lookup_{toc_string.strip()}"
    cached = album_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        clean_toc = re.sub(r'[\s+]+', '+', toc_string.strip())
        url = f"{MUSICBRAINZ_BASE_URL}discid/-?toc={clean_toc}&fmt=json"
        # Rate-limited like every other MusicBrainz call. This one used to be
        # a raw session.get, and it immediately calls
        # get_musicbrainz_album_details() - two MusicBrainz requests back to
        # back, which is precisely the pattern the 1 req/sec limit rejects.
        resp = _mb_get(url, timeout=6)
        if resp is not None:
            data = resp.json()
            releases = data.get("releases", [])
            if releases:
                rel_id = releases[0].get("id")
                details = get_musicbrainz_album_details(rel_id)
                if details:
                    album_cache.set(cache_key, details, ttl=86400)
                    return details
    except Exception as e:
        print(f"[CD TOC] Lookup error: {e}")
    return None
# ==========================================================
# XML BUILDER (WMP & ZUNE COMPATIBLE)
# ==========================================================
def parse_mdq_content_ids(mdq_xml):
    """Extract the real per-track WMContentIDs from an MDQ document.

    window.external.GetMDQByRequestID returns an MDQ describing the disc WMP
    currently has loaded, including the genuine <WMContentID> of every track.
    Those are the identifiers WMP uses to match incoming metadata to the
    physical tracks - generated GUIDs match nothing, so the write is ignored.

    Parsed with regex rather than an XML parser on purpose: the MDQ contains a
    zero-width space inside some tag names (e.g. '<\\u200bfilename>'), which
    makes the document invalid for a strict parser.

    Returns {mdq_index: WMContentID} where mdq_index is the 1-based position of
    the track *in the MDQ* (i.e. on the disc). Matching by the album track
    number is unreliable - the disc's own numbering and the online album's
    numbering frequently disagree, which would hand a track the wrong ID.
    """
    if not mdq_xml or '<MDQ' not in mdq_xml:
        log_line("MDQ", f"no usable MDQ (len={len(mdq_xml or '')}, "
                        f"has_MDQ_tag={'<MDQ' in (mdq_xml or '')})")
        return {}
    # A STUB MDQ - one bare track entry with an id but no title/artist/album -
    # is what WMP returns when there is no disc in the drive (a library
    # "Update album info"). Its content ID belongs to no real track: it has
    # been observed as the same value across every album. Injecting it makes
    # WMP try to match metadata to a track that does not exist, so ignore it.
    if not re.search(r"<title>\s*[\r\n]*\s*<text>", mdq_xml, re.IGNORECASE):
        log_line("MDQ", "stub MDQ (no track titles) - ignoring its content IDs")
        return {}
    content_ids = {}
    try:
        blocks = re.findall(r"<track>(.*?)</track>", mdq_xml,
                            re.DOTALL | re.IGNORECASE)
        for idx, block in enumerate(blocks, start=1):
            cid = re.search(r"<WMContentID>\s*\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}"
                            r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?"
                            r"\s*</WMContentID>", block, re.IGNORECASE)
            if cid:
                content_ids[idx] = cid.group(1).upper()
    except Exception as e:
        print(f"[MDQ] Content-ID parse error: {e}")
    if not content_ids:
        # Log the shape, not the document: this is the case that means every
        # track gets a GENERATED WMContentID, which WMP cannot match, and the
        # write is then silently ignored with no other symptom.
        log_line("MDQ", f"no WMContentID recovered from {len(blocks)} track "
                        f"block(s); all track ids will be GENERATED and WMP "
                        f"will ignore the write. mdq={mdq_xml[:700]!r}")
    else:
        log_line("MDQ", f"parsed {len(content_ids)} real content IDs: "
                        f"{sorted(content_ids.items())[:5]}")
    return content_ids


def build_wmp_xml(album_data, selected_tracks=None, request_id="",
                  content_ids=None, wmid="", cd=""):
    # The document's identity must be something WMP can correlate. A library
    # dialog gives us the requestid. A CD rip arrives with NO requestid at all,
    # and this used to fall through to uuid4() - a fresh random value on every
    # staging, leaving WMP nothing stable to match the document against. The one
    # stable identifier a CD flow supplies is the ?cd= disc content id.
    req_id = request_id or (guid(cd) if cd else str(uuid.uuid4()).upper())
    # Use the WMID WMP itself supplied when it has one: that is the exact GUID
    # of the library collection being updated, so the document lines up with
    # WMP's own entry. Only generate a GUID when WMP gave us nothing.
    #
    # THIS NOW APPLIES IN THE CD FLOW TOO, which it did not until 5e4b946. The
    # old guard stamped guid(album_id) instead, on the theory that a disc
    # "borrows" the collection. Two field observations killed that:
    #
    #   * WMP's own wmid for this disc, 64C552E6-0C68-5C6A-A02A-617A084205C1,
    #     is EXACTLY guid('1122792846') - our own deterministic id for the
    #     iTunes album. WMP adopted it and echoes it back, so the guard was a
    #     no-op here and only its [CDGUID] line was visible.
    #   * At 15:43 the same disc arrived with 2A4F0191.., which is NOT
    #     guid(album_id) - WMP had made a collection of its own. There the
    #     guard was not a no-op: we would have stamped a collection WMP was
    #     not asking about.
    #
    # Preferring WMP's wmid is correct in both cases and needs no theory about
    # discs: it is the collection WMP just told us it is asking about.
    album_guid = _remember_wmid(wmid) or guid(album_data.get("id") or cd)
    provider = "iTunes" if album_data.get("source") == "itunes" else "MusicBrainz"
    content_ids = content_ids or {}

    art_url = album_data.get("art_url", "")
    # THE ARTWORK URL. Two shapes, selected by _ART_MODE (see its definition for
    # why the direct form is now the default):
    #
    #   direct : https://is1-ssl.mzstatic.com/.../600x600bb.jpg
    #   proxy  : http://127.0.0.1/cover/fai-<token>/album.jpg?url=<quoted upstream>
    #
    # In the proxy form the version token goes in the PATH, not the query. As a
    # second query parameter it introduced a bare '&' into this XML - and a bare
    # '&' makes the whole document not-well-formed, so WMP rejected the ENTIRE
    # response and stopped applying tags as well as artwork:
    #   ET.fromstring(xml) -> not well-formed (invalid token): line 16, column 198
    # The proxy serves /cover/<path:ignore>, so a path segment costs nothing and
    # leaves exactly one '?' and no '&' in the value.
    #
    # The token changes on EVERY APPLY, not just per album. It used to be
    # md5(album_id), i.e. stable per album, on the reasoning that a stable URL
    # saves WMP a needless re-download. That is backwards for the only case that
    # matters - retrying because the artwork was wrong:
    #
    #   15:43:43  [IMAGE] 170405B  /cover/fai-51822f10/...   (fetched twice)
    #   15:52:15  staged, same album, same token
    #   15:52:16  delivered  - and NO [IMAGE] line at all
    #
    # WMP had that exact URL on file for that exact collection already, from the
    # 15:43 delivery, and did not fetch it again. So a stable token silently
    # disables every retry. _COVER_SEQ makes each apply present a URL WMP has
    # never seen, which is the only way a re-apply can change the art.
    #
    # safe="/" keeps the slashes readable in the proxy form: quoting them makes
    # the value double-encoded once WMP passes it back through a query string,
    # and the image proxy then fetches an unparseable 'https%3A%2F%2F...' string.
    if not art_url:
        proxy_art = ""
    elif _ART_MODE == "direct":
        # Upstream URLs carry no '&' in practice, but xesc unconditionally, so
        # this cannot reintroduce the well-formedness bug above.
        proxy_art = xesc(art_url)
    else:
        global _COVER_SEQ
        _COVER_SEQ += 1
        ver = hashlib.md5(
            f"{album_data.get('id', '')}|{art_url}|{_COVER_SEQ}".encode(
                "utf-8", "replace")).hexdigest()[:8]
        proxy_art = xesc(f"http://127.0.0.1/cover/fai-{ver}/album.jpg?url="
                         f"{requests.utils.quote(art_url, safe='/:?=&')}")

    tracks_to_include = selected_tracks if selected_tracks is not None else album_data.get("tracks", [])

    track_nodes = []
    for pos, t in enumerate(tracks_to_include, start=1):
        t_id = t.get("id", str(t.get("number", 1)))
        # Prefer the REAL WMContentID from the disc's MDQ so WMP can match this
        # track. Indexed by the track's position in the selection (which follows
        # disc order), NOT by the online album's track number - the disc's own
        # numbering can disagree with the release, which would bind the metadata
        # to the wrong physical track. Falls back to a generated GUID only when
        # the MDQ had no entry for this position.
        real_cid = content_ids.get(pos, "")
        t_guid = real_cid or guid(f"{album_guid}_{t_id}")
        t_title = xesc(t.get("name", "Unknown Track"))
        t_num = str(t.get("number", 1))
        t_artist = xesc(t.get("artist", album_data.get("artist", "Unknown Artist")))
        t_performer = xesc(t.get("performer", t_artist))
        # Only emit a composer when the provider actually gave us one. The old
        # fallback to the artist wrote a false credit into the user's library:
        # for 'So What' it claimed Miles Davis wrote his own composition, and
        # for a jazz album it misattributed the bandleader on every track.
        t_composer_raw = t.get("composer", "") or ""
        composer_tag = (f"\n  <trackComposer>{xesc(t_composer_raw)}</trackComposer>"
                        if t_composer_raw.strip() else "")
        t_disc = str(t.get("disc", 1))
        t_genre = xesc(t.get("genre", album_data.get("genre", "Rock")))

        track_xml = f"""<track>
  <WMContentID>{t_guid}</WMContentID>
  <ZuneMediaID>{t_guid}</ZuneMediaID>
  <trackTitle>{t_title}</trackTitle>
  <trackNumber>{t_num}</trackNumber>
  <discNumber>{t_disc}</discNumber>
  <trackArtist>{t_artist}</trackArtist>
  <trackPerformer>{t_performer}</trackPerformer>{composer_tag}
  <genre>{t_genre}</genre>
</track>"""
        track_nodes.append(track_xml)

    joined_tracks = "\n".join(track_nodes)
    album_title = xesc(album_data.get("title", "Unknown Album"))
    album_artist = xesc(album_data.get("artist", "Unknown Artist"))
    album_genre = xesc(album_data.get("genre", "Rock"))
    release_date = f"{album_data.get('year', '2000')}/01/01"

    # <status> is REQUIRED, not optional. EMPTY_METADATA_XML declares
    # <status>NOTFOUND</status> in exactly this position, but a SUCCESSFUL
    # document used to declare no status at all - so WMP read the response as
    # 'no result', never completed the collection, never fetched the artwork
    # from largeCoverParams, and re-opened the FAI dialog for the very
    # collection id we had just supplied. Logged from a real rip of
    # 'Sun Kil Moon - Tiny Cities':
    #   12:29:30 [STAGED] album='Tiny Cities'   (no [IMAGE] served anywhere)
    #   12:29:45 ReturnToMainTask-ok
    #   12:29:49 GET /FAI/default.aspx?...&wmid=B17CF884-...
    # B17CF884 was our own generated WMCollectionID: WMP had adopted it and was
    # asking for it again, which is the reported "hang then search again".
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<METADATA>
  <version>5.0</version>
  <status>OK</status>
  <requestID>{req_id}</requestID>
  <MDR-CD>
    <version>5.0</version>
    <mdr-id>{req_id}</mdr-id>
    <WMCollectionID>{album_guid}</WMCollectionID>
    <WMCollectionGroupID>{album_guid}</WMCollectionGroupID>
    <ZuneAlbumMediaID>{album_guid}</ZuneAlbumMediaID>
    <albumTitle>{album_title}</albumTitle>
    <albumArtist>{album_artist}</albumArtist>
    <genre>{album_genre}</genre>
    <releaseDate>{release_date}</releaseDate>
    <largeCoverParams>{proxy_art}</largeCoverParams>
    <smallCoverParams>{proxy_art}</smallCoverParams>
    <dataProvider>{provider}</dataProvider>
    {joined_tracks}
  </MDR-CD>
  <Backoff>
    <Time>2</Time>
  </Backoff>
</METADATA>"""
    return xml

# ==========================================================
# NO-ARTWORK PLACEHOLDER
# ==========================================================
def _build_noart_png(size=48):
    """Render the 'no artwork' placeholder in-process.

    Every <img> in the dialog falls back to /static/noart.png. Without a real
    file behind that path the host draws its broken-image glyph right where
    the authentic Find Album Information window shows a disc, so the
    placeholder is generated here instead of shipped as a binary asset.
    """
    import struct
    import zlib

    bg = b"\xf0\xf3\xf7"    # #F0F3F7 - matches .album-thumb background-color
    edge = b"\xc9\xd3\xde"  # #C9D3DE - matches .album-thumb border-color
    disc = b"\xdc\xe3\xea"  # #DCE3EA
    hub = b"\xaa\xb7\xc4"   # #AAB7C4
    cx = cy = (size - 1) / 2.0
    r_disc = size * 0.30
    r_hub = size * 0.08

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            if x == 0 or y == 0 or x == size - 1 or y == size - 1:
                raw.extend(edge + b"\xff")
                continue
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= r_hub:
                raw.extend(hub + b"\xff")
            elif d <= r_disc:
                raw.extend(disc + b"\xff")
            else:
                raw.extend(bg + b"\xff")

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


NOART_PNG = _build_noart_png()


@app.route("/static/noart.png")
def noart():
    resp = Response(NOART_PNG, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["Content-Length"] = str(len(NOART_PNG))
    return resp


# ==========================================================
# IMAGE PROXY WITH IN-MEMORY TTL CACHE
# ==========================================================
@app.route("/get_image")
@app.route("/cover/album.jpg")
@app.route("/cover/<path:ignore>")
def get_image(ignore=None):
    url = request.args.get("url")
    if url:
        # WMP hands the URL back exactly as we gave it. Older staged documents
        # (and any client that re-encodes) can arrive double-encoded, i.e.
        # 'https%3A%2F%2F...', which is not a fetchable URL. Unwrap once so a
        # 404 upstream never turns into a missing album cover.
        if url.startswith("http%3A") or url.startswith("https%3A"):
            try:
                url = requests.utils.unquote(url)
            except Exception:
                pass
    if not url or not url.lower().startswith(("http://", "https://")):
        log_line("IMAGE", f"rejecting unusable url={str(url)[:60]!r}")
        return Response("", 404)

    cached_img = image_cache.get(url)
    if cached_img:
        resp = Response(cached_img[0], mimetype=cached_img[1])
        resp.headers['Content-Length'] = len(cached_img[0])
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp

    try:
        r = session.get(url, headers={"User-Agent": BROWSER_AGENT}, timeout=8, verify=False)
        if r.status_code == 200 and r.content:
            mimetype = r.headers.get("Content-Type", "image/jpeg")
            if "image" not in mimetype:
                mimetype = "image/jpeg"
            image_cache.set(url, (r.content, mimetype), ttl=86400)
            with NAV_LOCK:
                FAI_NAVIGATION['stats']['images_served'] += 1
            log_line("IMAGE", f"served {len(r.content)}B {mimetype} from {url[:70]}")
            resp = Response(r.content, mimetype=mimetype)
            resp.headers['Content-Length'] = len(r.content)
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp
        log_line("IMAGE", f"upstream {r.status_code} for {url[:70]}")
    except Exception as e:
        print(f"[!] PROXY IMAGE FAILED for {url[:80]}: {e}")
        log_line("IMAGE", f"failed for {url[:70]}: {e}")

    return Response("", 404)

# ==========================================================
# DISCOVERY (WMP & ZUNE EXPECTS THIS)
# ==========================================================
@app.route("/cdinfo/GetMDRCDPOSTURL.aspx")
@app.route("/redir/getmdrcdposturl/")
@app.route("/redir/getmdrcdposturlbackground/")
@app.route("/redir/getmdrcdposturlbackgroundzune/")
def get_post_url():
    if "zune" in request.path.lower():
        return Response("http://127.0.0.1/redir/getmdrcdbackgroundzune/", mimetype="text/plain")
    return Response("http://127.0.0.1/redir/getmdrcdbackground/", mimetype="text/plain")

def _retarget_collection_id(xml, wmid):
    """Point every album-level collection GUID in the document at WMP's wmid.

    WMP fetches metadata for a collection it already tracks and identifies it
    by WMID. Our staged XML carries a generated collection GUID that WMP has
    never seen, so it would apply nothing. Rewriting WMCollectionID /
    WMCollectionGroupID / ZuneAlbumMediaID (and mdr-id) to the wmid makes the
    document describe the collection WMP actually asked about.
    """
    if not xml or not wmid:
        return xml
    wmid = str(wmid).strip().strip('{}').upper()
    # Grab the collection GUID currently in the document (all three normally
    # share one value) and substitute the wmid for it.
    m = re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>", xml, re.IGNORECASE)
    old = m.group(1).strip().upper() if m else ""
    if not old or old == wmid:
        return xml
    out = re.sub(r"(<WMCollectionID>)[^<]+(</WMCollectionID>)",
                 r"\g<1>" + wmid + r"\g<2>", xml, flags=re.IGNORECASE)
    out = re.sub(r"(<WMCollectionGroupID>)[^<]+(</WMCollectionGroupID>)",
                 r"\g<1>" + wmid + r"\g<2>", out, flags=re.IGNORECASE)
    out = re.sub(r"(<ZuneAlbumMediaID>)[^<]+(</ZuneAlbumMediaID>)",
                 r"\g<1>" + wmid + r"\g<2>", out, flags=re.IGNORECASE)
    # mdr-id normally mirrors the collection id; keep them in step.
    out = out.replace(f"<mdr-id>{old}</mdr-id>", f"<mdr-id>{wmid}</mdr-id>")
    log_line("WMID", f"retargeted collection {old[:8]}.. -> {wmid[:8]}..")
    return out


# ==========================================================
# METADATA DELIVERY - WHERE WMP/ZUNE FETCHES XML
# ==========================================================
@app.route("/cdinfo/GetMDRCD.aspx", methods=["GET", "POST"])
@app.route("/redir/getmdrcdbackground/", methods=["GET", "POST"])
@app.route("/redir/getmdrcdbackgroundzune/", methods=["GET", "POST"])
@app.route("/redir/getmdrcd/", methods=["GET", "POST"])
def mdr_post():
    global LAST_XML
    with NAV_LOCK:
        FAI_NAVIGATION['stats']['xml_deliveries'] += 1

    # Prefer XML staged for this exact requestid/TOC/wmid (echo correct XML back
    # to WMP's own background fetch), else fall back to the latest staged XML.
    staged = _lookup_staged_xml()
    # WMP asks by wmid, which is the GUID of ITS OWN library collection. Any
    # collection IDs we generated are unrelated to it, so rewrite them to the
    # wmid - otherwise WMP sees a document for a collection it does not have.
    wmid_q = _remember_wmid(raw_query_arg('wmid'))
    log_line("MDR", f"path={request.path} "
                    f"qs={request.query_string.decode('utf-8', 'replace')!r} "
                    f"method={request.method} staged={'yes' if staged else 'no'} "
                    f"wmid={wmid_q or '-'}")
    if staged:
        # Retarget whenever WMP names a collection, disc or not. A request that
        # carries both ?cd= and ?wmid= is WMP saying "give me the document for
        # THIS collection" - the collection it made for that disc on a previous
        # apply. Serving a document stamped with a different collection id means
        # the album-level fields, including where the cover attaches, are keyed
        # to a collection WMP is not tracking for this disc.
        #
        # This reverses an earlier guard, added on the theory that retargeting
        # was what stopped artwork appearing on a re-ripped disc. That theory
        # was wrong: the cover was not being RE-FETCHED because the URL never
        # changed, and for a while because the document was not well-formed. With
        # a per-album token on the cover URL the fetch happens again either way
        # (logged on the Prospekt disc: 15:43:43 and 15:43:45, 170405B, fetched
        # while the request still named 2A4F0191).
        if wmid_q:
            staged = _retarget_collection_id(staged, wmid_q)
            # Remember this wmid -> document pairing: WMP re-fetches the same
            # collection moments later, and by then LAST_XML may have moved on.
            # claim_wmid=wmid_q PRESERVES the existing claim. Without it a new
            # document is seen here (prev_xml is not xml), which resets
            # PENDING_WRITE['wmid'] to '' - and the next unrelated collection to
            # ask would then be able to claim this same document, so a second
            # album would be served the first one's tags.
            _stage_request_xml(staged, wmid=wmid_q, claim_wmid=wmid_q)
        # Log which album WMP is actually being served, so a mismatch between
        # the dialog selection and what WMP applied is visible in the log.
        m = re.search(r"<albumTitle>([^<]*)</albumTitle>", staged)
        served = m.group(1) if m else "?"
        log_line("MDR", f"  -> serving album={served!r} to WMP "
                        f"(wmid={wmid_q[:8] if wmid_q else '-'})")
        return Response(staged, mimetype='text/xml')

    # No metadata is staged for this disc. Return an EMPTY document so WMP
    # leaves the album untouched. Previously we fell back to the most recently
    # staged album, which wrote that album onto every other CD.
    return Response(EMPTY_METADATA_XML, mimetype='text/xml')

    return Response("<METADATA><version>5.0</version><status>OK</status></METADATA>", mimetype='text/xml')

# ==========================================================
# LEGACY REDIRECTS (CD TOC & WMP BROWSING)
# ==========================================================
@app.route("/FAI/default.aspx", methods=["GET", "POST"])
@app.route("/redir/submittoc/", methods=["GET", "POST"])
@app.route("/redir/submittoc.asp", methods=["GET", "POST"])
@app.route("/redir/GetMDRCD.asp", methods=["GET", "POST"])
@app.route("/redir/QueryTOC.asp", methods=["GET", "POST"])
def legacy_redirects():
    global LAST_XML, LAST_TOC
    user_agent = request.headers.get('User-Agent', '')

    # Check for CD TOC in query parameters or POST body.
    # Read raw: request.args would turn the leading '+' of a TOC into a space.
    toc_param = raw_query_arg('toc') or raw_query_arg('TOC') or ''
    if not toc_param and request.method == "POST":
        post_body = request.data.decode("utf-8", errors="ignore")
        match = re.search(r'toc=([^&]+)', post_body, re.IGNORECASE)
        if match:
            toc_param = requests.utils.unquote(match.group(1))

    if toc_param:
        print(f"[CD TOC] Detected TOC signature: {toc_param[:60]}")
        with XML_LOCK:
            LAST_TOC = toc_param
            staged_xml = STAGED_REQUESTS.get(toc_param)
        if staged_xml:
            # We already staged metadata for this exact TOC (confirm page) -
            # echo it straight back instead of re-querying MusicBrainz.
            print(f"[CD TOC] Returning previously staged XML for this TOC: {toc_param[:60]}")
            log_line("TOC", f"staged-hit toc={toc_param!r} ua={user_agent[:60]}")
            if not ('MSIE' in user_agent or 'Mozilla' in user_agent):
                return Response(staged_xml, mimetype='text/xml')
            # Browser re-submission: go straight to the UI, no network lookup.
            # Forward the RAW query string so the '+' of the TOC survives
            # (re-encoding request.args would corrupt it to a space/%20).
            qs = request.query_string.decode('utf-8', 'replace')
            target = url_for('unified_ui') + ('?' + qs if qs else '')
            return redirect(target)
        # NOTE: TOC -> MusicBrainz auto-lookup is disabled for delivery. Even
        # though it is keyed to the real disc, applying an album you never
        # selected is how discs ended up sharing one album. The lookup result
        # is logged for reference; the disc is left untouched.
        album_details = lookup_by_discid_or_toc(toc_param)
        if album_details:
            print(f"[CD TOC] Match found (not applied): {album_details.get('title')} by {album_details.get('artist')}")
            log_line("TOC", f"match-not-applied toc={toc_param[:40]!r} "
                            f"album={album_details.get('title')!r}")

    if 'MSIE' in user_agent or 'Mozilla' in user_agent:
        # Forward the RAW query string verbatim - re-encoding request.args
        # corrupts the leading '+' of a CD TOC.
        qs = request.query_string.decode('utf-8', 'replace')
        target = url_for('unified_ui') + ('?' + qs if qs else '')
        return redirect(target)

    # No metadata for this TOC: return an EMPTY document rather than the last
    # album staged, which would tag an unrelated disc.
    return Response(EMPTY_METADATA_XML, mimetype='text/xml')
# ==========================================================
# CONCURRENT HYBRID SEARCH API (ITUNES + MUSICBRAINZ)
# ==========================================================
@app.route("/api_search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return Response('<div style="padding:15px;color:#888;">Enter artist, album, or track name to search...</div>', mimetype="text/html")

    with NAV_LOCK:
        FAI_NAVIGATION['stats']['total_searches'] += 1

    # Which of the three filter-strip tabs is asking. The authentic dialog has
    # always shown Artists / Albums / Tracks; previously the strip was purely
    # decorative, so clicking it did nothing. 'album' stays the default so every
    # existing caller and test is unaffected.
    view = (request.args.get("view", "album") or "album").lower()
    if view not in ("album", "artist", "track"):
        view = "album"

    # Paging. The dialog pane is only ~450px tall, so the list is several screens
    # deep - which is why it carries its own scrollbar, exactly like the
    # reference dialog. Paging stays available for API callers that want a
    # slice, but the default is large enough that the scrollbar alone reaches
    # every fetched row, so the UI needs no 'next page' control.
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 0))
    except (TypeError, ValueError):
        per_page = 0
    if per_page <= 0:
        per_page = 100
    per_page = max(1, min(per_page, 100))

    artist_hint = ""
    album_hint = ""
    if " - " in q:
        parts = q.split(" - ", 1)
        artist_hint = parts[0].strip()
        album_hint = parts[1].strip()
    elif " " in q:
        tokens = q.split()
        if len(tokens) >= 2:
            artist_hint = " ".join(tokens[:len(tokens)//2])
            album_hint = " ".join(tokens[len(tokens)//2:])

    itunes_results = []
    mb_results = []
    dg_results = []
    totals = {"albums": None, "tracks": None, "artists": None,
              "albums_shown": 0, "tracks_shown": 0}

    # Ask the providers for enough rows to fill the requested slice: page 1
    # fetches 2x so 'Show more' has somewhere to go, later pages extend the
    # window. Capped so a huge ?page= cannot ask for the whole catalogue.
    _fetch_mult = min(page + 1, 4)
    _itunes_limit = min(25 * _fetch_mult, 100)
    _mb_limit = min(15 * _fetch_mult, 60)
    _view_limit = min(20 * _fetch_mult, 80)
    # Discogs is only asked when a token exists, and only for the Albums view -
    # the Artists/Tracks tabs are MusicBrainz entity views and Discogs has no
    # equivalent. With no token this is False and the call is never made.
    _dg_enabled = _discogs_configured() and view == "album"
    _dg_limit = min(15 * _fetch_mult, 60)

    # Parallel execution of iTunes, MusicBrainz and the exact-count lookups. Four
    # workers so the count round-trips never hold up the result rows. The
    # artist/track views are fetched lazily - only the tab that is open.
    # A fifth worker covers Discogs when it is enabled; the pool is sized for the
    # enabled case so the extra request never serialises behind the others.
    with ThreadPoolExecutor(max_workers=5 if _dg_enabled else 4) as executor:
        f_itunes = executor.submit(search_itunes, q, limit=_itunes_limit)
        f_mb = executor.submit(search_musicbrainz, q, artist_hint=artist_hint, album_hint=album_hint, limit=_mb_limit)
        f_cnt = executor.submit(count_search_totals, q, artist_hint, album_hint)
        f_view = executor.submit(search_mb_entities, q, "artist" if view == "artist" else "recording",
                                 artist_hint, album_hint, _view_limit) if view != "album" else None
        f_dg = executor.submit(search_discogs, q, limit=_dg_limit,
                               artist_hint=artist_hint) if _dg_enabled else None
        try:
            itunes_results = f_itunes.result(timeout=6)
        except Exception as e:
            print(f"[SEARCH] iTunes task error: {e}")
        try:
            mb_results = f_mb.result(timeout=7)
        except Exception as e:
            print(f"[SEARCH] MusicBrainz task error: {e}")
        if f_dg is not None:
            try:
                dg_results = f_dg.result(timeout=7) or []
            except Exception as e:
                print(f"[SEARCH] Discogs task error: {e}")
        try:
            totals = f_cnt.result(timeout=10) or totals
        except Exception as e:
            print(f"[SEARCH] count task error: {e}")
        view_rows = []
        if f_view is not None:
            try:
                view_rows = f_view.result(timeout=9) or []
            except Exception as e:
                print(f"[SEARCH] {view} task error: {e}")

    # How many rows we render vs how many exist upstream. The lead-in needs
    # both: the total alone hides that only 25 are on screen.
    # The album total comes from the release search's own 'count', which
    # MusicBrainz returns for free. Reading it here - after the search has
    # finished - is deterministic, whereas asking for it inside the concurrent
    # count task raced the search and cost an extra rate-limited request.
    if totals.get("albums") is None:
        _known_total = _MB_RELEASE_TOTAL.get(
            _build_lucene_query(q, artist_hint, album_hint))
        if _known_total is not None:
            totals["albums"] = _known_total
    totals["albums_shown"] = len(itunes_results) + len(mb_results) + len(dg_results)
    totals["tracks_shown"] = sum(
        int(i.get("track_count") or 0) for i in itunes_results) or 0
    # Paging state, consumed by the client to build the 'Show more' strip.
    _available = totals["albums_shown"] if view == "album" else len(view_rows)
    _start = (page - 1) * per_page
    totals["page"] = page
    totals["per_page"] = per_page
    totals["available"] = _available
    totals["has_more"] = _start + per_page < _available
    totals["start_index"] = min(_start, _available)
    totals["end_index"] = min(_start + per_page, _available)
    for key in ("albums", "tracks", "artists"):
        totals[key + "_exact"] = totals.get(key) is not None
        if totals.get(key) is None:
            totals[key] = 0          # unknown, but never blank / never fake
    # A MusicBrainz total of 0 while iTunes still returned rows would render as
    # 'Found 0 Album(s)' above 25 visible albums. The albums total counts only
    # MusicBrainz releases, so floor it at the number of rows actually shown and
    # flag it, so the client can say 'at least N' instead of a false 0.
    if totals["albums"] < totals["albums_shown"]:
        totals["albums"] = totals["albums_shown"]
        totals["albums_exact"] = False
    log_line("SEARCH", f"q={q!r} albums={totals['albums']} "
                       f"tracks={totals['tracks']} artists={totals['artists']} "
                       f"shown={totals['albums_shown']}")

    with NAV_LOCK:
        if itunes_results:
            FAI_NAVIGATION['stats']['itunes_hits'] += len(itunes_results)
        if mb_results:
            FAI_NAVIGATION['stats']['musicbrainz_hits'] += len(mb_results)

    def _respond(body):
        # Counts ride along in a header so the payload stays a plain HTML
        # fragment (the client assigns it straight to innerHTML) while the
        # lead-in can still be rewritten from the real numbers.
        resp = Response(body, mimetype="text/html")
        try:
            resp.headers["X-Search-Totals"] = json.dumps(totals)
        except Exception:
            pass
        return resp

    # ---- Artists / Tracks filter views -----------------------------------
    # These tabs now do real work instead of sitting there as decoration.
    if view != "album":
        label = "Artists" if view == "artist" else "Tracks"
        if not view_rows:
            return _respond(
                f'<div class="section-label">{label}</div>'
                f'<div class="empty-msg">No matching {label.lower()} found. '
                f'Try different search terms or check spelling.</div>')
        out = [f'<div class="section-label">{label}</div>']
        for row in view_rows[_start:_start + per_page]:
            if view == "artist":
                nm = esc(row.get("name", "Unknown Artist"))
                bits = [b for b in (row.get("type", ""), row.get("country", "")) if b]
                sub = esc(" • ".join(bits))
                out.append(
                    f'<div class="album-item">'
                    f'  <div class="album-meta">'
                    f'    <div class="album-artist">{nm} <span class="badge badge-mb">MusicBrainz</span></div>'
                    f'    <div class="album-sub">{sub}</div>'
                    f'  </div>'
                    f'</div>')
            else:
                ti = esc(row.get("title", "Unknown Track"))
                ar = esc(row.get("artist", ""))
                ms = row.get("length_ms") or 0
                dur = f"{ms//60000}:{(ms%60000)//1000:02d}" if ms > 0 else ""
                sub = esc(dur)
                out.append(
                    f'<div class="album-item">'
                    f'  <div class="album-meta">'
                    f'    <div class="album-artist">{ti} <span class="badge badge-mb">MusicBrainz</span></div>'
                    f'    <div class="album-title">{ar}</div>'
                    f'    <div class="album-sub">{sub}</div>'
                    f'  </div>'
                    f'</div>')
        return _respond("".join(out))

    # ---- Albums view (default) ------------------------------------------
    html_out = '<div class="section-label">Search Results</div>'

    if not itunes_results and not mb_results and not dg_results:
        return _respond(
            '<div class="section-label">Search Results</div>'
            '<div class="empty-msg">No matching albums found. Try different search terms or check spelling.</div>')

    # One interleaved list, so BOTH providers get a share of every page.
    # Slicing 'iTunes first, then MusicBrainz' was wrong: a query with 40
    # iTunes rows filled the whole 40-row window and MusicBrainz got ZERO
    # slots, so the dialog showed no MusicBrainz results at all despite
    # albums_shown reporting them. Round-robin keeps both sources visible.
    # Discogs joins the same round-robin as a third source. When it is absent
    # (no token, or the query returned nothing) the ORIGINAL two-argument call
    # is used, so the iTunes/MusicBrainz page is byte-for-byte what it always was.
    if dg_results:
        _combined = _interleave_sources([("itunes", itunes_results),
                                         ("musicbrainz", mb_results),
                                         ("discogs", dg_results)])
    else:
        _combined = _interleave_providers(itunes_results, mb_results)
    _window = _combined[_start:_start + per_page]

    # Render in ONE pass over the interleaved window. Splitting it back into an
    # iTunes list and a MusicBrainz list and rendering those in two loops would
    # regroup the rows - the counts would be right but the list would read as
    # a block of iTunes followed by a block of MusicBrainz, which is the
    # original behaviour this replaced.
    for _prov, item in _window:
        title = esc(item.get("title"))
        artist = esc(item.get("artist"))
        art = item.get("art_thumb") or ""
        year = esc(item.get("year", ""))

        if _prov == "itunes":
            genre = esc(item.get("genre", ""))
            tracks = item.get("track_count", "")
            meta_sub = []
            if tracks:
                meta_sub.append(f"{tracks} Track(s)")
            if genre:
                meta_sub.append(genre)
            sub_text = "  ".join(meta_sub)
            if year:
                sub_text = (sub_text + " • " + year) if sub_text else year
        else:
            country = esc(item.get("country", ""))
            meta_sub = []
            if year: meta_sub.append(year)
            # Discogs rows carry a 'format' ('Vinyl • Album • LP') instead of a
            # country. It is the single most useful thing on a Discogs row -
            # it is how a user tells the CD pressing from the vinyl one - so it
            # takes the same slot rather than being dropped.
            if _prov == "discogs":
                fmt = esc(item.get("format", ""))
                if fmt: meta_sub.append(fmt)
            elif country:
                meta_sub.append(country)
            sub_text = "  ".join(meta_sub)

        rid = esc(item.get("id"))
        # Each source gets its own badge so a user can always tell which
        # provider a row came from - the whole point of a third provider is
        # that its tracklist can be checked against the others.
        if _prov == "itunes":
            badge = "badge badge-itunes\">iTunes"
        elif _prov == "discogs":
            badge = "badge badge-dg\">Discogs"
        else:
            badge = "badge badge-mb\">MusicBrainz"
        html_out += f'''<div class="album-item" onclick="pick('{_prov}', '{rid}')">
  <div class="tick">&#9654;</div>
  <img src="{art}" class="album-thumb" alt="" onerror="this.src='/static/noart.png';this.onerror=null;">
  <div class="album-meta">
    <div class="album-artist">{artist} <span class="{badge}</span></div>
    <div class="album-title">{title}</div>
    <div class="album-sub">{sub_text}</div>
    <div class="item-links"><span class="link">More&#8230;</span><span class="link-gap">&nbsp;&nbsp;&nbsp;</span><span class="link">Buy</span></div>
  </div>
</div>'''

    return _respond(html_out)
# ==========================================================
# UNIFIED MODERN FAI UI (HTML/CSS/JS)
# ==========================================================
COMMON_CSS = """
* { box-sizing: border-box; }
html { height: 100%; }
/* ---- Windows Vista / 7 "Aero" Find Album Information dialog -------------
   The real FAI window is not a themed application: the Aero glass is only
   the host frame (title bar + close box) drawn by the player. The page we
   serve is the plain white IE7 content area underneath it, containing
     * a blue lead-in  'Found 500+ Album(s) containing "...",',
     * two hairline separated columns, 'Existing Information' and 'Search',
     * a flat result list of 48px covers with artist / album title, a grey
       'N Track(s)  Genre' line and small 'More...  Buy' links,
     * a white command strip: privacy link left, Next + Cancel right.
   Both columns and every list row are laid out with floats and fixed
   widths instead of flexbox, so IE7 renders this exact markup with only
   the outer flex context switched off below, and every gradient has a
   progid filter equivalent in the conditional block at the end. ------- */
body { font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; line-height: 1.35; color: #1A1A1A; margin: 0; padding: 0; height: 100%; overflow: hidden; background-color: #FFFFFF; display: flex; flex-direction: column; }
/* Lead-in: 'Found 500+ Album(s) containing "...",' in FAI link blue. */
.header-area { flex-shrink: 0; padding: 11px 12px 9px 12px; border-bottom: 1px solid #DCE6F0; background-color: #FFFFFF; background-image: linear-gradient(to bottom, #FFFFFF 0%, #F4F8FC 100%); }
.header-text { margin: 0; font-size: 12pt; font-weight: 400; color: #0B5AA6; line-height: 1.4; }
.header-tag { font-size: 12pt; color: #0B5AA6; }
/* Two hairline separated columns, 48 / 52. Only the RESULT LIST scrolls, so
   the query box and filter strip stay put - see .results-scroll below. */
.main-container { flex: 1; overflow: hidden; }
.left-pane { float: left; width: 48%; height: 100%; overflow-y: auto; padding: 11px 12px; border-right: 1px solid #DCE6F0; }
/* Plain bold column caption - the authentic FAI headings carry no rule,
   no caps and no colour. */
.section-label { margin: 0 0 8px 0; padding: 0; font-size: 9pt; font-weight: 700; color: #1A1A1A; border: 0; text-transform: none; letter-spacing: 0; }
/* Result row: 48px cover on the left, then artist / album / grey meta and
   the small 'More...  Buy' link pair. Flat - no card, no rule, no radius;
   only the selected / hovered row gets a tinted field and a hairline. */
/* overflow:hidden is load-bearing: the cover is floated inside the row, so
   without a formatting context here the row box would collapse and the
   float would spill through its background and border. */
.album-item { position: relative; float: left; width: 100%; padding: 4px 6px; overflow: hidden; cursor: pointer; border: 1px solid transparent; }
.album-item:hover { border-color: #C6D8EA; background-color: #F4F8FC; }
.album-item.sel { border-color: #7FA8CE; background-color: #E4EFFA; }
.tick { display: none; }
.album-item:hover .tick { display: block; position: absolute; left: 5px; top: 22px; font-size: 9pt; color: #0B5AA6; }
.album-thumb { width: 48px; height: 48px; float: left; margin-right: 10px; border: 1px solid #C9D3DE; background-color: #F0F3F7; }
.album-meta { margin-left: 60px; }
.album-artist { font-size: 9pt; font-weight: 700; color: #1A1A1A; }
.album-title { font-size: 9pt; color: #1A1A1A; }
.album-sub { margin-top: 1px; font-size: 9pt; color: #7F7F7F; }
.item-links { margin-top: 2px; font-size: 8.5pt; }
.badge { font-size: 8pt; }
.badge-itunes { color: #7A6A3E; }
.badge-mb { color: #4A6B52; }
/* Discogs: a distinct hue from the other two so three sources stay tellable
   apart at a glance in a long interleaved list. */
.badge-dg { color: #6B4A7A; }
/* 'Existing Information' block: 48px cover plus a three line summary. */
.existing-info { padding: 8px; overflow: hidden; border: 1px solid #E4E9EF; background-color: #FBFCFE; }
.existing-thumb { width: 48px; height: 48px; float: left; margin-right: 10px; border: 1px solid #C9D3DE; background-color: #F0F3F7; }
.existing-body { margin-left: 60px; }
.existing-title { font-size: 9pt; color: #1A1A1A; }
.existing-artist { margin-top: 1px; font-size: 9pt; color: #0B5AA6; }
.existing-sub { margin-top: 1px; font-size: 9pt; color: #1A1A1A; }
.existing-links { margin-top: 5px; font-size: 9pt; }
/* FAI hyperlink blue, underlined only on hover. */
.link { color: #0B5AA6; cursor: pointer; text-decoration: none; }
/* Where the panel's values came from. The panel reports what WMP HOLDS, and
   the matched album is a different thing entirely - labelling it stops the two
   being read as the same information. */
.existing-source { margin-top: 4px; font-size: 8pt; color: #5A6B7B; }
.existing-empty { font-size: 9pt; color: #5A6B7B; font-style: italic; }
/* Inline editor behind the Edit link. Floats and plain block layout only - the
   dialog host is an IE7-era engine and must not be handed flexbox. */
.existing-edit { margin-top: 8px; padding-top: 8px; border-top: 1px solid #E4E9EF; }
.edit-field { margin-bottom: 6px; }
.edit-label { display: block; font-size: 8pt; color: #404A55; margin-bottom: 2px; }
.edit-input { width: 200px; padding: 2px 4px; font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; color: #1A1A1A; border: 1px solid #ADADBD; background-color: #FFFFFF; }
.edit-actions { margin-top: 6px; }
.edit-note { margin-top: 6px; font-size: 8pt; color: #5A6B7B; }
.link:hover { text-decoration: underline; }
.link-gap { color: #0B5AA6; }
.empty-msg { padding: 18px 8px; font-size: 9pt; color: #7F7F7F; text-align: center; }
/* Command strip: privacy link left, Next + Cancel right. */
.footer { flex-shrink: 0; padding: 8px 12px; overflow: hidden; border-top: 1px solid #DCE6F0; background-color: #FFFFFF; background-image: linear-gradient(to bottom, #F4F8FC 0%, #FFFFFF 100%); }
.footer-left { float: left; padding-top: 4px; font-size: 9pt; }
.footer-right { float: right; }
/* Windows 7 command button: neutral glass, 2px radius, 1px grey edge. The
   default command (Next) swaps that edge for the Aero focus blue. */
.btn { display: inline-block; min-width: 74px; margin-left: 6px; padding: 3px 12px; font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; color: #1A1A1A; text-align: center; cursor: pointer; outline: none; border: 1px solid #ADADAD; border-radius: 2px; background-color: #F0F0F0; background-image: linear-gradient(to bottom, #FCFCFC 0%, #F2F2F2 48%, #E6E6E6 52%, #F0F0F0 100%); }
.btn:hover { border-color: #7EB4EA; background-image: linear-gradient(to bottom, #FFFFFF 0%, #F3F8FD 48%, #E0EBF8 52%, #F3F8FD 100%); }
.btn:active { background-image: linear-gradient(to bottom, #E0E0E0 0%, #E8E8E8 48%, #F4F4F4 52%, #E8E8E8 100%); }
.btn-default { border-color: #7EB4EA; background-image: linear-gradient(to bottom, #FFFFFF 0%, #EFF6FD 48%, #DCEAF9 52%, #EFF6FD 100%); }
.btn-primary { font-weight: 700; }
/* Disabled last so it wins over :hover / :active at equal specificity. */
.btn[disabled] { color: #A6A6A6; cursor: default; border-color: #D6D6D6; background-image: linear-gradient(to bottom, #F6F6F6 0%, #EFEFEF 48%, #E6E6E6 52%, #F6F6F6 100%); }
/* Search box with the in-field clear glyph, then the
   'Artists | Albums | Tracks' filter strip underneath it. */
.search-box-row { position: relative; margin-bottom: 4px; }
.search-input { width: 100%; padding: 3px 24px 3px 6px; font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; color: #1A1A1A; border: 1px solid #7F9DB9; background-color: #FFFFFF; outline: none; }
.search-input:focus { border-color: #0B5AA6; }
.search-clear { position: absolute; right: 4px; top: 5px; width: 15px; height: 15px; padding: 0; font-size: 8pt; line-height: 13px; text-align: center; color: #6A7B8C; cursor: pointer; border: 1px solid #C3CFDA; background-color: #F1F4F8; }
.filter-row { padding: 2px 0 6px 0; margin-bottom: 6px; font-size: 9pt; color: #1A1A1A; border-bottom: 1px solid #E4E9EF; }
.filter-active { font-weight: 700; }
/* The strip is now a real control: pointer + hover so it reads as clickable. */
.filter-tab { cursor: pointer; padding: 1px 2px; }
.filter-tab:hover { text-decoration: underline; }
.filter-sep { margin: 0 5px; color: #9AA7B4; }
/* ---- The results list owns its scrollbar ---------------------------
   The authentic FAI dialog (reference screenshot) has ONE scrollbar, and
   it belongs to the result list only: it starts just below the
   'Artists | Albums | Tracks' strip and runs to the foot of the pane, with
   the usual Win32 arrow buttons top and bottom. The search box and the
   filter strip sit ABOVE it and never scroll away.

   Two things made that impossible before:
     1. .album-item is float:left, and a float does not contribute to its
        container's scroll height, so the pane reported scrollHeight ==
        clientHeight and drew no bar at all.
     2. the pane itself was the scroller, which would have dragged the
        search box off the top.
   The list is therefore its own flex item that takes the leftover height
   and scrolls, with 'overflow: hidden' establishing the formatting context
   that makes the float stack measurable. 'scroll' rather than 'auto' so
   the bar is always drawn, exactly as in the reference. --------------- */
.right-pane { margin-left: 48%; height: 100%; padding: 11px 12px; overflow: hidden; display: flex; flex-direction: column; }
.section-label, .search-box-row, .filter-row { flex: 0 0 auto; }
.results-scroll { flex: 1 1 auto; min-height: 0; overflow: hidden; overflow-y: scroll; }
/* Track rows on /confirm share the result row look: flat, hairline on
   hover, tinted field when the track is staged for writing. */
.track-row { padding: 3px 6px; margin-bottom: 1px; overflow: hidden; cursor: pointer; font-size: 9pt; color: #1A1A1A; border: 1px solid transparent; }
.track-row:hover { border-color: #C6D8EA; background-color: #F4F8FC; }
.track-row.selected { border-color: #7FA8CE; background-color: #E4EFFA; }
.track-row input[type="checkbox"] { float: left; margin: 3px 7px 0 0; cursor: pointer; }
.track-num { float: left; width: 22px; color: #7F7F7F; }
/* title and artist are siblings, so both float side by side inside the row
   the way the previous flex row rendered them; the time floats right. */
.track-title { float: left; max-width: 55%; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.track-artist { float: left; max-width: 33%; margin-left: 7px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 8.5pt; color: #7F7F7F; }
.track-time { float: right; font-size: 8.5pt; color: #7F7F7F; }
/* Composer line, rendered only when the provider supplied a real credit. */
.track-composer { clear: both; padding-left: 30px; font-size: 8pt; color: #8C7B5A; }
.disc-header { margin: 12px 0 4px 0; padding-bottom: 2px; font-size: 9pt; font-weight: 700; color: #1A1A1A; border-bottom: 1px solid #E4E9EF; }
.selection-badge { margin-top: 12px; padding: 6px 8px; font-size: 9pt; font-weight: 700; color: #1A1A1A; text-align: center; border: 1px solid #E4E9EF; background-color: #F4F8FC; }
.wmp-context-card { padding: 8px 9px; margin-bottom: 10px; font-size: 9pt; color: #1A1A1A; border: 1px solid #E4E9EF; background-color: #FBFCFE; }
.nav-steps { padding: 7px 9px; margin-bottom: 10px; font-size: 8.5pt; color: #404040; border: 1px solid #E4E9EF; background-color: #F4F8FC; }
/* ---- IE7 fallback: no CSS3 gradients, no flexbox -------------------
   IE7 has neither flexbox nor linear-gradient(), so all it really needs
   is the outer flex context replaced by normal flow plus the float based
   two column layout. Gradients become DXImageTransform filter
   equivalents, percentage widths become fixed pixels and radii go. --- */
<!--[if lt IE 8]>
body { display: block; height: auto; overflow: auto; }
.main-container, .header-area, .footer, .left-pane, .right-pane { display: block; }
.left-pane { float: left; width: 300px; height: auto; }
.right-pane { margin-left: 312px; height: auto; }
/* IE7 has no flexbox, so it cannot let the list take the leftover height.
   Give it a fixed height instead so it STILL gets its own scrollbar with
   arrow buttons, rather than growing forever and pushing the command strip
   off the dialog.

   This one number is what sets the HEIGHT OF THE WHOLE DIALOG in WMP. WMP sizes
   the Find Album Information frame to the content area, and in the IE7 branch
   `body` is `height: auto`, so the document is exactly as tall as the stacked
   header + panes + footer. Every pixel here is a pixel of dialog height.

   It was 360px, which made the dialog about 110px taller than the reference
   FAI window and left a large empty field under the results. 260px is the
   list area in the reference screenshot. The list still scrolls on its own -
   it is `overflow-y: scroll`, so the bar is always drawn, arrows and all. */
.results-scroll { height: 260px; overflow-y: scroll; }
.footer-left, .footer-right, .search-clear, .existing-thumb, .album-thumb, .track-num, .track-time, .track-title { display: inline; }
.search-clear { position: static; margin-left: 4px; }
.btn { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#FCFCFC', endColorstr='#E6E6E6', type='0'); }
.btn-default, .btn:hover { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#FFFFFF', endColorstr='#DCEAF9', type='0'); }
.btn:active { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#E0E0E0', endColorstr='#F4F4F4', type='0'); }
.btn[disabled] { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#F6F6F6', endColorstr='#E6E6E6', type='0'); }
.album-item:hover, .track-row:hover { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#FFFFFF', endColorstr='#DCEAF9', type='0'); }
.album-item.sel, .track-row.selected { background-image: none !important; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#E4EFFA', endColorstr='#E4EFFA', type='0'); }
.btn, .search-input, .search-clear, .album-item, .track-row, .existing-info, .section-label, .filter-row, .results-scroll, .edit-input, .existing-edit { border-radius: 0; }
.track-row, .album-item, .footer, .main-container, .existing-info, .results-scroll { zoom: 1; }
<![endif]-->
"""

@app.route("/FAI/ui")
def unified_ui():
    q = (request.args.get("artist", "") + " " + request.args.get("album", "")).strip()
    wmp_artist = request.args.get("artist", "")
    wmp_album = request.args.get("album", "")
    wmp_track = request.args.get("track", "")
    # A library "Update album info" arrives with NOTHING but ?requestid= - no
    # disc, no wmid, and no artist/album/track. The old copy read that as
    # "Windows Media Player did not pass any disc information", which is both
    # wrong (WMP did open the dialog, for a specific library album) and useless.
    # Logged from a real session on 'The Blue Room - EP' (Coldplay):
    #   GET /FAI/ui?...&requestid=D86F70C1-08E2-4219-8F86-7FE6A1C98974
    # GetMDQByRequestID() still answers for that id and carries the album's
    # CURRENT tags, so the panel is filled in from the MDQ on load.
    request_id = (request.args.get("requestid") or request.args.get("requestID")
                  or "").strip()
    wmp_cd = (raw_query_arg("cd") or raw_query_arg("CD") or "").strip()
    wmp_toc = (raw_query_arg("toc") or raw_query_arg("TOC") or "").strip()
    has_context = bool(wmp_track or wmp_artist or wmp_album)
    # Track count and running time are read from the disc's own TOC, so they are
    # available even when WMP sends no tags and the MDQ is empty. Verified on
    # the Course of Nature CD: 10 tracks, and the first track comes out at 3:03
    # - exactly the length WMP shows behind the dialog. This is what the panel
    # can honestly report instead of "No existing information".
    disc_durations = parse_wmp_toc(wmp_cd or wmp_toc)
    disc_track_count = len(disc_durations)
    disc_total_ms = sum(disc_durations)
    disc_total_text = format_duration(disc_total_ms)
    disc_summary = ""
    if disc_track_count:
        # Only append the running time when it is real. A TOC whose frames all
        # collapse to a sub-second length would otherwise render "2 audio
        # tracks -  total" with an empty gap where the time should be.
        disc_summary = (f"{disc_track_count} audio track"
                        f"{'s' if disc_track_count != 1 else ''}")
        if disc_total_text:
            disc_summary += f" \u2022 {disc_total_text} total"
    if wmp_cd or wmp_toc:
        flow = "disc"
    elif has_context or request_id:
        flow = "library"
    else:
        flow = "unknown"
    # The authentic FAI lead-in names the whole rip it was opened for, e.g.
    #   Found 500+ Album(s) containing "Mr. E's Beautiful Blues ... Various Artists".
    # WMP hands us those pieces as separate query values; join them back up.
    rip_name = " ".join(x for x in (wmp_track, wmp_artist, wmp_album) if x).strip() or q

    session_id = get_session_id()
    track_fai_navigation(session_id, 'ui_search', {
        'artist': wmp_artist,
        'album': wmp_album,
        'track': wmp_track,
        'query': q
    })

    return render_template_string("""<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Find Album Information</title>
  <style>{{ css|safe }}</style>
</head>
<body>
  <div class="header-area">
    <div class="header-text" id="leadIn">Searching for &quot;{{ rip_name|e }}&quot;...</div>
  </div>
  <div class="main-container">
    <div class="left-pane">
      <div class="section-label">Existing Information</div>
      <div class="existing-info">
        <img src="/static/noart.png" class="existing-thumb" id="existingThumb" alt="" onerror="this.onerror=null;">
        <div class="existing-body">
          <div class="existing-title" id="existingTitle">{% if wmp_album or wmp_track %}{{ (wmp_album or wmp_track)|e }}{% elif disc_summary %}<span class="existing-empty">{{ disc_summary|e }}</span>{% elif flow == 'disc' %}<span class="existing-empty">Disc in the drive&hellip;</span>{% else %}<span class="existing-empty">Reading current information&hellip;</span>{% endif %}</div>
          <div class="existing-artist" id="existingArtist">{% if wmp_artist %}{{ wmp_artist|e }}{% elif disc_track_count %}Audio CD in the drive{% endif %}</div>
          <div class="existing-sub" id="existingSub">{% if wmp_track and wmp_album %}{{ wmp_track|e }}{% elif disc_track_count %}No album or artist tags on the disc yet{% endif %}</div>
          <div class="existing-source" id="existingSource">{% if has_context %}Currently stored by Windows Media Player.{% elif flow == 'library' %}Reading the current tags of this library album&hellip;{% elif disc_track_count %}Read from the disc&hellip;{% elif flow == 'disc' %}Reading the disc currently in the drive&hellip;{% else %}No disc in the drive&hellip;{% endif %}</div>
          <div class="existing-links"><span class="link" id="editLink" onclick="editExisting(); return false;">Edit</span><span class="link-gap">&nbsp;&nbsp;&nbsp;</span><span class="link">Buy</span></div>
          <div class="existing-edit" id="editNote" style="display:none;"></div>
        </div>
      </div>
    </div>
    <div class="right-pane">
      <div class="section-label">Search</div>
      <div class="search-box-row">
        <input type="text" id="sq" class="search-input" value="{{ q|e }}" onkeydown="if(event.keyCode==13) doSearch();">
        <button class="search-clear" onclick="clearSearch();" title="Clear">X</button>
      </div>
      <div class="filter-row" id="filterRow">
        <span class="link filter-tab" id="tabArtists" onclick="switchView('artist');">Artists</span><span class="filter-sep">|</span><span class="link filter-active filter-tab" id="tabAlbums" onclick="switchView('album');">Albums</span><span class="filter-sep">|</span><span class="link filter-tab" id="tabTracks" onclick="switchView('track');">Tracks</span>
      </div>
      <div id="results_area">
        <div class="results-scroll" id="results_scroll">
          {% if q %}<div class="empty-msg">Searching metadata databases...</div>{% else %}<div class="empty-msg">Enter a search and press Enter.</div>{% endif %}
        </div>
      </div>
    </div>
  </div>
  <div class="footer">
    <div class="footer-left"><span class="link" title="How Windows Media Player uses the data you supply">Read the privacy statement.</span></div>
    <div class="footer-right">
      <button class="btn btn-default" onclick="nextToSearch();">Next</button>
      <button class="btn" onclick="cancelToMainTask()">Cancel</button>
    </div>
  </div>
  <script>
    // Direct COM call - a truthiness guard on window.external.ReturnToMainTask
    // evaluates falsy inside the WMP dialog and silently skips the return.
    function cancelToMainTask() {
      try {
        if (window.external) {
          try { window.external.ReturnToMainTask(); } catch (e) { try { window.close(); } catch (x) {} }
        } else {
          window.close();
        }
      } catch (e) { try { window.close(); } catch (x) {} }
    }
  </script>
  <script>
    var SESSION_ID = "{{ session_id }}";
    // The strip used to be inert decoration. These tabs now drive a real
    // server-side view: artist | album | track.
    var CURRENT_VIEW = 'album';
    function setActiveTab(view) {
      CURRENT_VIEW = view;
      var ids = {artist: 'tabArtists', album: 'tabAlbums', track: 'tabTracks'};
      for (var k in ids) {
        var el = document.getElementById(ids[k]);
        if (!el) { continue; }
        var cls = 'link filter-tab';
        if (k === view) { cls += ' filter-active'; }
        el.className = cls;
      }
    }
    function switchView(view) {
      setActiveTab(view);
      doSearch();
    }
    // The whole result set is rendered and reached with the list's own
    // scrollbar, exactly like the reference dialog, so there is no paging
    // state and no 'next page' control to keep in sync.
    function doSearch() {
      var query = document.getElementById('sq').value.trim();
      if (!query) return;
      var resultsDiv = document.getElementById('results_area');
      var scrollBox = document.getElementById('results_scroll');
      var target = scrollBox || resultsDiv;
      target.innerHTML = '<div class="empty-msg">Searching Apple Music &amp; MusicBrainz...</div>';
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/api_search?q=' + encodeURIComponent(query) + '&view=' + CURRENT_VIEW, true);
      xhr.onreadystatechange = function() {
        if (xhr.readyState == 4) {
          target.innerHTML = xhr.responseText;
          applyTotals(xhr.getResponseHeader('X-Search-Totals'), query);
          target.scrollTop = 0;
        }
      };
      xhr.send();
    }
    // Render the REAL number of matches instead of a hardcoded '500+'.
    // The server sends MusicBrainz's exact totals in X-Search-Totals; when it
    // cannot, we fall back to the number of rows on screen instead of
    // inventing a figure. IE7-safe: no JSON.parse, no let/const, no arrows,
    // and no toLocaleString (absent in older engines).
    function escHtml(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function toNum(v) {
      if (v === null || v === undefined || v === '') { return null; }
      v = parseInt(v, 10);
      return isNaN(v) ? null : v;
    }
    function groupDigits(n) {
      if (n === null) { return ''; }
      var s = String(n), out = '', c = 0, i;
      for (i = s.length - 1; i >= 0; i--) {
        out = s.charAt(i) + out;
        c++;
        if (c % 3 === 0 && i > 0) { out = ',' + out; }
      }
      return out;
    }
    function applyTotals(header, query) {
      var t = null, lead = document.getElementById('leadIn');
      try { if (header) { t = eval('(' + header + ')'); } } catch (e) { t = null; }
      if (!t) {
        if (lead) { lead.innerHTML = 'Results for &quot;' + escHtml(query) + '&quot;'; }
        return;
      }
      var albums = toNum(t.albums), tracks = toNum(t.tracks), artists = toNum(t.artists);
      var shown = toNum(t.albums_shown) || 0;
      var exact = (t.albums_exact === true);
      if (lead) {
        if (albums === null || !exact) {
          lead.innerHTML = 'Found at least ' + shown + ' Album(s) containing &quot;'
                           + escHtml(query) + '&quot;.';
        } else if (albums > shown) {
          lead.innerHTML = 'Found ' + groupDigits(albums) + ' Album(s) containing &quot;'
                           + escHtml(query) + '&quot; - showing the top ' + shown + '.';
        } else {
          lead.innerHTML = 'Found ' + groupDigits(albums) + ' Album(s) containing &quot;'
                           + escHtml(query) + '&quot;.';
        }
      }
      var aEl = document.getElementById('tabArtists');
      var tEl = document.getElementById('tabTracks');
      var rEl = document.getElementById('tabAlbums');
      if (aEl) { aEl.innerHTML = 'Artists' + (artists === null ? '' : ' (' + groupDigits(artists) + ')'); }
      if (tEl) { tEl.innerHTML = 'Tracks' + (tracks === null ? '' : ' (' + groupDigits(tracks) + ')'); }
      if (rEl) { rEl.innerHTML = 'Albums' + (albums === null ? '' : ' (' + groupDigits(albums) + ')'); }
    }
    // The in-field 'X' in the authentic dialog wipes the query and the list.
    function clearSearch() {
      var el = document.getElementById('sq');
      el.value = '';
      var box = document.getElementById('results_scroll') || document.getElementById('results_area');
      box.innerHTML = '<div class="empty-msg">Enter a search and press Enter.</div>';
      el.focus();
    }
    // 'Next' is the default command: move focus into the search field and run
    // the current query, exactly as the real dialog jumps to the search pane.
    function nextToSearch() {
      var el = document.getElementById('sq');
      el.focus();
      if (el.value.replace(/^\\s+|\\s+$/g, '')) {
        doSearch();
      }
    }
    // ---- 'Existing Information' on the search page --------------------------
    // A library "Update album info" arrives with only ?requestid=, so there is
    // no album, artist or track in the URL to render. GetMDQByRequestID() still
    // answers for that id and carries the CURRENT tags, which is exactly what
    // this panel is for. Same helpers as the confirmation page.
    var SEARCH_REQUEST_ID = {{ request_id|tojson }};
    var SEARCH_FLOW = {{ flow|tojson }};
    // Decoded from the disc TOC on the server. Empty when WMP sent no TOC.
    var DISC_SUMMARY = {{ disc_summary|tojson }};

    // Tolerant reader for the disc's current tags. The library MDQ is ~1.3kB
    // and does contain a <track> block, but it does NOT use the
    // <tag><text>value</text></tag> shape everywhere - a prefix-free
    // <title>...<text> match came back empty in EVERY logged session, so the
    // element names differ from the obvious guess. Try each candidate name in
    // each shape rather than assuming one.
    // Regexes are built from LITERAL sources, never from escaped string
    // literals: a backslash-s inside a JS string literal collapses to a plain
    // 's', so the character-class form shipped as [sS] and matched nothing.
    // That shipped silently for a long time: disc_track / disc_artist /
    // disc_album came back empty in EVERY logged session because of it, and so
    // did the library panel. A regex literal has no such ambiguity.
    function reFrom(literal, name, prefix) {
      var src = literal.source;
      if (prefix) { src = src.replace('PREFIX', prefix); }
      if (name) { src = src.replace('TAG', name); }
      return new RegExp(src, 'i');
    }
    var RE_PREFIX_TEXT  = /(?:<PREFIX>)\\s*<TAG>[\\s\\S]*?<text>\\s*([\\s\\S]*?)\\s*<\\/text>/i;
    var RE_PREFIX_INNER = /(?:<PREFIX>)\\s*<TAG>\\s*([\\s\\S]*?)\\s*<\\/TAG>/i;
    var RE_TEXT         = /<TAG>[\\s\\S]*?<text>\\s*([\\s\\S]*?)\\s*<\\/text>/i;
    var RE_INNER        = /<TAG>\\s*([\\s\\S]*?)\\s*<\\/TAG>/i;

    function mqField(mdq, names, prefix) {
      if (!mdq) return '';
      for (var n = 0; n < names.length; n++) {
        var name = names[n], m;
        try {
          if (prefix) {
            // <text> FIRST. The MDQ nests a <word> breakdown inside every
            // <title>/<artist>, so reading the element's inner text yields
            // "Miracles (Someone Special) Miracles" - the value repeated with
            // its own word list. Verified against the real library MDQ.
            m = mdq.match(reFrom(RE_PREFIX_TEXT, name, prefix));
            if (m && m[1]) return String(m[1]).trim();
            m = mdq.match(reFrom(RE_PREFIX_INNER, name, prefix));
            if (m && m[1]) {
              var i = String(m[1]).replace(/<[^>]*>/g, '').trim();
              if (i) return i;
            }
          }
          m = mdq.match(reFrom(RE_TEXT, name, null));
          if (m && m[1]) return String(m[1]).trim();
          m = mdq.match(reFrom(RE_INNER, name, null));
          if (m && m[1]) {
            var inner = String(m[1]).replace(/<[^>]*>/g, '').trim();
            if (inner) return inner;
          }
        } catch (e) {}
      }
      return '';
    }

    // The library track's real content id. WMP reveals the COLLECTION guid only
    // AFTER the dialog closes - a real library run:
    //   15:08:16  write=WriteNamesEx-mdq-tagsonly-ok   (no wmid available yet)
    //   15:08:26  [WMID] captured C52A9FE8-...        (too late for this write)
    // Writing by MDQ is a CD_BY_MDQCD call and a library album has no disc, so
    // it is accepted and applies nothing. The MDQ's WMContentID is a real
    // handle on the track, and type 1 with it is the same call shape as the
    // collection write that DOES work (WriteNamesEx-wmid-ok).
    function mqContentId(mdq) {
      if (!mdq) return '';
      // Regex LITERAL, not an escaped string: a backslash-s inside a JS
      // string literal collapses to a plain 's' and matches nothing.
      try {
        var m = mdq.match(/<WMContentID>\s*([^<]+?)\s*<\/WMContentID>/i);
        return m ? String(m[1]).trim() : '';
      } catch (e) { return ''; }
    }

    // One-shot probe. The MDQ's element names are not guessable from the first
    // 120 characters the log keeps, and guessing at them has already cost a
    // wrong fix, so report the real inventory once and parse against fact.
    function mdqTags(mdq) {
      var out = {}, m, re = /<\\/?([A-Za-z0-9_:-]+)/g;
      while (mdq && (m = re.exec(mdq)) !== null) { out[m[1]] = 1; }
      var names = [], k;
      for (k in out) { if (out.hasOwnProperty(k)) names.push(k); }
      names.sort();
      return names;
    }

    function reportExisting(payload) {
      try {
        var b = new XMLHttpRequest();
        b.open('POST', '/client_error', true);
        b.setRequestHeader('Content-Type', 'application/json');
        b.send(JSON.stringify(payload));
      } catch (e) {}
    }

    function renderExistingInfo(mdq) {
      var tEl = document.getElementById('existingTitle');
      if (!tEl) return;
      var aEl = document.getElementById('existingArtist');
      var sEl = document.getElementById('existingSub');
      var srcEl = document.getElementById('existingSource');
      var d = {track_count: 0, disc_track: '', disc_artist: '', disc_album: ''};
      if (mdq) {
        try {
          d.track_count = (mdq.match(/<track>[\\s\\S]*?<\\/track>/g) || []).length;
          d.disc_album = mqField(mdq, ['albumTitle'], '')
                      || mqField(mdq, ['title', 'album'], 'album');
          d.disc_artist = mqField(mdq, ['artist', 'trackArtist', 'albumArtist']);
          // Strip the <album> block before looking for the track title. A lazy
          // <title>...</title> otherwise matches the ALBUM's nested title first
          // and the panel shows the album name as the track name - verified
          // against a nested MDQ, which returned 'The Blue Room' for a track
          // called 'See You Soon'.
          d.disc_track = mqField(
            String(mdq).replace(/<album>[\\s\\S]*?<\\/album>/gi, ''),
            ['trackTitle', 'title']);
        } catch (e) {}
      }
      reportExisting({
        page: 'existing_info_probe', flow: SEARCH_FLOW,
        has_request_id: !!SEARCH_REQUEST_ID,
        mdq_len: mdq ? mdq.length : 0,
        mdq_tags: mdqTags(mdq).join(','),
        mdq_head: mdq ? mdq.substr(0, 400) : '',
        found_album: d.disc_album, found_artist: d.disc_artist,
        found_track: d.disc_track,
        href: String(window.location.href)
      });
      var album  = d.disc_album  || {{ wmp_album|tojson  }} || '';
      var artist = d.disc_artist || {{ wmp_artist|tojson }} || '';
      var track  = d.disc_track  || {{ wmp_track|tojson  }} || '';
      if (!album && !artist && !track) {
        // A disc rip carries no tags, but the TOC still yields the track count
        // and running time the server decoded. Reporting those beats "No
        // existing information", which claimed there was nothing to say about
        // a disc that was plainly in the drive.
        if (DISC_SUMMARY) {
          tEl.innerHTML = escHtml(DISC_SUMMARY);
          if (aEl) aEl.innerHTML = 'Audio CD in the drive';
          if (sEl) sEl.innerHTML = 'No album or artist tags on the disc yet';
          if (srcEl) srcEl.innerHTML = 'Read from the disc&hellip;';
          return;
        }
        tEl.innerHTML = '<span class="existing-empty">No existing information</span>';
        if (aEl) aEl.innerHTML = '';
        if (sEl) sEl.innerHTML = '';
        if (srcEl) {
          // A CD RIP arrives with a TOC but NO requestid, so
          // GetMDQByRequestID() cannot be asked and 'mdq' is empty by
          // construction - NOT because the drive is empty. The old copy said
          // "No disc in the drive" here, which is plainly false while a disc
          // is in it and searching for it. Verified from a real session: the
          // client probe reported flow='disc' with a 15-track TOC in the URL
          // and mdq_len=0. Only claim the drive is empty when WMP really
          // gave us no disc at all.
          if (SEARCH_FLOW === 'library') {
            srcEl.innerHTML = 'Windows Media Player did not report the current tags of this album.';
          } else if (SEARCH_FLOW === 'disc') {
            srcEl.innerHTML = 'Disc in the drive has no stored tags yet. '
              + 'Pick an album below to tag it.';
          } else {
            srcEl.innerHTML = 'No disc in the drive - use the search box to find the album.';
          }
        }
        return;
      }
      tEl.innerHTML = escHtml(album || track);
      if (aEl) aEl.innerHTML = artist ? escHtml(artist) : '';
      if (sEl) sEl.innerHTML = (album && track) ? escHtml(track) : '';
      if (srcEl) {
        srcEl.innerHTML = d.track_count
          ? ('Currently stored by Windows Media Player (' + d.track_count + ' track(s)).')
          : 'Currently stored by Windows Media Player.';
      }
    }

    // Edit on the search page. There is no matched album here yet, so the
    // authentic action is WMP's own metadata editor. When the host does not
    // expose it the link must still say something useful rather than sit
    // inert - editing happens on the confirmation page, once an album is picked.
    function editExisting() {
      try {
        if (window.external && window.external.EditMetadata) {
          window.external.EditMetadata();
          return;
        }
      } catch (e) { /* not available in this host - explain instead */ }
      var note = document.getElementById('editNote');
      if (!note) return;
      if (note.style.display !== 'none') { note.style.display = 'none'; return; }
      note.style.display = 'block';
      note.innerHTML = '<div class="edit-note">Pick an album from the results '
        + 'first &mdash; on the next screen you can edit its title, artist, year '
        + 'and genre before applying.</div>';
    }
    function pick(source, id) {
      var url = "/confirm?source=" + encodeURIComponent(source) + "&id=" + encodeURIComponent(id) + "&" + window.location.search.substring(1);
      window.location.href = url;
    }
    window.onload = function() {
      // Fill 'Existing Information' from the disc / library album WMP is
      // actually asking about. A library update carries only ?requestid=,
      // and GetMDQByRequestID is the only way to learn what is stored there.
      try {
        var mdq = '';
        if (window.external && SEARCH_REQUEST_ID) {
          mdq = window.external.GetMDQByRequestID(SEARCH_REQUEST_ID) || '';
        }
        renderExistingInfo(mdq);
      } catch (e) { try { renderExistingInfo(''); } catch (e2) {} }
      if (document.getElementById('sq').value.trim()) {
        doSearch();
      }
    };
  </script>
</body>
</html>""", css=COMMON_CSS, q=q, wmp_artist=wmp_artist, wmp_album=wmp_album, wmp_track=wmp_track, rip_name=rip_name, has_context=has_context, flow=flow, request_id=request_id, session_id=session_id, disc_summary=disc_summary, disc_track_count=disc_track_count)
# ==========================================================
# UNIFIED CONFIRMATION & TRACK SELECTION PAGE
# ==========================================================
@app.route("/confirm")
@app.route("/confirm_musicbrainz")
def confirm():
    source = request.args.get("source", "").lower()
    album_id = request.args.get("id", "")
    request_id = request.args.get("requestid") or request.args.get("requestID") or ""

    # Support legacy route /confirm_musicbrainz where source was not explicitly passed
    if not source and request.path == "/confirm_musicbrainz":
        source = "musicbrainz"
    elif not source:
        # If ID looks like a UUID, assume musicbrainz, otherwise iTunes integer ID
        source = "musicbrainz" if "-" in album_id else "itunes"

    wmp_track = request.args.get("track", "")
    wmp_artist = request.args.get("artist", "")
    wmp_album = request.args.get("album", "")
    # WMP's real CD flow passes ?cd=<hex>+<hex>... (e.g. 5+96+554B+83B5)
    # and often NO requestid at all. Read it raw: the '+' separators would
    # otherwise be decoded as spaces and stop identifying the disc.
    # WMP's library "Update album info" dialog carries ?wmid=<collection GUID>,
    # the exact collection it wants updated. Read it raw and reuse it so the
    # write targets the real library entry instead of a generated one.
    wmp_wmid = raw_query_arg("wmid") or raw_query_arg("WMID") or ""
    wmid_from_url = bool(wmp_wmid)
    # Kept separate from wmp_wmid: only a wmid WMP actually put in THIS
    # dialog's URL is authoritative enough to pre-claim a staged document.
    # wmp_wmid may be the LAST_WMID guess set below, and pre-binding a document
    # to a guess stops the genuine collection from ever claiming it.
    wmp_wmid_auth = wmp_wmid
    # Read the disc id BEFORE deciding anything about collections. A CD rip
    # never has - and never will have - a collection id, and stamping one onto
    # a disc document is actively harmful: logged from a real rip of
    # 'Sun Kil Moon - Tiny Cities', whose document inherited the previous
    # library album's collection, the tags landed but the cover did not, and
    # WMP then REOPENED the FAI dialog 3s after ReturnToMainTask:
    #
    #   12:00:49 [CLIENT] finish
    #   12:00:56 done_close / ReturnToMainTask-ok
    #   12:00:59 GET /FAI/default.aspx?...&cd=B+96+...&wmid=F62C9D85-...
    wmp_cd = raw_query_arg("cd") or raw_query_arg("CD") or ""
    if wmp_cd:
        log_line("CDID", f"dialog opened for cd={wmp_cd!r}")
    if wmp_wmid:
        _remember_wmid(wmp_wmid)
        log_line("WMID", f"dialog opened for wmid={wmp_wmid!r}")
    elif LAST_WMID and wmp_cd:
        log_line("WMID", f"CD flow: ignoring last seen collection "
                         f"{LAST_WMID!r} so the disc is not stamped with it")
    elif LAST_WMID:
        # WMP opened a LIBRARY dialog without ?wmid= - logged from real
        # sessions arriving with only ?requestid=. That leaves the confirm page
        # with no collection id, so it falls through to WriteNamesEx(2, STUB_MDQ,
        # ...) as the write target, and a stub MDQ's content id belongs to no
        # real track (see parse_mdq_content_ids). WMP accepts the call and
        # applies nothing, with no other symptom.
        #
        # LAST_WMID is the collection GUID WMP itself most recently asked us
        # about. A real collection id is strictly better than a stub that
        # matches nothing - but only for a LIBRARY album. A disc write must
        # never borrow it.
        wmp_wmid = LAST_WMID
        log_line("WMID", f"dialog had no wmid; using last seen "
                         f"collection {wmp_wmid!r} as the write target")
    wmp_cd = raw_query_arg("cd") or raw_query_arg("CD") or ""
    if wmp_cd:
        log_line("CDID", f"dialog opened for cd={wmp_cd!r}")
    # CD TOC string WMP passed into the FAI dialog - required by
    # window.external.WriteNames / WriteNamesEx on IWMPCDDVDWizardExternal.
    # Read raw (not request.args): WMP TOCs start with a literal '+' which
    # request.args would decode as a space, giving WriteNamesEx a TOC that
    # does not match the disc (WMP then silently applies nothing).
    wmp_toc = raw_query_arg("toc") or raw_query_arg("TOC") or ""
    if not wmp_toc and raw_query_arg("mdq"):
        wmp_toc = raw_query_arg("mdq")
    # NOTE: deliberately NO LAST_TOC fallback here. WMP opens the FAI dialog with
    # only ?requestid=... (no toc), so a TOC left over from an earlier disc
    # would be sent to WriteNamesEx and target the wrong disc. The authoritative
    # path is the MDQ: GetMDQByRequestID(REQUEST_ID) -> WriteNamesEx type 2.
    if not wmp_toc:
        with XML_LOCK:
            LAST_TOC = ""   # never let a stale TOC leak into this session
    log_line("CONFIRM", f"requestid={request_id!r} toc={wmp_toc!r} "
                        f"source={source} album_id={album_id!r} "
                        f"ua={request.headers.get('User-Agent', '')[:60]}")
    session_id = get_session_id()

    track_fai_navigation(session_id, 'confirm_view', {
        'source': source,
        'album_id': album_id,
        'request_id': request_id,
        # Recorded so the log shows whether the write target was a real
        # collection id and where it came from, or fell back to the stub-MDQ
        # path that WMP silently ignores.
        'wmid': wmp_wmid,
        'wmid_from_url': wmid_from_url
    })

    # Three sources, dispatched by name. The 'else' stays MusicBrainz so an
    # unknown or legacy source value keeps its original meaning - a request
    # carrying a bad source must not be silently reinterpreted as Discogs.
    if source == "itunes":
        details = get_itunes_album_details(album_id)
    elif source == "discogs":
        details = get_discogs_album_details(album_id)
    else:
        details = get_musicbrainz_album_details(album_id)

    if not details:
        return f"<h3>Error: Could not retrieve details for {esc(source)} album ID {esc(album_id)}. <a href='javascript:history.back()'>Go Back</a></h3>"

    tracks = details.get("tracks", [])
    album_guid = guid(details.get("id"))
    proxy_art = f"http://127.0.0.1/cover/album.jpg?url={requests.utils.quote(details.get('art_url',''))}" if details.get("art_url") else ""

    # Group tracks by disc number for clean multi-disc presentation
    discs = {}
    for t in tracks:
        d_num = t.get("disc", 1)
        if d_num not in discs:
            discs[d_num] = []
        discs[d_num].append(t)

    track_html = ""
    multi_disc = len(discs) > 1
    for d_num in sorted(discs.keys()):
        if multi_disc:
            track_html += f'<div class="disc-header">Disc {d_num}</div>'
        for t in discs[d_num]:
            t_num = t.get("number", 0)
            t_name = esc(t.get("name", "Unknown Track"))
            t_artist = esc(t.get("artist", details.get("artist")))
            # Show the composer when the provider gave us a real one. Empty
            # stays empty - a row that invents a composer is worse than none.
            t_composer = esc(t.get("composer", "") or "")
            composer_html = (f'<div class="track-composer">Composer: {t_composer}</div>'
                             if t_composer.strip() else "")
            dur_ms = t.get("duration_ms", 0)
            dur_str = f"{dur_ms//60000}:{(dur_ms%60000)//1000:02d}" if dur_ms > 0 else ""

            # Check if this track might match the WMP playing track
            is_match = False
            if wmp_track and wmp_track.lower() in t_name.lower():
                is_match = True

            checked = 'checked' if is_match or len(tracks) == 1 else ''
            cls_match = 'selected' if checked else ''

            track_html += f'''<div class="track-row {cls_match}" id="row_{t['id']}" onclick="toggleRow('{t['id']}')">
  <input type="checkbox" id="chk_{t['id']}" value="{t['id']}" {checked} onclick="event.stopPropagation(); syncRow('{t['id']}');">
  <div class="track-num">{t_num}.</div>
  <div class="track-title">{t_name}</div>
  <div class="track-artist">{t_artist}</div>
  <div class="track-time">{dur_str}</div>
  {composer_html}
</div>'''

    return render_template_string("""<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Find Album Information</title>
  <style>{{ css|safe }}</style>
</head>
<body>
  <div class="header-area">
    <div class="header-text">Found the album &quot;{{ details.title|e }}&quot; by {{ details.artist|e }}.</div>
  </div>
  <div id="discBanner" style="display:none;margin:0;padding:9px 12px;font-size:8.5pt;line-height:1.4;border-bottom:1px solid #E4E9EF;background:#FDF6E3;color:#7A5B12;">
  </div>
  <div class="main-container">
    <div class="left-pane">
      <div class="section-label">Existing Information</div>
      <div class="existing-info">
        <img src="/static/noart.png" class="existing-thumb" id="existingThumb" alt="" onerror="this.src='/static/noart.png';this.onerror=null;">
        <div class="existing-body">
          <div class="existing-title" id="existingTitle">{% if wmp_album or wmp_track %}{{ (wmp_album or wmp_track)|e }}{% else %}<span class="existing-empty">Reading current information&hellip;</span>{% endif %}</div>
          <div class="existing-artist" id="existingArtist">{% if wmp_artist %}{{ wmp_artist|e }}{% endif %}</div>
          <div class="existing-sub" id="existingSub">{% if wmp_track and wmp_album %}{{ wmp_track|e }}{% endif %}</div>
          <div class="existing-source" id="existingSource">{% if wmp_album or wmp_artist or wmp_track %}Currently stored by Windows Media Player.{% else %}Checking the disc and your library&hellip;{% endif %}</div>
          <div class="existing-links"><span class="link" id="editLink" onclick="editExisting(); return false;">Edit</span><span class="link-gap">&nbsp;&nbsp;&nbsp;</span><span class="link">Buy</span></div>
        </div>
        <div class="existing-edit" id="existingEdit" style="display:none;"></div>
      </div>
      <div class="existing-source" style="margin-top:6px;">The matched album <b>{{ details.title|e }}</b>{% if details.artist %} by {{ details.artist|e }}{% endif %} is what <b>Finish &amp; Apply</b> will write.</div>

      <div class="section-label" style="margin-top:16px;">Tracks to update</div>
      <div style="margin-bottom:8px;">
        <button class="btn" onclick="selectAll()">Select All</button>
        <button class="btn" onclick="selectNone()">Clear All</button>
      </div>
      <div class="existing-links" style="font-size:8.5pt;line-height:1.45;">
        Tick a single track to rename just that item, or use <b>Select All</b> to apply the metadata and cover art to the whole album.
      </div>
      <div id="selection_badge" class="selection-badge">
        0 tracks selected
      </div>
    </div>
    <div class="right-pane">
      <div class="section-label">Tracks</div>
      <div id="trackList">
        {{ track_html|safe }}
      </div>
    </div>
  </div>
  <div class="footer">
    <div class="footer-left"><span class="link">Read the privacy statement.</span></div>
    <div class="footer-right">
      <button class="btn" onclick="history.back()">Back</button>
      <button id="btnFinish" class="btn btn-primary" onclick="finishSync()" disabled>Finish &amp; Apply</button>
    </div>
  </div>

  <script>
    var ALL_TRACKS = {{ tracks_json|safe }};
    var ALBUM_DETAILS = {{ details_json|safe }};
    var REQUEST_ID = "{{ request_id }}";
    var SESSION_ID = "{{ session_id }}";
    var WMP_TOC = {{ wmp_toc_json|safe }};
    // WMP's real CD flow identifies the disc with ?cd=<hex>+<hex>... and may
    // send no requestid at all. Passed through raw so the '+' separators
    // survive (request.args would turn them into spaces).
    var WMP_CD = {{ wmp_cd_json|safe }};
    // Library flow: the collection GUID WMP is updating. Used as the write
    // type id and as the WMCollectionID in the generated XML.
    var WMP_WMID = {{ wmp_wmid_json|safe }};
    // Only a wmid that was really in this dialog's URL. wmp_wmid may be the
    // server's LAST_WMID guess, and a guess must not pre-claim a document.
    var WMP_WMID_AUTH = {{ wmp_wmid_auth_json|safe }};
    // MDQ for the current dialog session. Shared because finishSync() resolves it
    // and applyMetadata() needs it - a local var in finishSync is invisible to
    // the callback, which produced "'mdq' is undefined" on every apply.
    var CACHED_MDQ = '';
    var RESOLVED_MDQ = '';

    // ---- read the disc identity out of the MDQ WMP handed us -------------
    // WMP returns the ACTUAL contents of the disc in the drive. If that does
    // not match the album being applied, WMP may legitimately refuse the
    // write - so tell the user before they click Finish instead of after.
    // Regex LITERALS, never escaped string literals: a backslash-s inside a JS
    // string literal collapses to a plain 's', so the character-class form
    // shipped as [sS] and matched nothing at all. That is why disc_track /
    // disc_artist / disc_album were empty in EVERY logged session.
    var RE_TEXT_ANY   = /<TAG>[\\s\\S]*?<text>\\s*([\\s\\S]*?)\\s*<\\/text>/i;
    var RE_PREFIX_ANY = /(?:PREFIX)\\s*<TAG>[\\s\\S]*?<text>\\s*([\\s\\S]*?)\\s*<\\/text>/i;

    function mqText(xml, tag, prefix) {
      // 'prefix' is a full element name INCLUDING its angle brackets, e.g.
      // '<album>' - so PREFIX is substituted whole, not wrapped again.
      try {
        var lit = prefix ? RE_PREFIX_ANY : RE_TEXT_ANY;
        var src = lit.source;
        if (prefix) { src = src.replace('PREFIX', prefix); }
        var m = xml.match(new RegExp(src.replace('TAG', tag), 'i'));
        return m ? m[1] : '';
      } catch (e) { return ''; }
    }
    // The library track's real content id, from the MDQ. WMP reveals the
    // COLLECTION guid only AFTER the dialog closes, so on the first library
    // update of a session there is no wmid to write against:
    //   15:08:16  write=WriteNamesEx-mdq-tagsonly-ok   (wmid=-)
    //   15:08:26  [WMID] captured C52A9FE8-...        (10s too late for the write)
    // Writing by MDQ is a CD_BY_MDQCD call and a library album has no disc, so
    // WMP accepts it and applies nothing. The MDQ's WMContentID is a real
    // handle on the track, and type 1 with it is the same call shape as the
    // collection write that does work.
    function mqContentId(mdq) {
      if (!mdq) return '';
      // Regex LITERAL, not an escaped string: a backslash-s inside a JS
      // string literal collapses to a plain 's' and matches nothing.
      try {
        var m = mdq.match(/<WMContentID>\s*([^<]+?)\s*<\/WMContentID>/i);
        return m ? String(m[1]).trim() : '';
      } catch (e) { return ''; }
    }

    function parseDiscIdentity(mdq) {
      // What is ACTUALLY on the disc, as reported by WMP. Compared against the
      // album being applied this shows whether WMP has any chance of matching
      // the metadata to real tracks.
      var info = {track_count: 0, disc_track: '', disc_artist: '', disc_album: ''};
      if (!mdq) return info;
      try {
        var blocks = mdq.match(/<track>[\\s\\S]*?<\\/track>/g) || [];
        info.track_count = blocks.length;
        info.disc_track = mqText(mdq, 'title', '');
        info.disc_artist = mqText(mdq, 'artist', '');
        info.disc_album = mqText(mdq, 'title', '<album>');
      } catch (e) {}
      return info;
    }
    function norm(s) {
      return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    }

    // ---- 'Existing Information': what WMP currently holds -------------------
    // The panel used to show the MATCHED album, which made the incoming tags
    // look like tags already on the disc. It now reports the disc/library's
    // own state, and says so plainly when there is none - which is the common
    // case for a fresh rip and is exactly what the user needs to know.
    var WMP_CTX_ALBUM  = {{ wmp_album|tojson }};
    var WMP_CTX_ARTIST = {{ wmp_artist|tojson }};
    var WMP_CTX_TRACK  = {{ wmp_track|tojson }};

    function escHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function renderExistingInfo(mdq) {
      var tEl = document.getElementById('existingTitle');
      if (!tEl) return;
      var aEl = document.getElementById('existingArtist');
      var sEl = document.getElementById('existingSub');
      var srcEl = document.getElementById('existingSource');
      var img = document.getElementById('existingThumb');
      var d = parseDiscIdentity(mdq);
      // The MDQ is the disc as WMP sees it and outranks the query arguments.
      var album  = d.disc_album  || WMP_CTX_ALBUM  || '';
      var artist = d.disc_artist || WMP_CTX_ARTIST || '';
      var track  = d.disc_track  || WMP_CTX_TRACK  || '';
      if (img) { img.src = '/static/noart.png'; img.onerror = null; }
      if (!album && !artist && !track) {
        tEl.innerHTML = '<span class="existing-empty">No existing information</span>';
        if (aEl) aEl.innerHTML = '';
        if (sEl) sEl.innerHTML = d.track_count ? (d.track_count + ' untagged track(s)') : '';
        if (srcEl) srcEl.innerHTML = 'Nothing is stored for this disc yet - every field will be written.';
        return;
      }
      tEl.innerHTML = escHtml(album || track);
      if (aEl) aEl.innerHTML = artist ? escHtml(artist) : '';
      if (sEl) {
        sEl.innerHTML = (album && track) ? escHtml(track)
                    : (d.track_count ? (d.track_count + ' track(s)') : '');
      }
      if (srcEl) {
        srcEl.innerHTML = d.track_count
          ? ('Currently on the disc (' + d.track_count + ' track(s)).')
          : 'Currently stored by Windows Media Player.';
      }
    }

    // ---- Edit ---------------------------------------------------------------
    // Try WMP's own metadata editor first - that is what the authentic dialog
    // does, and the host exposes it on window.external. Never truthiness-test a
    // COM member (see the notes further down): call it inside try/catch and
    // fall back to the inline editor so the link is never dead.
    function editExisting() {
      var panel = document.getElementById('existingEdit');
      try {
        if (window.external && window.external.EditMetadata) {
          window.external.EditMetadata();
          return;
        }
      } catch (e) { /* not available in this host - use the inline editor */ }
      if (panel && panel.style.display !== 'none') { closeExistingEdit(); return; }
      openExistingEdit();
    }

    function fieldHtml(id, label, value) {
      return '<div class="edit-field"><label class="edit-label" for="' + id + '">'
           + label + '</label><input type="text" class="edit-input" id="'
           + id + '" value="' + escHtml(value) + '"></div>';
    }

    function openExistingEdit() {
      var panel = document.getElementById('existingEdit');
      if (!panel) return;
      panel.innerHTML =
          '<div style="font-size:8.5pt;font-weight:700;margin-bottom:6px;">'
        + 'Edit the album before applying</div>'
        + fieldHtml('editTitle', 'Album', ALBUM_DETAILS.title || '')
        + fieldHtml('editArtist', 'Artist', ALBUM_DETAILS.artist || '')
        + fieldHtml('editYear', 'Year', ALBUM_DETAILS.year || '')
        + fieldHtml('editGenre', 'Genre', ALBUM_DETAILS.genre || '')
        + '<div class="edit-actions">'
        + '<button class="btn" onclick="saveExistingEdit();">Save</button>'
        + '&nbsp;&nbsp;<button class="btn" onclick="closeExistingEdit();">Cancel</button>'
        + '</div>'
        + '<div class="edit-note">Changes the album that <b>Finish &amp; Apply</b> '
        + 'writes. Per-track titles are not changed.</div>';
      panel.style.display = 'block';
    }

    function closeExistingEdit() {
      var panel = document.getElementById('existingEdit');
      if (!panel) return;
      panel.style.display = 'none';
      panel.innerHTML = '';
    }

    // Writes straight into ALBUM_DETAILS - the object POSTed to
    // /store_staged_xml - so an edit genuinely reaches the applied document
    // rather than only changing what is displayed.
    function saveExistingEdit() {
      function val(id) {
        var el = document.getElementById(id);
        return el ? String(el.value).replace(/^\\s+|\\s+$/g, '') : '';
      }
      var t = val('editTitle'), a = val('editArtist'),
          y = val('editYear'), g = val('editGenre');
      if (!t && !a) {
        alert('Enter at least an album title or an artist.');
        return;
      }
      if (t) ALBUM_DETAILS.title = t;
      if (a) ALBUM_DETAILS.artist = a;
      if (y) ALBUM_DETAILS.year = y;
      if (g) ALBUM_DETAILS.genre = g;
      // Keep the lead-in honest about what is about to be written.
      try {
        var hdr = document.getElementsByTagName('*');
        for (var i = 0; i < hdr.length; i++) {
          if (hdr[i].className === 'header-text') {
            hdr[i].innerHTML = 'Found the album &quot;'
              + escHtml(ALBUM_DETAILS.title) + '&quot; by '
              + escHtml(ALBUM_DETAILS.artist) + '.';
            break;
          }
        }
      } catch (e2) {}
      beacon({page: 'existing_edit_saved', title: ALBUM_DETAILS.title,
              artist: ALBUM_DETAILS.artist, year: ALBUM_DETAILS.year,
              genre: ALBUM_DETAILS.genre, href: String(window.location.href)});
      closeExistingEdit();
    }

    function showDiscBanner(mdq) {
      var banner = document.getElementById('discBanner');
      if (!banner) return;
      var wantAlbum = ALBUM_DETAILS.title || '';
      var wantArtist = ALBUM_DETAILS.artist || '';
      var discTrack = mqText(mdq, 'title', '');
      var discArtist = mqText(mdq, 'artist', '');
      var discAlbum = mqText(mdq, 'title', '<album>');
      var discCount = parseDiscIdentity(mdq).track_count;
      if (!discTrack && !discAlbum && !discArtist) {
        // A stub MDQ just means there is no disc in the drive being
        // fingerprinted. That is NORMAL for a library "Update album info":
        // WMP opens the dialog with only ?requestid= and identifies the target
        // album by wmid, applying the metadata to library tracks instead of
        // burning it onto a CD. Do not warn about a missing disc here.
        banner.style.display = 'block';
        banner.style.background = '#E0E7FF';
        banner.style.color = '#312E81';
        banner.innerHTML = '<b>Library album</b> - applying to the tracks in '
          + 'your library (no disc required).';
        return;
      }
      var mismatch = (discAlbum && norm(discAlbum) !== norm(wantAlbum)) ||
                     (discArtist && norm(discArtist) !== norm(wantArtist));
      if (mismatch) {
        banner.style.display = 'block';
        banner.innerHTML = '<b>Disc mismatch:</b> the disc in the drive is ' +
          '<b>' + discTrack + '</b>' + (discArtist ? ' by ' + discArtist : '') +
          (discAlbum ? ' (' + discAlbum + ')' : '') +
          '. You are applying <b>' + wantAlbum + '</b>' +
          (wantArtist ? ' by ' + wantArtist : '') +
          ' - Windows Media Player may refuse to apply it.';
      } else {
        banner.style.display = 'block';
        banner.style.background = '#DCFCE7';
        banner.style.color = '#14532D';
        banner.innerHTML = 'Disc in drive matches: <b>' + discTrack + '</b>' +
          (discArtist ? ' by ' + discArtist : '') + '.';
      }
    }

    function syncRow(id) {
      var chk = document.getElementById('chk_' + id);
      var row = document.getElementById('row_' + id);
      if (chk.checked) {
        row.className = 'track-row selected';
      } else {
        row.className = 'track-row';
      }
      updateStatus();
    }

    function toggleRow(id) {
      var chk = document.getElementById('chk_' + id);
      chk.checked = !chk.checked;
      syncRow(id);
    }

    function selectAll() {
      for (var i = 0; i < ALL_TRACKS.length; i++) {
        var id = ALL_TRACKS[i].id;
        var chk = document.getElementById('chk_' + id);
        if (chk) {
          chk.checked = true;
          document.getElementById('row_' + id).className = 'track-row selected';
        }
      }
      updateStatus();
    }

    function selectNone() {
      for (var i = 0; i < ALL_TRACKS.length; i++) {
        var id = ALL_TRACKS[i].id;
        var chk = document.getElementById('chk_' + id);
        if (chk) {
          chk.checked = false;
          document.getElementById('row_' + id).className = 'track-row';
        }
      }
      updateStatus();
    }

    function getSelectedTracks() {
      var selected = [];
      for (var i = 0; i < ALL_TRACKS.length; i++) {
        var t = ALL_TRACKS[i];
        var chk = document.getElementById('chk_' + t.id);
        if (chk && chk.checked) {
          selected.push(t);
        }
      }
      return selected;
    }

    function updateStatus() {
      var sel = getSelectedTracks();
      var btn = document.getElementById('btnFinish');
      var badge = document.getElementById('selection_badge');
      if (sel.length === 0) {
        badge.innerHTML = "No tracks selected";
        btn.disabled = true;
      } else if (sel.length === ALL_TRACKS.length) {
        badge.innerHTML = "Entire album selected (" + sel.length + " tracks)";
        btn.disabled = false;
      } else {
        badge.innerHTML = sel.length + " track(s) selected";
        btn.disabled = false;
      }
    }

    // ---- diagnostics: report the dialog-host reality back to the server ----
    function safeLog(msg) {
      try { if (window.console && console.log) { console.log(msg); } } catch (e) {}
    }
    function listExternalMethods() {
      // Probe the COM members of IWMPCDDVDWizardExternal individually -
      // COM dispatch members are not enumerable with for..in.
      //
      // This list is the COMPLETE set of members on the interface, read from the
      // type library of {2D7EF888-1D3C-484A-A906-9F49D99BB344} in
      // C:\WINDOWS\System32\wmp.dll. The interface derives from
      // IWMPExternalColors -> IWMPExternal; it adds seven methods of its own and
      // inherits only the read-only properties listed at the end.
      //
      // Close and Finish are NOT members. They were probed here as if they were,
      // which made two permanent "undefined" entries in every log line look
      // like a host problem. The real close is ReturnToMainTask (disp 10002).
      var names = ['WriteNames', 'ReturnToMainTask', 'WriteNamesEx',
                   'GetMDQByRequestID', 'EditMetadata',
                   'IsMetadataAvailableForEdit', 'BuyCD', 'version',
                   'appColorLight', 'appColorMedium', 'appColorDark',
                   'appColorButtonHighlight', 'appColorButtonShadow',
                   'appColorButtonHoverFace'];
      var found = {};
      var hasExt = false;
      try { hasExt = !!window.external; } catch (e) { hasExt = false; }
      found.__has_external = hasExt;
      if (!hasExt) return found;
      for (var i = 0; i < names.length; i++) {
        try {
          found[names[i]] = (typeof window.external[names[i]]);
        } catch (e) {
          found[names[i]] = 'error:' + (e && e.message ? e.message : e);
        }
      }
      return found;
    }
    function beaconSync(payload) {
      // Report just before navigating away, WITHOUT blocking. A synchronous
      // XHR here wedges the WMP dialog's UI thread: the write succeeds and the
      // tags apply, but the page never navigates to /done and the dialog hangs
      // on "Applying..." forever. Fire-and-forget is the safe option.
      try {
        var b = new XMLHttpRequest();
        b.open('POST', '/client_error', true);
        b.setRequestHeader('Content-Type', 'application/json');
        b.send(JSON.stringify(payload));
      } catch (e) { safeLog('beaconSync failed: ' + e); }
    }
    function beacon(payload) {
      try {
        var b = new XMLHttpRequest();
        b.open('POST', '/client_error', true);
        b.setRequestHeader('Content-Type', 'application/json');
        b.send(JSON.stringify(payload));
      } catch (e) { safeLog('beacon failed: ' + e); }
    }
    window.onerror = function (msg, src, line) {
      beacon({page: 'onerror', msg: String(msg), src: String(src), line: line,
              href: String(window.location.href)});
      return false;
    };

    function finishSync() {
      var sel = getSelectedTracks();
      if (sel.length === 0) {
        alert('Please check at least one track to update.');
        return;
      }
      var btn = document.getElementById('btnFinish');
      if (btn.disabled) return;   // never fire two writes from one dialog
      btn.disabled = true;
      btn.innerHTML = 'Applying...';

      var diag = {
        page: 'finish',
        href: String(window.location.href),
        toc: WMP_TOC,
        requestid: REQUEST_ID,
        selected: sel.length,
        external_methods: listExternalMethods()
      };

      // 1. Resolve the disc identifier FIRST. WMP opens the dialog with only
      //    ?requestid=..., so the MDQ returned by GetMDQByRequestID is the one
      //    identifier guaranteed to match the current disc. It also carries the
      //    real per-track WMContentIDs, which WMP needs in order to match the
      //    metadata we send to the physical tracks.
      // 'applied' is shared state between finishSync() and applyMetadata(),
      // which is a SEPARATE top-level function. It used to be a `var` local to
      // finishSync(), so applyMetadata() read a name that did not exist in its
      // scope: "if (!applied) is undefined" was thrown from the FAI dialog,
      // i.e. only inside WMP, where window.onerror reports it - and a browser
      // test just swallowed the ReferenceError. It now lives on `diag`, which
      // is already threaded through every call, so there is one real object.
      diag.applied = false;
      var mdq = '';
      diag.external_present = false;
      try {
        diag.external_present = !!window.external;
      } catch (e) {}

      if (window.external) {
        if (REQUEST_ID) {
          try {
            // Reuse the MDQ fetched on load when available.
            mdq = CACHED_MDQ || window.external.GetMDQByRequestID(REQUEST_ID) || '';
            RESOLVED_MDQ = mdq;
            // The MDQ is a large XML document - only log a short prefix so the
            // server log stays readable.
            diag.mdq = String(mdq).substr(0, 120);
            diag.mdq_len = String(mdq).length;
          } catch (e) {
            diag.mdq_error = String(e && e.message ? e.message : e);
            safeLog('GetMDQByRequestID failed: ' + e);
          }
        } else {
          diag.no_request_id = true;
        }
      } else {
        diag.external_missing = true;
      }

      // 2. Stage the XML ASYNCHRONOUSLY.
      //    The MDQ travels along so the server can substitute the real
      //    WMContentIDs into the generated document.
      //
      // CRITICAL: a synchronous XHR here blocks the WMP dialog's UI thread for
      // the whole server round-trip (which includes iTunes / MusicBrainz
      // lookups taking seconds). WMP treats an unresponsive dialog as a hang
      // and terminates the player - observed as an AppHangB1 crash.
      var payload = JSON.stringify({
        album: ALBUM_DETAILS,
        selected_tracks: sel,
        request_id: REQUEST_ID,
        session_id: SESSION_ID,
        toc: WMP_TOC,
        mdq: mdq,
        cd: WMP_CD,
        // The collection this document is actually being WRITTEN to. WMP_WMID
        // may be the server's LAST_WMID fallback, but it is what step 3 hands
        // to WriteNamesEx, so the document must describe THAT collection -
        // otherwise its WMCollectionID is a generated GUID WMP does not
        // recognise, and WMP's own follow-up fetch by wmid gets served the
        // previously applied album instead. Logged from a real library update:
        //   12:56:29  [WMID] dialog had no wmid; using last seen collection
        //              'B17CF884' as the write target
        //   12:56:44  [MDR] -> serving album='Mylo Xyloto' (wmid=-)   <- no retarget
        //   12:56:52  [MDR] -> serving album='Tiny Cities' (wmid=B17CF884)
        wmid: WMP_WMID,
        // Only a wmp that WMP actually put in THIS dialog's URL. A fallback
        // guess must not pre-claim the pending document, or a genuinely
        // different collection can never claim it.
        wmid_auth: WMP_WMID_AUTH
      });

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/store_staged_xml', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        var generatedXml = xhr.responseText || '';
        diag.store_status = xhr.status;
        diag.xml_len = generatedXml.length;
        applyMetadata(generatedXml, diag);
      };
      xhr.onerror = function () {
        diag.store_error = 'staging request failed';
        // Don't leave the user stuck on a disabled button if staging failed.
        try {
          var b2 = document.getElementById('btnFinish');
          if (b2) { b2.disabled = false; b2.innerHTML = 'Finish &amp; Apply'; }
        } catch (e) {}
        applyMetadata('', diag);
      };
      try {
        xhr.send(payload);
      } catch (e) {
        diag.store_error = String(e && e.message ? e.message : e);
        applyMetadata('', diag);
      }
    }

    function applyMetadata(generatedXml, diag) {
      var mdq = RESOLVED_MDQ || '';
      // Read the SHARED flag, not a local. This function is top-level and
      // finishSync() is not its parent, so a bare 'applied' here was an
      // undeclared identifier -> ReferenceError on every code path that
      // reached the write. Alias it locally and write back through diag.
      var applied = !!(diag && diag.applied);
      var setApplied = function (v) {
        applied = v;
        if (diag) diag.applied = v;
      };

      // 3. Feed directly into WMP's FAI dialog COM interface
      //    (IWMPCDDVDWizardExternal exposed as window.external on the dialog page).
      //    WriteNamesEx(type, bstrTypeId, bstrMetadata, fRenameRegroupFiles):
      //      type 0 = WMP_WRITENAMES_TYPE_CD_BY_TOC (bstrTypeId holds the CD TOC).
      //      type 1 = WMP_WRITENAMES_TYPE_CD_BY_CONTENT_ID (bstrTypeId holds the
      //               disc content id).
      //      type 2 = WMP_WRITENAMES_TYPE_CD_BY_MDQCD (bstrTypeId holds the MDQ).
      //      type 3 = WMP_WRITENAMES_TYPE_DVD_BY_DVDID.
      //    These four values are confirmed from the type library; the enum has no
      //    library/collection member, which is the hard limit described at the
      //    wmid branch below.
      //    This is what makes WMP apply the tags/artwork and rename/regroup the
      //    files automatically when the dialog finishes - no manual
      //    "Update album info" click required. Falls back to WriteNames(toc, xml).
      //
      // IMPORTANT: never guard a COM call with an `if (window.external.X)`
      // truthiness test. In the WMP dialog these host objects report
      // typeof == "unknown" and evaluate FALSY, so every such guard silently
      // skipped the real method. Attempt each call directly and let the
      // try/catch be the only gate.
      // Parse the disc identity BEFORE the write: whether the MDQ identifies a
      // real CD (burn onto disc) or is just a stub (library update) decides
      // which write path is correct.
      var disc = parseDiscIdentity(mdq);
      var discKnown = !!(disc.disc_album || disc.disc_track || disc.disc_artist);

      if (window.external) {
        // A stub MDQ means WMP is updating a LIBRARY album (no disc in the
        // drive), a real one means a CD. The MDQ is still what WMP expects as
        // the type id in both cases - it hands us an MDQ for exactly this
        // request, and it is the only disc identifier available here.
        //
        // fRenameRegroupFiles: renaming/regrouping FILES only makes sense for
        // a real disc being written. For a library album it asks WMP to rename
        // existing library tracks, which is the most likely reason the write
        // is accepted but no tags land - so ask for a tags-only write.
        var mdqUsable = !!(mdq);
        // A CD rip is NOT a library album, whatever the MDQ says. WMP's CD flow
        // sends no requestid, so GetMDQByRequestID('') returns nothing and
        // discKnown is false - which reported every rip as a library update
        // ("library_mode": true on a real rip of Sun Kil Moon - Tiny Cities).
        // WMP_CD is authoritative: if WMP named the disc, it is one.
        // A CD RIP and a LIBRARY ALBUM are told apart by the URL, not by the
        // MDQ. A rip arrives with ?cd= (or ?toc=); "Update album info" arrives
        // with nothing but ?requestid=. The MDQ is a terrible discriminator
        // because a library track's MDQ carries perfectly good titles - it only
        // looked empty because the reader's regex never matched (see the RE_
        // literals above). With a working reader, testing discKnown here would
        // call every tagged library album a DISC and ask WMP to rename and
        // regroup its files. WMP_CD / WMP_TOC are authoritative.
        var isLibrary = !WMP_CD && !WMP_TOC;
        diag.mdq_usable = mdqUsable;
        diag.library_mode = isLibrary;
        diag.rename_flag = isLibrary ? false : true;
        if (WMP_CD) {
          // WMP's CD flow: ?cd=<hex>+<hex>... with no requestid, so no MDQ
          // could be fetched. That value is the disc content id -
          // WMP_WRITENAMES_TYPE_CD_BY_CONTENT_ID (1). This path WORKS.
          try {
            window.external.WriteNamesEx(1, WMP_CD, generatedXml, true);
            setApplied(true);
            diag.write = 'WriteNamesEx-cdid-ok';
          } catch (e) {
            diag.write_ex_cd_error = String(e && e.message ? e.message : e);
            safeLog('WriteNamesEx by content id failed: ' + e);
          }
        } else if (mdqUsable && !isLibrary) {
          // A real disc MDQ: write by MDQ (type 2), rename/regroup allowed.
          try {
            window.external.WriteNamesEx(2, mdq, generatedXml, true);
            setApplied(true);
            diag.write = 'WriteNamesEx-mdq-ok';
          } catch (e) {
            diag.write_ex_mdq_error = String(e && e.message ? e.message : e);
            safeLog('WriteNamesEx by MDQ failed: ' + e);
          }
        } else if (WMP_WMID) {
          // Library flow: ?wmid= is the collection GUID WMP is updating, so
          // use it as the type id (CD_BY_CONTENT_ID) rather than a stub MDQ
          // that belongs to no real track. Tags only - never rename library
          // files.
          //
          // WHY THIS IS THE RIGHT SHAPE, AND WHY IT IS STILL A GUESS:
          // WMP_WRITENAMES_TYPE has exactly four members in the type library -
          // CD_BY_TOC (0), CD_BY_CONTENT_ID (1), CD_BY_MDQCD (2),
          // DVD_BY_DVDID (3). Every one of them names a physical disc or DVD.
          // There is no library/collection/playlist type, so WriteNamesEx has
          // no documented way to target a library album at all. Passing the
          // collection GUID as a disc content id is type-correct (a GUID in
          // the content-id slot) and is what the host logs show being accepted
          // ("WriteNamesEx-wmid-ok"), so it is the only call available.
          // A sibling path passes the album track's real WMContentID instead,
          // which lands in the same slot from the MDQ.
          // Treat both as best-effort: WMP accepting the call is not the same
          // as WMP applying it to the library, and the two are not
          // distinguishable from in-page code. See README "Library writes".
          try {
            window.external.WriteNamesEx(1, WMP_WMID, generatedXml, false);
            setApplied(true);
            diag.write = 'WriteNamesEx-wmid-ok';
          } catch (e) {
            diag.write_ex_wmid_error = String(e && e.message ? e.message : e);
            safeLog('WriteNamesEx by wmid failed: ' + e);
          }
        } else if (mdqUsable) {
          // Last resort for a library album with no wmid. Writing by MDQ is a
          // CD_BY_MDQCD call and a library album has no disc, so WMP accepts
          // it and applies NOTHING - which is why library tags appeared to
          // work twice and then stopped. The MDQ still carries the track's
          // real WMContentID, and type 1 with it is the same call shape as the
          // collection write that does work. Logged from a failing run:
          //   15:08:16  write=WriteNamesEx-mdq-tagsonly-ok  (wmid=-)
          //   15:08:26  [WMID] captured C52A9FE8-...       (10s too late)
          var libCid = mqContentId(mdq);
          if (libCid) {
            try {
              window.external.WriteNamesEx(1, libCid, generatedXml, false);
              setApplied(true);
              diag.write = 'WriteNamesEx-lib-cid-ok';
              diag.lib_content_id = libCid;
            } catch (e) {
              diag.write_ex_libcid_error = String(e && e.message ? e.message : e);
              safeLog('WriteNamesEx by library content id failed: ' + e);
            }
          }
          if (!applied) {
            try {
              window.external.WriteNamesEx(2, mdq, generatedXml, false);
              setApplied(true);
              diag.write = 'WriteNamesEx-mdq-tagsonly-ok';
            } catch (e) {
              diag.write_ex_mdq_error = String(e && e.message ? e.message : e);
              safeLog('WriteNamesEx by MDQ failed: ' + e);
            }
          }
        }

        // Fallback: write by TOC (WMP_WRITENAMES_TYPE_CD_BY_TOC = 0) when WMP did
        // hand us a TOC on the dialog URL.
        if (!applied && WMP_TOC) {
          try {
            window.external.WriteNamesEx(0, WMP_TOC, generatedXml, true);
            setApplied(true);
            diag.write = 'WriteNamesEx-toc-ok';
          } catch(e) {
            diag.write_ex_toc_error = String(e && e.message ? e.message : e);
            safeLog('window.external.WriteNamesEx failed: ' + e);
          }
          if (!applied) {
            try {
              window.external.WriteNames(WMP_TOC, generatedXml);
              setApplied(true);
              diag.write = 'WriteNames-toc-ok';
            } catch(e) {
              diag.write_names_error = String(e && e.message ? e.message : e);
              safeLog('window.external.WriteNames failed: ' + e);
            }
          }
        }
      } else {
        diag.external_missing = true;
      }
      diag.applied = applied;
      // Record what the disc really contains vs what we are applying - the
      // single most useful clue when WMP accepts a write but nothing shows up.
      try {
        diag.disc = disc;
        diag.applying_tracks = sel.length;
        diag.applying_album = (ALBUM_DETAILS.title || '') + ' - ' + (ALBUM_DETAILS.artist || '');
        // A stub MDQ means there is no disc being targeted; report that as
        // "unknown" rather than claiming a mismatch we cannot prove.
        diag.disc_known = discKnown;
        diag.disc_matches = discKnown
          ? (norm(disc.disc_album) === norm(ALBUM_DETAILS.title))
          : null;
        diag.disc_looks_empty = !discKnown && disc.track_count <= 1;
      } catch (e) {}

      // 3. Trigger WMP background sync endpoint (self-heals when step 2 failed
      //    or when running outside the WMP dialog host)
      try {
        var xhrSync = new XMLHttpRequest();
        xhrSync.open('GET', '/redir/getmdrcdbackground/' + window.location.search, true);
        xhrSync.send();
      } catch(e) {}

      // 4. Hand the diagnostic to /done, then leave in the SAME tick.
      //
      // A setTimeout used to drive this redirect, and the log shows precisely
      // how that fails - a real rip of 'Sun Kil Moon - Tiny Cities':
      //   12:37:41  [CLIENT] {"page":"finish","write":"WriteNamesEx-cdid-ok",
      //                      "applied":true}
      //   12:40:06  [REQ] GET /done          <-- 2m25s later
      // The write returned and the beacon went out, yet the dialog sat on
      // "Applying..." the whole time: WMP's dialog host is single-threaded and
      // was busy applying the write, so it never serviced the timer. By then
      // btnFinish was disabled and the user had no way out of the dialog at all.
      //
      // Navigating in the same tick cannot be delayed by the host. The beacon
      // alone is not safe to rely on - leaving the page can cancel the
      // in-flight XHR - so the diagnostic is stashed in sessionStorage and
      // replayed from /done, which is a different page in the same origin.
      try {
        window.sessionStorage.setItem('fai_diag', JSON.stringify(diag));
      } catch (e) { /* host policy may refuse storage; the beacon still stands */ }
      beaconSync(diag);
      leaveDialog();
    }

    // Get off the "Applying..." screen. Never leave the user trapped: if the
    // navigation itself fails the button is handed back rather than staying
    // permanently disabled with no way forward.
    function leaveDialog() {
      try {
        window.location.href = "/done";
        return;
      } catch (e) { /* fall through and recover the button */ }
      try {
        var b = document.getElementById('btnFinish');
        if (b) {
          b.disabled = false;
          b.innerHTML = 'Close';
          b.onclick = leaveDialog;
        }
      } catch (e2) {}
    }

    window.onload = function() {
      // If nothing was auto-selected by track match, select all by default for fast 1-click experience
      var sel = getSelectedTracks();
      if (sel.length === 0) {
        selectAll();
      } else {
        updateStatus();
      }
      // Report what this page actually got from the dialog host. A missing
      // TOC or a missing window.external member is the #1 reason
      // WriteNamesEx silently applies nothing.
      beacon({
        page: 'confirm_load',
        href: String(window.location.href),
        toc: WMP_TOC,
        requestid: REQUEST_ID,
        doc_mode: String(document.documentMode || ''),
        external_methods: listExternalMethods()
      });
      // Fetch the MDQ up front: it identifies the disc actually in the drive,
      // which we show as a warning if it does not match the chosen album.
      if (window.external && REQUEST_ID) {
        try {
          CACHED_MDQ = window.external.GetMDQByRequestID(REQUEST_ID) || '';
          if (CACHED_MDQ) showDiscBanner(CACHED_MDQ);
        } catch (e) {}
      }
      // 'Existing Information' reports what WMP CURRENTLY holds, which is a
      // different thing from the album about to be applied. The MDQ is the best
      // source when there is one; a CD rip sends no requestid, so the query
      // context the dialog was opened with is the fallback. Runs in both cases.
      try { renderExistingInfo(CACHED_MDQ); } catch (e2) {}
    };
  </script>
</body>
</html>""",
      css=COMMON_CSS,
      details=details,
      tracks=tracks,
      track_html=track_html,
      tracks_json=json.dumps(tracks),
      details_json=json.dumps(details),
      request_id=request_id,
      session_id=session_id,
      # Rendered server-side so 'Existing Information' shows WMP's CURRENT state
      # even before (or without) any script running. renderExistingInfo() refines
      # this from the disc MDQ once it has been fetched.
      wmp_album=wmp_album,
      wmp_artist=wmp_artist,
      wmp_track=wmp_track,
      wmp_toc_json=json.dumps(wmp_toc),
      wmp_cd_json=json.dumps(wmp_cd),
      wmp_wmid_json=json.dumps(wmp_wmid),
      wmp_wmid_auth_json=json.dumps(wmp_wmid_auth))
# ==========================================================
# XML STAGING ENDPOINT (JSON PAYLOAD + ROBUST FALLBACK)
# ==========================================================
@app.route("/store_staged_xml", methods=["POST"])
def store_staged_xml():
    global LAST_XML
    try:
        data = request.get_json(silent=True)
        if not data:
            data = request.form

        album = data.get("album", {})
        selected_tracks = data.get("selected_tracks", [])
        req_id = data.get("request_id", "")
        session_id = data.get("session_id", "")
        # The MDQ from GetMDQByRequestID carries the real WMContentID of every
        # track on the disc. WMP matches incoming metadata by that ID, so we
        # must use it instead of generated GUIDs or the write is ignored.
        mdq_xml = data.get("mdq", "") or ""
        content_ids = parse_mdq_content_ids(mdq_xml)
        if content_ids:
            log_line("MDQ", f"parsed {len(content_ids)} real content IDs: "
                            f"{sorted(content_ids.items())[:5]}")

        # Do NOT rely on a wmid captured earlier: the dialog never reveals the
        # wmid, so whatever we saw last may belong to a different album.
        # Retargeting happens at delivery time instead.
        # BUT WMP fetches by wmid only AFTER the dialog closes, so bind the
        # freshly staged document to the wmid we have seen most recently as a
        # best effort - if the user is updating that same album (the common
        # case) the lookup will hit directly instead of falling through to
        # LAST_XML, which by then may hold a completely different album.
        toc_val = str(data.get("toc", "") or "").strip()
        cd_val = str(data.get("cd", "") or "").strip()
        # The collection the dialog is writing TO. The client sends WMP_WMID,
        # which may be the server's LAST_WMID fallback rather than something
        # WMP put in this dialog's URL. The document must still describe that
        # collection - it is what WriteNamesEx is handed - otherwise its
        # WMCollectionID stays a generated GUID and WMP's own follow-up fetch
        # by wmid is answered with whatever album was applied BEFORE this one:
        #   12:56:29  [WMID] using last seen collection 'B17CF884' as write target
        #   12:56:44  [MDR] -> serving album='Mylo Xyloto' (wmid=-)   <- no retarget
        #   12:56:52  [MDR] -> serving album='Tiny Cities' (wmid=B17CF884)
        # wmid_auth is the stricter value: only a wmid WMP really put in THIS
        # dialog's URL. Only that one may pre-claim the pending document,
        # because a fallback guess must not stop a genuinely different
        # collection from ever claiming it.
        wmid_target = str(data.get("wmid", "") or "").strip()
        wmid_auth = str(data.get("wmid_auth", "") or "").strip()
        xml = build_wmp_xml(album, selected_tracks=selected_tracks,
                            request_id=req_id, content_ids=content_ids,
                            wmid=wmid_target,
                            cd=cd_val)
        with XML_LOCK:
            LAST_XML = xml
            # Index under the collection we are writing to, so the delivery
            # lookup resolves exactly instead of reaching a stale binding.
            _stage_request_xml(xml, req_id, toc_val, wmid_target,
                               claim_wmid=wmid_auth)
            if cd_val:
                # WMP fetches by ?cd=... after the dialog closes; bind the
                # staged document to that id so the lookup hits directly.
                _stage_request_xml(xml, cd=cd_val)

        track_fai_navigation(session_id, 'finish_applied', {
            'count': len(selected_tracks),
            'album': album.get('title'),
            'artist': album.get('artist'),
            'source': album.get('source')
        })

        print(f"\n[METADATA APPLIED] Successfully staged XML for '{album.get('title')}' ({len(selected_tracks)} tracks)")
        log_line("STAGED", f"album={album.get('title')!r} artist={album.get('artist')!r} "
                           f"tracks={len(selected_tracks)} req_id={req_id!r} toc={toc_val!r} "
                           f"xml_bytes={len(xml)} art={_ART_MODE}")
        return Response(xml, mimetype='text/xml')
    except Exception as e:
        import traceback
        print(f"[STORE ERROR] {e}")
        log_line("STORE-ERR", f"{e}\n{traceback.format_exc()[:1500]}")
        return Response("<ERROR>Failed to stage XML</ERROR>", status=500, mimetype='text/xml')

# Backward compatibility with existing UI calls
@app.route("/store_single_track_xml", methods=["POST"])
def store_single_track_xml():
    global LAST_XML
    xml = request.form.get('xml', '')
    if xml:
        with XML_LOCK:
            LAST_XML = xml
        count = request.form.get('count', '1')
        print(f"\n[LEGACY STORE] XML stored ({len(xml)} bytes, count={count})")
        return "OK"
    return "ERROR"

@app.route("/track_navigation", methods=["POST"])
def track_navigation():
    session_id = request.form.get('session_id', '')
    step = request.form.get('step', '')
    album_id = request.form.get('album_id', '')
    if session_id and step:
        track_fai_navigation(session_id, step, {'album_id': album_id})
        return "OK"
    return "ERROR"

# ==========================================================
# DIAGNOSTICS & SYSTEM STATUS ENDPOINT
# ==========================================================
@app.route("/fai_status")
@app.route("/status")
def fai_status():
    with NAV_LOCK:
        stats = FAI_NAVIGATION['stats'].copy()
        current_steps = FAI_NAVIGATION['current_step'].copy()

    status_data = {
        "status": "healthy",
        "version": "2.0-HighPerformance",
        "uptime_seconds": int(time.time() - SERVER_START_TIME),
        "caches": {
            "search_entries": len(search_cache._cache),
            "album_entries": len(album_cache._cache),
            "image_entries": len(image_cache._cache)
        },
        "stats": stats,
        "active_sessions": len(current_steps),
        "has_staged_xml": LAST_XML is not None
    }

    if request.args.get("format") == "json" or "application/json" in request.headers.get("Accept", ""):
        return jsonify(status_data)

    return Response(f"""<!DOCTYPE html>
<html>
<head>
  <title>WMP FAI Server 2.0 - Diagnostics</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; padding: 24px; background: #F8FAFC; color: #1E293B; }}
    .card {{ background: #FFF; padding: 20px; border-radius: 8px; border: 1px solid #E2E8F0; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    h2, h3 {{ margin-top: 0; color: #0F172A; }}
    pre {{ background: #0F172A; color: #38BDF8; padding: 15px; border-radius: 6px; overflow-x: auto; font-size: 9pt; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }}
    .metric {{ background: #F1F5F9; padding: 15px; border-radius: 6px; text-align: center; }}
    .metric-val {{ font-size: 20pt; font-weight: 700; color: #2563EB; }}
    .metric-lbl {{ font-size: 8pt; color: #64748B; text-transform: uppercase; font-weight: 600; margin-top: 4px; }}
  </style>
</head>
<body>
  <h2>WMP &amp; Zune FAI Metadata Server 2.0</h2>
  <div class="grid">
    <div class="metric"><div class="metric-val">{stats['total_searches']}</div><div class="metric-lbl">Total Searches</div></div>
    <div class="metric"><div class="metric-val">{stats['images_served']}</div><div class="metric-lbl">Images Served</div></div>
    <div class="metric"><div class="metric-val">{stats['xml_deliveries']}</div><div class="metric-lbl">XML Deliveries</div></div>
    <div class="metric"><div class="metric-val">{len(search_cache._cache) + len(album_cache._cache) + len(image_cache._cache)}</div><div class="metric-lbl">Cached Objects</div></div>
  </div>
  <div class="card">
    <h3>System Status JSON</h3>
    <pre>{json.dumps(status_data, indent=2)}</pre>
  </div>
</body>
</html>""", mimetype="text/html")

# ==========================================================
# COMPLETION PAGE
# ==========================================================
@app.route("/done")
def done():
    return """<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Metadata Applied</title>
  <style>
    /* Windows Vista / 7 Aero - the plain white FAI content area. Same
       palette as COMMON_CSS so the hand-off page does not flash a
       different theme on its way back to the player. */
    body { font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; color: #1A1A1A; margin: 0; padding: 0; background-color: #FFFFFF; }
    .card { max-width: 400px; margin: 70px auto; padding: 26px 28px; text-align: center; border: 1px solid #DCE6F0; background-color: #FFFFFF; background-image: linear-gradient(to bottom, #FFFFFF 0%, #F7FAFD 100%); }
    .icon { font-size: 30pt; color: #4C8B2B; margin-bottom: 10px; }
    h3 { margin: 0 0 8px 0; font-size: 12pt; font-weight: 700; color: #0B5AA6; }
    p { margin: 0; color: #404040; font-size: 9pt; line-height: 1.5; }
    .btn { display: inline-block; min-width: 96px; margin-top: 20px; padding: 3px 16px; font-family: "Segoe UI", Tahoma, Arial, sans-serif; font-size: 9pt; color: #1A1A1A; text-align: center; cursor: pointer; border: 1px solid #7EB4EA; border-radius: 2px; background-color: #F0F0F0; background-image: linear-gradient(to bottom, #FFFFFF 0%, #EFF6FD 48%, #DCEAF9 52%, #EFF6FD 100%); }
    .btn:hover { background-image: linear-gradient(to bottom, #FFFFFF 0%, #F3F8FD 48%, #E0EBF8 52%, #F3F8FD 100%); }
    <!--[if lt IE 8]>
    .card { background-image: none; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#FFFFFF', endColorstr='#F7FAFD', type='0'); }
    .btn { background-image: none; filter: progid:DXImageTransform.Microsoft.gradient(startColorstr='#FFFFFF', endColorstr='#DCEAF9', type='0'); }
    .card, .btn { border-radius: 0; }
    <![endif]-->
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✓</div>
    <h3>Metadata Applied Successfully!</h3>
    <p id="statusMsg">Windows Media Player is updating your library tracks. Returning to the player&hellip;</p>
    <button class="btn" onclick="returnToMainTask()">Close Window</button>
  </div>
  <script>
    // Never truthiness-test a COM member here: in the WMP dialog these host
    // objects evaluate FALSY, so the old guarded version silently skipped
    // ReturnToMainTask and fell through to window.close() - the wizard never
    // handed control back to WMP and the pending write was never finalised.
    function report(obj) {
      try {
        var b = new XMLHttpRequest();
        b.open('POST', '/client_error', true);
        b.setRequestHeader('Content-Type', 'application/json');
        b.send(JSON.stringify(obj));
      } catch (e) {}
    }
    var CLOSING = false;
    function returnToMainTask() {
      // WMP's dialog host is single-threaded: firing a COM call twice (e.g. the
      // timer plus a click) or during page teardown can deadlock wmplayer.
      if (CLOSING) return;
      CLOSING = true;
      var diag = {page: 'done_close', href: String(window.location.href)};
      var handled = false;
      // Report BEFORE the COM call - if the call hangs WMP we still see that
      // the page tried to close.
      report(diag);
      try {
        if (window.external) {
          // IWMPCDDVDWizardExternal has no Close method. Verified against the
          // type library for {2D7EF888-1D3C-484A-A906-9F49D99BB344} in
          // C:\WINDOWS\System32\wmp.dll: the whole interface is WriteNames,
          // ReturnToMainTask, WriteNamesEx, GetMDQByRequestID, EditMetadata,
          // IsMetadataAvailableForEdit, BuyCD, plus the inherited read-only
          // appColor*/version properties. The dialog host agrees - the probe in
          // listExternalMethods() reports Close and Finish as "undefined" while
          // every real member reports "unknown".
          //
          // This call therefore could only ever throw, and it was reached on
          // every close where ReturnToMainTask() did not succeed. ReturnToMainTask
          // (disp 10002) is the ONLY way to dismiss the wizard; the log shows it
          // succeeding as "ReturnToMainTask-ok". Fall straight through to
          // window.close() instead of provoking a COM error.
          try {
            window.external.ReturnToMainTask();
            handled = true;
            diag.close = 'ReturnToMainTask-ok';
          } catch (e1) {
            diag.rtmt_error = String(e1 && e1.message ? e1.message : e1);
          }
        }
      } catch (e) {
        diag.outer_error = String(e && e.message ? e.message : e);
      }
      if (!handled) {
        try { window.close(); } catch (e3) {}
      }
      report(diag);
    }
    // The confirm page stashes its write diagnostic here before navigating
    // away. Its own beacon can be cancelled mid-flight by that navigation, and
    // the write outcome is the single most useful thing in the log - it is what
    // proved the 2m25s "Applying..." stall. Replay it on load so a cancelled
    // beacon never costs us the diagnosis. One-shot: remove it immediately so a
    // later visit to /done cannot re-report a stale write.
    (function replayStagedDiag() {
      var raw = '';
      try { raw = window.sessionStorage.getItem('fai_diag') || ''; } catch (e) { return; }
      if (!raw) return;
      try { window.sessionStorage.removeItem('fai_diag'); } catch (e2) {}
      var d;
      try { d = JSON.parse(raw); } catch (e3) { return; }
      if (!d || typeof d !== 'object') return;
      d.page = 'finish';
      d.replayed_from = 'sessionStorage';
      report(d);
    })();
    // NOTE: no setTimeout-driven ReturnToMainTask here. An automatic COM call
    // from a timer in WMP's dialog host was a deadlock suspect - the player
    // froze (AppHangB1) before ever loading a page. The user closes the dialog
    // with the button, which is a normal UI-initiated call.
  </script>
</body>
</html>"""

# ==========================================================
# SSL CERTIFICATE GENERATOR (MODERN CRYPTOGRAPHY)
# ==========================================================
def _install_cert_as_trusted_root(cert_path):
    """Add the self-signed cert to the machine + user Trusted Root stores.

    Without this, the WMP FAI dialog hits a TLS trust prompt inside its own
    modal host window. That prompt is invisible/unresponsive, so wmplayer
    appears to freeze and Windows kills it (AppHangB1).
    """
    if os.environ.get("WMP_FAI_NO_TRUST"):
        return
    for args in (
        ["certutil", "-addstore", "-f", "Root", cert_path],
        ["certutil", "-addstore", "-f", "-user", "Root", cert_path],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=30)
        except Exception as e:
            print(f"[!] certutil trust install failed ({args[1]}): {e}")


def ensure_ssl_certificates():
    # Always re-verify trust: a cert generated before the SAN fix is invalid for
    # hostname verification and will hang the dialog.
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        try:
            from cryptography import x509
            with open(CERT_FILE, "rb") as f:
                existing = x509.load_pem_x509_certificate(f.read())
            san = existing.extensions.get_extension_for_class(
                x509.SubjectAlternativeName)
            if "musicmatch-ssl.xboxlive.com" in [str(v) for v in san.value]:
                _install_cert_as_trusted_root(CERT_FILE)
                return
            print("[*] Existing certificate has no usable SAN - regenerating.")
        except Exception as e:
            print(f"[!] Existing certificate unusable ({e}) - regenerating.")

    print(f"[*] Generating modern RSA certificate for musicmatch-ssl.xboxlive.com at {APP_DATA_DIR}...")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import ipaddress

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "musicmatch-ssl.xboxlive.com"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WMP FAI Server")
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(int(time.time()))
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            # Modern Windows ignores the legacy CN for hostname validation, so a
            # SubjectAlternativeName is REQUIRED or TLS verification fails.
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("musicmatch-ssl.xboxlive.com"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False)
            # Self-signed cert is its own CA; without this, Windows refuses to
            # use it as a trust anchor.
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(private_key, hashes.SHA256())
        )

        with open(CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(KEY_FILE, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))
        print("[*] SSL Certificates generated successfully (with SAN).")
        _install_cert_as_trusted_root(CERT_FILE)
    except Exception as e:
        print(f"[!] Fallback SSL generation error: {e}")

# ==========================================================
# SERVER LAUNCHER
# ==========================================================
SERVER_START_TIME = time.time()

if __name__ == "__main__":
    def run_http():
        print(f"[*] Starting HTTP server on port {HTTP_PORT}...")
        try:
            app.run(host=HOST, port=HTTP_PORT, debug=False, use_reloader=False)
        except Exception as e:
            print(f"[!] HTTP Server error (port {HTTP_PORT}): {e}")

    threading.Thread(target=run_http, daemon=True).start()

    ensure_ssl_certificates()

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.set_ciphers('DEFAULT@SECLEVEL=1')
        except Exception:
            pass
        context.load_cert_chain(CERT_FILE, KEY_FILE)

        print(f"[*] Starting HTTPS server on port {HTTPS_PORT}...")
        print("[*] WMP FAI Metadata Server 2.0 READY")
        app.run(host=HOST, port=HTTPS_PORT, ssl_context=context, debug=False, use_reloader=False)
    except Exception as e:
        print(f"[!] HTTPS server startup warning (running HTTP only): {e}")
        # Keep process alive with HTTP server
        while True:
            time.sleep(1)
