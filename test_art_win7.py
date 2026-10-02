# -*- coding: utf-8 -*-
"""
Tests for the Windows 7 artwork fix: cover art must be delivered THROUGH the
/cover endpoint for the Windows 7 client, and only there.

The report was that on Windows 7 WMP asks for

    GET /cover/https://some.url/image.jpg?locale=409   -> 404

and the album comes back with no picture, because in "direct" mode the document
carries a bare absolute URL and the client has to rewrite it back onto this
host before anything is fetched. The fix emits a /cover/ URL in the document for
that client instead, so there is no rewriting left to fail.

Two things have to be true at once, and both are asserted here:

  1. the Windows 7 build produces a /cover/ URL
  2. NOTHING ELSE changes - WMP 12 on Windows 11, the source checkout, and the
     official build all keep the "direct" shape they had

(2) is the one that matters. A fix for a Win7 bug that quietly alters Windows
11 behaviour is a regression wearing a fix's clothes.
"""
import contextlib
import importlib.util
import os
import sys
import xml.etree.ElementTree as ET


@contextlib.contextmanager
def pretending_frozen(exe_path):
    """Temporarily make the process look like the given frozen build.

    The server reads sys.frozen and sys.executable, and `sys` is a single
    shared module - so assigning to it in a test leaks into every test that
    runs afterwards. That is not theoretical: the "source checkout" case below
    failed for exactly this reason, reporting that an unfrozen build produced
    Win7 artwork, because the previous case had left sys.frozen = True behind.

    Snapshot and restore, so each case is hermetic.
    """
    had_frozen = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    old_executable = sys.executable
    try:
        sys.frozen = True
        sys.executable = exe_path
        yield
    finally:
        if had_frozen:
            sys.frozen = old_frozen
        else:
            del sys.frozen
        sys.executable = old_executable


_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

PASS = []
FAIL = []

#: The frozen exe name the Windows 7 test build is published under. Copied from
#: NAME in build_exe_win7.py; if that ever changes this must change with it, or
#: the fix silently stops applying.
WIN7_EXE = r"D:\somewhere\WMP-FAI-Server-Win7Test.exe"
OFFICIAL_EXE = r"D:\somewhere\WMP-FAI-Server.exe"


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} :: {detail}")


