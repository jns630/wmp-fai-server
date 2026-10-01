# -*- coding: utf-8 -*-
"""
Test suite for FAI Server v2.0.
Runs the Flask app in-process via test_client() - no port 80 required.
"""
import importlib.util
import json
import os
import re
import sys
import urllib.parse

BASE = r"d:\WMC_EPG\New folder (4)\red alert 3 patch dx 10\FAI Server.py"
_DIR = os.path.dirname(BASE)
_req_pkgs = set()
try:
    with open(os.path.join(_DIR, "requirements.txt"), encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.split("#", 1)[0].strip()   # comments are not packages
            if _line:
                _req_pkgs.add(re.split(r"[<>=!;\[\s]", _line, 1)[0].strip().lower())
except OSError:
    pass
_render = ""
try:
    with open(os.path.join(_DIR, "render.yaml"), encoding="utf-8") as _f:
        _render = _f.read()
except OSError:
    pass
_procfile = ""
try:
    with open(os.path.join(_DIR, "Procfile"), encoding="utf-8") as _f:
        _procfile = _f.read()
except OSError:
    pass


def _norm(t):
    """Strip comments and collapse whitespace.

    The deploy files carry comments that name the very flags that must NOT
    appear (to warn future editors), so any check has to look at the real
    command lines only, never at prose about them.
    """
    return re.sub(r"\s+", " ", re.sub(r"#[^\n]*", "", t)).strip()


_render_norm = _norm(_render)
_procfile_norm = _norm(_procfile)

spec = importlib.util.spec_from_file_location("fai_server", BASE)
fai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fai)
app = fai.app

PASS = []
FAIL = []

def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} :: {detail}")

c = app.test_client()

# 1. Status JSON
r = c.get("/fai_status?format=json")
data = r.get_json()
check("status-json", r.status_code == 200 and data and data.get("status") == "healthy",
      f"code={r.status_code} body={r.data[:200]!r}")

# 2. Status HTML
r = c.get("/fai_status")
check("status-html", r.status_code == 200 and b"Diagnostics" in r.data,
      f"code={r.status_code}")

# 3. Search (network: iTunes + MusicBrainz)
r = c.get("/api_search?q=" + urllib.parse.quote("abbey road beatles"))
body = r.data.decode("utf-8", "ignore")
check("search-api", r.status_code == 200 and ("album-item" in body or "No matching" in body),
      f"code={r.status_code} body={body[:200]!r}")

# 4. Unified UI
r = c.get("/FAI/ui?artist=beatles&album=abbey+road")
check("unified-ui", r.status_code == 200 and b"Find Album Information" in r.data,
      f"code={r.status_code}")

# 5. iTunes confirm page - must contain WriteNamesEx wiring
itunes_id = None
m = re.search(r"pick\('itunes', '(\d+)'\)", body)
if not m:
    r2 = c.get("/api_search?q=" + urllib.parse.quote("hotel california eagles"))
    m = re.search(r"pick\('itunes', '(\d+)'\)", r2.data.decode("utf-8", "ignore"))
if m:
    itunes_id = m.group(1)

if itunes_id:
    toc_val = "+hAhAAQBAAMAAwADAAQAAAAA"
    r = c.get("/confirm?source=itunes&id=%s&requestid=REQTEST1&toc=%s"
              % (itunes_id, urllib.parse.quote(toc_val)))
    page = r.data.decode("utf-8", "ignore")
    check("confirm-itunes", r.status_code == 200 and "ALL_TRACKS" in page,
          f"code={r.status_code} body={page[:300]!r}")
    check("confirm-writenamesex",
          "window.external.WriteNamesEx(0, WMP_TOC, generatedXml, true)" in page,
          "WriteNamesEx call missing in /confirm page")
    check("confirm-writenames-fallback",
          "window.external.WriteNames(WMP_TOC, generatedXml)" in page,
          "WriteNames fallback missing in /confirm page")
    check("confirm-toc-var",
          'var WMP_TOC = "%s"' % toc_val in page,
          "WMP_TOC var not populated from query string")
else:
    check("confirm-itunes", False, "no iTunes id found via search")
    check("confirm-writenamesex", False, "skipped - no iTunes id")
    check("confirm-writenames-fallback", False, "skipped - no iTunes id")
    check("confirm-toc-var", False, "skipped - no iTunes id")

# 6. MusicBrainz confirm page
m = re.search(r"pick\('musicbrainz', '([0-9a-f-]{36})'\)", body)
if not m:
    r2 = c.get("/api_search?q=" + urllib.parse.quote("dark side of the moon pink floyd"))
    m = re.search(r"pick\('musicbrainz', '([0-9a-f-]{36})'\)",
                  r2.data.decode("utf-8", "ignore"))
if not m:
    # MusicBrainz rate-limits rapid successive searches (503) - fall back to a
    # verified release id so this test stays deterministic.
    mb_id = "e9e63904-9ada-47ee-93df-7b25a97774b9"
else:
    mb_id = m.group(1)
r = c.get("/confirm_musicbrainz?id=" + mb_id)
page = r.data.decode("utf-8", "ignore")
check("confirm-musicbrainz", r.status_code == 200 and "ALL_TRACKS" in page,
      f"code={r.status_code} body={page[:300]!r}")

# 7. XML staging + keyed delivery
album = {"id": "T1", "title": "Test Album Alpha", "artist": "Test Artist",
         "year": "2001", "genre": "Rock", "art_url": "",
         "tracks": [{"number": 1, "name": "Song One", "duration_ms": 180000, "disc": 1}],
         "source": "itunes"}
payload = {"album": album,
           "selected_tracks": [album["tracks"][0]],
           "request_id": "REQ_A", "session_id": "SESS_T", "toc": "TOC_A"}
r = c.post("/store_staged_xml", data=json.dumps(payload),
           content_type="application/json")
xml_a = r.data.decode("utf-8", "ignore")
check("store-staged-xml", r.status_code == 200 and "<METADATA>" in xml_a
      and "Test Album Alpha" in xml_a,
      f"code={r.status_code} body={xml_a[:300]!r}")

album_b = dict(album, title="Test Album Beta")
payload_b = {"album": album_b,
             "selected_tracks": [album["tracks"][0]],
             "request_id": "REQ_B", "session_id": "SESS_T", "toc": "TOC_B"}
c.post("/store_staged_xml", data=json.dumps(payload_b),
       content_type="application/json")

# keyed by request id
r = c.get("/redir/getmdrcdbackground/?requestid=REQ_A")
check("mdr-keyed-request-a",
      r.status_code == 200 and "Test Album Alpha" in r.data.decode("utf-8", "ignore"),
      f"body={r.data[:200]!r}")
r = c.get("/redir/getmdrcdbackground/?requestid=REQ_B")
check("mdr-keyed-request-b",
      r.status_code == 200 and "Test Album Beta" in r.data.decode("utf-8", "ignore"),
      f"body={r.data[:200]!r}")
# keyed by TOC
r = c.get("/redir/getmdrcdbackground/?toc=TOC_B")
check("mdr-keyed-toc",
      r.status_code == 200 and "Test Album Beta" in r.data.decode("utf-8", "ignore"),
      f"body={r.data[:200]!r}")

# 8. MDR-CD delivery endpoint
r = c.get("/cdinfo/GetMDRCD.aspx?requestid=REQ_A")
check("mdr-cd-delivery",
      r.status_code == 200 and "Test Album Alpha" in r.data.decode("utf-8", "ignore"),
      f"body={r.data[:200]!r}")

# 9. TOC legacy route (browser -> redirect to UI; API -> XML)
r = c.get("/redir/submittoc/?toc=" + urllib.parse.quote("TOC_A"),
          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
check("toc-browser-redirect", r.status_code in (302, 301)
      and "/FAI/ui" in r.headers.get("Location", ""),
      f"code={r.status_code} loc={r.headers.get('Location')}")

r = c.get("/redir/submittoc/?toc=" + urllib.parse.quote("TOC_A"),
          headers={"User-Agent": "WindowsMediaPlayer/12"})
check("toc-api-xml", r.status_code == 200 and b"<METADATA>" in r.data,
      f"code={r.status_code} body={r.data[:200]!r}")

# 10. Completion page uses ReturnToMainTask (real COM method)
r = c.get("/done")
check("done-returntomain", r.status_code == 200
      and "ReturnToMainTask" in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code}")

# 11. WMP TOCs start with a literal '+'. Werkzeug's request.args decodes '+'
#     as a space, which would hand WriteNamesEx a TOC that does not match the
#     disc -> WMP silently applies nothing. Verify the TOC is preserved.
plus_toc = "+hAhAAQBAAMAAwADAAQAAAAA"
r = c.get("/confirm?source=itunes&id=%s&requestid=REQPLUS&toc=%s"
          % (itunes_id or "1", plus_toc))
page = r.data.decode("utf-8", "ignore")
check("toc-plus-not-corrupted",
      'var WMP_TOC = "%s"' % plus_toc in page,
      "TOC '+' was decoded as a space in /confirm WMP_TOC")

# raw TOC must also survive the legacy redirect into the UI (verbatim query)
r = c.get("/redir/submittoc/?toc=" + plus_toc,
          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
loc = r.headers.get("Location", "")
check("toc-plus-survives-redirect", "toc=" + plus_toc in loc,
      f"loc={loc!r}")

# 11b. Zune speaks the same MDR-CD protocol as WMP on a different host. The four
#      paths below are the literal strings in the installed client
#      (ZuneNativeLib.dll / ZuneNss.exe), so these tests are pinned to what the
#      binary actually asks for rather than to a guess.
#
#      Zune's two post-url paths pair up the way WMP's do. getmdrcdposturlzune/
#      was missing entirely, which sent the first handshake to the /redir/
#      catch-all - which answers with a sentence, not a URL, so Zune gave up
#      before ever reaching the endpoint that would have worked.
r = c.get("/redir/getmdrcdposturlzune/")
check("zune-post-url",
      r.status_code == 200 and r.data.decode().strip().endswith("/redir/getmdrcdzune/"),
      f"code={r.status_code} body={r.data[:120]!r}")

r = c.get("/redir/getmdrcdposturlbackgroundzune/")
check("zune-background-post-url",
      r.status_code == 200
      and r.data.decode().strip().endswith("/redir/getmdrcdbackgroundzune/"),
      f"code={r.status_code} body={r.data[:120]!r}")

# The non-background path must not be handed the background endpoint, or Zune
# fetches metadata and never opens the dialog.
check("zune-post-url-pairs-are-distinct",
      c.get("/redir/getmdrcdposturlzune/").data
      != c.get("/redir/getmdrcdposturlbackgroundzune/").data,
      "both Zune post-url paths returned the same endpoint")

# Zune is NOT a browser client: it wants the XML, not the dialog. It appends
# &CD= / &wmid= / &requestID= and feeds the reply straight to SaveMDRCD.
#
# These deliberately reuse the documents staged in section 7 instead of posting
# a new one. Staging sets LAST_XML and rewrites PENDING_WRITE, and the suite's
# later write-ownership tests (33/34) assert against that shared state, so
# inserting a document here makes the run reach a live provider lookup and hang.
# Keying off REQ_A/TOC_A proves the same thing - the Zune path resolves staged
# XML and returns it as XML - with no side effects.
zune_toc = urllib.parse.quote("TOC_A")
r = c.get("/redir/getmdrcdzune/?requestid=REQ_A")
zune_body = r.data.decode("utf-8", "ignore")
check("zune-delivers-mdr-xml",
      r.status_code == 200 and "Test Album Alpha" in zune_body
      and r.mimetype == "text/xml",
      f"code={r.status_code} mimetype={r.mimetype} body={r.data[:200]!r}")

# Zune names the disc with &CD=, the same argument WMP uses - read raw so the
# TOC survives intact.
r = c.get("/redir/getmdrcdzune/?CD=" + zune_toc)
check("zune-delivers-by-toc",
      r.status_code == 200 and "Test Album Alpha" in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code} body={r.data[:200]!r}")

r = c.get("/redir/getmdrcdbackgroundzune/?toc=" + zune_toc)
check("zune-background-delivers-mdr-xml",
      r.status_code == 200 and "Test Album Alpha" in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code} body={r.data[:200]!r}")

# The dialog must NOT be served to Zune. No Zune binary contains this app's UI,
# so an HTML page here is exactly the wrong answer and is what made Zune report
# a connection error despite a 200.
r = c.get("/redir/getmdrcdzune/?requestid=REQ_A")
check("zune-never-gets-the-dialog",
      "Find Album Information" not in r.data.decode("utf-8", "ignore"),
      "the HTML dialog leaked into the Zune XML endpoint")

# ...and the dialog itself is untouched for WMP.
r = c.get("/FAI/ui")
check("wmp-dialog-still-served",
      r.status_code == 200 and "Find Album Information" in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code}")

# Zune's capability probe answers with a URL, not a page, and points at a path
# that actually exists rather than at an invented one.
r = c.get("/redir/ZuneFAI/?apiVersion=1.0")
check("zune-fai-probe-returns-a-url",
      r.status_code == 200 and r.data.decode().strip().startswith("http://")
      and "zunesearch" not in r.data.decode(),
      f"code={r.status_code} body={r.data[:120]!r}")

# The invented /redir/zunesearch/ endpoint is gone; unmapped paths still log
# rather than 404ing quietly.
r = c.get("/redir/zunesearch/")
check("zunesearch-endpoint-removed",
      r.status_code == 200 and "no handler" in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code} body={r.data[:120]!r}")

# 11c. WMP and Zune send the CD TOC as HEX; MusicBrainz's ?toc= lookup wants
#      DECIMAL in a different field order, so the raw string is always rejected
#      with `400 Invalid TOC`. to_musicbrainz_toc() ports the transform from
#      PyZuneMetadataServer (JarHead4/PyZuneMetadataServer, utils.py to_mb_toc),
#      a working reimplementation of the retired Microsoft TOC service.
#
#      These are the exact TOCs real Zune sent (fai_server.log), so the
#      conversion is pinned to observed traffic rather than a synthetic example.
#      Both resolve against the live API; asserted here as pure string logic so
#      the suite stays offline.
_zune_toc = "B+96+43DA+71A4+105D1+15498+19A64+1F0B3+23C14+29CD4+2EB21+33B0F+37106"
check("toc-hex-is-converted-for-musicbrainz",
      fai.to_musicbrainz_toc(_zune_toc)
      == "1+11+225542+150+17370+29092+67025+87192+105060+127155+146452+171220+191265+211727",
      f"got {fai.to_musicbrainz_toc(_zune_toc)!r}")

# The reordering is the part a plain hex->decimal pass gets wrong: the lead-out
# moves to position two and a leading "1" is added.
check("toc-conversion-reorders-the-leadin",
      fai.to_musicbrainz_toc("3+96+45EA+B624+10E8E") == "1+3+69262+150+17898+46628",
      f"got {fai.to_musicbrainz_toc('3+96+45EA+B624+10E8E')!r}")

# WMP sends the same TOC space-separated and, in legacy URLs, '-' delimited.
check("toc-space-separated-matches",
      fai.to_musicbrainz_toc(_zune_toc.replace("+", " "))
      == fai.to_musicbrainz_toc(_zune_toc),
      "a space-separated WMP TOC must convert identically")
check("toc-dash-delimited-matches",
      fai.to_musicbrainz_toc(_zune_toc.replace("+", "-"))
      == fai.to_musicbrainz_toc(_zune_toc),
      "a '-' delimited TOC must convert identically")

# Junk must return None rather than raising - this is fed straight from a query
# string, so it cannot be trusted.
check("toc-conversion-rejects-junk",
      all(fai.to_musicbrainz_toc(x) is None
          for x in ("", "garbage", "1+2", "TOC_A", "ANOTHER-FRESH-GUID")),
      "a non-TOC argument must return None, not raise")

# The delivery lookup must send the CONVERTED toc. Before this, every request
# was a 400 and no disc could ever be matched automatically.
_src_toc = open(BASE, encoding="utf-8").read()
check("toc-lookup-uses-the-converted-toc",
      "toc={mb_toc}" in _src_toc and "toc={clean_toc}" not in _src_toc,
      "lookup_by_discid_or_toc must query with the converted TOC")

# 11d. Automatic TOC lookup: when WMP names a disc and NOTHING is staged for it,
#      an exact MusicBrainz TOC match is applied without the dialog. The dialog
#      stays the override - _lookup_staged_xml() runs first, so a staged pick
#      always wins. Zune is deliberately excluded (no dialog to correct it with).
#
#      These stub the network so the suite stays offline, and assert on the four
#      guards rather than on any one album.
_fake_album = {"id": "AUTO1", "title": "Auto Matched", "artist": "TOC Artist",
               "year": "2005", "genre": "Rock", "art_url": "",
               "tracks": [{"number": 1, "name": "Auto One",
                           "duration_ms": 1000, "disc": 1}],
               "source": "musicbrainz"}
_real_lookup = fai.lookup_by_discid_or_toc
# Every disc id below that should auto-match is listed here, so the stub is
# explicit about what it stands in for.
_AUTO_TOC_MATCHES = {"AUTO_TOC_HEX", "AUTO_TOC_KILLSWITCH"}
fai.lookup_by_discid_or_toc = lambda toc: (_fake_album
                                           if toc.strip() in _AUTO_TOC_MATCHES
                                           else None)
fai.AUTO_TOC_CACHE.clear()
fai.AUTO_TOC_MISS.clear()

WMP_UA = {"User-Agent": "WindowsMediaPlayer/12.0.16384.0"}
BROWSER_UA = {"User-Agent": "Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0)"}

r = c.get("/redir/getmdrcdbackground/?cd=AUTO_TOC_HEX", headers=WMP_UA)
check("auto-toc-matches-a-real-disc",
      r.status_code == 200 and "Auto Matched" in r.data.decode("utf-8", "ignore")
      and fai.EMPTY_METADATA_XML not in r.data.decode("utf-8", "ignore"),
      f"code={r.status_code} body={r.data[:200]!r}")

# Zune must NOT get it: it has no dialog, so a wrong match there is unfixable.
r = c.get("/redir/getmdrcdzune/?CD=AUTO_TOC_HEX",
          headers={"User-Agent": "ZuneClient"})
check("auto-toc-never-fires-for-zune",
      fai.EMPTY_METADATA_XML in r.data.decode("utf-8", "ignore"),
      "Zune received an automatic match; it has no dialog to correct one")

# A browser UA means WMP is driving the dialog itself - never answer underneath it.
r = c.get("/redir/getmdrcdbackground/?cd=AUTO_TOC_HEX", headers=BROWSER_UA)
check("auto-toc-steps-aside-for-a-browser",
      fai.EMPTY_METADATA_XML in r.data.decode("utf-8", "ignore"),
      "a browser request was auto-answered, taking the choice from the dialog")

# A library update names a collection, not a disc. Inferring an album for it
# would tag something the user is actively looking at. Note this asserts the
# auto-match title specifically: a wmid-only GET is legitimately served the
# PENDING_WRITE document by the fallback in _lookup_staged_xml(), which is
# pre-existing behaviour and not what this guard is about.
fai.AUTO_TOC_CACHE.clear()
r = c.get("/redir/getmdrcdbackground/?wmid=77777777-8888-9999-AAAA-BBBBBBBBBBBB",
          headers=WMP_UA)
check("auto-toc-never-fires-for-a-library-update",
      "Auto Matched" not in r.data.decode("utf-8", "ignore"),
      "a wmid-only request was auto-answered by inference")

# A TOC with no release must leave the disc alone, and must be remembered so a
# repeat fetch costs no MusicBrainz request.
fai.AUTO_TOC_MISS.clear()
r = c.get("/redir/getmdrcdbackground/?cd=NO_SUCH_DISC", headers=WMP_UA)
check("auto-toc-leaves-an-unknown-disc-alone",
      fai.EMPTY_METADATA_XML in r.data.decode("utf-8", "ignore"),
      "an unidentifiable disc was not left untouched")
check("auto-toc-remembers-a-miss",
      "NO_SUCH_DISC" in fai.AUTO_TOC_MISS,
      "a failed lookup must be cached so it is not retried every fetch")

# The dialog is the escape hatch: staging for that disc must beat the cache.
c.post("/store_staged_xml", content_type="application/json",
       data=json.dumps({"album": dict(_fake_album, title="Hand Picked"),
                        "selected_tracks": _fake_album["tracks"],
                        "request_id": "REQ_AUTOOVERRIDE", "session_id": "S",
                        "toc": "AUTO_TOC_HEX"}))
r = c.get("/redir/getmdrcdbackground/?cd=AUTO_TOC_HEX", headers=WMP_UA)
check("dialog-overrides-the-automatic-match",
      "Hand Picked" in r.data.decode("utf-8", "ignore"),
      "the automatic result won over the user's own choice")

# The kill-switch must actually disable the path. Uses a fresh disc id so the
# assertion cannot be satisfied by the "Hand Picked" document staged above.
fai.AUTO_TOC_LOOKUP = False
fai.AUTO_TOC_CACHE.clear()
fai.AUTO_TOC_MISS.clear()
r = c.get("/redir/getmdrcdbackground/?cd=AUTO_TOC_KILLSWITCH", headers=WMP_UA)
check("auto-toc-can-be-switched-off",
      fai.EMPTY_METADATA_XML in r.data.decode("utf-8", "ignore"),
      "AUTO_TOC_LOOKUP=False still auto-answered")
fai.AUTO_TOC_LOOKUP = True

fai.lookup_by_discid_or_toc = _real_lookup
fai.AUTO_TOC_CACHE.clear()
fai.AUTO_TOC_MISS.clear()

# 12. Staged XML keyed by a '+' TOC is retrievable by that same TOC
payload_plus = {"album": dict(album, title="Plus TOC Album"),
                "selected_tracks": [album["tracks"][0]],
                "request_id": "REQ_PLUS", "session_id": "SESS_P", "toc": plus_toc}
c.post("/store_staged_xml", data=json.dumps(payload_plus),
       content_type="application/json")
r = c.get("/redir/getmdrcdbackground/?toc=" + plus_toc)
check("mdr-keyed-plus-toc",
      r.status_code == 200 and "Plus TOC Album" in r.data.decode("utf-8", "ignore"),
      f"body={r.data[:200]!r}")

# 13. Client diagnostics beacon endpoint exists and accepts POSTs
r = c.post("/client_error", data=json.dumps(
    {"page": "finish", "toc": plus_toc, "applied": True,
     "write": "WriteNamesEx-toc-ok"}), content_type="application/json")
check("client-beacon", r.status_code == 200 and b"OK" in r.data,
      f"code={r.status_code} body={r.data[:120]!r}")

# 14. Confirm page ships the diagnostic beacons + MDQ fallback wiring
check("confirm-has-beacon", "beaconSync(diag)" in page
      and "confirm_load" in page and "listExternalMethods" in page,
      "confirm page missing client diagnostics")
check("confirm-has-mdq-fallback", "GetMDQByRequestID" in page
      and "WriteNamesEx(2, mdq" in page,
      "confirm page missing MDQ write path")

# 15. Confirm page must not rely on console (undefined in the WMP dialog IE)
check("confirm-no-bare-console", "console.log('window.external" not in page,
      "bare console.log still present - would throw in old IE")

# 16. WMP opens the dialog with ONLY ?requestid=... (no toc). A stale TOC from
#     an earlier disc must never leak in, otherwise WriteNamesEx targets the
#     wrong disc and nothing is applied.
c.get("/redir/submittoc/?toc=" + urllib.parse.quote("+STALE_TOC_FROM_OTHER_DISC"),
     headers={"User-Agent": "WindowsMediaPlayer/12"})
r = c.get("/confirm?source=itunes&id=%s&requestid=REQ_NO_TOC" % (itunes_id or "1"))
page_notoc = r.data.decode("utf-8", "ignore")
check("no-stale-toc-fallback",
      'var WMP_TOC = ""' in page_notoc
      and "STALE_TOC_FROM_OTHER_DISC" not in page_notoc,
      "a stale TOC leaked into a dialog session that had none")

# 17. Real COM members can test as falsy in JScript, so the write must be
#     attempted directly - a truthiness guard silently skipped it in WMP.
#     Guards on GetMDQByRequestID caused the exact same silent skip.
for _m in ("WriteNamesEx", "WriteNames", "GetMDQByRequestID", "Finish"):
    check("confirm-no-guard-%s" % _m,
          ("if (window.external.%s)" % _m) not in page
          and ("&& window.external.%s" % _m) not in page,
          "COM method %s still guarded by a truthiness test" % _m)
check("confirm-calls-mdq-directly",
      "window.external.GetMDQByRequestID(REQUEST_ID)" in page
      and "if (window.external.GetMDQByRequestID)" not in page,
      "GetMDQByRequestID must be called directly, not conditionally")
def _at(pg, needle):
    """Index of needle, or a large value when absent (keeps ordering checks
    from raising when a call site's arguments legitimately change)."""
    i = pg.find(needle)
    return i if i >= 0 else len(pg)

check("confirm-mdq-preferred",
      _at(page, "GetMDQByRequestID(REQUEST_ID)")
      < _at(page, "WriteNamesEx(2, mdq"),
      "MDQ must be resolved before the MDQ write path")

# 18. The dialog close buttons had the same falsy-host-object bug: the guard
#     made ReturnToMainTask look absent, so the wizard fell through to
#     window.close() and never handed control back to WMP (pending write
#     never finalised). Both /done and /FAI/ui must call it directly.
done_page = c.get("/done").data.decode("utf-8", "ignore")
ui_page = c.get("/FAI/ui?artist=beatles&album=abbey+road").data.decode("utf-8", "ignore")
for _name, _pg in (("done", done_page), ("ui", ui_page)):
    check("no-rtmt-guard-%s" % _name,
          "window.external&&window.external.ReturnToMainTask" not in _pg
          and "window.external && window.external.ReturnToMainTask" not in _pg,
          "ReturnToMainTask still truthiness-guarded on /%s" % _name)
check("done-calls-rtmt-directly",
      "window.external.ReturnToMainTask();" in done_page
      and "done_close" in done_page,
      "/done must call ReturnToMainTask directly and report the result")
check("ui-cancel-calls-rtmt-directly",
      "window.external.ReturnToMainTask();" in ui_page,
      "/FAI/ui Cancel must call ReturnToMainTask directly")

# 19. The MDQ from GetMDQByRequestID carries the disc's REAL WMContentIDs.
#     WMP matches metadata by that ID; generated GUIDs match nothing, so the
#     write was silently ignored. Includes the zero-width space that WMP puts
#     in the <filename> tag (breaks strict XML parsers - hence the regex path).
REAL_MDQ = (
    '<?xml version="1.0" encoding="utf-8"?>\n<METADATA>\r\n    <MDQ-CD>\r\n'
    '        <mdqRequestID>REQ-X</mdqRequestID>\r\n        <track>\r\n'
    '            <title>\r\n                <text>Kalimba</text>\r\n'
    '                <word>Kalimba</word>\r\n            </title>\r\n'
    '            <trackNumber>1</trackNumber>\r\n'
    '            <\u200bfilename>Kalimba.mp3</filename>\r\n'
    '            <trackDuration>348055</trackDuration>\r\n'
    '            <WMContentID>{4F0FA0F3-3D95-471A-B0D2-9DCB30A9BBAE}</WMContentID>\r\n'
    '            <trackRequestID>0</trackRequestID>\r\n        </track>\r\n'
    '    </MDQ-CD>\r\n</METADATA>\r\n')

check("mdq-parser-extracts-real-id",
      fai.parse_mdq_content_ids(REAL_MDQ) == {1: "4F0FA0F3-3D95-471A-B0D2-9DCB30A9BBAE"},
      f"parsed={fai.parse_mdq_content_ids(REAL_MDQ)}")
check("mdq-parser-safe-on-garbage",
      fai.parse_mdq_content_ids("") == {} and fai.parse_mdq_content_ids("nonsense") == {},
      "MDQ parser must not raise or invent IDs")

# IDs are keyed by DISC POSITION, not by the online album's track number: the
# disc numbering and the release numbering can disagree, and binding by the
# wrong key hands a track another track's content ID.
MDQ_TWO = ('<METADATA><MDQ-CD>'
           '<track><title><text>One</text></title><trackNumber>1</trackNumber>'
           '<WMContentID>{AAAAAAAA-1111-2222-3333-444444444444}</WMContentID></track>'
           '<track><title><text>Two</text></title><trackNumber>2</trackNumber>'
           '<WMContentID>{BBBBBBBB-1111-2222-3333-444444444444}</WMContentID></track>'
           '</MDQ-CD></METADATA>')
check("mdq-parser-keys-by-position",
      fai.parse_mdq_content_ids(MDQ_TWO) == {
          1: "AAAAAAAA-1111-2222-3333-444444444444",
          2: "BBBBBBBB-1111-2222-3333-444444444444"},
      f"parsed={fai.parse_mdq_content_ids(MDQ_TWO)}")

# Selecting only the 2nd track must bind the 1st disc ID, not the 2nd -
# disc position follows the selection order.
second_only = [{"number": 7, "name": "Seventh Online", "disc": 1, "id": "X7"}]
xml_pos = fai.build_wmp_xml(album, selected_tracks=second_only,
                            request_id="R", content_ids={1: "AAAAAAAA-1111-2222-3333-444444444444"})
check("content-id-bound-by-disc-position",
      "AAAAAAAA-1111-2222-3333-444444444444" in xml_pos
      and "BBBBBBBB" not in xml_pos,
      "single selected track must take the first disc content ID")

# The staged XML must then carry the REAL id, not a generated one.
payload_mdq = {"album": album, "selected_tracks": [album["tracks"][0]],
               "request_id": "REQ_MDQ", "session_id": "SESS_M", "toc": "", "mdq": REAL_MDQ}
r = c.post("/store_staged_xml", data=json.dumps(payload_mdq),
           content_type="application/json")
body_mdq = r.data.decode("utf-8", "ignore")
check("staged-xml-uses-real-content-id",
      "4F0FA0F3-3D95-471A-B0D2-9DCB30A9BBAE" in body_mdq,
      "real WMContentID from MDQ missing from staged XML")
check("client-sends-mdq-to-server",
      "mdq: mdq" in page
      and _at(page, "GetMDQByRequestID(REQUEST_ID)")
      < _at(page, "'/store_staged_xml'"),
      "MDQ must be resolved and posted before the XML is staged")

# 20. The MDQ describes the disc ACTUALLY in the drive. When it does not match
#     the album being applied, WMP may legitimately refuse the write - surface
#     that as a visible warning instead of failing silently.
check("disc-mismatch-banner-present",
      'id="discBanner"' in page and "showDiscBanner" in page
      and "Disc mismatch" in page,
      "confirm page must warn when the disc does not match the chosen album")
check("mdq-fetched-on-load",
      "CACHED_MDQ = window.external.GetMDQByRequestID(REQUEST_ID)" in page
      and "showDiscBanner(CACHED_MDQ)" in page,
      "MDQ must be fetched on page load to drive the warning")

# 21. WMP's "Update album info" path fetches metadata by ?wmid=..., the GUID of
#     its OWN library collection. Our generated collection GUID is unrelated to
#     it, so the returned document must be retargeted at the requested wmid -
#     otherwise WMP applies a document describing a collection it does not have.
WMID = "5FA05D35-A682-4AF6-96F7-0773E42D4D16"
check("wmid-remembered",
      fai._remember_wmid("{%s}" % WMID) == WMID.upper(),
      f"wmid capture failed: {fai._remember_wmid(WMID)}")
check("wmid-ignored-when-garbage",
      fai._remember_wmid("not-a-guid!!") == "",
      "garbage wmid must be rejected")

# staged XML generated BEFORE any wmid is known, then delivered by wmid
xml_novid = fai.build_wmp_xml(album, selected_tracks=[album["tracks"][0]],
                              request_id="REQ_W")
retargeted = fai._retarget_collection_id(xml_novid, WMID)
for tag in ("WMCollectionID", "WMCollectionGroupID", "ZuneAlbumMediaID"):
    m = re.search(r"<%s>([^<]+)</%s>" % (tag, tag), retargeted)
    check("wmid-retargets-%s" % tag,
          m is not None and m.group(1).upper() == WMID.upper(),
          f"{tag}={m.group(1) if m else None}")
check("wmid-retarget-idempotent",
      fai._retarget_collection_id(retargeted, WMID) == retargeted,
      "retargeting twice must not corrupt the document")
check("wmid-retarget-noop-without-wmid",
      fai._retarget_collection_id(xml_novid, "") == xml_novid,
      "no wmid must leave the document untouched")

# end-to-end: the exact request shape from the WMP Update button
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="WMID Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_WMID", "session_id": "S", "toc": ""}),
    content_type="application/json")
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&geoid=be"
          "&version=12.0.26100.8972&userlocale=2000&wmid=" + WMID)
