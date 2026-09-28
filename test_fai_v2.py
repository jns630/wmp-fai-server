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
ART = "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/52/aa/85/x.jpg/600x600bb.jpg"
xml_art = fai.build_wmp_xml(dict(album, art_url=ART), selected_tracks=[album["tracks"][0]])
m = re.search(r"<largeCoverParams>([^<]+)</largeCoverParams>", xml_art)
check("cover-url-not-double-encoded",
      m is not None and m.group(1).startswith("http://127.0.0.1/cover/album.jpg?url=https://"),
      f"cover url mangled: {m.group(1) if m else None}")

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
      "    return None" in _src.split("def _lookup_staged_xml(")[1][:2500],
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
      "window.location.href = \"/done\";" in page and "catch (e) {" in page,
      "the /done redirect must be guarded so the dialog cannot get stuck")
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
      "Found 500+ Album(s) containing" in ui_html,
      "the authentic blue lead-in sentence is missing")
check("fai-two-columns",
      'class="section-label">Existing Information' in ui_html
      and 'class="section-label">Search' in ui_html,
      "the 'Existing Information' / 'Search' column pair is missing")
check("fai-filter-strip",
      "Artists (500+)" in ui_html and "Albums (500+)" in ui_html
      and "Tracks (500+)" in ui_html,
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
      all(s.strip() in ("body",) for s in _flex_sel)
      and all(s.strip() in (".header-area", ".main-container", ".footer") for s in _flex1_sel),
      f"flexbox leaked into the inner layout: display:flex={_flex_sel} flex:1={_flex1_sel}")
check("fai-columns-are-floats",
      ".left-pane { float: left;" in _src and ".right-pane { margin-left: 48%;" in _src,
      "the two columns must be float based so IE7 lays them out")
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
      "gunicorn" in _render and "uvicorn" not in _render,
      "render.yaml must start gunicorn, not uvicorn")
check("render-targets-wsgi-shim",
      "wsgi:app" in _render,
      "render.yaml must target wsgi:app, not the space-containing module path")
check("render-single-worker",
      "WEB_CONCURRENCY" in _render and 'value: "1"' in _render,
      "in-memory staged XML must stay on a single worker")
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

print()
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
