# -*- coding: utf-8 -*-
"""
Tests for Windows Media Player 11 support.

WMP 11 was previously filed with WMC and WMP 7-9 as a "legacy XML client",
purely because it reports the NSPlayer/ prefix those players also report. That
was wrong, and the type library on an actual WMP 11 install says why: it carries
IWMPCDDVDWizardExternal under the SAME IID ({2D7EF888-...}) the dialog already
writes through, with WriteNames, WriteNamesEx, ReturnToMainTask,
GetMDQByRequestID, EditMetadata, BuyCD and IsMetadataAvailableForEdit all
present. It is a browser client and it can host the dialog.

So the fix is a version test, not a string test - and this file asserts both
halves of that, which is the whole risk of the change:

  1. WMP 11 (NSPlayer/11...) is recognised, gets the dialog, and gets relative
     artwork.
  2. NOTHING ELSE MOVES. WMP 12, WMC and WMP 7-9 must classify exactly as they
     did before, because a change that helped WMP 11 by pulling WMP 9 into the
     same branch would break the .asp-era clients.

(2) is the part that matters. Everything here is derived from the version
number in the User-Agent, so a wrong regex is indistinguishable from a right
one until it reclassifies a client nobody was testing.
"""
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

PASS = []
FAIL = []

#: The client families.
#:
#: WMP11 / WMP11_DIALOG are the REAL agents, read out of fai_server.log on this
#: machine. The first version of this file used "NSPlayer/11.0.0.5720", which was
#: an invented guess - and every assertion built on it passed while the feature
#: did nothing at all, because no real WMP 11 request ever carried that string.
#: These are the agents that actually appear in the log:
#:
#:   WMP11        the player's own service request
#:   WMP11_DIALOG the dialog it opens next, carrying an ordinary IE7 agent and
#:                ?version=11.0.6003.22165 - the player version in the query
WMP11 = "Windows-Media-Player/11.0.6003.22165"
WMP11_VERSION = "11.0.6003.22165"
WMP11_DIALOG = ("Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 6.2; WOW64; "
                "Trident/7.0; .NET4.0C; .NET4.0E; Media Center PC 6.0; "
                ".NET CLR 2.0.50727; .NET CLR 3.0.30729; .NET CLR 3.5.30729)")
WMP9 = "NSPlayer/9.0.0.4509"
WMP9_HYPHEN = "Windows-Media-Player/9.0.0.4509"
WMP12 = "WindowsMediaPlayer/12.0.19041.4046"
WMP12_HYPHEN = "Windows-Media-Player/12.0.26100.9549"
WMP12_SHORT = "WindowsMediaPlayer/12"
WMP12_VERSION = "12.0.26100.9549"
WMP12_WIN7 = "WindowsMediaPlayer/12.0.7601.17514"
WMC = "Windows-Media-Center/6.0.8002.0"
IE7 = "Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 6.1)"

TOC = "B%2B96%2B43DA%2B71A4%2B105D1%2B15498"


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} :: {detail}")