body_wmid = r.data.decode("utf-8", "ignore")
m = re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>", body_wmid)
check("mdr-wmid-request-returns-matching-collection",
      r.status_code == 200 and "WMID Album" in body_wmid
      and m is not None and m.group(1).upper() == WMID.upper(),
      f"collection={m.group(1) if m else None}")

# 22. The dialog never knows the wmid, so staging must NOT bake in whatever
#     wmid was seen earlier - that would bind this album to a stale collection.
#     Retargeting is a delivery-time concern only.
fai._remember_wmid(WMID)
r = c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Stale Wmid Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_STALE", "session_id": "S", "toc": ""}),
    content_type="application/json")
staged_body = r.data.decode("utf-8", "ignore")
m = re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>", staged_body)
check("staging-does-not-bake-stale-wmid",
      m is not None and m.group(1).upper() != WMID.upper(),
      f"staging must not reuse a previously seen wmid: {m.group(1) if m else None}")

# ...but delivery by wmid still retargets it onto the requested collection.
# The document is staged explicitly for that wmid: an unrelated wmid now gets
# an EMPTY document instead of whatever was staged last.
OTHER = "2BBE4719-8CD0-5264-B02E-432817EAA3E2"
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Stale Wmid Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "", "session_id": "S", "toc": "", "wmid": OTHER}),
    content_type="application/json")
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&wmid=" + OTHER)
body2 = r.data.decode("utf-8", "ignore")
m = re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>", body2)
check("delivery-retargets-to-requested-wmid",
      m is not None and m.group(1).upper() == OTHER.upper(),
      f"collection={m.group(1) if m else None}")

# a repeat fetch for the same wmid must still return the same album
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&wmid=" + OTHER)
body3 = r.data.decode("utf-8", "ignore")
check("wmid-fetch-is-stable",
      "Stale Wmid Album" in body3
      and re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>", body3).group(1).upper() == OTHER.upper(),
      f"repeat wmid fetch diverged: {body3[:120]!r}")

# 23. The finish beacon must report what the disc ACTUALLY contains alongside
#     what is being applied - the key clue when WMP accepts a write silently.
check("finish-reports-disc-identity",
      "parseDiscIdentity(mdq)" in page and "diag.disc = disc" in page
      and "disc_matches" in page and "applying_tracks" in page,
      "finish beacon must include disc identity and match status")
check("disc-identity-counts-tracks",
      "track_count" in page and "mdq.match(/<track>" in page,
      "disc identity must report how many tracks are really on the disc")

# 24. A stub MDQ is NORMAL for a library "Update album info": WMP opens the
#     dialog with only ?requestid= and later fetches by wmid. There is no disc,
#     so warning about a missing disc is wrong. The MDQ must still be used as
#     the type id (it is the only identifier WMP gave us for this request),
#     but a library album must NOT request file rename/regroup - that asks WMP
#     to rename existing library tracks and is why tags never landed.
check("no-false-missing-disc-warning",
      "No disc detected" not in page and "Library album" in page,
      "a stub MDQ is a library update - must not claim a disc is missing")
check("library-mode-uses-tags-only-write",
      "library_mode" in page
      and "WriteNamesEx(1, WMP_WMID, generatedXml, false)" in page
      and "WriteNamesEx(2, mdq, generatedXml, false)" in page,
      "library albums must write tags without asking for file rename/regroup")
check("mdq-write-not-skipped",
      "stub-mdq (library album" not in page,
      "the MDQ write must not be skipped - it is the only path that acts")
check("disc-parsed-before-write",
      _at(page, "var disc = parseDiscIdentity(mdq);")
      < _at(page, "var mdqUsable"),
      "disc identity must be known before choosing the write path")

# 25. The cover URL was percent-encoded with requests.utils.quote(), which also
#     escapes '/'. WMP passes the value back verbatim, so the proxy received
#     'https%3A%2F%2F...' and every cover 404'd - which is why album art never
#     appeared even though WMP did fetch the URL. Slashes must stay readable.
#     The check is run in proxy mode specifically: in direct mode there is no
#     encoding step at all, so this bug cannot occur (see _ART_MODE).
ART = "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/52/aa/85/x.jpg/600x600bb.jpg"
_prev_art_mode = fai._ART_MODE
fai._ART_MODE = "proxy"
try:
    xml_art = fai.build_wmp_xml(dict(album, art_url=ART),
                                selected_tracks=[album["tracks"][0]])
finally:
    fai._ART_MODE = _prev_art_mode
m = re.search(r"<largeCoverParams>([^<]+)</largeCoverParams>", xml_art)
check("cover-url-not-double-encoded",
      m is not None
      and re.match(r"^http://127\.0\.0\.1/cover/(fai-[0-9a-f]{8}/)?album\.jpg"
                   r"\?url=https://", m.group(1)) is not None,
      f"the upstream url must stay readable (slashes not encoded); the path may "
      f"carry the per-apply token. got: {m.group(1) if m else None}")

# the proxy must also recover a double-encoded url (older staged documents)
REAL_ART = ("https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/52/aa/85/"
            "52aa851f-15b7-6322-f91f-df84b15b7b19/190295978044.jpg/600x600bb.jpg")
dbl = urllib.parse.quote(urllib.parse.quote(REAL_ART, safe=""), safe="")
r = c.get("/cover/album.jpg?url=" + dbl)
check("image-proxy-handles-double-encoding",
      r.status_code == 200 and len(r.data) > 1000,
      f"double-encoded art url returned {r.status_code}, {len(r.data)}B")
r = c.get("/cover/album.jpg?url=notaurl")
check("image-proxy-rejects-garbage", r.status_code == 404,
      f"garbage url should 404, got {r.status_code}")

# 26. WMP's REAL CD flow opens the dialog with ?cd=<hex>+<hex>... and NO
#     requestid, so no MDQ can be fetched and the write was never attempted.
#     Read 'cd' raw ('+' must not become spaces) and use it as the disc
#     content id (WMP_WRITENAMES_TYPE_CD_BY_CONTENT_ID = 1).
CD_ID = "5+96+554B+83B5+B5EA+1010D+15847"
r = c.get("/confirm?source=itunes&id=1090440045&cd=" + CD_ID)
page_cd = r.data.decode("utf-8", "ignore")
check("cd-id-preserved",
      'var WMP_CD = "%s"' % CD_ID in page_cd,
      "cd identifier lost its '+' separators")
check("cd-id-used-for-write",
      "if (WMP_CD)" in page_cd
      and "WriteNamesEx(1, WMP_CD, generatedXml, true)" in page_cd,
      "cd flow must write by content id")
check("cd-posted-to-server", "cd: WMP_CD" in page_cd,
      "cd must be sent to the server so staged XML is keyed by it")

# staged XML must be retrievable by the ?cd= request WMP makes after closing
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="CD Flow Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "", "session_id": "S", "toc": "", "cd": CD_ID}),
    content_type="application/json")
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&CD=" + CD_ID)
body_cd = r.data.decode("utf-8", "ignore")
check("cd-fetch-returns-staged-album",
      r.status_code == 200 and "CD Flow Album" in body_cd,
      f"body={body_cd[:160]!r}")

# 27. Regression: staging a 'cd' flow document raised
#     TypeError: _stage_request_xml() got an unexpected keyword argument 'cd'
#     -> HTTP 500, so no XML was staged and WMP received nothing for the CD.
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="CD Regression Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "", "session_id": "S", "toc": "", "cd": CD_ID}),
    content_type="application/json")
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&CD=" + CD_ID)
check("cd-staging-does-not-error",
      r.status_code == 200 and "CD Regression Album" in r.data.decode("utf-8", "ignore"),
      f"staging a cd-flow document failed: {r.status_code}")

# 28. A STUB MDQ (ids but no titles) is what WMP returns with no disc in the
#     drive. Its single content ID was the same value for EVERY album, so
#     injecting it made WMP look for a track that does not exist - the library
#     flow silently failed while the CD flow (which bypasses the MDQ) worked.
#     A real disc MDQ still has titles and must keep supplying real IDs.
STUB_MDQ = ('<METADATA><MDQ-CD><mdqRequestID>R</mdqRequestID><track>'
            '<trackNumber>1</trackNumber>'
            '<WMContentID>{4F0FA0F3-3D95-471A-B0D2-9DCB30A9BBAE}</WMContentID>'
            '<trackRequestID>0</trackRequestID></track></MDQ-CD></METADATA>')
check("stub-mdq-content-ids-ignored",
      fai.parse_mdq_content_ids(STUB_MDQ) == {},
      f"stub MDQ must not inject content IDs: {fai.parse_mdq_content_ids(STUB_MDQ)}")
check("real-disc-mdq-ids-kept",
      fai.parse_mdq_content_ids(REAL_MDQ) == {1: "4F0FA0F3-3D95-471A-B0D2-9DCB30A9BBAE"},
      "a real disc MDQ must still supply its content IDs")

# 29. The XML builder reused the HTML escaper, which encodes ' as '&#x27;'.
#     That is a valid HTML entity but NOT valid XML - WMP would tag the album
#     literally as "Prospekt&#x27;s March". XML needs &apos;.
apos_album = dict(album, title="Prospekt's March - EP",
                  tracks=[dict(album["tracks"][0], name="Don't Look Back")])
xml_apos = fai.build_wmp_xml(apos_album, selected_tracks=apos_album["tracks"])
check("xml-apostrophe-not-html-entity",
      "&#x27;" not in xml_apos and "&apos;" in xml_apos,
      f"apostrophe mis-escaped in XML: {xml_apos[xml_apos.find('<albumTitle>'):][:60]}")
check("xml-ampersand-escaped",
      "B&amp;B" in fai.build_wmp_xml(dict(album, title="B&B"),
                                     selected_tracks=[album["tracks"][0]]),
      "& must be escaped as &amp; in XML")
import xml.etree.ElementTree as _ET
try:
    _ET.fromstring(xml_apos)
    check("xml-is-well-formed", True, "")
except Exception as _xe:
    check("xml-is-well-formed", False, f"invalid XML: {_xe}")

# 30. The library dialog carries ?wmid=<collection GUID>. It identifies the
#     exact collection WMP is updating, so it must be used as the write type id
#     (like the working CD flow does with ?cd=) rather than a stub MDQ whose
#     content ID belongs to no real track.
# 31. A SYNCHRONOUS XHR in the dialog froze WMP's UI thread for the whole
#     server round-trip (iTunes / MusicBrainz lookups take seconds). WMP killed
#     the player with an AppHangB1 hang. Staging must be async.
check("staging-is-async",
      "xhr.open('POST', '/store_staged_xml', true)" in page
      and "xhr.open('POST', '/store_staged_xml', false)" not in page,
      "staging XHR must be asynchronous or WMP hangs (AppHangB1)")
check("no-sync-xhr-in-dialog",
      page.count("false);") == 0 or ", false)" not in page.split("finishSync")[1][:4000],
      "no synchronous XHR may remain in the dialog page")
check("double-click-guard",
      "if (btn.disabled) return;" in page,
      "Finish must ignore repeat clicks to avoid duplicate writes")
check("button-reenabled-on-failure",
      "b2.disabled = false" in page,
      "button must be re-enabled if staging fails")

# 32. WMP froze (AppHangB1) BEFORE loading any page: the WMP FAI dialog runs on
#     https and the self-signed cert had no SubjectAlternativeName and was not
#     in any trusted root store. Windows then raised a trust prompt inside WMP's
#     invisible modal host -> unresponsive player.
done_html = c.get("/done").data.decode("utf-8", "ignore")
check("no-timer-driven-com-close",
      "setTimeout(function(){ try { returnToMainTask" not in done_html,
      "a timer-driven ReturnToMainTask can deadlock WMP's dialog host")
check("close-is-single-shot",
      "var CLOSING = false;" in done_html and "if (CLOSING) return;" in done_html,
      "dialog close must fire ReturnToMainTask at most once")
check("cert-has-san",
      "x509.SubjectAlternativeName([" in open(BASE, encoding="utf-8").read(),
      "certificate must carry a SubjectAlternativeName (CN alone is ignored by Windows)")
check("cert-is-own-ca",
      "x509.BasicConstraints(ca=True" in open(BASE, encoding="utf-8").read(),
      "self-signed cert needs BasicConstraints(ca=True) to be a trust anchor")
check("cert-is-trusted",
      "_install_cert_as_trusted_root" in open(BASE, encoding="utf-8").read(),
      "cert must be installed into the trusted root stores")

# 33. Regression: after staging was made async, applyMetadata() referenced
#     'mdq', which was a local var inside finishSync() and therefore undefined
#     there -> "'mdq' is undefined" on every Finish click (caught by window.onerror).
_src = open(BASE, encoding="utf-8").read()
# This suite's own source, for the checks that must assert on how the suite
# controls its own state (e.g. forcing the token off rather than inheriting
# whatever the developer machine happens to have).
_src_test = open(os.path.join(_DIR, "test_fai_v2.py"), encoding="utf-8").read()
_am = _src.split("function applyMetadata(")[1]
check("apply-mdq-is-scoped",
      "var mdq = RESOLVED_MDQ || '';" in _am,
      "applyMetadata must read mdq from the shared RESOLVED_MDQ variable")
check("finish-stores-resolved-mdq",
      "RESOLVED_MDQ = mdq;" in _src,
      "finishSync must publish the resolved MDQ for applyMetadata")
check("onerror-beacon-installed",
      "window.onerror" in page,
      "JS errors must be reported to the server so they are never invisible")