def load_server(tag):
    """Import a FRESH copy of the server, so tests cannot leak into each other."""
    spec = importlib.util.spec_from_file_location(
        "fai_%s" % tag, os.path.join(_DIR, "FAI Server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ART = "https://i.discogs.com/some-image.jpg"
ALBUM = {
    "id": "1122792846",
    "title": "Test Album",
    "artist": "Test Artist",
    "source": "itunes",
    "art_url": ART,
    "tracks": [{"title": "One", "artist": "Test Artist", "length_ms": 214000}],
}


def cover_url(module):
    """Build a document and return its largeCoverParams.

    The cover fields are NOT on the document root - the root is <METADATA> and
    the fields sit one level down, under <WMMetadata>. A findtext() on the root
    silently returns None, which looks exactly like "no artwork" and sends you
    hunting for a bug in code that is working. Every element is therefore
    searched for, rather than looked up by a guessed path.
    """
    root = ET.fromstring(module.build_wmp_xml(dict(ALBUM)))
    for element in root.iter("largeCoverParams"):
        return (element.text or "").strip()
    return ""


def build_as(tag, frozen_exe=None, client_ua="", env_art_mode=None):
    """Build a document under a simulated deployment and return the cover URL."""
    module = load_server(tag)
    if env_art_mode is not None:
        module._ART_MODE_EXPLICIT = env_art_mode
    if client_ua:
        module._LAST_CLIENT_UA = client_ua
    if frozen_exe is None:
        return cover_url(module)
    # The context manager matters: writing sys.frozen directly would leak.
    with pretending_frozen(frozen_exe):
        return cover_url(module)


# ======================================================================
# 1. The fix: the Win7 build points at /cover/
# ======================================================================
url = build_as("win7", frozen_exe=WIN7_EXE,
               client_ua="WindowsMediaPlayer/12.0.7601.17514")
check("win7 build emits a /cover/ URL in largeCoverParams",
      url.startswith("/cover/"), url[:80])
check("win7 build carries the upstream URL in the query",
      "url=" in url, url[:120])
# A bare '&' anywhere in the value makes the whole MDR-CD document
# not-well-formed, and WMP then rejects the ENTIRE response - tags and artwork
# together. That has happened before; asserted so the win7 shape cannot
# reintroduce it.
check("win7 cover URL contains no bare '&' (document stays well-formed)",
      "&" not in url, url[:140])
check("win7 cover URL contains exactly one '?'",
      url.count("?") == 1, url[:140])

# ======================================================================
# 2. Everything else must be untouched
# ======================================================================
# A source checkout: not frozen, so the Win7 rule cannot reach it.
url_src = build_as("src", client_ua="WindowsMediaPlayer/12.0.7601.17514")
check("source checkout is UNAFFECTED and still emits the direct URL",
      url_src == ART, url_src[:80])

# The official (Windows 11) build: frozen, but not named Win7Test.
url_official = build_as("official", frozen_exe=OFFICIAL_EXE,
                        client_ua="WindowsMediaPlayer/12.0.19041.4046")
check("official build is UNAFFECTED and still emits the direct URL",
      url_official == ART, url_official[:80])

# ======================================================================
# 3. Legacy clients get the same shape, and for the same reason
# ======================================================================
for agent, label in (
        ("NSPlayer/12.00.7601.17514", "WMP 7-9 (NSPlayer)"),
        ("Windows-Media-Center/9.0", "Windows Media Center"),
        ("Windows-Media-Player/9.0", "Windows-Media-Player")):
    got = build_as("legacy_%s" % label.split()[0], client_ua=agent)
    check(f"legacy client {label} also gets a /cover/ URL",
          got.startswith("/cover/"), got[:80])

# ...but a browser UA is not a legacy client, and must not be treated as one.
url_browser = build_as(
    "browser", client_ua="Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 6.1)")
check("a browser UA is not treated as a legacy client",
      url_browser == ART, url_browser[:80])

# ======================================================================
# 4. An explicit WMP_ART_MODE still wins over everything
# ======================================================================
url_forced = build_as("forced_direct", frozen_exe=WIN7_EXE,
                      client_ua="WindowsMediaPlayer/12.0.7601.17514",
                      env_art_mode="direct")
check("WMP_ART_MODE=direct overrides the Win7 default",
      url_forced == ART, url_forced[:80])

url_forced2 = build_as("forced_relative", frozen_exe=OFFICIAL_EXE,
                       client_ua="WindowsMediaPlayer/12.0.19041.4046",
                       env_art_mode="relative")
check("WMP_ART_MODE=relative works on a non-Win7 build",
      url_forced2.startswith("/cover/"), url_forced2[:80])

# ======================================================================
# 5. The URL the Win7 document now contains is actually servable
# ======================================================================
# This is the point of the change: the URL WMP is told to fetch must return
# the image. Before the fix the client had to construct this URL itself; now
# the document contains it verbatim, so the server is what has to honour it.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


class _FakeImage:
    status_code = 200
    content = PNG
    headers = {"Content-Type": "image/png"}


module = load_server("served")
module._LAST_CLIENT_UA = "WindowsMediaPlayer/12.0.7601.17514"
module.session.get = lambda url, **kw: _FakeImage()

client = module.app.test_client()
with pretending_frozen(WIN7_EXE):
    target = cover_url(module)
module.image_cache.clear()
response = client.get(target)
check("the URL the document contains is served by /cover/ (200)",
      response.status_code == 200, response.status_code)
check("the served body really is the image",
      response.data.startswith(b"\x89PNG"), response.data[:8])

# The version token must still vary per apply, or WMP keeps the first art it
# ever saw and a re-apply can never change it. Both builds must happen INSIDE
# the pretending-frozen block, or the second one is a Windows 11 build and the
# two are trivially different for the wrong reason.
with pretending_frozen(WIN7_EXE):
    first = cover_url(module)
    second = cover_url(module)
check("the per-apply token still changes between builds",
      first != second, "identical across two builds")

# ======================================================================
# 6. The classic direct-mode rewrite must still work
# ======================================================================
# The Windows 11 shape is unchanged, and if WMP ever does rewrite the way it
# did on Windows 7, that path must keep working. This is the regression guard
# for the OTHER failure mode: fixing the document shape must not have broken
# the handler that was already correct.
module2 = load_server("rewrite")
module2.session.get = lambda url, **kw: _FakeImage()
c2 = module2.app.test_client()
for label, path in (
        ("plain // in the path", "/cover/https://i.discogs.com/x.jpeg?locale=409"),
        ("no query string", "/cover/https://i.discogs.com/x.jpeg"),
        ("percent-encoded path", "/cover/https%3A%2F%2Fi.discogs.com%2Fx.jpeg"),
):
    module2.image_cache.clear()
    r = c2.get(path)
    check(f"direct-mode rewrite still served: {label}",
          r.status_code == 200, r.status_code)

# ======================================================================
# 7. The SCHEME-LESS path form - the actual Windows 7 shape
# ======================================================================
# With images.metaservices.microsoft.com pointed at this server, WMP on
# Windows 7 sends the *partial* URL - no scheme - terminated with an image
# extension:
#
#   GET /cover/i.discogs.com/some-image.jpg?locale=409
#
# The handler used to require `startswith("http")` and discarded every one of
# these before any fetch was attempted, answering a bare 404. That is the
# reported symptom: the request arrives, the server 404s it, and the album has
# no picture.
#
# These assert both halves - that the request is SERVED, and that it is served
# by fetching the RIGHT url (https:// plus the host), not merely that something
# came back.
for partial, expected in (
        ("i.discogs.com/some-image.jpg",
         "https://i.discogs.com/some-image.jpg"),
        ("coverartarchive.org/release/1234/front-500.jpg",
         "https://coverartarchive.org/release/1234/front-500.jpg"),
        ("is1-ssl.mzstatic.com/image/thumb/Music/v4/1a/abc/600x600bb.jpg",
         "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/1a/abc/600x600bb.jpg"),
        ("i.discogs.com/abc123.jpeg?locale=409",
         "https://i.discogs.com/abc123.jpeg")):
    tag = "partial_%d" % len(PASS)
    module3 = load_server(tag)
    fetched = []

    def _grab(url, _f=fetched, **kw):
        _f.append(url)
        return _FakeImage()

    module3.session.get = _grab
    c3 = module3.app.test_client()
    module3.image_cache.clear()
    r = c3.get("/cover/" + partial)
    check(f"scheme-less path served: /cover/{partial[:42]}",
          r.status_code == 200, r.status_code)
    check(f"scheme-less path fetches the https:// url: /cover/{partial[:34]}",
          fetched == [expected], fetched)

# The image extension is part of the real path on every artwork URL this
# project emits, so it must survive the round trip rather than be stripped.
module4 = load_server("ext")
module4.session.get = lambda url, **kw: _FakeImage()
c4 = module4.app.test_client()
module4.image_cache.clear()
r = c4.get("/cover/i.discogs.com/some-image.jpeg")
check("a .jpeg extension survives the scheme-less round trip",
      r.status_code == 200, r.status_code)

# ...but an internal path that merely LOOKS like one must not be turned into a
# fetch. '/cover/fai-<token>/album.jpg' is our own proxy path, and rewriting it
# to 'https://fai-<token>/album.jpg' would break the relative art mode.
for internal, label in (("/cover/fai-1a2b3c4d/album.jpg", "relative art mode"),
                        ("/cover/album.jpg", "the album.jpg special case")):
    module5 = load_server("internal_%s" % label.split()[0])
    hit = []

    def _grab5(url, _h=hit, **kw):
        _h.append(url)
        return _FakeImage()

    module5.session.get = _grab5
    c5 = module5.app.test_client()
    module5.image_cache.clear()
    c5.get(internal)
    check(f"internal path is not rewritten into a fetch: {label}",
          not any("fai-" in u or u.startswith("https://album") for u in hit), hit)


print("")
print("=" * 60)
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("Failures:")
    for name in FAIL:
        print(f"  - {name}")
print("=" * 60)
sys.exit(1 if FAIL else 0)