def load_server(tag):
    """Import a fresh, isolated copy of the server module."""
    spec = importlib.util.spec_from_file_location(
        "fai_wmp11_" + tag, os.path.join(_DIR, "FAI Server.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fai = load_server("main")

# ======================================================================
# 1. The wizard external interface really is the same on both generations.
#    These are the facts the whole change rests on; if the constants drift
#    from what the type library was read to contain, the change is wrong.
# ======================================================================
check("the wizard external IID is the one the dialog already uses",
      fai.WMP_WIZARD_EXTERNAL_IID == "{2D7EF888-1D3C-484A-A906-9F49D99BB344}",
      fai.WMP_WIZARD_EXTERNAL_IID)

for _m in ("WriteNames", "WriteNamesEx", "ReturnToMainTask",
           "GetMDQByRequestID", "EditMetadata", "BuyCD",
           "IsMetadataAvailableForEdit"):
    check(f"{_m} is a member shared by WMP 11 and WMP 12",
          _m in fai.WMP11_EXTERNAL_MEMBERS)

check("RenameRegroupFiles is recorded as WMP 12 only",
      "RenameRegroupFiles" in fai.WMP12_ONLY_EXTERNAL_MEMBERS
      and "RenameRegroupFiles" not in fai.WMP11_EXTERNAL_MEMBERS)

# ======================================================================
# 2. Recognition is by VERSION, and only WMP 11 is recognised.
# ======================================================================
check("WMP 11 is recognised", fai._is_wmp11_client(WMP11) is True)
check("WMP 9 is NOT recognised as WMP 11", fai._is_wmp11_client(WMP9) is False)
check("WMP 12 is NOT recognised as WMP 11", fai._is_wmp11_client(WMP12) is False)
check("WMC is NOT recognised as WMP 11", fai._is_wmp11_client(WMC) is False)
check("a browser is NOT recognised as WMP 11",
      fai._is_wmp11_client(IE7) is False)
check("an empty User-Agent is NOT recognised as WMP 11",
      fai._is_wmp11_client("") is False)

# A bare "NSPlayer/" with no version must not be guessed into WMP 11: a
# truncated agent is more likely than a claim about the player.
check("NSPlayer with no version is NOT guessed into WMP 11",
      fai._is_wmp11_client("NSPlayer/") is False)
check("NSPlayer with a non-numeric version is NOT guessed into WMP 11",
      fai._is_wmp11_client("NSPlayer/vista") is False)

# Case-insensitivity: the agent arrives from the client, not from us.
check("the agent match is case-insensitive",
      fai._is_wmp11_client("nsplayer/11.0.0.5720") is True)

# ======================================================================
# 3. WMP 11 gets the DIALOG. It is a browser client - this is the fix.
# ======================================================================
check("WMP 11 is a dialog host client",
      fai._is_dialog_host_client(WMP11) is True)
check("WMP 11 is NOT a browser-less legacy XML client",
      fai._is_legacy_xml_client(WMP11) is False)

# ======================================================================
# 4. WMP 7-9 and WMC keep exactly what they had. If any of these move, the
#    .asp-era clients break and the change is a net regression.
# ======================================================================
check("WMP 9 is still refused the dialog",
      fai._is_dialog_host_client(WMP9) is False)
check("WMP 9 is still a legacy XML client",
      fai._is_legacy_xml_client(WMP9) is True)
check("WMC is still refused the dialog",
      fai._is_dialog_host_client(WMC) is False)
check("WMC is still a legacy XML client",
      fai._is_legacy_xml_client(WMC) is True)
check("WMP 12 is still not a legacy XML client",
      fai._is_legacy_xml_client(WMP12) is False)
check("a plain browser still gets the dialog",
      fai._is_dialog_host_client(IE7) is True)

# The WMP 12 BACKGROUND fetch announces itself as "WindowsMediaPlayer/12...",
# which is NOT a browser and must stay XML-only. It is the agent WMP uses when
# it fetches metadata on its own; the dialog itself is opened later by the IE
# control, which is the browser agent above. Both halves are asserted so a
# "fix" that simply made every WMP agent a dialog host would be caught.
check("the WMP 12 background-fetch agent is not a dialog host",
      fai._is_dialog_host_client(WMP12) is False)
check("the WMP 12 background-fetch agent is not classified as legacy either",
      fai._is_legacy_xml_client(WMP12) is False)

# ======================================================================
# 5. Artwork: WMP 11 takes the ABSOLUTE "proxy" shape; WMP 12 takes "direct".
#    WMP 11 is "proxy" rather than "relative" because WMP re-prefixes a
#    host-relative largeCoverParams with its own /cover/ - see section 13.
# ======================================================================
_m = load_server("art")
for _ua, _want, _label in ((WMP11, "proxy", "WMP 11"),
                           (WMP9, "relative", "WMP 9"),
                           (WMC, "relative", "WMC"),
                           (WMP12, "direct", "WMP 12 on Windows 11"),
                           (WMP12_WIN7, "direct", "WMP 12 on Windows 7"),
                           (IE7, "direct", "a browser")):
    _m._LAST_CLIENT_UA = _ua
    check(f"artwork mode for {_label} is {_want!r}",
          _m._effective_art_mode() == _want, _m._effective_art_mode())

# An operator override still wins over everything, including WMP 11.
_m._LAST_CLIENT_UA = WMP11
_m._ART_MODE_EXPLICIT = "proxy"
check("an explicit WMP_ART_MODE still overrides WMP 11",
      _m._effective_art_mode() == "proxy", _m._effective_art_mode())
_m._ART_MODE_EXPLICIT = ""

# ======================================================================
# 6. End to end, on the route that actually decides.
#
#    Two DIFFERENT handlers are involved and conflating them is the trap:
#
#      /redir/QueryTOC.asp -> legacy_redirects() -> redirects a browser
#                             client to the dialog, hands an XML client the
#                             document. This is where the dialog decision bites.
#      /redir/getmdrcd/   -> mdr_post()          -> ALWAYS the document. This
#                             is WMP's own background metadata fetch; it is not
#                             a negotiation point and must not start redirecting.
#
#    So WMP 11 is asserted to redirect on the first and still receive XML on
#    the second. Getting the second wrong would send a background fetch into a
#    dialog loop.
# ======================================================================
_c = fai.app.test_client()

r11 = _c.get("/redir/QueryTOC.asp?toc=" + TOC, headers={"User-Agent": WMP11})
check("WMP 11 is redirected to the dialog from the TOC route",
      r11.status_code in (301, 302, 303, 307, 308),
      f"status={r11.status_code} body={r11.data[:120]!r}")

r9 = _c.get("/redir/QueryTOC.asp?toc=" + TOC, headers={"User-Agent": WMP9})
check("WMP 9 still receives the MDR-CD XML document from the same route",
      r9.status_code == 200 and b"<METADATA>" in r9.data,
      f"status={r9.status_code} body={r9.data[:160]!r}")

rwmc = _c.get("/toc/getmdrcd.aspx?CD=" + TOC, headers={"User-Agent": WMC})
check("WMC still receives the MDR-CD XML document",
      rwmc.status_code == 200 and b"<METADATA>" in rwmc.data,
      f"status={rwmc.status_code} body={rwmc.data[:160]!r}")

# The background fetch must stay XML for EVERY client, WMP 11 included.
r11bg = _c.get("/redir/getmdrcd/?toc=" + TOC, headers={"User-Agent": WMP11})
check("WMP 11 still gets XML from the background fetch route, not a redirect",
      r11bg.status_code == 200 and b"<METADATA>" in r11bg.data,
      f"status={r11bg.status_code} body={r11bg.data[:160]!r}")

r12bg = _c.get("/redir/getmdrcd/?toc=" + TOC, headers={"User-Agent": WMP12})
check("WMP 12 still gets XML from the background fetch route",
      r12bg.status_code == 200 and b"<METADATA>" in r12bg.data,
      f"status={r12bg.status_code} body={r12bg.data[:160]!r}")

# A WMP 11 browser-style agent is a redirect on the TOC route, which is the
# actual observable behaviour the fix is for.
r11ie = _c.get("/redir/QueryTOC.asp?toc=" + TOC,
               headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 7.0)"})
check("a browser is redirected from the TOC route (unchanged control)",
      r11ie.status_code in (301, 302, 303, 307, 308),
      f"status={r11ie.status_code} body={r11ie.data[:120]!r}")

# ======================================================================
# 7. The capability route reports the facts and the live classification.
# ======================================================================
_m2 = load_server("caps")
_c2 = _m2.app.test_client()
_m2._LAST_CLIENT_UA = WMP11
_cap = json.loads(_c2.get("/wmp_capabilities").data)
check("the capability route reports the shared wizard IID",
      _cap["wizard_external_iid"] == fai.WMP_WIZARD_EXTERNAL_IID)
check("the capability route marks WMP 11 as lacking the WMP 12 only members",
      _cap["generations"]["wmp11"]["wmp12_only_members_present"] is False)
check("the capability route marks WMP 12 as having them",
      _cap["generations"]["wmp12"]["wmp12_only_members_present"] is True)
check("the capability route classifies a WMP 11 client correctly",
      _cap["last_client"]["is_wmp11"] is True
      and _cap["last_client"]["is_dialog_host_client"] is True,
      _cap["last_client"])

_m2._LAST_CLIENT_UA = WMP12
_cap12 = json.loads(_c2.get("/wmp_capabilities").data)
check("the capability route classifies a WMP 12 client correctly",
      _cap12["last_client"]["is_wmp11"] is False
      and _cap12["last_client"]["art_mode"] == "direct",
      _cap12["last_client"])

# ======================================================================
# 8. The dialog JS must not call a member WMP 11 lacks. RenameRegroupFiles is
#    a WriteNamesEx PARAMETER on both, never a call, and this asserts that so
#    a future edit that calls it directly fails here rather than on a WMP 11
#    machine with no member to call.
# ======================================================================
_src = open(os.path.join(_DIR, "FAI Server.py"), encoding="utf-8").read()
check("the server source never calls RenameRegroupFiles as a method",
      "external.RenameRegroupFiles(" not in _src)
check("the server source never calls the WMP 12 only error members",
      "external.OnChangeViewError(" not in _src
      and "external.OnChangeViewOnlineListError(" not in _src)

# ======================================================================
# 9. REGRESSION: THE REAL AGENTS. This is the section that matters most.
#
#    An earlier version of this file asserted everything against an invented
#    agent, "NSPlayer/11.0.0.5720". Every one of those assertions passed and
#    the feature did nothing, because no real WMP 11 request carries that
#    string. The real agents are in fai_server.log:
#
#      Windows-Media-Player/11.0.6003.22165    the player itself
#      Mozilla/4.0 ... Media Center PC 6.0      the dialog it opens, with
#                                              ?version=11.0.6003.22165
#
#    So these tests use the observed strings, and one of them makes a real
#    request carrying the version parameter - which is how the artwork bug
#    below is reproduced.
# ======================================================================
check("the REAL WMP 11 agent is recognised",
      fai._is_wmp11_client(WMP11) is True, WMP11)
check("the real WMP 11 agent is a dialog host",
      fai._is_dialog_host_client(WMP11) is True)

# The hyphenated WMP 12 spelling must NOT be caught by the WMP 11 branch.
# "Major version below 12" would have caught it; the range 10-11 does not.
check("the hyphenated WMP 12 agent is NOT WMP 11",
      fai._is_wmp11_client(WMP12_HYPHEN) is False, WMP12_HYPHEN)
check("the short WMP 12 agent is NOT WMP 11",
      fai._is_wmp11_client(WMP12_SHORT) is False, WMP12_SHORT)

# The version= parameter is the stronger signal, because the dialog request
# carries an ordinary IE7 agent that says nothing about the player.
check("version=11.0.6003.22165 identifies WMP 11 on its own",
      fai._is_wmp11_client("", WMP11_VERSION) is True)
check("version=12.0.26100.9549 identifies WMP 12 on its own",
      fai._is_wmp11_client("", WMP12_VERSION) is False)

# ======================================================================
# 10. THE ARTWORK BUG, REPRODUCED AND FIXED.
#
#     The reported symptom was "cover art is not updating after the FAI
#     dialog completes". The document is built while the dialog request is in
#     flight, so it is built under the IE7 agent - not under the player agent.
#     Before the fix that resolved to "direct", which puts a bare upstream
#     CDN URL in largeCoverParams. WMP 11 then never asks this server for the
#     picture, and the album keeps its old (or missing) cover.
#
#     The fix routes the version= parameter into the art-mode decision, so
#     the document carries a /cover/ URL that comes back through here.
# ======================================================================
ALBUM = {"id": "49537", "title": "Standing On The Shoulder Of Giants",
         "artist": "Oasis (2)", "year": "1994", "genre": "Rock",
         "art_url": "https://i.discogs.com/real-cover-abc/rs:fit/x.jpeg",
         "tracks": [{"number": 1, "name": "One", "duration_ms": 1000,
                     "disc": 1}]}


def cover_field(module, ua, version=None):
    """Build a document as this client would see it, return largeCoverParams."""
    module._LAST_CLIENT_UA = ua
    module._ART_MODE_EXPLICIT = ""
    if version is None:
        return module.build_wmp_xml(dict(ALBUM))
    # A request context carrying ?version=, which is how the real dialog
    # request arrives.
    with module.app.test_request_context(
            "/redir/submittoc/?version=" + version):
        return module.build_wmp_xml(dict(ALBUM))


def cover_of(xml):
    root = ET.fromstring(xml)
    for element in root.iter("largeCoverParams"):
        return (element.text or "").strip()
    return ""


_a = load_server("artfix")

# THE BUG: the dialog request (IE7 agent + version=11) must still carry a
# /cover/ URL. This is the exact shape of the reported failure. It must be
# ABSOLUTE, because WMP re-prefixes a relative one - see section 13.
_dialog_xml = cover_field(_a, WMP11_DIALOG, WMP11_VERSION)
_dialog_cover = cover_of(_dialog_xml)
check("a WMP 11 DIALOG request builds a /cover/ URL, not a bare CDN URL",
      "/cover/" in _dialog_cover, _dialog_cover[:90])

# ...and the WMP 12 dialog request must be untouched by that.
_a12 = load_server("artfix12")
_w12 = cover_of(cover_field(_a12, IE7, WMP12_VERSION))
check("a WMP 12 dialog request still gets the direct CDN URL",
      _w12.startswith("https://") and "/cover/" not in _w12, _w12[:90])

# The player-agent request and the dialog request must agree, or the
# document WMP is handed changes shape between the two halves of one flow.
_player_cover = cover_of(cover_field(_a, WMP11))
check("the WMP 11 player and dialog requests agree on the artwork shape",
      _player_cover.startswith("http://"), _player_cover[:90])

# And the URL in the document must actually be servable by this server.
# Both shapes WMP 11 has been observed to send are checked: the ?url= form
# (seen in the log at 23:47:15) and the scheme-less rewrite.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


class _FakeImage:
    status_code = 200
    content = PNG
    headers = {"Content-Type": "image/png"}


_upstream = ALBUM["art_url"]
for _label, _target in (
        ("?url= form (as seen in the log)",
         "/cover/album.jpg?url=" + _upstream),
        ("scheme-less rewrite",
         "/cover/" + _upstream.split("://", 1)[1])):
    _hit = []

    def _grab(url, _h=_hit, **kw):
        _h.append(url)
        return _FakeImage()

    _a.session.get = _grab
    _a.image_cache.clear()
    _r = _a.app.test_client().get(_target)
    check(f"WMP 11 is served the cover via the {_label}",
          _r.status_code == 200 and _hit == [_upstream],
          f"status={_r.status_code} fetched={_hit}")

# ======================================================================
# 11. REGRESSION: THE URL WMP 11 ACTUALLY SENT. Taken verbatim from
#     fai_server.log, 2026-10-05 00:12:32, where it answered 404:
#
#   GET /cover/https:/is1-ssl.mzstatic.com/image/thumb/Music211/v4/cc/b0/d2/
#       ccb0d2f6-60a7-1ee6-3386-6cd85764ab22/5051961002102.jpg/600x600bb.jpg
#       ?fai_v=1?locale=409&geoid=f4&version=11.0.6003.22165&userlocale=409
#
#     Two injuries, either of which is fatal on its own:
#
#       "https:/host"   ONE slash. Passes the "does this look like a URL"
#                       test (first four characters are "http"), then fails
#                       every validity check. This is the 404.
#       "?fai_v=1?"      a SECOND '?' where the separator belonged, because WMP
#                       appended its parameters with '?' to a value that already
#                       had one. The token then survives stripping and would be
#                       forwarded to the CDN.
# ======================================================================
WMP11_MANGLED_PATH = (
    "/cover/https:/is1-ssl.mzstatic.com/image/thumb/Music211/v4/cc/b0/d2/"
    "ccb0d2f6-60a7-1ee6-3386-6cd85764ab22/5051961002102.jpg/600x600bb.jpg")
WMP11_UPSTREAM = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music211/v4/cc/b0/d2/"
    "ccb0d2f6-60a7-1ee6-3386-6cd85764ab22/5051961002102.jpg/600x600bb.jpg")

check("the single-slash scheme is repaired",
      fai._normalize_mangled_url("https:/is1-ssl.mzstatic.com/a.jpg")
      == "https://is1-ssl.mzstatic.com/a.jpg")
check("http single-slash is repaired too",
      fai._normalize_mangled_url("http:/i.discogs.com/a.jpeg")
      == "http://i.discogs.com/a.jpeg")
check("a well-formed URL is left alone",
      fai._normalize_mangled_url("https://good.com/a.jpg")
      == "https://good.com/a.jpg")
check("the repair does not eat a second real slash",
      fai._normalize_mangled_url("https://a.com//b.jpg") == "https://a.com//b.jpg")

check("a token followed by '?' is stripped",
      fai._strip_art_token("https://x.com/a.jpg?fai_v=1?locale=409&geoid=f4")
      == "https://x.com/a.jpg?locale=409&geoid=f4",
      fai._strip_art_token("https://x.com/a.jpg?fai_v=1?locale=409&geoid=f4"))
check("a token followed by '&' still strips correctly",
      fai._strip_art_token("https://x.com/a.jpg?fai_v=1&locale=409")
      == "https://x.com/a.jpg?locale=409")

# The whole logged request, through the real handler.
_m = load_server("logged404")
_hit = []


def _grab(url, _h=_hit, **kw):
    _h.append(url)
    return _FakeImage()


_m.session.get = _grab
_m.image_cache.clear()
_r = _m.app.test_client().get(WMP11_MANGLED_PATH + "?fai_v=1?locale=409&geoid=f4")
check("the exact request from the log is served, not 404",
      _r.status_code == 200, f"status={_r.status_code}")
check("...and it fetches the correct upstream URL",
      _hit == [WMP11_UPSTREAM], _hit)
check("...and the per-apply token never reaches the CDN",
      _hit and "fai_v" not in _hit[0], _hit)

# And the same request with ?version= present, as WMP really sent it.
_hit2 = []


def _grab2(url, _h=_hit2, **kw):
    _h.append(url)
    return _FakeImage()


_m.session.get = _grab2
_m.image_cache.clear()
_r2 = _m.app.test_client().get(
    WMP11_MANGLED_PATH + "?fai_v=1?locale=409&geoid=f4&version=11.0.6003.22165")
check("the request with ?version= is served too",
      _r2.status_code == 200 and _hit2 == [WMP11_UPSTREAM],
      f"status={_r2.status_code} fetched={_hit2}")

# ======================================================================
# 12. REGRESSION: the version must survive the request that has no query
#     string. The document is built during POST /store_staged_xml, which is
#     same-origin and normally carries no ?version= - yet the artwork decision
#     runs there. That is what logged "[STAGED] art=direct" while the next
#     request logged "(mode=relative)".
# ======================================================================
_mv = load_server("versionmem")
_mv._LAST_CLIENT_VERSION = ""
with _mv.app.test_request_context("/redir/submittoc/?version=11.0.6003.22165"):
    _mv._log_request(_mv.app.response_class())
check("a request carrying version= is remembered for later requests",
      _mv._LAST_CLIENT_VERSION == "11.0.6003.22165", _mv._LAST_CLIENT_VERSION)

_mv._LAST_CLIENT_UA = WMP11_DIALOG
with _mv.app.test_request_context("/store_staged_xml"):
    check("a later request with NO query string still sees the version",
          _mv._client_version_arg() == "11.0.6003.22165",
          _mv._client_version_arg())
    check("...so the artwork mode is right on the request that BUILDS the doc",
          _mv._effective_art_mode() == "proxy", _mv._effective_art_mode())

# The same flow on WMP 12 must still be "direct".
_mv12 = load_server("versionmem12")
_mv12._LAST_CLIENT_VERSION = WMP12_VERSION
_mv12._LAST_CLIENT_UA = IE7
with _mv12.app.test_request_context("/store_staged_xml"):
    check("the remembered WMP 12 version still yields direct mode",
          _mv12._effective_art_mode() == "direct", _mv12._effective_art_mode())

# ======================================================================
# 13. REGRESSION: WMP 11 must get an ABSOLUTE cover URL, not a relative one.
#
#     A relative largeCoverParams is re-prefixed by the client. Observed on WMP
#     11, 2026-10-05 03:05:21, with the document value in brackets:
#
#       GET /cover[/cover/fai-585c2699/album.jpg]?url=...   -> 200
#
#     It still served - the ?url= parameter survived and the handler fetched
#     from that - which is why it looked right. The PATH was malformed though,
#     and anything relying on the path form (the scheme-less rewrite) would
#     miss. An absolute URL leaves the client nothing to resolve.
# ======================================================================
_ALBUM = {"id": "1", "title": "April", "artist": "Sun Kil Moon",
          "year": "1996", "genre": "Rock",
          "art_url": "https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/x/"
                     "634457196322_cover.jpg/600x600bb.jpg",
          "tracks": [{"number": 1, "name": "Lost Verses",
                      "duration_ms": 1000, "disc": 1}]}


def cover_for(module, ua, version=""):
    """The largeCoverParams a document carries for this client."""
    module._LAST_CLIENT_UA = ua
    module._LAST_CLIENT_VERSION = version
    module._ART_MODE_EXPLICIT = ""
    root = ET.fromstring(module.build_wmp_xml(dict(_ALBUM)))
    for element in root.iter("largeCoverParams"):
        return (element.text or "").strip()
    return ""


_art = load_server("arturl")

for _label, _ua, _ver in (
        ("WMP 11 dialog (IE7 + version=)", WMP11_DIALOG, WMP11_VERSION),
        ("WMP 11 player agent", WMP11, WMP11_VERSION)):
    _v = cover_for(_art, _ua, _ver)
    check("%s gets an absolute cover URL" % _label,
          _v.startswith("http://") or _v.startswith("https://"), _v[:70])
    check("%s does not get a re-prefixable /cover/ path" % _label,
          not _v.startswith("/cover/"), _v[:70])
    check("%s cover URL is served by this server" % _label,
          "/cover/" in _v, _v[:70])

for _label, _ua in (("WMP 12", WMP12), ("a plain browser", IE7)):
    _v = cover_for(_art, _ua, "")
    check("%s artwork is untouched by the WMP 11 change" % _label,
          _v.startswith("https://i") or _v.startswith("https://is"),
          _v[:70])

# The per-apply token must still change, or a re-apply would not re-fetch.
_a1 = cover_for(_art, WMP11_DIALOG, WMP11_VERSION)
_a2 = cover_for(_art, WMP11_DIALOG, WMP11_VERSION)
check("the per-apply token still changes on WMP 11 (re-apply re-fetches)",
      _a1 != _a2, "%s == %s" % (_a1[-24:], _a2[-24:]))

print("")
print("=" * 60)
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("Failures:")
    for name in FAIL:
        print(f"  - {name}")
print("=" * 60)
sys.exit(1 if FAIL else 0)