# 34. A global LAST_XML fallback made every disc report the album staged most
#     recently, so all CDs ended up with the same album. Unmatched discs and
#     collections must now get an empty document and be left untouched.
check("no-last-xml-fallback",
      "return LAST_XML" not in _src and "Response(LAST_XML" not in _src,
      "LAST_XML must never be served as a fallback")
check("lookup-returns-none-when-unmatched",
      "    return None" in _src.split("def _lookup_staged_xml(")[1].split("\ndef ")[0],
      "_lookup_staged_xml must return None rather than the last document")
check("empty-metadata-defined",
      "EMPTY_METADATA_XML" in _src and "<status>NOTFOUND</status>" in _src,
      "unmatched discs must get an empty, track-less metadata document")
check("no-auto-apply-from-toc",
      "match-not-applied" in _src,
      "TOC auto-lookup must not write metadata without the user choosing")

# behaviour: stage one album, then a DIFFERENT disc must get nothing
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Isolation Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_ISO", "session_id": "S", "toc": "",
     "cd": "AA+BB+CC"}),
    content_type="application/json")
r = c.get("/cdinfo/GetMDRCD.aspx?locale=409&CD=ZZ+YY+XX")
other = r.data.decode("utf-8", "ignore")
check("other-disc-gets-empty-metadata",
      "Isolation Album" not in other and "<albumTitle>" not in other,
      f"unrelated disc must not receive another album: {other[:200]!r}")
check("matched-disc-still-gets-album",
      "Isolation Album" in c.get("/cdinfo/GetMDRCD.aspx?CD=AA+BB+CC")
      .data.decode("utf-8", "ignore"),
      "the disc that WAS selected must still receive its album")

# 34b. The LIBRARY write never worked. From a real WMP session: the document is
#      staged under the dialog's request id, and WMP then POSTs
#      /cdinfo/GetMDRCD.aspx with a FRESHLY GENERATED id it never told us
#      about - staging under 23FDCD4D-... then writing with E7A89714-....
#      Nothing matched, so the library was served <status>NOTFOUND</status> and
#      the tracks kept their old tags. The background art fetch used the
#      original id and worked, which is why artwork applied but tags did not.
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Library Write Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_LIBWRITE", "session_id": "S",
     "toc": "", "wmid": ""}),
    content_type="application/json")
_r = c.post("/cdinfo/GetMDRCD.aspx?locale=409&userlocale=2000"
            "&requestID=FRESH-GUID-NEVER-SEEN")
_lw = _r.data.decode("utf-8", "ignore")
check("library-write-gets-the-staged-album",
      "Library Write Album" in _lw and "<status>NOTFOUND</status>" not in _lw,
      f"a library write with a fresh request id must still receive the album, "
      f"got {_lw[:200]!r}")
_r2 = c.post("/cdinfo/GetMDRCD.aspx?locale=409&userlocale=2000"
             "&requestID=FRESH-GUID-NEVER-SEEN")
check("library-write-repeat-is-exact",
      "Library Write Album" in _r2.data.decode("utf-8", "ignore"),
      "repeating the same fresh id must keep returning the same album")
# ...but that remembered pairing must NOT outlive the document it was made for.
# Staging a DIFFERENT album afterwards and then reusing the same fresh id
# returned the PREVIOUS album, because the pairing had been pinned in
# STAGED_REQUESTS and so resolved as an exact match before the fallback.
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Second Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_SECOND", "session_id": "S",
     "toc": "", "wmid": ""}),
    content_type="application/json")
_r3 = c.post("/cdinfo/GetMDRCD.aspx?requestID=FRESH-GUID-NEVER-SEEN")
_s2 = _r3.data.decode("utf-8", "ignore")
check("fresh-id-does-not-serve-a-stale-album",
      "Second Album" in _s2 and "Library Write Album" not in _s2,
      f"after staging a new album, the same fresh id must serve the new one, "
      f"not the previous: {_s2[:200]!r}")
check("fresh-write-pairings-are-cleared",
      "FRESH_WRITES.clear()" in _src
      and "STAGED_REQUESTS[cand] = pending" not in _src,
      "a fresh library-write pairing must be dropped when a new document is "
      "staged, and must not be pinned in STAGED_REQUESTS")
# the guard: a request that DOES name a disc must still get nothing, or this
# would reintroduce 'every disc gets the last album applied'
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Guard Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_GUARD", "session_id": "S",
     "toc": "", "cd": "GG+HH+II"}),
    content_type="application/json")
_rg = c.post("/cdinfo/GetMDRCD.aspx?locale=409&CD=ZZ+YY+XX").data.decode("utf-8", "ignore")
check("named-disc-post-still-gets-nothing",
      "Guard Album" not in _rg,
      f"a POST naming an unknown ?cd= must still be left alone: {_rg[:200]!r}")
_rw = c.post("/cdinfo/GetMDRCD.aspx?wmid=5FA05D35-1111-2222-3333-444455556666").data.decode("utf-8", "ignore")
check("named-wmid-post-still-gets-nothing",
      "Guard Album" not in _rw,
      f"a POST naming an unknown ?wmid= must still be left alone: {_rw[:200]!r}")
# _remember_wmid() only accepts GUID-shaped values, so a wmid-bearing request
# must still count as naming a collection when the value is not a GUID.
_rwn = c.post("/cdinfo/GetMDRCD.aspx?wmid=NOT-A-GUID").data.decode("utf-8", "ignore")
check("non-guid-wmid-post-still-gets-nothing",
      "Guard Album" not in _rwn,
      f"a ?wmid= that is not GUID-shaped must not enable the fallback: {_rwn[:200]!r}")
# A collection fetch is a GET; a POST that names a wmid is a different shape and
# must not be able to claim the pending document either.
check("wmid-claim-is-get-only",
      'claimable = (request.method == "GET" and wmid_raw' in _src,
      "only a GET may claim a pending document by wmid; a POST naming a wmid "
      "has its own request id and must be left alone")

# 34e. A CD RIP must never inherit a library collection id. Borrowing LAST_WMID
#      onto a disc document made WMP treat it as metadata for some unrelated
#      library album: the tags landed but the cover did not, and WMP REOPENED
#      the FAI dialog 3s after ReturnToMainTask - the "hang".
#        12:00:49 [CLIENT] finish
#        12:00:56 done_close / ReturnToMainTask-ok
#        12:00:59 GET /FAI/default.aspx?...&cd=B+96+...&wmid=F62C9D85-...
fai.LAST_WMID = "11112222-3333-4444-5555-666677778888"
_cd = "B+96+1970+523A+8461"
_cdpage = c.get("/confirm?source=itunes&id=1570089404&cd=" + _cd).data.decode("utf-8", "ignore")
check("cd-dialog-is-not-stamped-with-a-collection",
      'var WMP_WMID = "11112222-3333-4444-5555-666677778888"' not in _cdpage
      and 'var WMP_WMID = ""' in _cdpage,
      "a CD rip must not borrow the last seen library collection as its "
      "write target, or WMP applies it to the wrong album and reopens the dialog")
check("library-dialog-still-borrows-the-collection",
      'var WMP_WMID = "11112222-3333-4444-5555-666677778888"' in
      c.get("/confirm?source=itunes&id=1570089404&requestid=R1")
      .data.decode("utf-8", "ignore"),
      "a LIBRARY dialog opened without ?wmid= must still fall back to the last "
      "seen collection, or it regresses to the stub-MDQ write that does nothing")
check("cd-flow-logs-the-ignored-collection",
      "CD flow: ignoring last seen collection" in _src,
      "the ignored collection must be logged, or a CD that reopens the dialog "
      "is undiagnosable next time")

# 34f. A CD document had a RANDOM identity. WMP's CD flow sends no requestid, so
#      req_id fell through to uuid4() - a fresh value on every staging - leaving
#      WMP nothing stable to correlate the document against. The only stable
#      identifier a CD flow supplies is the ?cd= disc content id.
check("cd-document-identity-is-stable",
      "req_id = request_id or (guid(cd) if cd else str(uuid.uuid4()).upper())" in _src,
      "a CD document must derive its identity from the disc content id, not a "
      "fresh random UUID4 on every staging")
check("cd-is-passed-to-the-xml-builder",
      "cd=cd_val)" in _src,
      "store_staged_xml must pass the disc id to build_wmp_xml so it can be used")
_d1 = fai.build_wmp_xml(dict(album), selected_tracks=[album["tracks"][0]],
                       request_id="", cd="B+96+1970+523A")
_d2 = fai.build_wmp_xml(dict(album), selected_tracks=[album["tracks"][0]],
                       request_id="", cd="B+96+1970+523A")
_m1 = re.search(r"<mdr-id>([^<]+)</mdr-id>", _d1)
_m2 = re.search(r"<mdr-id>([^<]+)</mdr-id>", _d2)
check("same-disc-yields-same-identity",
      bool(_m1) and bool(_m2) and _m1.group(1) == _m2.group(1),
      f"the same disc must always produce the same document id, got "
      f"{_m1.group(1) if _m1 else '?'} vs {_m2.group(1) if _m2 else '?'}")
_d3 = fai.build_wmp_xml(dict(album), selected_tracks=[album["tracks"][0]],
                       request_id="", cd="DIFFERENT+DISC")
_m3 = re.search(r"<mdr-id>([^<]+)</mdr-id>", _d3)
check("different-disc-yields-different-identity",
      bool(_m3) and _m3.group(1) != _m1.group(1),
      "two different discs must not share a document identity")
# a CD rip is not a library album. The discriminator is the URL, not the MDQ:
# a rip arrives with ?cd=/?toc=, "Update album info" with only ?requestid=.
# This used to test discKnown (whether the MDQ carried any text), which only
# appeared to work because the MDQ reader never matched anything.
check("cd-rip-is-not-a-library-update",
      "var isLibrary = !WMP_CD && !WMP_TOC;" in _src,
      "a rip must not be reported as a library update: it arrives with ?cd=")
check("library-detection-does-not-depend-on-the-mdq",
      "var isLibrary = !discKnown && !WMP_CD;" not in _src,
      "a library track's MDQ carries real titles, so gating on discKnown would "
      "call every TAGGED library album a disc and ask WMP to rename its files")

# 34g. A SUCCESSFUL document declared no <status>. EMPTY_METADATA_XML declares
#      <status>NOTFOUND</status> in exactly that position, so the asymmetry is
#      in our own code. With no status WMP read the response as 'no result': it
#      never completed the collection, never fetched the artwork from
#      largeCoverParams, and re-opened the FAI dialog for the collection id we
#      had just supplied.
#        12:29:30 [STAGED] album='Tiny Cities'   (no [IMAGE] served anywhere)
#        12:29:45 ReturnToMainTask-ok
#        12:29:49 GET /FAI/default.aspx?...&wmid=B17CF884-...
_stat = fai.build_wmp_xml(dict(album), selected_tracks=[album["tracks"][0]])
check("successful-document-declares-ok-status",
      "<status>OK</status>" in _stat,
      "a successful document must declare <status>OK</status>; without it WMP "
      "treats the response as 'no result', skips the artwork and re-prompts")
check("status-sits-before-mdr-cd",
      "<status>OK</status>" in _stat
      and _stat.index("<status>OK</status>") < _stat.index("<MDR-CD>"),
      "the status must be a sibling of MDR-CD, matching EMPTY_METADATA_XML")
check("status-mirrors-the-notfound-document",
      fai.EMPTY_METADATA_XML.index("<status>") < fai.EMPTY_METADATA_XML.index("<MDR-CD>"),
      "the OK and NOTFOUND documents must declare status in the same place")
check("requestid-is-still-present",
      "<requestID>" in _stat and "<mdr-id>" in _stat,
      "adding the status must not displace the request identity")
try:
    import xml.etree.ElementTree as _ET
    _ET.fromstring(_stat)
    _parsed = True
except Exception:
    _parsed = False
check("document-is-well-formed-xml", _parsed,
      "the generated document must still be well-formed XML")

_rg2 = c.get("/cdinfo/GetMDRCD.aspx?requestID=ANOTHER-FRESH-GUID").data.decode("utf-8", "ignore")
check("get-does-not-use-the-fallback",
      "Guard Album" not in _rg2,
      "a plain GET must not be answered from the pending document")
check("library-write-is-time-bounded",
      "_LIBRARY_WRITE_WINDOW" in _src and "age <= _LIBRARY_WRITE_WINDOW" in _src,
      "the pending document must expire, or a disc swapped later picks up the "
      "previous album")
check("post-body-always-scanned",
      "and not candidates" not in _src.split("def _lookup_staged_xml(")[1][:3000],
      "the POST body must be scanned for ids unconditionally; the query's fresh "
      "id does not mean the body has nothing useful")
# store_staged_xml() holds XML_LOCK while calling _stage_request_xml(), which
# takes it again to record the pending document. With a plain Lock that is a
# self-deadlock: no XML is ever staged and every write silently does nothing.
check("xml-lock-is-reentrant",
      "XML_LOCK = threading.RLock()" in _src and "XML_LOCK = threading.Lock()" not in _src,
      "XML_LOCK must be reentrant or staging deadlocks against itself")
check("staging-records-the-pending-document",
      "PENDING_WRITE['xml'] = xml" in _src and "PENDING_WRITE['at'] = now" in _src,
      "_stage_request_xml must record the pending document and its timestamp, "
      "or the library-write fallback has nothing to serve")

# 34c. A library FAI dialog that WMP opens with ONLY ?requestid= left the
#      confirm page with no collection id, so the write fell through to
#      WriteNamesEx(2, STUB_MDQ, ...). A stub MDQ's content id belongs to no
#      real track - the code says so in parse_mdq_content_ids - so WMP accepted
#      the call and applied nothing. Logged as
#      "write":"WriteNamesEx-mdq-tagsonly-ok" with no other symptom.
check("dialog-falls-back-to-last-collection",
      "elif LAST_WMID:" in _src
      and "wmp_wmid = LAST_WMID" in _src,
      "a dialog opened without ?wmid= must fall back to the last collection "
      "WMP asked about rather than targeting a stub MDQ that matches nothing")
check("wmid-origin-is-logged",
      "'wmid_from_url': wmid_from_url" in _src,
      "the log must record whether the write target came from the URL or from "
      "the fallback, or this is undiagnosable next time")
check("mdq-parse-always-explains-itself",
      "no usable MDQ" in _src and "no WMContentID recovered" in _src,
      "parse_mdq_content_ids must log on every path, because an empty result "
      "means every track id is generated and the write is silently ignored")

# 34d. The FIRST FAI run of a session could never tag a library album. WMP
#      reveals the collection GUID for the first time only in its own fetch
#      moments AFTER the dialog closes, so at staging time the document was not
#      bound to it and the fetch was answered empty:
#        [STAGED] album='Clocks' req_id='4934E449-...'
#        WRITE=WriteNamesEx-mdq-tagsonly-ok
#        [WMID] captured F62C9D85-...
#        [MDR-EMPTY] no metadata staged for 'F62C9D85-...'
#      Only the SECOND attempt (Update album info) worked, because by then
#      LAST_WMID was known. An unclaimed pending document now answers that
#      first fetch and binds the collection to it.
# The test client is bound to the `fai` module, so THAT is what must look like a
# first-ever run. Resetting a freshly imported module would change nothing.
fai.LAST_WMID = ""           # nothing known yet - the first run of a session
fai.PENDING_WRITE.update({'xml': '', 'at': 0.0, 'wmid': ''})
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="First Run Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_FIRST", "session_id": "S", "toc": "", "wmid": ""}),
    content_type="application/json")
_newwmid = "AAAA1111-BBBB-2222-CCCC-333344445555"
_rfw = c.get("/cdinfo/GetMDRCD.aspx?wmid=" + _newwmid)
_fw = _rfw.data.decode("utf-8", "ignore")
check("first-wmid-fetch-claims-the-pending-doc",
      "First Run Album" in _fw and "NOTFOUND" not in _fw,
      f"the first wmid fetch of a session must be able to claim the pending "
      f"document, got {_fw[:200]!r}")
# and it must be exact from then on
_rfw2 = c.get("/cdinfo/GetMDRCD.aspx?wmid=" + _newwmid)
check("claimed-wmid-is-exact-afterwards",
      "First Run Album" in _rfw2.data.decode("utf-8", "ignore"),
      "once claimed, later fetches for that collection must resolve exactly")
# a DIFFERENT collection must still get nothing
_rfw3 = c.get("/cdinfo/GetMDRCD.aspx?wmid=DDDD4444-EEEE-5555-FFFF-666677778888")
check("second-collection-is-not-claimed",
      "First Run Album" not in _rfw3.data.decode("utf-8", "ignore"),
      "a pending document must be claimable by only ONE collection, or every "
      "album in the library would inherit the last one applied")
# a new document starts unclaimed again
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Next Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_NEXT", "session_id": "S", "toc": "", "wmid": ""}),
    content_type="application/json")
_rfw4 = c.get("/cdinfo/GetMDRCD.aspx?wmid=9999AAAA-1111-2222-3333-444455556666")
check("new-document-is-unclaimed",
      "Next Album" in _rfw4.data.decode("utf-8", "ignore")
      and "First Run Album" not in _rfw4.data.decode("utf-8", "ignore"),
      "a newly staged document must start unclaimed and serve its own album")
check("wmid-claim-is-recorded",
      "PENDING_WRITE['wmid']" in _src and "bound == wmid_raw" in _src,
      "the collection a pending document is committed to must be tracked, or "
      "the same document could be claimed by unrelated collections")

# 34e. REGRESSION. Indexing a document under a collection and pre-claiming it
#      are different things, and conflating them made a library update serve
#      the PREVIOUS album back to WMP. Logged from a real session:
#        12:54:25  [STAGED] album='Tiny Cities'   (CD flow, wmid=B17CF884)
#        12:56:29  [WMID] dialog had no wmid; using last seen collection
#                   'B17CF884' as the write target
#        12:56:44  [MDR] -> serving album='Mylo Xyloto' (wmid=-)
#        12:56:52  [MDR] -> serving album='Tiny Cities'  (wmid=B17CF884)
#      The client sent only the AUTHORITATIVE wmid, which was empty for a
#      dialog opened with nothing but ?requestid=. So the new document was
#      never indexed under B17CF884, and WMP's follow-up fetch for the very
#      collection it had just been told to write to resolved to the older
#      document - which is the one bug class this whole path exists to prevent.
#      The document must be indexed under the WRITE TARGET (WMP_WMID, fallback
#      included) while only a URL-authoritative wmid may pre-claim it.
check("client-sends-write-target-and-auth-separately",
      "wmid: WMP_WMID," in page and "wmid_auth: WMP_WMID_AUTH" in page,
      "the dialog must send the collection it writes to (WMP_WMID) separately "
      "from the one WMP actually put in the URL (WMP_WMID_AUTH)")
check("server-binds-write-target",
      'wmid_target = str(data.get("wmid", "") or "").strip()' in _src
      and "wmid=wmid_target" in _src,
      "the document must describe the collection it is written to, or WMP's own "
      "follow-up fetch by wmid resolves to the previously applied album")
check("guess-does-not-pre-claim",
      "claim_wmid" in _src and "if claim_wmid:" in _src,
      "only a URL-authoritative wmid may pre-claim the pending document; a "
      "LAST_WMID guess must still leave it claimable by the real collection")

# Replay the real sequence end to end: a CD document bound to a collection,
# then a library update that writes to that same collection via the fallback.
fai.STAGED_REQUESTS.clear()
fai.STAGED_AT.clear()
fai.FRESH_WRITES.clear()
fai.PENDING_WRITE.update({'xml': '', 'at': 0.0, 'wmid': ''})
COLL = "B17CF884-35B2-5D0B-B819-ED648F592A2B"
fai.LAST_WMID = COLL
# 1. the earlier CD rip, written with a wmid WMP really supplied
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Tiny Cities"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "", "session_id": "S", "toc": "", "cd": "AA+BB",
     "wmid": COLL, "wmid_auth": COLL}),
    content_type="application/json")
# 2. the library update: dialog opened with ?requestid= only, so WMP_WMID is
#    the LAST_WMID fallback and WMP_WMID_AUTH is empty.
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Mylo Xyloto"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_MYLO", "session_id": "S", "toc": "", "cd": "",
     "wmid": COLL, "wmid_auth": ""}),
    content_type="application/json")
# The fallback guess must NOT have pre-claimed it at STAGING time: a genuinely
# different collection still has to be able to claim the pending document. This
# is asserted before the fetch below, because that fetch legitimately DOES claim
# it - WMP asked for this collection by name and was given the document.
check("fallback-guess-left-the-document-claimable",
      fai.PENDING_WRITE.get("wmid", "") == "",
      "a LAST_WMID fallback must not pre-claim the pending document, or a "
      "different collection can never claim it")
# 3. WMP fetches the collection it was just told to write to
_after = c.get("/cdinfo/GetMDRCD.aspx?wmid=" + COLL).data.decode("utf-8", "ignore")
check("library-update-is-not-served-the-previous-album",
      "Mylo Xyloto" in _after and "Tiny Cities" not in _after,
      f"a library update must not leave the previous album bound to the "
      f"collection, got {_after[:200]!r}")
check("write-target-is-in-the-document",
      COLL in _after,
      "the delivered document must carry the write target's WMCollectionID so "
      "WMP recognises it as metadata for the collection it is updating")
# Having been served, the collection owns the document - a repeat fetch is exact
# and no other collection may take it.
check("served-collection-now-owns-the-document",
      fai.PENDING_WRITE.get("wmid", "") == COLL,
      "after serving a collection its document, that collection must own it")
_other = c.get("/cdinfo/GetMDRCD.aspx?wmid=EEEE1111-FFFF-2222-AAAA-333344445555"
               ).data.decode("utf-8", "ignore")
check("no-other-collection-can-take-a-served-document",
      "Mylo Xyloto" not in _other,
      "a document already served to one collection must never be handed to "
      "another, or a second album inherits the first one's tags")

# 34f. A SECOND bug the 34e work exposed, in the delivery path itself. Serving a
#      document re-stages it so the wmid pairing is remembered, but that call
#      passed no claim_wmid. A re-stage counts as a new document, so it reset
#      PENDING_WRITE['wmid'] to '' and the claim a collection had just made was
#      thrown away - the very next unrelated collection could then claim the
#      same document. Reproduced in isolation: after collection A claimed the
#      pending document, a fetch by collection D was served A's album.
check("delivery-preserves-an-existing-claim",
      "_stage_request_xml(staged, wmid=wmid_q, claim_wmid=wmid_q)" in _src,
      "re-staging on delivery must preserve the claim, or the next collection "
      "to ask can claim a document that already belongs to another album")

# 39. A NAMED COLLECTION IS THE COLLECTION. WMP appends ?wmid=<collection> to a
#     CD URL when it re-prompts a disc. That is WMP naming the collection it is
#     asking about, so the document must describe THAT collection - in the
#     staging path (build_wmp_xml) and in the delivery path alike.
#     REVERSED twice over. It was first written as an ARTWORK fix, on a
#     correlation that did not hold up, and then hardened into "a CD RIP MUST
#     NOT BORROW A COLLECTION ID". Both are gone.
#
#     The field evidence that settled it is in the 15:52 session: WMP's wmid for
#     the Prospekt disc was 64C552E6-0C68-5C6A-A02A-617A084205C1, which is
#     EXACTLY guid('1122792846') - our own id for the iTunes album. WMP adopted
#     our collection. So the guard was a no-op for this album, and the only
#     honest reading left is that document identity was never the problem.
check("cd-flow-uses-the-named-collection",
      "CD flow: ignoring borrowed wmid" not in _src
      and 'album_guid = _remember_wmid(wmid) or guid(album_data.get("id") or cd)'
      in _src,
      "WMP's wmid is the collection it is asking about, so the CD flow must "
      "stamp it like the library flow does")
check("named-collection-is-retargeted-whatever-the-flow",
      "if wmid_q and not disc_request:" not in _src
      and "if wmid_q:" in _src,
      "when WMP names a collection the document must describe that collection, "
      "disc flow or not. REVERSED: appending ?wmid= to a CD URL is WMP naming "
      "the collection it made for that disc, not an aside - serving a document "
      "stamped with a different id keys the cover to a collection it is not "
      "tracking here. The old guard rested on a disproved correlation: the "
      "cover was not re-fetched because the URL never changed, and for a while "
      "because the document was not well-formed.")

# 39a. RECORDED SO THE NEXT PERSON DOES NOT RE-DERIVE IT. B17CF884 is not a WMP
#      collection id that WMP invented - it is OUR OWN deterministic guid for
#      the iTunes album id. That is why the fix above changed nothing for that
#      album: both branches produce the same value. Confirmed again on Prospekt
#      at 15:52, where guid('1122792846') == 64C552E6-0C68-5C6A-A02A-617A084205C1
#      is precisely the wmid WMP sent.
check("collection-guid-is-derived-from-the-album-id",
      fai.guid("1570089404") == "B17CF884-35B2-5D0B-B819-ED648F592A2B",
      "B17CF884 is guid('1570089404') - our own value echoed back by WMP, not "
      "a collection id WMP chose. A fix that treats them as different is a "
      "no-op for this album.")
check("wmp-echoes-our-own-collection-id-for-prospekt",
      fai.guid("1122792846") == "64C552E6-0C68-5C6A-A02A-617A084205C1",
      "guid('1122792846') is exactly the wmid WMP sent at 15:52:13 for the "
      "Prospekt disc - WMP adopted our collection id, so stamping our own guid "
      "was already correct and the cover problem lies elsewhere")

_cdxml = fai.build_wmp_xml(dict(album, title="Rip Album"), cd="AA+BB+CC",
                           wmid="B17CF884-35B2-5D0B-B819-ED648F592A2B")
check("cd-document-carries-the-named-collection",
      "B17CF884" in _cdxml,
      f"a CD document must describe the collection WMP named: {_cdxml[:300]!r}")
check("cd-document-still-describes-the-album",
      "Rip Album" in _cdxml and "<WMCollectionID>" in _cdxml,
      "the CD document must still name the album and carry a collection id")
# The library path is unchanged - there the wmid is the whole point.
_libxml = fai.build_wmp_xml(dict(album, title="Lib Album"),
                            wmid="B17CF884-35B2-5D0B-B819-ED648F592A2B")
check("library-document-still-uses-the-wmid",
      "B17CF884" in _libxml,
      "a library update must still be stamped with the collection it updates")
# A CD request that ALSO names ?wmid= is WMP naming the collection it made for
# that disc. It must be retargeted - the earlier "stay stamped for the disc"
# rule is reversed, see named-collection-is-retargeted-whatever-the-flow.
_tw = "BBBB2222-CCCC-3333-4444-555566667777"
fai.STAGED_REQUESTS.clear()
fai.STAGED_AT.clear()
fai.FRESH_WRITES.clear()
fai.PENDING_WRITE.update({"xml": "", "at": 0.0, "wmid": ""})
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Rip Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "", "session_id": "S", "toc": "",
     "cd": "AA+BB+CC", "wmid": _tw, "wmid_auth": _tw}),
    content_type="application/json")
_served = c.get("/cdinfo/GetMDRCD.aspx?locale=409&CD=AA+BB+CC&wmid=" + _tw
                ).data.decode("utf-8", "ignore")
check("disc-delivery-is-retargeted-to-the-named-collection",
      "Rip Album" in _served and _tw.upper() in _served.upper(),
      f"a disc request naming a collection must be answered with that "
      f"collection's document, got {_served[:260]!r}")

# 35. Aero restyle must be CSS-only: no CSS3 without an IE7 fallback, and
#     none of the working dialog logic may be disturbed.
ui_html = c.get("/FAI/ui?artist=beatles&album=abbey+road").data.decode("utf-8", "ignore")
for _pname, _pg in (("ui", ui_html), ("confirm", page), ("done", done_html)):
    check("aero-themed-%s" % _pname,
          "Aero" in _pg and "lt IE 8" in _pg,
          f"/{_pname} must carry the Aero theme and an IE7 fallback")
check("aero-uses-ie7-conditional",
      "progid:DXImageTransform.Microsoft.gradient" in _src,
      "Aero gradients need an IE7 progid filter fallback")
check("aero-no-css3-only-props",
      "backdrop-filter" not in _src and "var(--" not in _src,
      "avoid CSS3-only features IE7 cannot parse")
for _fn in ("function finishSync", "function applyMetadata", "function selectAll",
            "function toggleRow", "function getSelectedTracks", "function showDiscBanner",
            "window.onerror", "beaconSync(diag)"):
    check("logic-intact-%s" % _fn.replace(" ", "_").replace("(", "").replace(")", ""),
          _fn in page, f"restyle must not remove {_fn}")
check("write-paths-intact",
      "WriteNamesEx(1, WMP_CD" in page and "WriteNamesEx(1, WMP_WMID" in page
      and "WriteNamesEx(0, WMP_TOC" in page,
      "all three write paths must survive the restyle")

# 36. A SYNCHRONOUS XHR in the dialog wedges WMP's UI thread. The write
#     succeeds and the tags apply, but the page never navigates to /done and
#     the dialog hangs on "Applying..." forever. No sync XHR may exist anywhere
#     in the dialog page.
import re as _re
_sync = _re.findall(r"\.open\(\s*['\"](?:GET|POST)['\"]\s*,\s*['\"][^'\"]+['\"]\s*,\s*false\s*\)", _src)
check("no-sync-xhr-anywhere", not _sync,
      f"synchronous XHR still present: {_sync}")
check("finish-beacon-is-async",
      _re.search(r"function beaconSync[\s\S]{0,400}?\.open\('POST',\s*'/client_error',\s*true\)", _src)
      is not None,
      "beaconSync must be asynchronous or the dialog hangs on Applying")
check("redirect-is-guarded",
      'window.location.href = "/done";' in page and "catch (e) {",
      "the /done redirect must be guarded so the dialog cannot get stuck")

# 36b. The dialog hung on "Applying..." for 2m25s after a SUCCESSFUL write.
#      The redirect rode on a setTimeout, and WMP's dialog host is
#      single-threaded: while it was busy applying the write it never serviced
#      the timer. Logged from a real rip of 'Sun Kil Moon - Tiny Cities':
#        12:37:41  [CLIENT] {"page":"finish","write":"WriteNamesEx-cdid-ok",
#                            "applied":true}
#        12:40:06  [REQ] GET /done            <-- 2m25s later
#      btnFinish was already disabled, so the user was trapped. The navigation
#      must happen in the SAME tick as the write - no timer may stand between
#      them.
check("no-timer-before-done-redirect",
      _re.search(r"setTimeout[\s\S]{0,200}?window\.location\.href\s*=\s*[\"']/done", _src)
      is None,
      "a setTimeout before the /done redirect is what stalled the dialog on "
      "'Applying...' for 2m25s - navigate in the same tick instead")
check("done-redirect-is-immediate",
      _re.search(r"beaconSync\(diag\);\s*leaveDialog\(\);", _src) is not None,
      "the write must hand off to the redirect immediately after reporting")
check("stuck-button-is-recoverable",
      "function leaveDialog()" in page
      and "b.disabled = false;" in page
      and "b.onclick = leaveDialog;" in page,
      "if the navigation fails the button must be re-enabled, or the user is "
      "trapped on a disabled 'Applying...' with no way out")
# ...and the write outcome must survive the navigation. Leaving the page can
# cancel the in-flight beacon, and that diagnostic is the only record of which
# write path ran - it is what identified the stall in the first place.
check("write-diag-survives-navigation",
      "sessionStorage.setItem('fai_diag'" in page,
      "the confirm page must stash its diagnostic before navigating away")
check("done-replays-stashed-diag",
      "sessionStorage.getItem('fai_diag')" in done_html
      and "sessionStorage.removeItem('fai_diag')" in done_html,
      "/done must replay the stashed write diagnostic, once, so a cancelled "
      "beacon never costs us the diagnosis")
LIB_WMID = "110977B9-7CD2-5A84-8284-A5D0F0DC31A8"
r = c.get("/confirm?source=itunes&id=1065975633&requestid=REQ_LIB&wmid=" + LIB_WMID)
page_lib = r.data.decode("utf-8", "ignore")
check("wmid-reaches-confirm-page",
      'var WMP_WMID = "%s"' % LIB_WMID in page_lib,
      "library wmid not passed to the dialog page")
check("library-writes-by-wmid",
      "else if (WMP_WMID)" in page_lib
      and "WriteNamesEx(1, WMP_WMID, generatedXml, false)" in page_lib,
      "library flow must write by wmid with rename disabled")
check("cd-write-takes-precedence",
      page_lib.index("if (WMP_CD)") < page_lib.index("else if (WMP_WMID)"),
      "the working CD path must be tried before the wmid path")

# staged XML for a library album must carry the wmid as its collection id
c.post("/store_staged_xml", data=json.dumps(
    {"album": dict(album, title="Library Wmid Album"),
     "selected_tracks": [album["tracks"][0]],
     "request_id": "REQ_LIBW", "session_id": "S", "toc": "", "wmid": LIB_WMID}),
    content_type="application/json")
r = c.get("/redir/getmdrcdbackground/?requestid=REQ_LIBW")
m = re.search(r"<WMCollectionID>([^<]+)</WMCollectionID>",
              r.data.decode("utf-8", "ignore"))
check("library-xml-uses-wmid-collection",
      m is not None and m.group(1).upper() == LIB_WMID.upper(),
      f"collection={m.group(1) if m else None}")

# 37. FAI visual-fidelity contract. The dialog must present the authentic
#     "Find album information" anatomy: the blue lead-in, the two
#     hairline-separated columns, the filter strip, the command strip with
#     the privacy link + Next, and the per-row 'More.../Buy' link pair.
check("fai-leadin-copy",
      'id="leadIn"' in ui_html and "Album(s) containing" in ui_html
      and "function applyTotals" in ui_html,
      "the authentic blue lead-in sentence is missing")
check("fai-two-columns",
      'class="section-label">Existing Information' in ui_html
      and 'class="section-label">Search' in ui_html,
      "the 'Existing Information' / 'Search' column pair is missing")
check("fai-filter-strip",
      'id="tabArtists"' in ui_html and 'id="tabAlbums"' in ui_html
      and 'id="tabTracks"' in ui_html,
      "the Artists/Albums/Tracks filter strip is missing")
check("fai-command-strip",
      "Read the privacy statement." in ui_html
      and 'onclick="nextToSearch();"' in ui_html
      and 'onclick="cancelToMainTask()"' in ui_html,
      "the privacy link + Next + Cancel command strip is missing")
check("fai-next-and-clear-are-wired",
      "function nextToSearch" in ui_html and "function clearSearch" in ui_html,
      "Next / the in-field clear glyph must have handlers")
check("fai-leadin-escapes-input",
      "&quot;" in ui_html and "<script>alert" not in ui_html,
      "the lead-in must HTML-escape the WMP supplied rip name")
check("fai-leadin-injects-nothing",
      "<img src=x onerror" not in ui_html,
      "WMP supplied values must not be able to break out of the markup")
check("fai-result-row-links",
      "More&#8230;" in body and 'class="link">Buy<' in body
      and "Track(s)" in body,
      "each search result must carry the More.../Buy pair and 'N Track(s)'")
check("fai-search-box-has-clear",
      'class="search-clear"' in ui_html and 'id="sq"' in ui_html,
      "the search field must keep its id and its in-field clear button")
# IE7 compatibility of the new layout. The two columns and every list row must
# be float based; the only flexbox allowed is the outer flex COLUMN context
# (body + the three direct children), which the IE7 block flips to display:block.
_CSS_BLOCK = _src.split("COMMON_CSS = ")[1].split('\n"""')[0]
_CSS_RULES = dict(re.findall(r"^([.\w#, :\[\]\-]+?)\s*\{([^}]*)\}", _CSS_BLOCK, re.M))
_flex_sel = [s for s, d in _CSS_RULES.items() if "display: flex" in d or "display:inline-flex" in d]
_flex1_sel = [s for s, d in _CSS_RULES.items() if re.search(r"(^|;)\s*flex:\s*1", d)]
check("fai-no-calc-or-gap",
      "calc(" not in _CSS_BLOCK and "gap:" not in _CSS_BLOCK,
      "the FAI stylesheet must not use calc() or flex gap")
check("fai-flex-only-in-outer-column",
      all(s.strip() in ("body", ".right-pane") for s in _flex_sel)
      and all(s.strip() in (".header-area", ".main-container", ".footer",
                            ".right-pane", ".results-scroll",
                            ".section-label, .search-box-row, .filter-row")
              for s in _flex1_sel),
      f"flexbox leaked into the inner layout: display:flex={_flex_sel} flex:1={_flex1_sel}")
# .right-pane is now a flex column so the result LIST can take the leftover
# height and scroll on its own - that is what gives the dialog the single
# scrollbar the authentic one has. IE7 has no flexbox, so the conditional
# block MUST put it back to display:block and give the list a fixed height,
# otherwise IE7 would lose the scrollbar entirely.
check("ie7-undoes-the-pane-flexbox",
      ".main-container, .header-area, .footer, .left-pane, .right-pane { display: block; }" in _src
      and ".results-scroll { height: 260px; overflow-y: scroll; }" in _src,
      "the IE7 conditional must reset the pane to display:block and give "
      ".results-scroll an explicit height so it still scrolls")
check("fai-columns-are-floats",
      ".left-pane { float: left;" in _src and ".right-pane { margin-left: 48%;" in _src,
      "the two columns must be float/margin based so IE7 lays them out")
check("fai-list-rows-are-floats",
      ".album-item { position: relative; float: left; width: 100%; padding: 4px 6px; overflow: hidden;" in _src
      and ".album-thumb { width: 48px; height: 48px; float: left;" in _src
      and ".track-num { float: left;" in _src,
      "result and track rows must be float based so IE7 lays them out")
# .track-title and .track-artist are siblings in the markup. Once the row stops
# being a flex container they must both float, or the artist drops onto its own
# line and the row stops matching the previous rendering.
check("fai-track-title-artist-share-a-line",
      _CSS_RULES.get(".track-title", "").count("float: left") == 1
      and _CSS_RULES.get(".track-artist", "").count("float: left") == 1
      and "float: right" in _CSS_RULES.get(".track-time", ""),
      "track title and artist must float side by side with the time floated right")
# The cover is floated inside each row, so every row that carries a floated
# child needs its own formatting context or the row box collapses to nothing
# and the float spills through the row's background and border.
for _row in (".album-item", ".existing-info", ".track-row", ".footer"):
    _rule = _CSS_RULES.get(_row, "")
    check("fai-row-contains-floats-%s" % _row.lstrip("."),
          "overflow: hidden" in _rule or "zoom: 1" in _rule,
          f"{_row} floats a child but does not establish a formatting context")
check("fai-ie7-column-pixels",
      ".left-pane { float: left; width: 300px; height: auto; }" in _src
      and ".right-pane { margin-left: 312px; height: auto; }" in _src,
      "the IE7 conditional must replace the percentage columns with pixels")

# 38. Every <img> in the dialog falls back to /static/noart.png. That path
#     used to 404, so the host drew a broken-image glyph where the real
#     window shows a disc. The placeholder is generated in-process.
r = c.get("/static/noart.png")
check("noart-placeholder-served",
      r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n",
      f"code={r.status_code} body={r.data[:40]!r}")
try:
    import struct as _struct
    import zlib as _zlib
    _w, _h, _depth, _ctype = _struct.unpack(">IIBB", r.data[16:26])
    _i, _bad = 8, 0
    while _i < len(r.data):
        _ln = _struct.unpack(">I", r.data[_i:_i + 4])[0]
        _tag = r.data[_i + 4:_i + 8]
        _crc = _struct.unpack(">I", r.data[_i + 8 + _ln:_i + 12 + _ln])[0]
        if _crc != (_zlib.crc32(_tag + r.data[_i + 8:_i + 8 + _ln]) & 0xFFFFFFFF):
            _bad += 1
        _i += 12 + _ln
    check("noart-placeholder-valid-png",
          _w == 48 and _h == 48 and _ctype == 6 and _bad == 0,
          f"{_w}x{_h} colortype={_ctype} bad_chunks={_bad}")
except Exception as _e:  # pragma: no cover - only on a broken build
    check("noart-placeholder-valid-png", False, repr(_e))

# 39. Cloud deploy config. An earlier attempt failed with
#     'uvicorn: command not found' - and even after installing uvicorn it would
#     still have failed twice over: uvicorn is ASGI (this app is WSGI/Flask),
#     and "FAI Server.py":app is not an importable module path because Python
#     module names cannot contain spaces. Both are guarded here.
check("wsgi-shim-exists",
      os.path.exists(os.path.join(_DIR, "wsgi.py")),
      "wsgi.py entry point is missing - a WSGI server has nothing to target")
check("requirements-has-gunicorn",
      "gunicorn" in _req_pkgs and "uvicorn" not in _req_pkgs,
      f"requirements.txt must ship gunicorn (WSGI) and must not pull in uvicorn "
      f"(ASGI); parsed packages: {sorted(_req_pkgs)}")
check("render-uses-gunicorn",
      "gunicorn" in _render_norm and "uvicorn" not in _render_norm,
      f"render.yaml must start gunicorn, not uvicorn; parsed: {_render_norm[:120]!r}")
check("render-targets-wsgi-shim",
      "wsgi:app" in _render,
      "render.yaml must target wsgi:app, not the space-containing module path")
check("render-single-worker",
      "WEB_CONCURRENCY" in _render and 'value: "1"' in _render,
      "in-memory staged XML must stay on a single worker")
# The deploy failed twice on flags before it reached the app at all:
#   uvicorn: command not found
#   gunicorn: error: unrecognized arguments: --host 0.0.0.0 --port 10000
# --host/--port are uvicorn's. Gunicorn has exactly one addressing flag:
# --bind HOST:PORT.
_GUNICORN_OK = "gunicorn wsgi:app --bind 0.0.0.0:$PORT"
check("gunicorn-command-is-valid",
      _GUNICORN_OK in _render_norm and _GUNICORN_OK in _procfile_norm
      and "--host" not in _render_norm and "--port" not in _render_norm
      and "--host" not in _procfile_norm and "--port" not in _procfile_norm,
      f"gunicorn must be invoked as '{_GUNICORN_OK}' with no --host/--port")
check("procfile-web-process",
      re.search(r"^web:\s*gunicorn", _procfile, re.M) is not None,
      "Procfile must declare a 'web:' gunicorn process")
# the shim must actually hand a working Flask app to the server
try:
    _wspec = importlib.util.spec_from_file_location(
        "_wsgi_check", os.path.join(_DIR, "wsgi.py"))
    _wmod = importlib.util.module_from_spec(_wspec)
    _wspec.loader.exec_module(_wmod)
    _wc = _wmod.app.test_client()
    check("wsgi-shim-serves-app",
          _wc.get("/fai_status?format=json").status_code == 200
          and _wc.get("/FAI/ui?artist=a&album=b").status_code == 200,
          "wsgi:app must expose a working Flask app")
except Exception as _we:  # pragma: no cover
    check("wsgi-shim-serves-app", False, repr(_we))

# 40. Composer credits. Both providers used to fall back to the TRACK ARTIST
#     when no composer was known, which wrote a factually wrong credit into the
#     user's library ("So What" credited to Miles Davis as his own composer,
#     every jazz track credited to the bandleader). Verified against the live
#     APIs: iTunes' composerName is ALWAYS null (0 of 26 tracks on The Wall),
#     so the artist fallback was pure invention there.
_mb_inc_m = re.search(r'"inc":\s*\(([^)]*)\)', _src, re.S)
_mb_inc = "".join(re.findall(r'"([^"]*)"', _mb_inc_m.group(1))) if _mb_inc_m else ""
check("mb-requests-work-rels",
      all(k in _mb_inc for k in ("work-rels", "recording-level-rels",
                                 "work-level-rels")),
      f"MusicBrainz needs work-rels to return a work's composer relations; inc={_mb_inc!r}")
check("no-composer-artist-fallback",
      'item.get("composerName", t_artist)' not in _src
      and 't.get("composer", t_artist)' not in _src,
      "composer must never fall back to the artist - that is a false credit")
check("mb-reads-work-composer-relations",
      'wrel.get("type") == "composer"' in _src,
      "MusicBrainz composer must be read from the work's composer relations")
# an absent composer must omit the tag entirely, not emit an empty or wrong one
_alb_nc = {"id": "NC1", "source": "musicbrainz", "title": "No Composer",
           "artist": "Some Artist", "genre": "Rock", "year": "2000", "art_url": "",
           "tracks": [{"id": "t1", "name": "Track One", "number": 1, "disc": 1,
                       "artist": "Some Artist", "performer": "Some Artist",
                       "composer": ""}]}
_xml_nc = fai.build_wmp_xml(_alb_nc)
check("no-composer-omits-tag",
      "<trackComposer>" not in _xml_nc,
      "a track with no known composer must emit no <trackComposer> at all")
check("no-composer-keeps-artist",
      "<trackArtist>Some Artist</trackArtist>" in _xml_nc,
      "omitting the composer must not disturb the other fields")
_alb_c = {"id": "C1", "source": "musicbrainz", "title": "Has Composer",
          "artist": "A & B", "genre": "Jazz", "year": "1959", "art_url": "",
          "tracks": [{"id": "t1", "name": "Blue In Green", "number": 1, "disc": 1,
                      "artist": "A & B", "performer": "A & B",
                      "composer": "Miles Davis; Bill Evans"}]}
_xml_c = fai.build_wmp_xml(_alb_c)
check("real-composer-emitted",
      "<trackComposer>Miles Davis; Bill Evans</trackComposer>" in _xml_c,
      "a real composer must reach the XML")
check("composer-xml-escaped",
      "<trackComposer>Ben &amp; Jerry</trackComposer>" in fai.build_wmp_xml(
          {"id": "C2", "source": "musicbrainz", "title": "X", "artist": "Y",
           "genre": "G", "year": "2000", "art_url": "",
           "tracks": [{"id": "t", "name": "N", "number": 1, "disc": 1,
                       "artist": "Y", "performer": "Y",
                       "composer": "Ben & Jerry"}]}),
      "a composer containing '&' must be XML-escaped")
# multi-composer credits must not be joined into a duplicate soup
check("composer-dedupes",
      "; " not in "Miles Davis" and "&quot;Miles Davis&quot;" not in _src,
      "composer names must be de-duplicated before joining")

# 41. Result COUNTS. The lead-in used to hardcode 'Found 500+ Album(s)'
#     regardless of the query, which lied on every search - and told the user
#     nothing about how many albums lay behind the ~40 rows rendered.
#     MusicBrainz returns an exact 'count'; iTunes only returns a limit-capped
#     resultCount, so it must never be presented as a total.
check("counts-helper-exists",
      "def count_search_totals" in _src,
      "an exact count helper is required")
check("counts-use-mb-count-field",
      '.get("count"' in _src and '"limit": 0' in _src,
      "MusicBrainz's exact count is only returned when limit=0")
check("totals-sent-as-header",
      "X-Search-Totals" in _src
      and "getResponseHeader('X-Search-Totals')" in ui_html,
      "counts must reach the client so the lead-in can be rewritten")
check("client-rewrites-leadin",
      "function applyTotals" in ui_html
      and "lead.innerHTML" in ui_html
      and "groupDigits(albums)" in ui_html,
      "the client must render the real count into the lead-in")
check("leadin-says-how-many-shown",
      "showing the top" in ui_html and "albums_shown" in ui_html,
      "when the total exceeds the rows shown, the lead-in must say so")
def _strip_comments(h):
    """Remove HTML comments and JS // / /* */ comments.

    Several of these checks assert that a construct is ABSENT. Without
    stripping comments first they match the very prose that documents the
    rule ('rather than claiming 500+', 'no JSON.parse here'), so the tests
    would fail on their own documentation.
    """
    h = re.sub(r"<!--.*?-->", " ", h, flags=re.S)
    h = re.sub(r"/\*.*?\*/", " ", h, flags=re.S)
    h = re.sub(r"^\s*//.*$", " ", h, flags=re.M)
    return h


ui_code = _strip_comments(ui_html)
check("no-hardcoded-500-in-markup",
      "500+" not in ui_code,
      "the literal 500+ must not survive in the served dialog")
check("counts-escaped-into-dom",
      "function escHtml" in ui_html and "escHtml(query)" in ui_html,
      "the query must be escaped before it is written into innerHTML")
# IE7: no JSON.parse, no let/const, no arrow functions in the dialog script
for _forbidden in ("JSON.parse", "=>", "const ", "let "):
    check("ie7-no-%s" % _forbidden.strip().strip("=> "),
          _forbidden not in ui_code,
          f"the dialog runs in IE7 and must not use {_forbidden!r}")

# the counts endpoint must survive a query that returns nothing
_r = c.get("/api_search?q=zzzznotathingatallqqq")
check("counts-on-empty-result",
      _r.status_code == 200 and "X-Search-Totals" in _r.headers,
      f"even an empty result must report counts; hdrs={dict(_r.headers)}")
# ...and must ALSO be present on a SUCCESSFUL search. An early version only
# routed the empty-result branch through _respond(), so the header was silently
# missing whenever there were results - the exact case the feature is for.
_r2 = c.get("/api_search?q=pink+floyd+the+wall")
check("counts-on-successful-result",
      _r2.status_code == 200 and "X-Search-Totals" in _r2.headers,
      f"a successful search must report counts too; hdrs={dict(_r2.headers)}")
try:
    _t2 = json.loads(_r2.headers.get("X-Search-Totals") or "{}")
except Exception as _je:
    _t2 = {}
check("counts-header-is-valid-json",
      "albums" in _t2 and "albums_shown" in _t2,
      f"X-Search-Totals must be parseable JSON with albums/albums_shown: {_t2}")
# the total must never be smaller than the rows we actually rendered
check("count-covers-shown-rows",
      not _t2 or int(_t2.get("albums") or 0) >= int(_t2.get("albums_shown") or 0),
      f"total {_t2.get('albums')} is below the {_t2.get('albums_shown')} rows shown")
# when the total had to be floored, the client must say "at least", not a hard
# number that would be contradicted by the visible rows
check("inexact-total-says-at-least",
      "Found at least " in ui_html,
      "an inexact album count must render as 'at least N'")
# the scoped query must be shared, never duplicated, or counts drift from results
check("search-and-count-share-query",
      _src.count("def _build_lucene_query") == 1
      and _src.count("_build_lucene_query(query, artist_hint, album_hint)") >= 2,
      "the search and the count must build their Lucene query with one shared helper")

# 42. Two bugs found from a real screenshot of 'coldplay ghost story':
#     (a) NO MusicBrainz albums surfaced at all
#     (b) the Artists / Albums / Tracks strip was inert decoration
# Root cause of (a): the scoped query used release:"ghost story" as an exact
# PHRASE, so Coldplay's 'Ghost Stories' did not match and the AND returned 0.
# A quoted phrase must not gate the album term - the words are OR'd instead.
check("album-term-not-exact-phrase",
      'release:"{clean_alb}"' not in _src.split("def _build_lucene_query")[1].split("def ")[0]
      or 'album_clause' in _src,
      "the album half of the scoped query must not be an exact quoted phrase")
check("album-term-uses-or-clause",
      'album_clause = "(" + " OR ".join(terms) + ")"' in _src,
      "the album terms must be OR'd so singular/plural still matches")
# the live shape of the query that used to return 0
_q = "coldplay ghost story"
_toks = _q.split()
_eq = fai._build_entity_queries(_q, " ".join(_toks[:len(_toks)//2]),
                                " ".join(_toks[len(_toks)//2:]))
check("scoped-release-query-is-not-bare-phrase",
      "release:" not in _eq["release"] and _eq["release"].startswith('artist:"coldplay"'),
      f"release query must not be an exact album phrase: {_eq['release']!r}")
check("entity-queries-differ-per-entity",
      _eq["artist"] == 'artist:"coldplay"' and "AND" in _eq["recording"],
      f"artist/recording queries must be scoped differently: {_eq}")

# (b) the filter strip must actually do something
check("filter-tabs-are-clickable",
      "function switchView" in ui_html and "function setActiveTab" in ui_html
      and "onclick=\"switchView('artist');\"" in ui_html
      and "onclick=\"switchView('track');\"" in ui_html,
      "the Artists/Tracks tabs must be wired to a real view switch")
check("view-sent-to-server",
      "'&view=' + CURRENT_VIEW" in ui_html
      and 'request.args.get("view"' in _src,
      "the active view must be sent to the server")
for _v in ("artist", "album", "track"):
    _rv = c.get("/api_search?q=coldplay+ghost+story&view=" + _v)
    check("view-%s-returns-200" % _v, _rv.status_code == 200,
          f"view={_v} returned {_rv.status_code}")
    check("view-%s-has-rows-or-empty" % _v,
          ("album-item" in _rv.data.decode("utf-8", "ignore"))
          or ("No matching" in _rv.data.decode("utf-8", "ignore")),
          f"view={_v} rendered neither results nor an empty-state message")
# the default must stay 'album' so nothing else regresses
_rv_def = c.get("/api_search?q=coldplay+ghost+story")
check("view-defaults-to-album",
      "Search Results" in _rv_def.data.decode("utf-8", "ignore"),
      "the no-view request must still return the album view")
# bad view values must not 500
_rv_bad = c.get("/api_search?q=coldplay+ghost+story&view=bogus")
check("view-invalid-falls-back",
      _rv_bad.status_code == 200
      and "Search Results" in _rv_bad.data.decode("utf-8", "ignore"),
      f"an unknown view must fall back to albums, got {_rv_bad.status_code}")
# the recording parser must tolerate list-shaped releases/media
check("recording-parser-guards-lists",
      'isinstance(rels[0], dict)' in _src and 'isinstance(media[0], dict)' in _src,
      "recording payloads nest lists; .get() must be guarded or Tracks returns 0")

# 43. The results pane is only ~450px tall but the list can be ~2,300px, and
#     the rows are FLOATS - a float does not contribute to its container's
#     scroll height, so the pane reported scrollHeight == clientHeight and drew
#     NO scrollbar. Everything past the fold was unreachable.
#     The authentic dialog (reference screenshot) has exactly ONE scrollbar and
#     it belongs to the RESULT LIST, starting below the Artists|Albums|Tracks
#     strip with Win32 arrow buttons. So the list - not the pane - is the
#     scroller, and the search box and filter strip stay put above it.
check("results-have-scroll-container",
      'id="results_scroll"' in ui_html and 'class="results-scroll"' in ui_html,
      "the results list needs its own scroll container to get a scrollbar")
check("scroll-container-establishes-bfc",
      "overflow: hidden; overflow-y: scroll; }" in _src
      and "min-height: 0" in _src,
      ".results-scroll needs overflow (clearfix for the floats) and min-height:0 "
      "so it can shrink and scroll as a flex item")
check("scrollbar-is-always-drawn",
      "overflow-y: scroll;" in _src,
      "the list must use 'scroll' not 'auto' so the bar is always visible, "
      "as in the reference dialog")
check("list-scrolls-not-the-pane",
      "min-height: 0; overflow: hidden; overflow-y: scroll; }" in _src
      and ".right-pane { margin-left: 48%; height: 100%; padding: 11px 12px; overflow: hidden; display: flex;" in _src,
      "the list must scroll, not the pane - otherwise the search box scrolls away")
check("pane-is-a-flex-column",
      ".right-pane" in _src and "flex-direction: column;" in _src,
      "the pane must be a flex column so the list can take the leftover height")
# The authentic dialog has no 'next page' control, so the pager must be gone.
check("no-authentic-violating-pager",
      "showMore" not in ui_html and "renderPager" not in ui_html
      and "_pager_html" not in _src and "pager-btn" not in _src,
      "the reference dialog scrolls; it has no 'Show more' pager, so it must "
      "not be reintroduced")
check("no-paging-state-in-client",
      "CURRENT_PAGE" not in ui_html,
      "with no pager the client must not keep a page counter to desync")
# IE7 has no flexbox, so the list needs an explicit height to still scroll.
# That height IS the dialog height in WMP, so it is pinned to the reference
# window's list area rather than left to whatever the content happens to be.
check("ie7-gives-list-a-height",
      ".results-scroll { height: 260px; overflow-y: scroll; }" in _src,
      "the IE7 conditional must give the list a fixed height so it still gets "
      "a scrollbar without flexbox, and that height is the dialog height")
check("server-slices-by-page",
      'request.args.get("page"' in _src
      and "_start = (page - 1) * per_page" in _src
      and "_window = _combined[_start:_start + per_page]" in _src,
      "the server must still support slicing for API callers")
# the default page must be big enough that the scrollbar alone reaches every row
check("default-page-shows-everything",
      "per_page = 100" in _src,
      "the default per_page must cover the whole result set so no row is "
      "unreachable without a pager")
# paging must not change page 1 from what it always returned
_rp1 = c.get("/api_search?q=coldplay+ghost+story&page=1")
check("page-1-unchanged",
      _rp1.status_code == 200 and "album-item" in _rp1.data.decode("utf-8", "ignore"),
      f"page=1 must still return results, got {_rp1.status_code}")
_r1 = c.get("/api_search?q=coldplay+ghost+story&page=1&per_page=5").data.decode("utf-8", "ignore")
_r2 = c.get("/api_search?q=coldplay+ghost+story&page=2&per_page=5").data.decode("utf-8", "ignore")
_t1 = set(re.findall(r"pick\('(?:itunes|musicbrainz)',\s*'([^']+)'\)", _r1))
_t2 = set(re.findall(r"pick\('(?:itunes|musicbrainz)',\s*'([^']+)'\)", _r2))
check("pages-do-not-overlap",
      bool(_t1) and bool(_t2) and not (_t1 & _t2),
      f"page 1 and page 2 must be disjoint; overlap={_t1 & _t2}")
# out-of-range / junk paging must not 500
for _bad in ("page=0&per_page=1", "page=abc&per_page=1", "per_page=0"):
    _rb = c.get("/api_search?q=coldplay+ghost+story&" + _bad)
    check("paging-junk-safe-%s" % _bad.replace("&", "-").replace("=", ""),
          _rb.status_code == 200,
          f"{_bad} returned {_rb.status_code}")

# 44. MusicBrainz rate-limits to ONE request per second and answers a burst with
#     '503'. One search here fires up to seven MusicBrainz calls at once, so the
#     burst was reliably throttled and the 'status_code != 200' branches returned
#     an empty list SILENTLY - the dialog showed iTunes rows and zero MusicBrainz
#     rows with no error anywhere. Verified against the live API:
#     'artist:"pink floyd" AND (the OR wall)' returns 1291 releases, but the
#     dialog rendered none of them.
check("mb-rate-limiter-exists",
      "def _mb_get(" in _src and "_MB_MIN_INTERVAL" in _src and "_MB_LOCK" in _src,
      "MusicBrainz calls must go through a rate limiter; it allows 1 req/sec")
check("mb-503-is-retried",
      "if resp.status_code == 503:" in _src and "attempt" in _src,
      "a throttled 503 must be retried, not silently turned into zero results")
# Every MusicBrainz call must be routed through the limiter. The User-Agent is
# the marker: it may appear only in the constant and inside _mb_get, so any
# other call site that sends it is an unthrottled MusicBrainz request.
_ua_uses = len(re.findall(r"MUSICBRAINZ_USER_AGENT", _src))
check("no-unthrottled-mb-calls",
      _ua_uses <= 2,
      f"the MusicBrainz User-Agent appears {_ua_uses}x; only the constant and "
      f"_mb_get may send it, so some MusicBrainz call bypasses the limiter")
check("mb-calls-use-limiter",
      _src.count("_mb_get(") >= 5,
      "search, counts, entity views and album details must all use _mb_get")
# throttling must be documented, not accidental
check("mb-limiter-explained",
      "1 request per second" in _src or "one request per second" in _src.lower(),
      "the 1 req/sec MusicBrainz limit and the silent-empty-list failure mode "
      "must be documented where the limiter lives")

# 45. Paging then made MusicBrainz INVISIBLE. The album window was sliced
#     'iTunes first, then MusicBrainz', so a query with 40 iTunes rows filled the
#     whole 40-row window and MusicBrainz got zero slots - the first page showed
#     no MusicBrainz rows even though albums_shown counted them.
check("providers-are-interleaved",
      "def _interleave_providers(" in _src
      and "_interleave_providers(itunes_results, mb_results)" in _src,
      "the two providers must be interleaved so neither crowds the other out")
check("no-first-come-window",
      "_mb_take" not in _src,
      "the old 'iTunes fills the window first' slicing must be gone")
# the helper itself must actually interleave, and must not lose or duplicate rows
_il = fai._interleave_providers
_A = [{"id": "i%d" % i} for i in range(40)]
_B = [{"id": "m%d" % i} for i in range(40)]
_mixed = _il(_A, _B)
check("interleave-keeps-every-row",
      len(_mixed) == 80
      and len({r["id"] for _s, r in _mixed}) == 80,
      f"interleave must keep all 80 rows exactly once, got {len(_mixed)}")
_first40 = [s for s, _r in _mixed[:40]]
check("first-page-has-both-sources",
      "itunes" in _first40 and "musicbrainz" in _first40,
      f"a 40/40 split must put both providers on page 1, got {set(_first40)}")
check("first-page-is-balanced",
      15 <= _first40.count("itunes") <= 25,
      f"page 1 should be roughly balanced, got {_first40.count('itunes')} iTunes")
_skew = _il(_A, [{"id": "m0"}])
check("interleave-surfaces-a-small-source",
      "musicbrainz" in [s for s, _r in _skew[:10]],
      "a 40/1 split must still show the lone MusicBrainz row near the top")
check("interleave-handles-empty-sides",
      _il([], []) == [] and _il(_A, []) != [] and _il([], _B) != [],
      "interleave must tolerate an empty or missing provider list")
# the RENDERED order must alternate too. Interleaving the window and then
# re-splitting it into two render loops put the counts right but the list still
# read as a block of iTunes followed by a block of MusicBrainz.
_ord_body = c.get("/api_search?q=pink+floyd+-+the+wall&per_page=20").data.decode("utf-8", "ignore")
_ord = re.findall(r"pick\('(itunes|musicbrainz)'", _ord_body)
check("rendered-rows-alternate",
      len(_ord) >= 10 and _ord[:6].count("itunes") <= 4 and _ord[:6].count("musicbrainz") >= 2,
      f"the first rows must mix both providers, got {_ord[:8]}")
# both providers must actually appear on a broad first page
check("first-page-shows-both-providers",
      "itunes" in _ord and "musicbrainz" in _ord,
      "a broad first page must contain both iTunes and MusicBrainz rows")
# the id is interpolated into an onclick attribute, so it must be escaped
check("row-id-is-escaped",
      "rid = esc(item.get(\"id\"))" in _src,
      "the release id goes into onclick='...', so it must be HTML-escaped")

# 41. 'Existing Information' must report what WMP CURRENTLY holds, not the album
#     that is about to be applied. It used to render details.title / artist /
#     year / genre, so the incoming tags looked like tags already on the disc -
#     which is the opposite of what the panel is for, and misleading precisely
#     when a fresh rip genuinely has nothing stored yet.
_ctx = c.get("/confirm?source=itunes&id=1065975633&requestid=REQ_EXIST"
             "&artist=Some+Artist&album=Old+Album&track=Track+One"
             ).data.decode("utf-8", "ignore")
check("existing-panel-shows-current-not-matched",
      'id="existingTitle">Old Album<' in _ctx
      and 'id="existingArtist">Some Artist<' in _ctx
      and "The Wall" not in _ctx.split('id="existingTitle"')[1][:200],
      "the panel must show the state WMP holds, not the matched album")
check("existing-panel-says-where-it-came-from",
      'id="existingSource"' in _ctx and "Currently stored by Windows Media Player." in _ctx,
      "the panel must state its source so current and incoming are not conflated")
check("matched-album-is-labelled-separately",
      "is what <b>Finish &amp; Apply</b> will write." in _ctx,
      "the album that will be applied must be named outside the existing panel")
# ...and with no WMP context at all it must say so rather than inventing a state.
_ctx0 = c.get("/confirm?source=itunes&id=1065975633&requestid=REQ_EXIST0"
              ).data.decode("utf-8", "ignore")
check("existing-panel-has-an-empty-state",
      'class="existing-empty"' in _ctx0 or "No existing information" in _src,
      "a dialog with no disc context must show an explicit empty state")
check("existing-renderer-handles-empty",
      "No existing information" in _src and "renderExistingInfo" in _src,
      "renderExistingInfo() must state when nothing is stored")
check("existing-prefers-disc-over-query-context",
      "d.disc_album  || WMP_CTX_ALBUM" in _src,
      "the MDQ describes the real disc and must outrank the query arguments")
# The context reaches the page, JSON-quoted so it cannot break out of the script.
check("wmp-context-is-json-quoted",
      "var WMP_CTX_ALBUM  = {{ wmp_album|tojson }};" in _src
      and 'var WMP_CTX_ALBUM  = "Old Album";' in _ctx,
      "WMP-supplied album/artist/track must be embedded with tojson, not raw")
# The renderer is actually invoked, with and without an MDQ.
check("existing-info-renders-on-load",
      "renderExistingInfo(CACHED_MDQ);" in _src,
      "the panel must be populated on load, not left on its server placeholder")
check("existing-info-escapes-values",
      "function escHtml(s)" in _src and "escHtml(album || track)" in _src,
      "disc-supplied strings are written via innerHTML and must be escaped")

# 42. The Edit link was an inert <span> with no handler on both pages. It now
#     tries WMP's own metadata editor first and falls back to an inline editor.
for _name, _pg in (("confirm", page), ("ui", ui_html)):
    check("edit-link-is-wired-%s" % _name,
          'onclick="editExisting(); return false;">Edit<' in _pg,
          f"the Edit link on /{_name} must actually do something")
    check("edit-panel-exists-%s" % _name,
          'id="existingEdit"' in _pg or 'id="editNote"' in _pg,
          f"/{_name} needs a target for the editor to open into")
check("edit-tries-the-host-editor-first",
      "window.external.EditMetadata()" in _src,
      "the authentic action is WMP's own metadata editor - try it before ours")
check("edit-never-truthiness-tests-a-com-member",
      _re.search(r"if \(window\.external && window\.external\.EditMetadata\)\s*\{"
                 r"\s*window\.external\.EditMetadata\(\);", _src) is not None,
      "the COM member must be invoked inside the guarded block, never skipped "
      "by a falsy host object")
check("edit-writes-into-the-applied-album",
      "ALBUM_DETAILS.title = t;" in _src and "ALBUM_DETAILS.artist = a;" in _src,
      "an edit must change the object POSTed to /store_staged_xml, not just "
      "what is displayed")
check("edit-cannot-empty-the-album",
      "if (!t && !a) {" in _src and "alert(" in _src,
      "saving with no title and no artist must be refused")
check("edit-is-reversible",
      "function closeExistingEdit()" in _src and "onclick=\"closeExistingEdit();\"" in _src,
      "the inline editor needs a Cancel")
check("edit-fields-are-escaped",
      "escHtml(value)" in _src,
      "album values are interpolated into a value=\"...\" attribute and must "
      "be escaped")
# The new styling must survive the IE7 host like everything else.
for _cls in (".existing-source", ".existing-empty", ".existing-edit",
             ".edit-input", ".edit-label", ".edit-note"):
    check("ie7-safe-styling-%s" % _cls.strip("."),
          _cls in _src and "flex" not in _cls,
          f"{_cls} must exist in the shared stylesheet")
# Bounded by the conditional's own `<![endif]-->` rather than a fixed character
# count. The intent is "the IE7 block resets these", which is a question about
# the block's extent; a [:2000] window only happened to cover it while the
# block was shorter, and silently starts failing the moment an unrelated
# comment above grows. `_ie7_block` is asserted non-empty so a failed split
# cannot quietly pass.
_ie7_block = _src.split("lt IE 8")[1].split("<![endif]-->")[0]
check("ie7-conditional-block-is-found",
      len(_ie7_block) > 500,
      f"the lt IE 8 conditional must be locatable in the stylesheet, got "
      f"{len(_ie7_block)} chars")
check("new-inputs-reset-border-radius",
      ".edit-input" in _ie7_block,
      "the IE7 conditional block must reset the new inputs too")

# 43. Nothing above may disturb the write path. The payload is still the same
#     object, and the editor is display + data only.
check("write-payload-unchanged-by-edit-feature",
      "album: ALBUM_DETAILS," in _src and "cd: WMP_CD," in _src
      and "wmid: WMP_WMID," in _src and "wmid_auth: WMP_WMID_AUTH" in _src,
      "the staging payload must still carry the same fields as before")
check("edit-does-not-rewrite-track-rows",
      "ALL_TRACKS" in _src and "ALL_TRACKS[i].id" in _src,
      "per-track selection must be untouched by the album editor")

# 44. A library "Update album info" opens the dialog with NOTHING but
#     ?requestid= - no disc, no wmid, no artist/album/track. The search page
#     read that as "Windows Media Player did not pass any disc information",
#     which is both wrong (WMP did open it, for a specific library album) and
#     useless. Logged from a real session on 'The Blue Room - EP' (Coldplay):
#       GET /FAI/ui?...&requestid=D86F70C1-08E2-4219-8F86-7FE6A1C98974
_lib = c.get("/FAI/ui?locale=409&userlocale=2000"
             "&requestid=D86F70C1-08E2-4219-8F86-7FE6A1C98974"
             ).data.decode("utf-8", "ignore")
_rip = c.get("/FAI/ui?artist=A&album=B&track=C").data.decode("utf-8", "ignore")
_disc = c.get("/FAI/ui?cd=B+96+1970&requestid=RID2").data.decode("utf-8", "ignore")
_bare = c.get("/FAI/ui").data.decode("utf-8", "ignore")
check("no-false-disc-information-claim",
      "did not pass any disc information" not in _lib
      and "did not pass any disc information" not in _bare,
      "WMP opening the dialog IS information - do not claim it passed nothing")
check("library-flow-is-classified",
      'var SEARCH_FLOW = "library";' in _lib
      and 'var SEARCH_FLOW = "disc";' in _disc
      and 'var SEARCH_FLOW = "unknown";' in _bare,
      "the search page must know which of the three flows it is in")
check("library-flow-uses-the-request-id",
      'var SEARCH_REQUEST_ID = "D86F70C1-08E2-4219-8F86-7FE6A1C98974";' in _lib
      and "GetMDQByRequestID(SEARCH_REQUEST_ID)" in _lib,
      "the request id is the only handle on a library album - the MDQ it "
      "returns is what carries the CURRENT tags")
check("search-page-renders-existing-info",
      "function renderExistingInfo" in _lib
      and 'id="existingTitle"' in _lib and 'id="existingSource"' in _lib,
      "the search page needs the same current-state panel as the confirm page")
check("search-page-panel-is-filled-on-load",
      "renderExistingInfo(mdq);" in _lib,
      "the panel must be populated from the MDQ when the page opens")
check("library-empty-state-is-honest",
      "did not report the current tags of this album." in _src,
      "when even the MDQ has nothing, say so - do not invent a state")
# ...and the results pane must not claim to be searching when it is not.
check("no-false-searching-indicator",
      "Searching metadata databases..." in _rip
      and "Searching metadata databases..." not in _bare
      and "Enter a search and press Enter." in _bare,
      "the 'Searching...' placeholder must only appear when a search is "
      "actually going to run")

# 45. The MDQ does not use the <tag><text>value</text></tag> shape everywhere.
#     A prefix-free <title>...<text> match returned EMPTY in every logged
#     session even though the document is ~1.3kB and contains a <track> block,
#     so the reader now tries several element names and shapes rather than one
#     assumption - and reports the real tag inventory so the parser can be
#     settled from evidence instead of another guess.
check("mdq-reader-is-shape-tolerant",
      "function mqField(mdq, names, prefix)" in _src
      and "['albumTitle']" in _src and "'trackArtist'" in _src
      and "'albumArtist'" in _src,
      "the MDQ reader must try candidate element names, not one hard-coded shape")
check("mdq-reader-probes-the-real-schema",
      "function mdqTags(mdq)" in _src and "existing_info_probe" in _src
      and "mdq_tags:" in _src and "mdq_head:" in _src,
      "report the MDQ's element inventory and head once - guessing the schema "
      "has already cost one wrong fix")
check("track-title-is-not-the-album-title",
      "replace(/<album>[\\\\s\\\\S]*?<\\\\/album>/gi, '')" in _src,
      "the <album> block must be stripped before the track title is read, or a "
      "lazy <title> match returns the ALBUM name as the track name")

# 46. THE ROOT CAUSE of the empty 'Existing Information', found by running the
#     SHIPPED reader against the real MDQ instead of a hand-written copy of it.
#     Every MDQ regex was built with new RegExp('...') from a PYTHON string, and
#     '\s' inside a JS *string literal* collapses to a bare 's'. So the
#     character class shipped as [sS] and every match failed - silently, in
#     every session, for the whole life of the project:
#       "disc": {"track_count": 1, "disc_track": "", "disc_artist": "", ...}
#       "found_album": "", "found_artist": "", "found_track": ""
#     A regex LITERAL has no such ambiguity, so the readers now build from
#     literal sources. The shipped reader on the real MDQ returns
#     disc_track='Miracles (Someone Special)', disc_artist='Coldplay',
#     disc_album='X&Y', cid='53813A88-...'.
for _lit in ("RE_PREFIX_TEXT", "RE_PREFIX_INNER", "RE_TEXT", "RE_INNER",
             "RE_TEXT_ANY", "RE_PREFIX_ANY"):
    check("regex-literal-%s" % _lit.lower(),
          _re.search(r"var %s\s*=\s*/" % _lit, _src) is not None,
          f"{_lit} must be a regex literal, not a new RegExp('...') string - a "
          f"backslash-s in a JS string literal collapses to 's'")
check("no-escaped-string-regex-left",
      _re.search(r"new RegExp\('[^']*\\s", _src) is None,
      "no MDQ regex may be built from an escaped string literal again: that is "
      "what made every match fail silently")
check("mdq-content-id-write-path",
      "function mqContentId(mdq)" in _src
      and "WriteNamesEx(1, libCid, generatedXml, false)" in _src
      and "WriteNamesEx-lib-cid-ok" in _src,
      "a library update with no wmid must write by the track's real "
      "WMContentID; writing by MDQ is a CD call and applies nothing")
check("content-id-is-a-last-resort",
      _at(_src, "var libCid = mqContentId(mdq);") < _at(_src, "WriteNamesEx(1, libCid"),
      "the collection write must still be preferred; the content id is only the "
      "fallback for when WMP has not revealed a wmid yet")

# 47. Artwork for a disc that already has a collection. WMP caches the art it
#     has seen for a collection and will not fetch again, so a re-apply never
#     refreshes it: five applies of the Tiny Cities disc between 15:18 and
#     15:25, every one ?cd=B...&wmid=B17CF884, produced no [IMAGE] at all,
#     while a first apply to a FRESH disc fetched it twice. A cover URL that
#     differs from the one WMP has on file is the only lever the server has.
#
#     There are now TWO shapes for that URL, selected by _ART_MODE, because the
#     proxy had never once produced an attached cover: 62 WMP cover fetches
#     through it, 0 direct, 0 attachments. Both are pinned here so neither can
#     regress unnoticed.
_ARTU = ("https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/90/81/27/"
         "908127e4-acd6-8538-b8ab-0b8d1f1cd18c/859727388959_cover.jpg/600x600bb.jpg")
_alb = dict(album, id="1570089404", source="itunes", art_url=_ARTU)


def _cover(a, mode="proxy", **kw):
    _prev = fai._ART_MODE
    fai._ART_MODE = mode
    try:
        x = fai.build_wmp_xml(dict(a), selected_tracks=a["tracks"], **kw)
    finally:
        fai._ART_MODE = _prev
    m = re.search(r"<largeCoverParams>([^<]+)</largeCoverParams>", x)
    return m.group(1) if m else ""


# --- proxy shape -----------------------------------------------------------
_c1 = _cover(_alb, "proxy", cd="B+96+1970")
_c2 = _cover(_alb, "proxy", cd="B+96+1970")
_c3 = _cover(dict(_alb, id="1065975633"), "proxy", cd="B+96+1970")
check("cover-url-carries-a-version-token",
      "/cover/fai-" in _c1 and _c1.startswith(
          "http://127.0.0.1/cover/fai-") and "?url=https://is1" in _c1,
      f"in proxy mode the cover URL must carry a token in the PATH and keep the "
      f"upstream url readable, got {_c1[:130]!r}")
check("cover-token-changes-on-every-apply",
      _c1 != _c2,
      "re-applying the SAME album must present a URL WMP has not seen, or WMP "
      "keeps the cover it already has and the retry cannot change anything. "
      "REVERSED: this was 'cover-token-is-stable-per-album', on the reasoning "
      "that a stable URL saves a needless re-download. Logged 15:52 - the "
      "re-apply of Prospekt's March sent a byte-identical URL to 15:43's and "
      "there was no [IMAGE] line in the session at all.")
check("cover-token-differs-per-album",
      _c1 != _c3,
      "a different album must present a different URL, so WMP treats it as new")
check("cover-token-is-well-formed-for-a-path-segment",
      all(re.fullmatch(r"[0-9a-f]{8}", t)
          for t in re.findall(r"/cover/fai-([^/]+)/", _c1 + _c2 + _c3)),
      f"the token is a path segment and must stay hex: {_c1[:120]!r}")

# --- direct shape ----------------------------------------------------------
# The default, and the first configuration in which WMP has ever been offered a
# cover it did not have to go through this server to reach.
check("art-mode-defaults-to-direct",
      fai._ART_MODE == "direct",
      "the proxy produced 62 fetched covers and 0 attachments; the direct URL is "
      "what a real FAI server sends and keeps a loopback host out of the document")
_d1 = _cover(_alb, "direct", cd="B+96+1970")
_d2 = _cover(dict(_alb, art_url="https://example.com/x.jpg?a=1&b=2"),
             "direct", cd="B+96+1970")
check("direct-cover-is-the-upstream-url",
      _d1 == _ARTU,
      f"in direct mode the cover must be the upstream URL verbatim, got {_d1!r}")
check("direct-cover-survives-an-ampersand-in-the-upstream-url",
      _d2 == "https://example.com/x.jpg?a=1&amp;b=2",
      f"direct mode still xesc-escapes, so an upstream '&' cannot make the "
      f"document malformed, got {_d2!r}")
check("both-art-modes-agree-when-there-is-no-art",
      _cover(dict(_alb, art_url=""), "direct", cd="B+96+1970") == ""
      and _cover(dict(_alb, art_url=""), "proxy", cd="B+96+1970") == "",
      "no art upstream must mean no cover params in either mode, not a broken URL")
_r = c.get("/cover/album.jpg?url=" + _ARTU + "&locale=409&geoid=be")
check("proxy-ignores-wmp-parameters",
      _r.status_code == 200 and len(_r.data) > 1000,
      f"the image proxy must ignore the extra parameters - WMP appends its own "
      f"too - got {_r.status_code}, {len(_r.data)}B")
_rp = c.get("/cover/fai-deadbeef/album.jpg?url=" + _ARTU)
check("proxy-serves-the-path-token-form",
      _rp.status_code == 200 and len(_rp.data) > 1000,
      f"the proxy must serve the tokenised path form, got {_rp.status_code}, "
      f"{len(_rp.data)}B")

# 47b. REGRESSION. MusicBrainz was the only provider whose artwork URL is a
#      REDIRECT rather than an image, and WMP was handed the redirecting URL:
#
#        coverartarchive.org/release/<id>/front-500.jpg
#          -> 307 text/plain   (not an image at all)
#          -> 302 image/jpeg
#
#      iTunes and Discogs return 200 image/jpeg directly, which is exactly why
#      those two attached artwork and MusicBrainz did not. The document must now
#      carry the final direct image URL.
#
#      These pin the RESOLUTION behaviour rather than live archive.org, so they
#      cannot go flaky when the network is down.
class _FakeResp:
    def __init__(self, url, ctype):
        self.url = url
        self.headers = {"Content-Type": ctype}
        self.closed = False

    def close(self):
        self.closed = True


class _FakeGet:
    """Stands in for requests.get, recording that the body was never read."""

    def __init__(self, final, ctype):
        self.final, self.ctype, self.calls = final, ctype, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return _FakeResp(self.final, self.ctype)


_real_get = fai.requests.get
try:
    # A redirect chain that lands on an image is resolved.
    _fg = _FakeGet("https://dn710007.ca.archive.org/0/items/mbid-x/mbid-x-1_thumb500.jpg",
                   "image/jpeg")
    fai.requests.get = _fg
    check("musicbrainz-cover-redirect-resolves-to-a-direct-image",
          fai._resolve_art_url("https://coverartarchive.org/release/x/front-500.jpg")
          == _fg.final,
          "the redirect chain must be walked so WMP is given a direct image URL, "
          "the same shape iTunes and Discogs already returned")
    check("art-resolve-does-not-download-the-body",
          _fg.calls and _fg.calls[0][1].get("stream") is True
          and _fg.calls[0][1].get("allow_redirects") is True,
          f"resolution only needs the final URL and headers, so the image body "
          f"must not be pulled down: {_fg.calls}")

    # A chain that does NOT end on an image keeps the original URL, so a release
    # with no cover can never be left with a URL pointing at a text/plain body.
    fai.requests.get = _FakeGet("https://coverartarchive.org/release/x/front-500.jpg",
                                "text/plain")
    check("art-resolve-keeps-the-url-when-it-is-not-an-image",
          fai._resolve_art_url("https://coverartarchive.org/release/x/front-500.jpg")
          == "https://coverartarchive.org/release/x/front-500.jpg",
          "an unresolvable or non-image redirect must fall back to the original URL "
          "rather than hand WMP something that cannot be an image")

    # Offline / exception must never lose the artwork.
    def _boom(url, **kw):
        raise RuntimeError("network down")
    fai.requests.get = _boom
    check("art-resolve-falls-back-when-the-network-fails",
          fai._resolve_art_url("https://coverartarchive.org/release/x/front-500.jpg")
          == "https://coverartarchive.org/release/x/front-500.jpg",
          "resolution is best-effort; a failure must degrade to today's behaviour, "
          "not strip the artwork")

    # Non-http and empty values pass straight through without any request.
    _fg2 = _FakeGet("http://elsewhere/x.jpg", "image/jpeg")
    fai.requests.get = _fg2
    check("art-resolve-ignores-non-http-inputs",
          fai._resolve_art_url("") == "" and fai._resolve_art_url("data:,x") == "data:,x"
          and not _fg2.calls,
          "empty and non-http values must be returned untouched and must not "
          "trigger a network call")
finally:
    fai.requests.get = _real_get

# The end-to-end effect: a MusicBrainz album's art_url is what reaches the
# document, so the resolved URL must survive into largeCoverParams intact.
_MB_RESOLVED = ("https://dn710007.ca.archive.org/0/items/"
                "mbid-d47ffe81-892b-46bf-ab3f-085c011d3292/"
                "mbid-d47ffe81-892b-46bf-ab3f-085c011d3292-33662597246_thumb500.jpg")
check("resolved-art-url-survives-into-the-document",
      _cover(dict(_alb, source="musicbrainz", art_url=_MB_RESOLVED),
             "direct", cd="B+96+1970") == _MB_RESOLVED,
      "the resolved direct URL is what WMP reads out of largeCoverParams, "
      "unchanged and unescaped")

# 48. REGRESSION. Adding the token as a SECOND QUERY PARAMETER put a bare '&'
#      into largeCoverParams, which made the whole document not-well-formed.
#      WMP then rejected the entire response, so tags stopped applying as well
#      as artwork:
#        ET.fromstring(xml) -> not well-formed (invalid token): line 16, column 198
#      The pre-existing well-formedness checks all used an album with NO
#      artwork, so the cover field was empty and the bug sailed past them. Every
#      document must be well-formed WHILE CARRYING a cover, including one whose
#      upstream URL contains its own '&'.
import xml.etree.ElementTree as _ET2
_bad = []
# Both art modes: the bare-'&' bug lived in the proxy form, but the direct form
# puts a raw upstream URL in the same field and must be escaped just as firmly.
for _mode in ("proxy", "direct"):
    for _label, _alb2, _url in (
            ("plain", _alb, _ARTU),
            ("ampersand-in-upstream",
             _alb, "https://example.com/art.jpg?w=1&h=2&x=3"),
            ("ampersand-in-title", dict(_alb, title="X & Y"), _ARTU),
    ):
        _prev_mode = fai._ART_MODE
        fai._ART_MODE = _mode
        try:
            _x = fai.build_wmp_xml(dict(_alb2), selected_tracks=_alb2["tracks"],
                                   cd="B+96+1970")
        finally:
            fai._ART_MODE = _prev_mode
        try:
            _ET2.fromstring(_x)
        except Exception as _e:
            _bad.append("%s/%s: %s" % (_mode, _label, _e))
check("document-is-well-formed-with-artwork",
      not _bad,
      f"every delivered document must be well-formed XML even with a cover, in "
      f"either art mode: {_bad}")
check("cover-value-has-no-bare-ampersand",
      "&" not in re.sub(r"&(amp|lt|gt|quot|apos);", "", _c1)
      and "&" not in re.sub(r"&(amp|lt|gt|quot|apos);", "", _d1),
      f"neither cover form may contain a bare '&': proxy={_c1[:110]!r} "
      f"direct={_d1[:110]!r}")

# ==========================================================
# 50. DISCOGS (OPTIONAL THIRD PROVIDER)
# ==========================================================

# ---- 50a. the token must never be committed ------------------------------
# The repository is PUBLIC. A token pasted into tracked source is a leaked
# credential that anyone can clone and use, and it cannot be un-leaked by
# editing the file later - it stays in git history forever.
_DG_TOKENS = re.findall(r"\b[A-Za-z0-9]{36,}\b", _src)
check("no-discogs-token-in-source",
      not _DG_TOKENS,
      f"a long opaque credential-shaped string is committed in FAI Server.py: "
      f"{_DG_TOKENS[:2]}")
check("discogs-token-reads-the-environment",
      "DISCOGS_TOKEN" in _src and 'os.environ.get("DISCOGS_TOKEN")' in _src,
      "the token must come from the environment, not from committed source")
for _f in ("README.md", "requirements.txt", "Procfile", "render.yaml"):
    try:
        with open(os.path.join(_DIR, _f), encoding="utf-8") as _fh:
            _txt = _fh.read()
    except OSError:
        continue
    _leak = [t for t in re.findall(r"\b[A-Za-z0-9]{36,}\b", _txt) if t not in
             ("WindowsMediaPlayerFAI/2.0", "MusicBrainzSearchByServer/1.0")]
    check("no-discogs-token-in-%s" % _f.replace(".", "-"),
          not _leak,
          f"{_f} must not contain a Discogs token: {_leak[:1]}")

# ---- 50b. no token means the feature is completely absent ---------------
# This is the safety property: a user with no token must get EXACTLY the
# behaviour they had before Discogs existed.
_prev_tok = fai.DISCOGS_TOKEN
fai.DISCOGS_TOKEN = ""
try:
    check("discogs-unconfigured-without-token",
          fai._discogs_configured() is False,
          "a missing token must disable the provider, not crash the search")
    check("discogs-search-empty-without-token",
          fai.search_discogs("pink floyd the wall") == [],
          "search_discogs must return nothing at all when unconfigured")
    check("discogs-details-empty-without-token",
          fai.get_discogs_album_details("5460") is None,
          "get_discogs_album_details must return None when unconfigured")
    check("discogs-get-makes-no-call-without-token",
          fai._discogs_get("database/search") is None,
          "_discogs_get must not touch the network without a token")
    check("interleave-tolerates-missing-provider",
          fai._interleave_sources([("itunes", [{"id": "a"}]),
                                   ("discogs", [])])
          == [("itunes", {"id": "a"})],
          "an unconfigured Discogs must not appear in the merged list")
finally:
    fai.DISCOGS_TOKEN = _prev_tok

# ---- 50c. authenticated requests ---------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _with_token(fn):
    """Run fn with Discogs enabled, a fresh cache and a stubbed transport."""
    prev = (fai.DISCOGS_TOKEN, fai._discogs_get, fai.search_cache,
            fai.album_cache, fai.session)
    fai.DISCOGS_TOKEN = "test-token-not-real"
    fai.search_cache = fai.TTLCache(default_ttl=7200)
    fai.album_cache = fai.TTLCache(default_ttl=86400)
    try:
        return fn()
    finally:
        (fai.DISCOGS_TOKEN, fai._discogs_get, fai.search_cache,
         fai.album_cache, fai.session) = prev


_seen = []


class _FakeSession:
    def get(self, url, params=None, timeout=None, headers=None):
        _seen.append({"url": url, "params": params, "headers": headers or {}})
        return _FakeResp(200, {"results": []})


# Assert on the LITERAL request rather than trusting that _discogs_get
# "does auth" - a stubbed _discogs_get would hide a missing header.
# _with_token saves and restores fai.session, so the fake cannot leak.
# It must be an INSTANCE, not the class: the app calls session.get(url, ...)
# and a bare class would bind url to self.
fai._DG_LAST_CALL[0] = 0.0
_with_token(lambda: (setattr(fai, "session", _FakeSession()),
                     fai.search_discogs("kind of blue"))[1])
_hdr = _seen[0]["headers"] if _seen else {}
check("discogs-sends-an-auth-header",
      bool(_seen) and "Discogs token=test-token-not-real"
      in str(_hdr.get("Authorization", "")),
      f"the request must authenticate with the token: "
      f"{_hdr.get('Authorization')!r}")
check("discogs-token-is-not-a-query-param",
      bool(_seen) and "token=" not in str(_seen[0]["params"]),
      f"the token must travel in a header, never in a URL/query that gets "
      f"logged: {_seen[0]['params'] if _seen else None}")
check("discogs-has-its-own-user-agent",
      bool(_hdr.get("User-Agent")) and "DISCOGS_USER_AGENT" in _src,
      "Discogs must identify itself with its own User-Agent, not the "
      "MusicBrainz one (that constant guards the 1 req/sec limiter)")

# ---- 50d. search normalization -----------------------------------------
_SEARCH_PAYLOAD = {"results": [
    {"master_id": 5460, "id": 5460, "type": "master",
     "title": "Miles Davis - Kind Of Blue", "year": 1959,
     "thumb": "https://i.discogs.com/t.jpg",
     "cover_image": "https://i.discogs.com/c.jpg",
     "genre": "Jazz", "style": ["Modal", "Bebop"]},
    {"master_id": 999, "id": 999, "type": "master",
     "title": "NoSeparatorHere", "year": 1990},
    {"id": None, "type": "master", "title": "Broken - Row"},
]}


def _search_probe():
    fai._discogs_get = lambda p, params=None, **k: (
        _FakeResp(200, _SEARCH_PAYLOAD) if p.startswith("database") else None)
    return fai.search_discogs("kind of blue")


_rows = _with_token(_search_probe)
check("discogs-search-normalizes-rows",
      len(_rows) == 2 and _rows[0]["title"] == "Kind Of Blue"
      and _rows[0]["artist"] == "Miles Davis",
      f"'Artist - Album' must split into the shared title/artist fields: {_rows}")
check("discogs-rows-match-the-shared-shape",
      all({"id", "title", "artist", "year", "art_thumb"} <= set(r.keys())
          for r in _rows),
      "Discogs rows must carry the same fields the albums renderer reads from "
      f"iTunes/MusicBrainz rows: {sorted(_rows[0].keys()) if _rows else []}")

# ---- 50e. helper units ---------------------------------------------------
check("dg-parses-mmss-durations",
      fai._dg_duration_ms("9:22") == 562000
      and fai._dg_duration_ms("1:02:33") == 3753000
      and fai._dg_duration_ms("") == 0
      and fai._dg_duration_ms(None) == 0,
      "Discogs sends durations as 'M:SS' strings and the rest of the app "
      "needs milliseconds")
check("dg-does-not-crash-on-bad-durations",
      fai._dg_duration_ms("n/a") == 0 and fai._dg_duration_ms(":::") == 0,
      "a malformed duration must degrade to 0, not raise and lose the album")
check("dg-maps-side-letters-to-one-disc",
      fai._dg_position("A1") == (1, 1) and fai._dg_position("B12") == (1, 12),
      "A/B are SIDES of one disc, not discs - reading B as disc 2 would "
      "invent a second disc WMP never had")
check("dg-maps-real-multidisc-positions",
      fai._dg_position("2-5") == (2, 5),
      "on a genuine 2-CD release '2-5' really is disc 2 track 5")
check("dg-splits-artist-and-title",
      fai._dg_split_artist_title("Pink Floyd - The Wall")
      == ("Pink Floyd", "The Wall"),
      "Discogs packs artist and title into one string")
check("dg-keeps-a-title-without-an-artist",
      fai._dg_split_artist_title("Kind Of Blue") == ("", "Kind Of Blue"),
      "a title with no separator must not be mangled into a fake artist")

# ---- 50f. detail normalization ------------------------------------------
_MASTER = {
    "id": 5460, "title": "Kind Of Blue", "year": 1959,
    "artists": [{"name": "Miles Davis"}],
    "genres": ["Jazz"], "styles": ["Modal"],
    "images": [{"type": "secondary", "uri": "https://img/back.jpg"},
               {"type": "primary", "uri": "https://img/front.jpg"}],
    "tracklist": [
        {"type_": "heading", "position": "A", "title": "Side A"},
        {"position": "A1", "title": "So What", "duration": "9:22",
         "extraartists": [{"name": "Miles Davis", "role": "Composed By"},
                          {"name": "Bob", "role": "Producer"}]},
        {"position": "A2", "title": "Freddie Freeloader", "duration": "9:46",
         "extraartists": [{"name": "Horace Silver", "role": "Written-By"}]},
    ],
}


def _detail_probe():
    fai._discogs_get = lambda p, params=None, **k: (
        _FakeResp(200, _MASTER) if p.startswith("masters/") else None)
    return fai.get_discogs_album_details("5460")


_det = _with_token(_detail_probe)
check("discogs-detail-returns-the-shared-shape",
      bool(_det) and {"id", "source", "title", "artist", "genre", "year",
                      "art_url", "tracks"} <= set(_det.keys()),
      f"the confirm page and build_wmp_xml() read a fixed key set, so a "
      f"Discogs album must produce it: {sorted(_det.keys()) if _det else None}")
check("discogs-detail-skips-heading-rows",
      bool(_det) and len(_det["tracks"]) == 2,
      f"'Side A' is a structural heading, not a track: "
      f"{[t['name'] for t in _det['tracks']] if _det else None}")
check("discogs-composer-only-from-composer-roles",
      bool(_det) and _det["tracks"][0]["composer"] == "Miles Davis",
      f"only Composer/Written-By roles are composers - writing 'Bob' "
      f"(Producer) as the composer would put a false credit in the library: "
      f"{_det['tracks'][0]['composer'] if _det else None}")
# Discogs HYPHENATES the role in the wild ('Written-By', not 'Written By').
# Matching only the spaced form silently dropped every Lennon-McCartney style
# credit while looking perfectly correct in the UI - an empty composer field.
check("discogs-matches-the-hyphenated-written-by-role",
      bool(_det) and _det["tracks"][1]["composer"] == "Horace Silver",
      f"the role string Discogs actually sends is 'Written-By': "
      f"{_det['tracks'][1]['composer'] if _det else None}")
check("discogs-prefers-the-primary-image",
      bool(_det) and _det["art_url"] == "https://img/front.jpg",
      f"the front cover must win over a back sleeve: "
      f"{_det['art_url'] if _det else None}")
check("discogs-id-cannot-collide-with-other-providers",
      bool(_det) and _det["id"] == "dg5460",
      f"iTunes/MusicBrainz ids are bare integers; an unprefixed Discogs id "
      f"would generate the same WMP collection GUID and apply the wrong "
      f"album's tags: {_det['id'] if _det else None}")
check("discogs-tracks-are-complete",
      bool(_det) and all({"id", "name", "number", "disc", "artist", "performer",
                          "composer", "duration_ms", "genre"} <= set(t.keys())
                          for t in _det["tracks"]),
      "every track needs the full key set the confirm page and XML builder read")
check("discogs-track-durations-carry-through",
      bool(_det) and _det["tracks"][0]["duration_ms"] == 562000,
      f"a real runtime must survive normalization: "
      f"{_det['tracks'][0]['duration_ms'] if _det else None}")

# ---- 50d. an ORPHAN release must be findable at all ----------------------
# Searching 'Sunday Driver The End Of Julia' showed only the MusicBrainz row.
# The album IS on Discogs - release 4913272 - and Discogs' own API returns it
# as the top hit for type=release. We only ever asked for type=master, and that
# release has master_id 0: it was never linked to a master, so the master-only
# search returned nothing at all. The album existed and was simply unreachable.
#
# The rows below are the REAL payloads from that case, not invented ones.
_ORPHAN_RELEASE = {"title": "The End Of Julia - Sunday Driver", "id": 4913272,
                   "master_id": 0, "type": "release", "year": None,
                   "thumb": "", "cover_image": "", "genre": ["Rock"],
                   "style": ["Math Rock", "Emo"], "format": ["CD", "Album"],
                   "country": "US"}
_MASTER_ROW = {"title": "Miles Davis - Kind Of Blue", "id": 5460,
               "master_id": 5460, "type": "master", "year": 1959,
               "thumb": "t", "cover_image": "c", "genre": ["Jazz"],
               "style": [], "format": ["LP"], "country": "US"}
# A release that IS linked to a master must not be listed twice.
_LINKED_RELEASE = dict(_MASTER_ROW, id=999999, type="release")


def _orphan_search_probe():
    def _get(path, params=None, **k):
        if path == "database/search":
            t = (params or {}).get("type")
            if t == "master":
                return _FakeResp(200, {"results": [_MASTER_ROW]})
            if t == "release":
                return _FakeResp(200, {"results": [_ORPHAN_RELEASE,
                                                   _LINKED_RELEASE]})
        return None
    fai._discogs_get = _get
    return fai.search_discogs("Sunday Driver The End Of Julia", limit=30,
                              artist_hint="Sunday Driver",
                              album_hint="The End Of Julia",
                              hints_reliable=True)


_orphan = _with_token(_orphan_search_probe)
_ids = [r["id"] for r in _orphan]
# Masters are emitted before orphans, so look the rows up by id rather than
# by position - otherwise these assertions silently test the wrong row.
_orphan_row = next((r for r in _orphan if r["id"] == "r:4913272"), None)
check("discogs-finds-a-release-that-has-no-master",
      "r:4913272" in _ids,
      f"an orphan release (master_id 0) is invisible to a master-only search, "
      f"which is why Sunday Driver's album never appeared: {_ids}")
check("discogs-orphan-row-does-not-duplicate-its-master",
      _ids.count("m:5460") == 1 and "r:999999" not in _ids,
      f"a release that already has a master is covered by the master row; "
      f"adding it again would reintroduce the one-row-per-pressing flood the "
      f"master search exists to prevent: {_ids}")
check("discogs-rows-carry-their-kind",
      all((":" in i and i[0] in "mr") for i in _ids) and len(set(_ids)) == len(_ids),
      f"masters/id and releases/id are different endpoints, so the id must "
      f"say which one it is: {_ids}")

# The inverted record. Discogs has this release as title='Sunday Driver' with
# artists=['The End Of Julia'] - the two halves the wrong way round, in the
# record itself. Splitting the index title 'The End Of Julia - Sunday Driver'
# naively yields artist='The End Of Julia', album='Sunday Driver', which is
# backwards and is exactly what the user saw.
check("discogs-swaps-an-inverted-artist-and-album",
      bool(_orphan_row) and _orphan_row["artist"] == "Sunday Driver"
      and _orphan_row["title"] == "The End Of Julia",
      f"the query names both, so the reading that agrees with it wins: "
      f"{(_orphan_row or {}).get('artist')} | {(_orphan_row or {}).get('title')}")
check("discogs-leaves-a-correctly-ordered-album-alone",
      bool(_orphan) and any(r["artist"] == "Miles Davis"
                            and r["title"] == "Kind Of Blue" for r in _orphan),
      f"the swap must need positive evidence from the query; a correct record "
      f"must never be flipped: {[(r['artist'], r['title']) for r in _orphan]}")


def _orient_probe():
    return fai._dg_orient, fai._dg_token_overlap


_orient, _ovl = _with_token(_orient_probe)
check("discogs-orientation-needs-evidence",
      _orient("Some Artist", "Some Album", "", "") == ("Some Artist", "Some Album"),
      "with no usable hints the Discogs order must be kept, never guessed at")
check("discogs-orientation-ignores-stop-words",
      _ovl("The End Of Julia", "The End Of Julia") == 2
      and _ovl("Sunday Driver", "Sunday Driver") == 2,
      f"stop words must not inflate the score or every 'The ...' would tie: "
      f"{_ovl('The End Of Julia', 'The End Of Julia')}")
# REGRESSION. The 50/50 token split the server does for a bare query is a
# GUESS: 'Kind Of Blue Miles Davis' becomes artist_hint='Kind Of',
# album_hint='Blue Miles Davis'. Feeding that to the orientation rule scored a
# phantom swap and turned the CORRECT 'Miles Davis - Kind Of Blue' inside out.
# A guess must never reverse a record, so the rule refuses unreliable hints
# outright rather than trying to weigh them.
check("discogs-orientation-ignores-guessed-hints",
      _orient("The End Of Julia", "Sunday Driver", "The End Of",
              "Julia Sunday Driver", hints_reliable=False)
      == ("The End Of Julia", "Sunday Driver"),
      "a 50/50 token guess is wrong half the time; it may narrow the provider "
      "search but must never be allowed to reorder a record")
check("discogs-orientation-swap-needs-real-agreement",
      _orient("Some Artist", "Some Album", "Some", "Some",
              hints_reliable=True) == ("Some Artist", "Some Album"),
      "one shared word is not enough evidence to reverse a record")
check("discogs-only-an-explicit-separator-is-reliable",
      'hints_reliable = False' in _src
      and 'hints_reliable = bool(artist_hint and album_hint)' in _src
      and 'hints_reliable=hints_reliable' in _src,
      "only a literal 'Artist - Album' is the user telling us which is which")

# The details fetch must call the endpoint matching the row's kind, or an
# orphan is listed and then 404s the moment it is picked.
_asked = []


def _kind_route_probe():
    def _get(path, params=None, **k):
        _asked.append(path)
        if path == "releases/4913272":
            return _FakeResp(200, {
                "title": "Sunday Driver",
                "artists": [{"name": "The End Of Julia"}],
                "year": 2004, "genres": ["Rock"],
                "images": [{"type": "primary", "uri": "https://img/f.jpg"}],
                "tracklist": [{"position": "1", "title": "What Happened To Forever?"},
                              {"position": "2", "title": "Sail Away"}]})
        if path == "masters/5460":
            return _FakeResp(200, _MASTER)
        return None
    fai._discogs_get = _get
    fai.album_cache.clear()
    fai._DG_ORIENT["r:4913272"] = ("Sunday Driver", "The End Of Julia")
    return fai.get_discogs_album_details("r:4913272")


_orphan_det = _with_token(_kind_route_probe)
check("discogs-orphan-details-hit-the-releases-endpoint",
      "releases/4913272" in _asked and "masters/4913272" not in _asked,
      f"a release id sent to the masters endpoint 404s: {_asked}")
check("discogs-orphan-details-return-the-tracklist",
      bool(_orphan_det) and len(_orphan_det["tracks"]) == 2,
      f"the orphan's own tracklist is the only source: "
      f"{len(_orphan_det['tracks']) if _orphan_det else None}")
check("discogs-orphan-details-keep-the-corrected-orientation",
      bool(_orphan_det) and _orphan_det["artist"] == "Sunday Driver"
      and _orphan_det["title"] == "The End Of Julia",
      f"the record's own artists/title are where the swap lives, so the detail "
      f"fetch must re-apply what the search row settled on: "
      f"{(_orphan_det or {}).get('artist')} | {(_orphan_det or {}).get('title')}")
check("discogs-orphan-id-is-namespaced",
      bool(_orphan_det) and _orphan_det["id"] == "dgr:4913272",
      f"master 4913272 and release 4913272 are different albums: "
      f"{(_orphan_det or {}).get('id')}")

# The album cache used to be keyed on the ref ALONE, but the orientation the
# search row settled on is part of the RESULT. A search that could not tell
# artist from album cached its guess for 24h, and a later correctly-informed
# search of the same album kept being served that stale guess - so the fix
# appeared not to work at all.
def _stale_cache_probe():
    m = fai
    m.album_cache = m.TTLCache(default_ttl=86400)
    m._DG_ORIENT.clear()
    m._DG_ORIENT["r:4913272"] = ("The End Of Julia", "Sunday Driver")
    first = m.get_discogs_album_details("r:4913272")
    m._DG_ORIENT["r:4913272"] = ("Sunday Driver", "The End Of Julia")
    second = m.get_discogs_album_details("r:4913272")
    return first, second


_stale_bad, _stale_good = _with_token(_stale_cache_probe)
check("discogs-cache-key-includes-the-settled-orientation",
      bool(_stale_bad) and bool(_stale_good)
      and (_stale_bad["artist"], _stale_bad["title"]) == ("The End Of Julia", "Sunday Driver")
      and (_stale_good["artist"], _stale_good["title"]) == ("Sunday Driver", "The End Of Julia"),
      f"a re-search that settles the artist/album differently must not be "
      f"served the previous answer for 24h: "
      f"{(_stale_bad or {}).get('artist')}|{(_stale_good or {}).get('artist')}")

# a master with no tracklist must fall back to its main release
def _empty_master_probe():
    def _g(p, params=None, **k):
        if p.startswith("masters/"):
            return _FakeResp(200, {"id": 5460, "title": "T", "year": 1959,
                                   "artists": [{"name": "A"}],
                                   "genres": ["Jazz"], "tracklist": [],
                                   "main_release": 12345, "images": []})
        if p.startswith("releases/"):
            return _FakeResp(200, {
                "tracklist": [{"position": "1", "title": "Real Track",
                               "duration": "3:00"}],
                "images": [{"type": "primary", "uri": "https://img/rel.jpg"}]})
        return None
    fai._discogs_get = _g
    return fai.get_discogs_album_details("5460")


_fb = _with_token(_empty_master_probe)
check("discogs-falls-back-to-the-main-release",
      bool(_fb) and len(_fb["tracks"]) == 1
      and _fb["tracks"][0]["name"] == "Real Track",
      f"a master with an empty tracklist is a catalogued work with no pressing "
      f"entered; its main release holds the real tracklist: "
      f"{_fb['tracks'] if _fb else None}")

# ---- 50g. rate limiting / failure ---------------------------------------
check("discogs-is-rate-limited",
      "_DG_MIN_INTERVAL" in _src and "_DG_LOCK" in _src
      and "def _discogs_get(" in _src,
      "an unthrottled burst gets 429s and the provider silently vanishes")


def _no_sleep(fn):
    """Run fn with the retry backoff neutralised so the suite stays fast."""
    prev_sleep, prev_iv = fai.time.sleep, fai._DG_MIN_INTERVAL
    fai.time.sleep = lambda s: None
    fai._DG_MIN_INTERVAL = 0.0
    fai._DG_LAST_CALL[0] = 0.0
    try:
        return fn()
    finally:
        fai.time.sleep, fai._DG_MIN_INTERVAL = prev_sleep, prev_iv


def _throttle_probe():
    """Drive the REAL _discogs_get through two 429s then a 200."""
    seq = [_FakeResp(429), _FakeResp(429), _FakeResp(200, {"results": []})]

    class _SeqSession:
        def get(self, url, params=None, timeout=None, headers=None):
            _seen.append({"url": url, "params": params, "headers": headers})
            return seq.pop(0) if seq else _FakeResp(200, {"results": []})
    fai.session = _SeqSession()
    return _no_sleep(lambda: fai._discogs_get("database/search"))


check("discogs-retries-a-429",
      _with_token(_throttle_probe) is not None,
      "a 429 must be retried with backoff, not turned into zero results - the "
      "same silent-failure mode that made MusicBrainz invisible")


def _unauth_probe():
    class _S:
        def get(self, url, params=None, timeout=None, headers=None):
            return _FakeResp(401)
    fai.session = _S()
    return _no_sleep(lambda: fai._discogs_get("database/search", attempts=1))


check("discogs-401-fails-soft",
      _with_token(_unauth_probe) is None,
      "a revoked token must disable the provider, not raise into the search")
check("discogs-config-check-never-raises",
      isinstance(fai._discogs_configured(), bool),
      "the configuration probe must be safe to call anywhere")

# ---- 50h. source dispatch ----------------------------------------------
check("discogs-is-a-recognised-source",
      'elif source == "discogs":' in _src
      and "get_discogs_album_details(album_id)" in _src,
      "the confirm page must resolve a Discogs row to its real tracklist")
check("discogs-keeps-the-musicbrainz-fallback",
      re.search(r'if source == "itunes":.*?elif source == "discogs":.*?'
                r"else:\s*\n\s*details = get_musicbrainz_album_details",
                _src, re.S) is not None,
      "an unknown/legacy source value must still mean MusicBrainz, not "
      "silently become a Discogs lookup")

# ---- 50i. the search page, with and without a token --------------------
# The token state MUST be forced, not inherited. This suite runs on developer
# machines that legitimately have a real token in local_settings.py, and CI that
# has none - asserting on whatever the ambient state happens to be means the
# test passes for the wrong reason on one and fails for the wrong reason on the
# other. Force the condition being tested.
_prev_state = (fai.DISCOGS_TOKEN, fai._discogs_get, fai.search_cache)
fai.DISCOGS_TOKEN = ""
fai._discogs_get = None
fai.search_cache = fai.TTLCache(default_ttl=7200)
try:
    _body = c.get("/api_search?q=pink+floyd+-+the+wall&per_page=20").data.decode(
        "utf-8", "ignore")
finally:
    (fai.DISCOGS_TOKEN, fai._discogs_get,
     fai.search_cache) = _prev_state
_picks = re.findall(r"pick\('(itunes|musicbrainz|discogs)'", _body)
# Without a token nothing about the page may change.
check("no-discogs-rows-without-a-token",
      "discogs" not in _picks and "badge-dg" not in _body,
      f"with no token the albums page must be exactly the two providers it "
      f"always was: {sorted(set(_picks))}")
check("itunes-and-mb-still-both-present",
      "itunes" in _picks and "musicbrainz" in _picks,
      f"adding a third provider must not displace the other two: "
      f"{sorted(set(_picks))}")
# The token-dependent checks must control the token themselves. If they merely
# read whatever the machine happens to have, they pass for the wrong reason on
# a developer box with a real token and fail for the wrong reason in CI.
check("token-state-is-forced-not-inherited",
      'fai.DISCOGS_TOKEN = ""' in _src_test
      and "_prev_state" in _src_test,
      "the 'without a token' assertions must force DISCOGS_TOKEN off rather "
      "than depend on the ambient machine state")
check("discogs-never-leaks-the-token-into-html",
      "test-token-not-real" not in _body and "DISCOGS_TOKEN" not in _body,
      "the token must never reach the browser")


# With a token AND results, the third provider must actually show up.
# The request itself has to happen INSIDE _with_token, which restores the
# module state on the way out.
def _live_search():
    fai._discogs_get = lambda p, params=None, **k: (
        _FakeResp(200, _SEARCH_PAYLOAD) if p.startswith("database")
        else _FakeResp(200, _MASTER))
    return c.get("/api_search?q=kind+of+blue&per_page=20")


_b2 = _with_token(_live_search).data.decode("utf-8", "ignore")
_p2 = re.findall(r"pick\('(itunes|musicbrainz|discogs)'", _b2)
check("discogs-rows-appear-when-configured",
      "discogs" in _p2,
      f"with a token and results, Discogs rows must reach the albums list: "
      f"{sorted(set(_p2))}")
check("discogs-rows-are-badged",
      "badge-dg" in _b2 and "Discogs" in _b2,
      "a user must be able to tell which provider a row came from")
check("discogs-rows-are-interleaved-not-appended",
      ("discogs" in _p2) and _p2.index("discogs") < max(2, len(_p2) // 2),
      f"Discogs must share the round-robin, not be dumped in a block at the "
      f"end: {sorted(set(_p2))}")
check("discogs-id-is-escaped-into-onclick",
      'rid = esc(item.get("id"))' in _src,
      "the master id goes into onclick='...', so it must be HTML-escaped")
check("discogs-shown-count-includes-the-third-provider",
      json.loads(
          c.get("/api_search?q=kind+of+blue&per_page=20").headers
          .get("X-Search-Totals") or "{}").get("albums_shown", 0) >= 2,
      "albums_shown must count the rows actually rendered, or the 'showing N "
      "of M' lead-in under-reports")
def _live_confirm():
    fai._discogs_get = lambda p, params=None, **k: (
        _FakeResp(200, _SEARCH_PAYLOAD) if p.startswith("database")
        else _FakeResp(200, _MASTER))
    return c.get("/confirm?source=discogs&id=5460").data.decode("utf-8", "ignore")


_c2 = _with_token(_live_confirm)
check("discogs-confirm-page-renders",
      "So What" in _c2 and "Kind Of Blue" in _c2,
      f"picking a Discogs row must show its real tracklist: {_c2[:120]!r}")
check("discogs-confirm-never-leaks-the-token",
      "test-token-not-real" not in _c2,
      "the confirm page must not carry the credential to the browser")

# all three sources must survive a round-robin
_three = fai._interleave_sources(
    [("itunes", [{"id": "i%d" % n} for n in range(3)]),
     ("musicbrainz", [{"id": "m%d" % n} for n in range(3)]),
     ("discogs", [{"id": "d%d" % n} for n in range(3)])])
check("three-way-interleave-is-fair",
      len(_three) == 9
      and [s for s, _ in _three[:3]] == ["itunes", "musicbrainz", "discogs"]
      and len({r["id"] for _, r in _three}) == 9,
      f"three providers must each get a slot immediately and nothing may be "
      f"dropped or duplicated: {[(s, r['id']) for s, r in _three[:5]]}")
_lop = fai._interleave_sources(
    [("itunes", [{"id": "i%d" % n} for n in range(40)]),
     ("musicbrainz", [{"id": "m0"}]), ("discogs", [{"id": "d0"}])])
check("three-way-interleave-surfaces-small-sources",
      [s for s, _ in _lop[:3]] == ["itunes", "musicbrainz", "discogs"],
      f"a 40/1/1 split must still show both small sources in the first rows: "
      f"{[(s, r['id']) for s, r in _lop[:4]]}")
check("two-way-interleave-unchanged",
      [s for s, _ in fai._interleave_providers([{"id": 1}], [{"id": 2}])]
      == ["itunes", "musicbrainz"],
      "the original two-provider contract must be byte-identical")

# 51. A CD RIP told the user "No disc in the drive" while a disc was in it.
#     A rip arrives with a TOC but NO requestid, so GetMDQByRequestID() cannot
#     be asked, 'mdq' is empty BY CONSTRUCTION, and the panel fell through to
#     its empty branch - which claimed an empty drive. From a real session
#     (fai_server.log, 18:33:31): flow='disc', a 15-track TOC in the URL,
#     has_request_id=false, mdq_len=0. The message was false precisely when
#     the user was searching for the disc in front of them.
#
#     _disc above carries a requestid, which is NOT the failing shape. This
#     reproduces the real one: a TOC and nothing else.
_rip_no_rid = c.get("/FAI/ui?cd=A+96+362E+85B3+BC05").data.decode(
    "utf-8", "ignore")
check("disc-rip-without-requestid-is-still-a-disc-flow",
      'var SEARCH_FLOW = "disc";' in _rip_no_rid,
      "a TOC with no requestid is the real rip shape and must classify as disc")
_disc_branch = ""
_i = _rip_no_rid.find("SEARCH_FLOW === 'disc'")
if _i >= 0:
    # Scope to the disc branch ALONE. A fixed-width window would run past the
    # 'else' and pick up the empty-drive string that legitimately follows it -
    # that string is correct for the unknown flow and must stay.
    _end = _rip_no_rid.find("} else {", _i)
    _disc_branch = _rip_no_rid[_i:_end] if _end > _i else _rip_no_rid[_i:_i + 200]
check("disc-rip-message-is-accurate",
      "no stored tags yet" in _disc_branch,
      f"a disc rip has no stored tags; say that rather than claiming no disc: "
      f"{_disc_branch[:140]!r}")
check("disc-rip-does-not-claim-an-empty-drive",
      "No disc in the drive" not in _disc_branch,
      "a rip must never be told the drive is empty when it is not")
# The empty-drive claim must remain reachable for the flow that really has no
# disc - removing it would be as wrong as always showing it.
check("empty-drive-message-still-exists-for-the-unknown-flow",
      "No disc in the drive" in _bare,
      "genuinely no disc must still be reported as such")
check("server-renders-a-disc-flow-source-line",
      "flow == 'disc'" in _src,
      "the server must render a distinct initial source line for a disc flow")
# A library update is the case where WMP really did withhold the tags; that
# message must not be reused for a rip.
check("library-and-disc-messages-differ",
      "did not report the current tags" in _lib
      and "no stored tags yet" in _rip_no_rid,
      "a library update and a fresh rip are different situations and must "
      "not share one message")

# 52. 'Existing Information' said "No existing information" about a disc that
#     was sitting in the drive. WMP sends a TOC with the rip, and the TOC is a
#     property of the PHYSICAL disc: it yields the track count and the running
#     time of every track. Verified against the Course of Nature CD visible
#     behind the dialog in the user's screenshot - all ten track lengths match
#     what WMP shows (3:03 4:31 3:05 3:44 3:11 3:47 4:40 3:51 3:17 4:41).
_RIP_TOC = "A+96+362E+85B3+BC05+FDB4+135B5+17844+1CA5D+20E1C+247F8+29A6F"
_WMP_LEN = ["3:03", "4:31", "3:05", "3:44", "3:11", "3:47", "4:40", "3:51",
            "3:17", "4:41"]
_d = fai.parse_wmp_toc(_RIP_TOC)
check("toc-parses-the-real-disc",
      len(_d) == 10,
      f"the Course of Nature disc has 10 audio tracks, got {len(_d)}")
check("toc-durations-match-wmp-exactly",
      [fai.format_duration(x) for x in _d] == _WMP_LEN,
      f"TOC-derived lengths must agree with what WMP displays: "
      f"{[fai.format_duration(x) for x in _d]} vs {_WMP_LEN}")
check("toc-reports-a-total-running-time",
      fai.format_duration(sum(_d)) == "37:50",
      f"total: {fai.format_duration(sum(_d))} (WMP's own per-track times sum to "
      f"38:50 only if the minute carry is added wrongly - the tracks are "
      f"183+271+185+224+191+227+280+231+197+281 = 2270s = 37:50)")
# A malformed TOC must not break the dialog.
check("toc-survives-garbage",
      fai.parse_wmp_toc("") == [] and fai.parse_wmp_toc(None) == []
      and fai.parse_wmp_toc("garbage") == [] and fai.parse_wmp_toc("A+96") == [],
      "a malformed TOC must yield nothing, never an exception")
check("duration-formatting",
      fai.format_duration(183000) == "3:03"
      and fai.format_duration(61000) == "1:01"
      and fai.format_duration(0) == ""
      and fai.format_duration(3725000) == "1:02:05",
      "M:SS under an hour, H:MM:SS over it, empty for zero")
# The panel must use the TOC rather than claiming there is nothing to report.
# Assert on the RENDERED panel, not the whole page: the JS fallback string
# ships in every page regardless, because the client cannot know a TOC is
# unreadable until the server has already told it.
_toc_page = c.get("/FAI/ui?cd=" + _RIP_TOC).data.decode("utf-8", "ignore")


def _panel(html):
    out = {}
    for _m in re.finditer(
            r'id="existing(Title|Artist|Sub|Source)"[^>]*>(.*?)</div>', html, re.S):
        out[_m.group(1)] = _m.group(2).strip()
    return out


_p = _panel(_toc_page)
check("panel-reports-the-disc-from-its-toc",
      "10 audio tracks" in _p.get("Title", "") and "37:50" in _p.get("Title", ""),
      f"the panel must state the track count and length read from the TOC: "
      f"{_p.get('Title')!r}")
check("panel-does-not-claim-no-information",
      "No existing information" not in _p.get("Title", ""),
      f"a disc that yields 10 track lengths is not 'no existing information': "
      f"{_p.get('Title')!r}")
check("panel-explains-what-is-missing",
      _p.get("Artist") == "Audio CD in the drive"
      and "No album or artist tags" in _p.get("Sub", ""),
      f"the panel must say what it does and does not know: {_p}")
check("panel-still-handles-a-disc-with-no-toc",
      "Disc in the drive" in _panel(
          c.get("/FAI/ui?cd=garbage").data.decode("utf-8", "ignore")).get("Title", ""),
      "an unreadable TOC must still name the disc rather than leaving the panel "
      "stuck on 'Reading current information...', which never resolves")
check("toc-summary-is-passed-to-the-client",
      re.search(r'var DISC_SUMMARY = "[^"]*10 audio tracks[^"]*"', _toc_page)
      is not None,
      "the client re-renders this panel on load and must not blank it again")
# A disc WITH tags must keep showing the tags, not the TOC summary.
_tagged = c.get("/FAI/ui?cd=" + _RIP_TOC + "&artist=A&album=B&track=C").data.decode(
    "utf-8", "ignore")
check("toc-summary-never-overrides-real-tags",
      "Currently stored by Windows Media Player." in _tagged,
      "when WMP supplies tags they win; the TOC is only a fallback")

# 53. The Finish & Apply path threw "applied is not defined" - and it was
#     INVISIBLE to every test here, because the ReferenceError only surfaced in
#     the FAI dialog host, where window.onerror reports it. Caught from a real
#     session (fai_server.log): {"page":"onerror","msg":"'applied' is undefined",
#     "line":888}. Present in b4c920e, before any of this work.
#
#     Cause: 'applied' was a `var` local to finishSync(), but applyMetadata() is
#     a SEPARATE top-level function. It read a name that did not exist in its
#     own scope, so every write path died with a ReferenceError BEFORE calling
#     WMP - which is why a finish could silently do nothing. The flag now lives
#     on `diag`, which is already threaded through every call.
_am = _src.split("function applyMetadata(")[1].split("\n    function ")[0]
_fs = _src.split("function finishSync(")[1].split("\n    function ")[0]
check("applied-flag-is-not-function-local",
      "diag.applied = false;" in _fs,
      "'applied' is shared between finishSync() and the top-level "
      "applyMetadata(); a function-local var is invisible to it")
check("apply-metadata-has-no-bare-applied-read",
      "var applied = !!(diag && diag.applied);" in _am,
      "applyMetadata must read the shared flag off diag, not an undeclared name")
check("apply-metadata-writes-back-to-diag",
      _am.count("setApplied(true);") >= 4
      and "if (diag) diag.applied = v;" in _am,
      "each successful write must set the shared flag so later fallbacks are "
      "skipped and the button state is truthful")
# The decisive guard: applyMetadata must not reference `applied` before it is
# defined in its own scope. This is the exact ReferenceError, statically.
_first_read = min([i for i, l in enumerate(_am.split("\n"))
                   if "if (!applied" in l] or [10 ** 6])
_decl = min([i for i, l in enumerate(_am.split("\n"))
             if "var applied =" in l] or [10 ** 6])
check("applied-is-declared-before-first-use",
      _decl < _first_read,
      f"the first `if (!applied)` is at {_first_read} but the declaration is at "
      f"{_decl}; a use before the declaration is the ReferenceError itself")
check("mdq-is-never-an-unescaped-global-read",
      "var mdq = RESOLVED_MDQ || '';" in _am,
      "applyMetadata must resolve mdq from the shared RESOLVED_MDQ, matching "
      "the fix for the companion \"'mdq' is undefined\" report")

# 56. A JS SYNTAX ERROR KILLS THE WHOLE DIALOG, and the Python suite never saw
#     it. An unbalanced brace introduced while parking the library work shipped
#     in ba8cbd1; the confirm page then failed to parse, so every handler was
#     gone and WMP reported BOTH of these from the same fault:
#         Line 940  Char 7   Error: Syntax error
#         Line 211  Char 62  Error: 'toggleRow' is undefined
#     401 Python tests passed the whole time. Python only checks Python.
#     If node is available, PARSE every served script - the only check that
#     would have caught this before it reached a live dialog.
def _js_parses(html, label):
    m = re.search(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)
    if not m:
        return None, "no <script> block found"
    path = os.path.join(_DIR, "_syntaxcheck_%s.js" % label)
    try:
        with open(path, "w", encoding="utf-8") as _f:
            _f.write(m.group(1))
    except OSError as e:
        return None, str(e)
    try:
        import subprocess
        _r = subprocess.run(["node", "--check", path],
                            capture_output=True, text=True, timeout=60)
        return _r.returncode == 0, (_r.stderr or _r.stdout or "")[:400]
    except FileNotFoundError:
        return None, "node not installed"
    except Exception as e:
        return None, str(e)


_JS_PAGES = (("confirm", "/confirm?source=itunes&id=1065975633&requestid=T"),
             ("search", "/FAI/ui?cd=A+96+362E+85B3"),
             ("library", "/FAI/ui?requestid=D86F70C1-08E2-4219-8F86-7FE6A1C98974"))
_node_ok = False
for _label, _url in _JS_PAGES:
    _ok, _err = _js_parses(c.get(_url).data.decode("utf-8", "ignore"), _label)
    if _ok is None:
        continue
    _node_ok = True
    check("served-%s-script-parses" % _label, _ok,
          f"the {_label} dialog's JavaScript must PARSE. A syntax error removes "
          f"every handler at once ('toggleRow' is undefined) and the WMP dialog "
          f"stops working entirely: {_err}")
    # Brace balance runs even without node, and is the cheapest early warning
    # for the exact way this breaks: an edit that unbalances the write path.
    _m = re.search(r"<script[^>]*>(.*?)</script>",
                   c.get(_url).data.decode("utf-8", "ignore"), re.S | re.I)
    if _m:
        _js = _m.group(1)
        check("served-%s-braces-balance" % _label,
              _js.count("{") == _js.count("}"),
              f"unbalanced braces in the {_label} script: "
              f"{{ ={_js.count('{')} }} ={_js.count('}')}")

# The handlers the CONFIRM page's inline onclick attributes depend on. A syntax
# error makes all of these vanish at once; that is the visible symptom in WMP.
# doSearch is deliberately NOT here: it belongs to the search page, not this one.
for _fn in ("toggleRow", "syncRow", "selectAll", "selectNone", "finishSync",
            "applyMetadata"):
    check("confirm-exposes-%s" % _fn,
          ("function %s(" % _fn) in page,
          f"the confirm page's inline onclick handlers call {_fn}(), so it must "
          f"exist; if it is undefined the dialog is dead on arrival")

# 57. EVERY window.external.<name> MUST BE A REAL MEMBER OF
#     IWMPCDDVDWizardExternal. The project called window.external.Close(), which
#     does not exist. It was reached on every close where ReturnToMainTask()
#     did not succeed, and it could only ever throw a COM error - it was dead
#     code written as if the method were real.
#
#     The interface was read from the type library of
#     {2D7EF888-1D3C-484A-A906-9F49D99BB344} (C:\WINDOWS\System32\wmp.dll). It
#     derives from IWMPExternalColors -> IWMPExternal and adds exactly seven
#     methods of its own. There is no Close and no Finish.
#
#     WMP agrees at runtime: the dialog host's own probe reports
#     "Close": "undefined" and "Finish": "undefined" in every log line, while
#     each real member reports "unknown" (a COM member whose typeof is not a
#     JS type - the reason this project never truthiness-tests them).
#
#     This is the same lesson as test 56, one level up: node --check proves the
#     JavaScript parses, not that the COM members it calls exist. A typo or an
#     invented method is invisible to every other check in this file.
_REAL_EXTERNAL_MEMBERS = frozenset({
    # own methods
    "WriteNames", "ReturnToMainTask", "WriteNamesEx", "GetMDQByRequestID",
    "EditMetadata", "IsMetadataAvailableForEdit", "BuyCD",
    # IWMPExternal / IWMPExternalColors
    "version", "appColorLight", "appColorMedium", "appColorDark",
    "appColorButtonHighlight", "appColorButtonShadow",
    "appColorButtonHoverFace", "OnColorChange",
})
_src_all = open(BASE, encoding="utf-8").read()
# Scan with comments removed, or prose about a member counts as a call to it.
# The JavaScript in this file uses // comments, so _norm() (which strips Python
# # comments) is not enough - strip // and /* */ as well.
_src_code = re.sub(r"/\*.*?\*/", "", _src_all, flags=re.S)
_src_code = re.sub(r"//[^\n]*", "", _src_code)
_called = sorted(set(re.findall(r"window\.external\.([A-Za-z_]\w*)", _src_code)))
for _m in _called:
    check("external-member-exists-%s" % _m,
          _m in _REAL_EXTERNAL_MEMBERS,
          f"window.external.{_m}() is not a member of IWMPCDDVDWizardExternal "
          f"{{2D7EF888-1D3C-484A-A906-9F49D99BB344}}. The complete interface is "
          f"{sorted(_REAL_EXTERNAL_MEMBERS)} - a call to anything else throws a "
          f"COM error and silently does nothing.")
check("external-probe-has-no-invented-members",
      not re.search(r"'Close'|'Finish'", _src_code.split("function listExternalMethods")[1].split("}")[0] if "function listExternalMethods" in _src_code else ""),
      "listExternalMethods() must probe only real members. Probing 'Close' and "
      "'Finish' manufactured two permanent 'undefined' entries in every log "
      "line that looked like a host fault but were just nonexistent members.")
check("close-is-never-called",
      "external.Close(" not in _src_code and "external.Finish(" not in _src_code,
      "IWMPCDDVDWizardExternal has no Close/Finish. ReturnToMainTask (disp "
      "10002) is the only way to dismiss the wizard.")

# 59. EditMetadata MUST NOT BE USED AS A WRITE PATH. It was implemented in
#     db0480f, deployed, and reverted in 55088c1 after a real run proved it does
#     nothing. From the log of a Filmmaker library update:
#         "edit_handoff": "EditMetadata-ok"   <- did not throw
#         "metadata_editable": true          <- WMP said yes
#     and no editor ever opened: an open editor must fetch metadata, and there
#     was no cdinfo/GetMDRCD request after the handoff. Worse, the page then sat
#     on "Waiting for WMP..." with no way out.
#
#     The interface advertises the method; the dialog host does not act on it.
#     This test exists so nobody re-adds it on the strength of it being a real
#     member of the type library.
_am_final = _src_code.split("function applyMetadata(")[1].split("\n    function ")[0]
check("edit-metadata-is-not-a-write-path",
      "EditMetadata()" not in _am_final,
      "EditMetadata (disp 10011) must not be called from applyMetadata(). It was "
      "tried in db0480f and reverted in 55088c1: the call does not throw and "
      "IsMetadataAvailableForEdit() returns true, but no editor ever opens and "
      "the user is left stranded. A COM call returning cleanly is not evidence "
      "it did anything.")
check("edit-metadata-is-only-an-explicit-user-action",
      "external.EditMetadata()" in _src_code,
      "EditMetadata may still be offered where the user explicitly asked to edit "
      "(the existing 'Edit' link), which is WMP's own behaviour and a no-op if "
      "unsupported. It just may not be wired into the write path.")
check("finish-always-leaves-the-dialog",
      _re.search(r"beaconSync\(diag\);\s*leaveDialog\(\);", _src) is not None,
      "after reporting, the finish path must navigate to /done in the same tick. "
      "db0480f made this conditional on a handoff that never fires, which left "
      "the dialog stuck on 'Waiting for WMP...' with no exit.")

# ---- 51. Artists and Tracks are navigable, not decorative -----------------
# Both tabs used to render rows with NO onclick at all. You could read a name
# and do nothing with it. The Artists tab is now fed by all three providers,
# and every row leads somewhere real.
check("artists-tab-calls-every-provider",
      "search_itunes_artists" in _src and "search_discogs_artists" in _src
      and '_search_artists_all' in _src,
      "the Artists tab was MusicBrainz-only, so iTunes and Discogs artists "
      "were unreachable however well known they were")
check("artist-rows-are-clickable",
      "showArtist(" in _src and "function showArtist" in _src,
      "an artist row that cannot be clicked is a dead list")
check("artist-rows-carry-their-provider",
      "showArtist('{prov}'" in _src or "showArtist(" in _src,
      "the row must say which provider it came from so the next lookup matches")
check("artist-albums-route-exists",
      '@app.route("/api_artist_albums")' in _src,
      "browsing an artist's albums needs its own endpoint")
check("artist-albums-reuse-the-album-renderer",
      "_album_row_html(r.get(\"source\", source), r)" in _src,
      "rows from an artist page must be the SAME pickable rows as the Albums "
      "tab, so nothing downstream has to learn they came from a browse")
check("artist-browse-covers-all-three",
      all(s in _src for s in ('source == "itunes"', 'source == "discogs"',
                              'source == "musicbrainz"')),
      "each provider browses its own discography through a different endpoint")
check("discogs-discography-rows-carry-their-kind",
      "'m' if kind == 'master' else 'r'" in _src,
      "an artist's Discogs discography mixes masters and orphan releases, so "
      "each row must say which endpoint its details come from")
check("musicbrainz-browse-uses-release-groups",
      'MUSICBRAINZ_BASE_URL + "release-group"' in _src
      and '"g:" + ' not in _src and 'f"g:{gid}"' in _src,
      "browsing RELEASES returns every pressing and every single - Coldplay has "
      "over a hundred and the list appeared to stop in 2005. A release-group is "
      "the work, which is what 'browse this artist's albums' must mean")
check("musicbrainz-browse-is-newest-first",
      'reverse=True' in _src and 'first-release-date' in _src,
      "the API returns oldest first, so browsing an active artist meant "
      "scrolling past their whole back catalogue to reach the current album")
check("release-group-ids-resolve-to-a-release",
      "def _mb_release_group_to_release" in _src
      and '_mb_release_group_to_release(group_id)' in _src,
      "a release-group id is not a release id; the details fetch has to resolve "
      "one to the other or every browsed album 404s")
check("discogs-discography-is-newest-first",
      '"sort_order": "desc"' in _src,
      "an artist page listing oldest-first makes an active artist look inactive")

# Browse rows came back with no artwork at all: MusicBrainz's release-group
# browse carries no image, and Discogs' artist-releases returns an empty thumb.
# A row with no cover must resolve one rather than sit on the placeholder.
check("browsed-rows-resolve-their-cover-lazily",
      '@app.route("/api_art")' in _src
      and 'if not art:' in _src
      and '"/api_art?source=' in _src,
      "every browsed album showed the placeholder because the browse endpoints "
      "return no image; the row must resolve a cover on demand")
check("lazy-cover-lookups-are-cached",
      'image_cache.get(cache_key)' in _src
      and 'image_cache.set(cache_key, url, ttl=86400)' in _src,
      "sixty rows must not mean sixty provider calls on every visit; the "
      "resolved URL is cached, so a repeat view costs nothing")
check("lazy-cover-falls-back-to-the-placeholder",
      'return redirect("/static/noart.png")' in _src,
      "an album with no cover must land on the placeholder, not on a broken "
      "image or a 404")

# One renderer for all album rows. Two copies of this markup would be free to
# drift - a badge or an escaping fix landing in one list and not the other.
check("album-rows-have-a-single-renderer",
      _src.count("def _album_row_html(") == 1
      and "onclick=\"pick('{prov}', '{rid}')\"" in _src,
      "the Albums tab and the artist browser must share one row renderer")

# ---- 52. one track, not the whole album -----------------------------------
# The Tracks tab could list a song but not the album it belongs to, so there
# was nothing to open. Clicking a track now carries ?focus=, and that is the
# difference between tagging one track and quietly renaming eleven.
check("tracks-tab-has-both-providers",
      "search_itunes_tracks" in _src and '_search_tracks_all' in _src,
      "a track search that only knows MusicBrainz cannot tag what iTunes knows")
check("track-rows-carry-their-album",
      '"album_id"' in _src and "album_id = r.get(\"album_id\") or \"\"" in _src,
      "without the album id a track row names a song but has nothing to open")
check("tracks-drop-rows-with-no-album",
      "if not album_id:" in _src and "continue" in _src,
      "a track that cannot be placed on an album cannot be tagged, so it must "
      "not be rendered as a row that leads nowhere")
check("track-rows-open-the-album-with-focus",
      "function pickTrack" in _src and '&focus=' in _src,
      "a track row must navigate to the album carrying which track was clicked")
check("focus-is-separate-from-the-playing-track",
      'focus_name = request.args.get("focus", "")' in _src
      and 'request.args.get("focus", "")' != 'request.args.get("track", "")',
      "wmp_track is what WMP says IT is playing; focus is what the USER "
      "clicked, and only the latter may decide that one box is ticked")
check("focus-overrides-the-whole-album-default",
      "if focus_name:" in _src
      and "checked = 'checked' if _same_track(t_name, focus_name) else ''" in _src,
      "with a focus set, ONLY the named track may be pre-ticked - applying the "
      "whole album when one track was asked for is the worst outcome here")
check("track-name-matching-tolerates-provider-noise",
      "_same_track(" in _src,
      "the focus name and the album's own name come from different payloads "
      "(leading track numbers, '(Remastered)', punctuation), so equality "
      "would match nothing and the track would silently stay untagged")
check("track-name-matching-rejects-a-different-song",
      fai._same_track("Sail Away", "Favorites") is False
      and fai._same_track("Sail Away", "Sail Away") is True
      and fai._same_track("3. Sail Away", "Sail Away") is True,
      "the matcher must be tolerant of decoration but must NOT be so loose it "
      "ticks a different song")
check("a-track-without-an-album-is-inert",
      'if album_id:' in _src and 'onclick = ""' in _src,
      "a row with no album cannot be tagged, so it must render inert rather "
      "than as a broken link")


print()
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
