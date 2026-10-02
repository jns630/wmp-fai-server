# -*- coding: utf-8 -*-
"""
Test suite for the WMP Online Store subsystem (online_store/).

Runs the Flask app in-process via test_client() - no port 80, no WMP, no
network and no registry writes required. Follows the same shape as
test_fai_v2.py: a flat list of check(name, condition, detail) calls and a
"==== N passed, M failed ====" summary.

What is deliberately NOT tested here: anything that needs a live WMP to
confirm. Whether WMP actually renders the Online Stores tab from these registry
entries and this ServiceInfo document is an empirical question about a
Microsoft client; it is written up in docs/ONLINE_STORE.md as unproven rather
than asserted here. What IS tested is everything this project controls: the
config contract, the ServiceInfo document, the registry plan, the provider
contract, the routes, and the fact that the store cannot break FAI.
"""
import json
import os
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from online_store import config as store_config          # noqa: E402
from online_store import registry, serviceinfo           # noqa: E402
from online_store.models import Album, Track, format_duration   # noqa: E402
from online_store.providers import (ProviderError, StoreProvider,   # noqa: E402
                                    available_providers, create_provider)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} :: {detail}")


def make_config(**overrides):
    """Build a StoreConfig from DEFAULTS with ``overrides`` applied."""
    values = dict(store_config.DEFAULTS)
    values.update({k: str(v) for k, v in overrides.items()})
    return store_config.StoreConfig(values, source={"test": True})


# ======================================================================
# 1. CONFIG: validation and the documented INI contract
# ======================================================================
cfg = make_config(enabled="true")
check("config: enabled parses", cfg.enabled is True)
check("config: default store id", cfg.store_id == "legacy_music_store",
      cfg.store_id)
check("config: default friendly name", cfg.friendly_name == "Legacy Music Store",
      cfg.friendly_name)

# The GUID is the store's identity, and it must be braced. WMP is documented as
# wanting "registry format, complete with the curly braces".
check("config: guid is braced registry format",
      cfg.subscription_object_guid.startswith("{")
      and cfg.subscription_object_guid.endswith("}"),
      cfg.subscription_object_guid)
check("config: guid is 36 chars inside braces",
      len(cfg.subscription_object_guid) == 38, cfg.subscription_object_guid)

# A bare GUID is the most likely hand-editing mistake, so it is normalised
# rather than rejected.
try:
    bare = make_config(subscription_object_guid="b4867d0f-fb79-4efb-86d0-c2c94410ee15")
    check("config: bare guid is auto-braced",
          bare.subscription_object_guid == "{B4867D0F-FB79-4EFB-86D0-C2C94410EE15}",
          bare.subscription_object_guid)
except store_config.ConfigError as exc:
    check("config: bare guid is auto-braced", False, str(exc))

# ...but garbage is still rejected, because a silently-wrong GUID registers a
# store under an identity nobody can find again.
for bad_guid in ("not-a-guid", "12345", "", "{B4867D0F-XXXX-4EFB-86D0-C2C94410EE15}"):
    try:
        make_config(subscription_object_guid=bad_guid)
        check(f"config: rejects bad guid {bad_guid!r}", False, "no error raised")
    except store_config.ConfigError:
        check(f"config: rejects bad guid {bad_guid!r}", True)

# store_id becomes a registry key component. A value with a backslash in it
# would install under one path and uninstall under another.
for bad_id in ("has\\backslash", "has space", "has/slash", "", "has:colon"):
    try:
        make_config(store_id=bad_id)
        check(f"config: rejects bad store_id {bad_id!r}", False, "no error")
    except store_config.ConfigError:
        check(f"config: rejects bad store_id {bad_id!r}", True)

# base_url must be http(s); the storefront concatenates paths onto it.
for bad_url in ("ftp://x/", "127.0.0.1", "javascript:alert(1)"):
    try:
        make_config(base_url=bad_url)
        check(f"config: rejects bad base_url {bad_url!r}", False, "no error")
    except store_config.ConfigError:
        check(f"config: rejects bad base_url {bad_url!r}", True)

# A trailing slash is stripped, because callers append "/path" and "//path"
# only breaks inside WMP's browser, never in a naive test.
try:
    slashed = make_config(base_url="http://127.0.0.1/online-store/")
    check("config: strips trailing slash from base_url",
          slashed.base_url == "http://127.0.0.1/online-store", slashed.base_url)
    check("config: url_for joins with a single slash",
          slashed.url_for("serviceinfo.xml")
          == "http://127.0.0.1/online-store/serviceinfo.xml",
          slashed.url_for("serviceinfo.xml"))
except store_config.ConfigError as exc:
    check("config: strips trailing slash from base_url", False, str(exc))

# Capabilities must be a non-negative integer; it is a bitmask.
for bad_caps, label in (("-1", "negative"), ("notanumber", "non-integer")):
    try:
        make_config(capabilities=bad_caps)
        check(f"config: rejects {label} capabilities", False, "no error")
    except store_config.ConfigError:
        check(f"config: rejects {label} capabilities", True)

# enabled is a boolean, and a typo must be loud rather than silently false.
for bad_bool in ("maybe", "2", "enabled"):
    try:
        make_config(enabled=bad_bool)
        check(f"config: rejects non-boolean enabled {bad_bool!r}", False, "no error")
    except store_config.ConfigError:
        check(f"config: rejects non-boolean enabled {bad_bool!r}", True)


# ======================================================================
# 2. SERVICEINFO: the document WMP actually reads
# ======================================================================
xml = serviceinfo.build_service_info(cfg)
try:
    root = ET.fromstring(xml)
    check("serviceinfo: parses as XML", True)
except ET.ParseError as exc:
    root = None
    check("serviceinfo: parses as XML", False, str(exc))

if root is not None:
    check("serviceinfo: root element is ServiceInfo", root.tag == "ServiceInfo",
          root.tag)
    # The Key attribute must equal the registry keyName. Microsoft states this
    # pairing for /DefaultService; a mismatch means WMP registers a store it
    # then cannot find.
    check("serviceinfo: Key matches the registry keyName",
          root.get("Key") == cfg.store_id, root.get("Key"))
    check("serviceinfo: Version is 1.00", root.get("Version") == "1.00",
          root.get("Version"))

    # FriendlyName and ServiceTask1/ButtonText are the documented REQUIRED
    # elements for a Type 2 commerce store.
    friendly = root.find("FriendlyName")
    check("serviceinfo: FriendlyName present and correct",
          friendly is not None and friendly.text == cfg.friendly_name,
          friendly.text if friendly is not None else "missing")

    task1 = root.find("ServiceTask1")
    check("serviceinfo: ServiceTask1 present (required for commerce store)",
          task1 is not None, "missing")
    if task1 is not None:
        task_url = task1.get("URL")
        check("serviceinfo: ServiceTask1 URL is absolute",
              bool(task_url) and task_url.startswith(("http://", "https://")),
              task_url)
        # The page the tab hosts must be the storefront, not the API.
        check("serviceinfo: ServiceTask1 points at the storefront",
              task_url == cfg.base_url + "/", task_url)
        button = task1.find("ButtonText")
        check("serviceinfo: ServiceTask1 has ButtonText (required)",
              button is not None and bool(button.text),
              button.text if button is not None else "missing")

    color = root.find("Color")
    check("serviceinfo: Color carries the configured button colour",
          color is not None and color.get("MediaPlayer") == cfg.button_color,
          color.get("MediaPlayer") if color is not None else "missing")

    # Elements Microsoft documents as IGNORED for a commerce store must not be
    # emitted - a document full of elements the client ignores is a document
    # nobody has verified against anything.
    for ignored in ("AlbumInfo", "BuyCD", "InfoCenter", "Install",
                    "ServiceTask2", "ServiceTask3"):
        check(f"serviceinfo: omits commerce-ignored element {ignored}",
              root.find(ignored) is None, "unexpectedly present")

    navigate = root.find("Navigate")
    check("serviceinfo: Navigate BaseURL is absolute",
          navigate is not None
          and navigate.get("BaseURL", "").startswith(("http://", "https://")),
          navigate.get("BaseURL") if navigate is not None else "missing")

    # Image is omitted when unconfigured, rather than emitted with an empty URL
    # (a consumer fetching "" gets the current page, not a placeholder).
    check("serviceinfo: omits Image when no URLs configured",
          root.find("Image") is None, "unexpectedly present")
    with_images = serviceinfo.build_service_info(
        make_config(enabled="true",
                    menu_image_url="http://127.0.0.1/online-store/art/lms-0001",
                    large_image_url="http://127.0.0.1/online-store/art/lms-0002"))
    image = ET.fromstring(with_images).find("Image")
    check("serviceinfo: emits Image when both URLs configured",
          image is not None and image.get("MenuURL", "").endswith("lms-0001"),
          ET.tostring(image) if image is not None else "missing")

# A friendly name with an ampersand must not produce a document WMP cannot
# parse - it would report the store as simply not found.
amp = ET.fromstring(serviceinfo.build_service_info(
    make_config(enabled="true", friendly_name="Rock & Roll & More")))
check("serviceinfo: escapes XML metacharacters in FriendlyName",
      amp.find("FriendlyName").text == "Rock & Roll & More",
      amp.find("FriendlyName").text)


# ======================================================================
# 3. REGISTRY PLAN: what install would write, and what it must not
# ======================================================================
plan = registry.describe_install(cfg)
locations = [("%s\\%s" % (step["root"], step["path"]),
              step["name"], step["value"]) for step in plan]
location_text = "\n".join("%s\\%s = %r" % (path, name, value)
                         for path, name, value in locations)


def planned(root, path, name):
    """Return the values install would write for one exact value name."""
    return [value for (p, n, value) in locations
            if p.startswith(root + "\\") and p.endswith(path) and n == name]


# The three documented locations. Exact key paths, because a key in the wrong
# place is the single most likely way for this to silently do nothing.
check("registry: writes HKLM Subscriptions keyName",
      bool(planned("HKLM",
                   "SOFTWARE\\Microsoft\\MediaPlayer\\Subscriptions\\%s"
                   % cfg.store_id, "FriendlyName")),
      location_text)
check("registry: writes documented Capabilities as a DWORD",
      any(step["type"] == registry.REG_DWORD
          and step["name"] == "Capabilities" for step in plan),
      location_text)
check("registry: writes documented SubscriptionObjectGUID",
      bool(planned("HKLM",
                   "SOFTWARE\\Microsoft\\MediaPlayer\\Subscriptions\\%s"
                   % cfg.store_id, "SubscriptionObjectGUID")),
      location_text)
check("registry: writes documented TestParameter gate",
      bool(planned("HKCU", "Software\\Microsoft\\MediaPlayer\\Services",
                   "TestParameter")),
      location_text)
check("registry: writes BASEURL for the self-hosted ServiceInfo",
      bool(planned("HKCU",
                   "Software\\Microsoft\\MediaPlayer\\Services\\%s" % cfg.store_id,
                   "BASEURL")),
      location_text)

# Capabilities must be 0. A non-zero mask makes WMP call IWMPSubscriptionService
# methods on a class that does not exist - this store is a commerce store and
# ships no plug-in.
cap_value = planned("HKLM",
                    "SOFTWARE\\Microsoft\\MediaPlayer\\Subscriptions\\%s"
                    % cfg.store_id, "Capabilities")
check("registry: Capabilities is 0 (no plug-in to call)",
      cap_value and cap_value[0] == 0, cap_value)

# Group policy is not a per-user feature toggle, and unrelated WMP settings are
# not this installer's business. These are matched as path fragments rather than
# bare substrings, because "MediaPlayer" appears in every key this store
# legitimately owns and a naive "Player" test would fail on all of them.
_FORBIDDEN_FRAGMENTS = ("Policies", "Explorer", "CurrentVersion",
                        "\\MediaPlayer\\Player", "\\MediaPlayer\\Settings")
for fragment in _FORBIDDEN_FRAGMENTS:
    check(f"registry: never writes to a {fragment!r} key",
          fragment not in location_text, location_text)

# ActiveService is written BY WMP when a user activates a store. The installer
# must not impersonate that.
check("registry: never writes ActiveService",
      "ActiveService" not in location_text, location_text)

# Reversibility: every key install creates, uninstall removes.
uninstall = registry.describe_uninstall(cfg)
uninstall_paths = {"%s\\%s" % (step["root"], step["path"])
                   for step in uninstall if step["action"] == "remove-key"}
created = {"%s\\%s" % (step["root"], step["path"])
           for step in plan if step["action"] == "create"}
check("registry: uninstall removes every key install creates",
      created.issubset(uninstall_paths),
      "creates=%s uninstalls=%s" % (sorted(created), sorted(uninstall_paths)))
check("registry: uninstall handles the TestParameter list",
      any(step["name"] == "TestParameter" for step in uninstall),
      str(uninstall))

# uninstall is idempotent in shape: removing an absent key is "was not present",
# not a crash, which _delete_tree returns False for.
try:
    check("registry: read of an absent key returns {} not an exception",
          registry.read_values("HKCU",
                               r"Software\Microsoft\MediaPlayer\Services"
                               r"\nonexistent_store_probe_12345") == {},
          "non-empty")
except Exception as exc:
    check("registry: read of an absent key returns {} not an exception",
          False, str(exc))


# ======================================================================
# 4. PROVIDER CONTRACT: the seam a real store has to fit through
# ======================================================================
check("providers: local_fake is registered",
      "local_fake" in available_providers(), str(available_providers()))

store = create_provider("local_fake", purchases_dir=tempfile.mkdtemp())
check("providers: create_provider builds a StoreProvider",
      isinstance(store, StoreProvider), type(store).__name__)

# A misspelled provider id must say what IS available, not just "no".
try:
    create_provider("7digital")
    check("providers: unknown id raises ProviderError", False, "no error")
except ProviderError as exc:
    check("providers: unknown id raises ProviderError", True)
    check("providers: unknown id lists the available providers",
          "local_fake" in str(exc), str(exc))

# The base class must not pretend to answer. A default that silently returns
# data is how a half-finished adapter ships.
for method, args in (("search", ("x",)), ("list_albums", ()), ("get_album", ("x",)),
                     ("get_track", ("x",))):
    try:
        getattr(StoreProvider, method)(object(), *args)
        check(f"providers: base.{method} raises NotImplementedError", False, "returned")
    except NotImplementedError:
        check(f"providers: base.{method} raises NotImplementedError", True)
    except Exception as exc:
        check(f"providers: base.{method} raises NotImplementedError", False, str(exc))

# The fake store must be a real catalog, not an empty shell.
albums = store.list_albums(limit=100)
check("providers: fake catalog is non-empty", len(albums) > 0, str(len(albums)))
check("providers: albums carry a full track list",
      all(album.track_count > 0 for album in albums),
      str([(a.title, a.track_count) for a in albums]))
check("providers: album ids are unique",
      len({album.album_id for album in albums}) == len(albums), "duplicates")
check("providers: track ids are unique across the catalog",
      len({track.track_id for album in albums for track in album.tracks})
      == sum(album.track_count for album in albums), "duplicates")

# An empty search returns the browse list; a real search filters.
check("providers: empty search falls back to browse",
      len(store.search("", limit=5)) == 5, str(len(store.search("", limit=5))))
hits = store.search("blues", limit=10)
check("providers: search matches on genre", len(hits) >= 1, str(hits))
check("providers: search matches on artist",
      any("Radio" in album.artist for album in store.search("radio", limit=10)),
      "no artist match")
check("providers: search for nonsense returns empty, not an error",
      store.search("zzzz-no-such-album-zzzz", limit=10) == [], "non-empty")
check("providers: limit is honoured", len(store.list_albums(limit=2)) == 2, "wrong")
check("providers: offset is honoured",
      [a.album_id for a in store.list_albums(limit=2, offset=1)]
      == [a.album_id for a in albums[1:3]], "wrong window")
check("providers: get_album(None) returns None rather than raising",
      store.get_album("no-such-album") is None, "returned something")

# Prices are strings, deliberately: binary floats lose cents.
track0 = albums[0].tracks[0]
check("models: price is a string, not a float",
      isinstance(track0.price, str), type(track0.price).__name__)

# format_duration is duplicated from FAI Server.py on purpose; the two must
# agree or the store shows a different length to WMP than the FAI dialog does.
check("models: format_duration M:SS", format_duration(214000) == "3:34",
      format_duration(214000))
check("models: format_duration H:MM:SS", format_duration(3725000) == "1:02:05",
      format_duration(3725000))
check("models: format_duration zero is empty", format_duration(0) == "", "not empty")
check("models: format_duration garbage is empty, not a crash",
      format_duration("abc") == "", "not empty")

# Artwork must be a real PNG. The FAI test suite checks its placeholder the same
# way (structure and CRCs), and a broken cover is visible in WMP's task pane.
art = store.artwork(albums[0].album_id)
png = art.png_bytes() if art is not None else b""
check("providers: artwork is a PNG", png.startswith(b"\x89PNG\r\n\x1a\n"),
      repr(png[:8]))
check("providers: artwork PNG has IEND", b"IEND" in png, "missing IEND")
check("providers: artwork for an unknown album is None",
      store.artwork("no-such-album") is None, "returned something")


# ======================================================================
# 5. PURCHASE: delivery must produce a genuinely playable file
# ======================================================================
# A "purchase" that returns a path to a file no player can open would pass a
# naive existence check and still be useless, so the WAV is opened with the
# stdlib and its header inspected.
purchases_dir = tempfile.mkdtemp()
buyer = create_provider("local_fake", purchases_dir=purchases_dir)
buyable = buyer.list_albums(limit=1)[0]
target = buyable.tracks[0]

check("providers: fake store supports purchase",
      buyer.supports_purchase is True, str(buyer.supports_purchase))
check("providers: purchase of an unknown track is None, not an error",
      buyer.purchase("no-such-track") is None, "returned something")
check("providers: tracks are marked purchasable when priced",
      target.is_purchasable is True, str(target.price))

purchase = buyer.purchase(target.track_id)
check("providers: purchase returns a Purchase", purchase is not None, "None")
if purchase is not None:
    check("purchase: reports the track id back",
          purchase.track_id == target.track_id, purchase.track_id)
    check("purchase: file exists on disk",
          os.path.isfile(purchase.local_path), purchase.local_path)
    check("purchase: size is reported and non-zero",
          purchase.size_bytes > 0, str(purchase.size_bytes))
    check("purchase: size matches what is on disk",
          purchase.size_bytes == os.path.getsize(purchase.local_path),
          "%d vs %d" % (purchase.size_bytes, os.path.getsize(purchase.local_path)))
    check("purchase: content type is audio", 
          purchase.content_type.startswith("audio/"), purchase.content_type)
    # The whole point of synthesising: a real file WMP can open.
    try:
        with wave.open(purchase.local_path, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            check("purchase: file is a readable mono WAV",
                  handle.getnchannels() == 1, str(handle.getnchannels()))
            check("purchase: WAV has frames", frames > 0, str(frames))
            # Duration must match the catalog, or the store lies about the
            # length of what it just handed the user.
            expected_ms = int(round(frames / float(rate) * 1000))
            check("purchase: duration matches the catalog entry",
                  abs(expected_ms - target.duration_ms) < 1500,
                  "file=%dms catalog=%dms" % (expected_ms, target.duration_ms))
    except Exception as exc:
        check("purchase: file is a readable WAV", False, str(exc))

    # Idempotent: buying twice must not regenerate, so a user who notes the
    # path and buys again sees the same file.
    stamp = os.path.getmtime(purchase.local_path)
    again = buyer.purchase(target.track_id)
    check("purchase: buying twice returns the same path",
          again.local_path == purchase.local_path, again.local_path)
    check("purchase: buying twice does not rewrite the file",
          os.path.getmtime(purchase.local_path) == stamp, "mtime changed")

    # Synthesis must be deterministic: the same track yields the same bytes on
    # a fresh provider, or a hash a user recorded becomes meaningless.
    other = create_provider("local_fake", purchases_dir=tempfile.mkdtemp())
    repeat = other.purchase(target.track_id)
    with open(purchase.local_path, "rb") as a, open(repeat.local_path, "rb") as b:
        check("purchase: generated audio is byte-identical across runs",
              a.read() == b.read(), "differs")


# Path safety: a catalog title becomes a directory name, so a title carrying
# path separators must not be able to write outside the purchases directory.
check("providers: purchase path stays under the purchases directory",
      os.path.commonpath([os.path.abspath(purchase.local_path),
                          os.path.abspath(purchases_dir)])
      == os.path.abspath(purchases_dir),
      purchase.local_path)


# ======================================================================
# 6. ROUTES: the storefront, mounted on a real Flask app
# ======================================================================
# Two apps are built: one with the store ENABLED and one DISABLED. The
# disabled one is the important case - it is what every existing user of this
# project has, and the store must be invisible there rather than broken.
import importlib                                            # noqa: E402
from flask import Flask                                     # noqa: E402
import online_store.views as store_views                    # noqa: E402

store_tmp = tempfile.mkdtemp()


def build_app(enabled):
    """Build a minimal app with the store mounted, enabled or not."""
    test_app = Flask("store_test_%s" % enabled)
    # The file name is not arbitrary: load_config() looks for exactly this one,
    # so writing "store.ini" here would silently leave every default in place
    # and the enabled app would come up disabled.
    ini = os.path.join(store_tmp, "online_store.ini")
    with open(ini, "w", encoding="utf-8") as handle:
        handle.write("[online_store]\n")
        handle.write("enabled = %s\n" % ("true" if enabled else "false"))
        handle.write("base_url = http://127.0.0.1/online-store\n")
        handle.write("purchases_dir = %s\n" % store_tmp.replace("\\", "\\\\"))
    # A fresh Blueprint object per app: Flask refuses to register the same
    # blueprint name twice, and two apps are built here.
    importlib.reload(store_views)
    ok = store_views.init_store(test_app, server_dir=store_tmp, environ={},
                                log=lambda *a: None)
    return test_app, ok


on_app, on_ok = build_app(True)
on = on_app.test_client()
check("routes: store enables when configured enabled", on_ok is True, str(on_ok))

r = on.get("/online-store/")
check("routes: storefront home is 200", r.status_code == 200, str(r.status_code))
home = r.data.decode("utf-8", "ignore")
check("routes: home shows the store name", "Legacy Music Store" in home, "absent")
check("routes: home lists albums from the provider",
      "lms-0001" in home, "no album id in the page")
check("routes: home links to the album page",
      "/online-store/album/" in home, "no album link")

# The ServiceInfo document over HTTP - the exact call WMP makes.
r = on.get("/online-store/serviceinfo.xml")
check("routes: serviceinfo.xml is 200", r.status_code == 200, str(r.status_code))
check("routes: serviceinfo is served as XML",
      "xml" in r.headers.get("Content-Type", ""), r.headers.get("Content-Type"))
try:
    http_root = ET.fromstring(r.data.decode("utf-8"))
    check("routes: served serviceinfo parses with the right Key",
          http_root.tag == "ServiceInfo"
          and http_root.get("Key") == "legacy_music_store",
          r.data[:200].decode("utf-8", "ignore"))
except ET.ParseError as exc:
    check("routes: served serviceinfo parses with the right Key", False, str(exc))

# The bare /serviceinfo alias exists because BASEURL + serviceinfo.xml is the
# inferred fetch shape; serving both halves makes that guess testable.
check("routes: serviceinfo (no extension) is also served",
      on.get("/online-store/serviceinfo").status_code == 200, "not 200")

# Album page, including the 404 for an unknown id.
r = on.get("/online-store/album/lms-0001")
check("routes: album page is 200", r.status_code == 200, str(r.status_code))
album_html = r.data.decode("utf-8", "ignore")
check("routes: album page lists its tracks", "t01" in album_html, "no track id")
check("routes: album page has a buy form", "/online-store/buy" in album_html,
      "no buy form")
check("routes: unknown album is 404, not 500",
      on.get("/online-store/album/no-such-album").status_code == 404,
      str(on.get("/online-store/album/no-such-album").status_code))

check("routes: search with a match is 200",
      on.get("/online-store/search?q=blues").status_code == 200, "not 200")
check("routes: search shows the hit",
      "lms-0002" in on.get("/online-store/search?q=blues")
      .data.decode("utf-8", "ignore"), "no hit in results")
check("routes: search with no match still renders",
      on.get("/online-store/search?q=zzzznope").status_code == 200, "not 200")
check("routes: search with an empty query still renders",
      on.get("/online-store/search?q=").status_code == 200, "not 200")

# Artwork: a PNG for a real album, and a transparent PNG (not a 404) for an
# unknown one, because a broken image in WMP's task pane reads as a fault.
r = on.get("/online-store/art/lms-0001")
check("routes: art is 200 PNG",
      r.status_code == 200 and r.data.startswith(b"\x89PNG\r\n\x1a\n"),
      "%s %r" % (r.status_code, r.data[:8]))
check("routes: art for an unknown album is a PNG, not a 404",
      on.get("/online-store/art/no-such-album").status_code == 200, "not 200")

# The buy flow, end to end, through HTTP.
track_id = on.get("/online-store/api/album/lms-0001") \
             .get_json()["tracks"][0]["track_id"]
r = on.post("/online-store/buy", data={"track_id": track_id})
check("routes: buy is 200", r.status_code == 200, str(r.status_code))
check("routes: buy reports the written file",
      "wav" in r.data.decode("utf-8", "ignore").lower(), "no file mentioned")
check("routes: buy with no track_id is 400",
      on.post("/online-store/buy", data={}).status_code == 400, "not 400")
check("routes: buy of an unknown track is 404",
      on.post("/online-store/buy", data={"track_id": "nope"}).status_code == 404,
      "not 404")

# Navigate and downloads - both named in the ServiceInfo document, so both must
# exist or WMP follows a URL this server does not serve.
check("routes: navigate base is served",
      on.get("/online-store/nav").status_code in (200, 302), "not 200/302")
check("routes: downloads page is served",
      on.get("/online-store/downloads").status_code == 200, "not 200")

# JSON API.
albums = on.get("/online-store/api/albums").get_json()
check("routes: api/albums returns JSON with albums",
      isinstance(albums.get("albums"), list) and len(albums["albums"]) > 0,
      str(type(albums)))
check("routes: api/albums honours limit",
      len(on.get("/online-store/api/albums?limit=2").get_json()["albums"]) == 2,
      "limit ignored")
# A nonsense or abusive limit must be clamped, not raise.
check("routes: api/albums clamps a nonsense limit",
      on.get("/online-store/api/albums?limit=abc").status_code == 200, "not 200")
check("routes: api/search returns JSON",
      isinstance(on.get("/online-store/api/search?q=blues")
                 .get_json().get("count"), int), "no count")
status_json = on.get("/online-store/api/status").get_json()
check("routes: api/status reports the store as enabled",
      status_json.get("enabled") is True, str(status_json.get("enabled")))
check("routes: api/status lists registered providers",
      "local_fake" in status_json.get("providers_registered", []),
      str(status_json.get("providers_registered")))
check("routes: api/status reports the serviceinfo url",
      status_json.get("serviceinfo_url", "").endswith("/serviceinfo.xml"),
      status_json.get("serviceinfo_url"))

# The disabled app: the store must be off, and off rather than 500.
#
# This is built AFTER every enabled check on purpose. The store's live config
# is module-level state (one process serves one store), so building the
# disabled app earlier would switch the enabled app's routes out from under
# these tests. Running the phases in order also matches reality: a user either
# has the store enabled or does not, never both at once.
off_app, off_ok = build_app(False)
off = off_app.test_client()
check("routes: store reports disabled when configured off", off_ok is False,
      str(off_ok))

r = off.get("/online-store/")
check("routes: home is 503 when the store is disabled",
      r.status_code == 503, str(r.status_code))
check("routes: disabled message names the file to edit",
      b"online_store.ini" in r.data, r.data[:200].decode("utf-8", "ignore"))
check("routes: serviceinfo is 503 when disabled",
      off.get("/online-store/serviceinfo.xml").status_code == 503, "not 503")
r = off.get("/online-store/api/status")
check("routes: api/status still answers when disabled", r.status_code == 200,
      str(r.status_code))
disabled_status = r.get_json()
check("routes: api/status explains why it is disabled",
      disabled_status.get("enabled") is False and bool(disabled_status.get("error")),
      str(disabled_status.get("error")))


# ======================================================================
# 7. NON-REGRESSION: the store must not disturb the FAI server
# ======================================================================
# This is the most important section in the file. Everything above tests the
# store; this tests that adding the store did not break the thing it was added
# to. A regression here is invisible to a store-only test suite and would hit
# every existing user of the project.
import importlib.util                                       # noqa: E402

_server_file = os.path.join(_DIR, "FAI Server.py")
if not os.path.exists(_server_file):
    check("regression: FAI Server.py is present next to the tests", False,
          "not found at %s" % _server_file)
else:
    # Loaded exactly as wsgi.py loads it: "FAI Server.py" is not a legal module
    # name, so importlib is the only way in.
    spec = importlib.util.spec_from_file_location("fai_server", _server_file)
    fai = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fai)
    fai_client = fai.app.test_client()

    # Every pre-existing FAI route must still answer, and none may have been
    # swallowed by a store route. /online-store/ is the only new prefix.
    for path in ("/fai_status", "/status", "/FAI/ui", "/client_error",
                 "/static/noart.png", "/store_staged_xml", "/done",
                 "/api_search", "/confirm", "/track_navigation"):
        response = fai_client.get(path)
        check(f"regression: FAI route {path} still answers (not 404)",
              response.status_code != 404, str(response.status_code))

    check("regression: /fai_status still returns its JSON",
          fai_client.get("/fai_status?format=json").get_json() is not None,
          "no JSON")
    check("regression: the FAI dialog still renders",
          b"<html" in fai_client.get("/FAI/ui?artist=Test").data.lower(),
          "no html")

    # The store's own prefix is present on the same app, which is the whole
    # point of using a Blueprint instead of a second server.
    check("regression: store routes are mounted on the FAI app",
          fai_client.get("/online-store/api/status").status_code == 200,
          str(fai_client.get("/online-store/api/status").status_code))

    # The store's after_request hook must only stamp store responses.
    store_headers = fai_client.get("/online-store/api/status").headers
    check("store: adds its own security headers",
          "X-Content-Type-Options" in store_headers, str(dict(store_headers)))

    # The store must be OFF for this section, and the repo's own
    # online_store.ini may well say enabled = true, so the switch is forced
    # through the environment. That is the real-world case being asserted: a
    # user who never touches the store config still gets a working FAI server.
    # Asserting against the repo's INI contents instead would make this test
    # fail the moment someone enables the store locally, which is nonsense.
    os.environ["WMP_STORE_ENABLED"] = "0"
    try:
        spec2 = importlib.util.spec_from_file_location("fai_server_disabled",
                                                       _server_file)
        fai_off = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(fai_off)
        check("regression: FAI server starts with the store forced off",
              fai_off._ONLINE_STORE_ENABLED is False,
              str(fai_off._ONLINE_STORE_ENABLED))
        off_client = fai_off.app.test_client()
        check("regression: FAI still serves with the store off",
              off_client.get("/fai_status").status_code == 200, "not 200")
        check("regression: the store is off, not broken, when disabled",
              off_client.get("/online-store/").status_code == 503,
              str(off_client.get("/online-store/").status_code))
    finally:
        os.environ.pop("WMP_STORE_ENABLED", None)



# ======================================================================
# 8. CLI: the documented install/uninstall commands
# ======================================================================
from online_store import cli as store_cli                   # noqa: E402

importlib.reload(store_cli)
check("cli: main with no arguments prints usage and exits 2",
      store_cli.main([]) == 2, "expected the usage/exit-2 path")
check("cli: help lists all three commands",
      all(name in " ".join(store_cli.COMMANDS)
          for name in ("install-online-store", "uninstall-online-store",
                       "online-store-status")), str(store_cli.COMMANDS))
# install must refuse to register a store that is not enabled, because pointing
# WMP at a URL that answers 503 helps nobody. The INI written by build_app(False)
# is the disabled one, and the second build_app call left it in place.
check("cli: install refuses while the store is disabled",
      store_cli.main(["--store-dir", store_tmp,
                      "install-online-store", "--dry-run"]) == 2,
      "did not refuse")
check("cli: status works against a real config",
      store_cli.main(["--store-dir", store_tmp, "online-store-status"]) == 0,
      "non-zero")
# Dry runs must not touch the registry. describe_install is what install calls
# before writing, so proving it is pure is what makes --dry-run trustworthy.
check("cli: the install plan is stable and side-effect free",
      registry.describe_install(cfg) == registry.describe_install(cfg),
      "plan changed between calls")

# ---- helpers for the local_settings / Discogs regression --------------------
def _settings_loader_finds_offpath_file():
    """The loader must work from a directory that is NOT on sys.path.

    Reproduces the frozen layout rather than mocking it: a temp dir holding
    local_settings.py, reached only through sys.executable, with sys.path
    cleared of it. If this regresses to a bare `import local_settings`, the
    Windows 7 build loses Discogs again with no error anywhere.
    """
    import importlib.util
    import os
    import sys as _sys
    import tempfile

    server = importlib.import_module("FAI Server")
    loader = getattr(server, "_load_local_settings", None)
    if loader is None:
        return False

    dist = tempfile.mkdtemp()
    with open(os.path.join(dist, "local_settings.py"), "w", encoding="utf-8") as fh:
        fh.write("DISCOGS_TOKEN = 'offpath-token'\n")

    real_path = list(_sys.path)
    real_frozen = getattr(_sys, "frozen", False)
    real_exec = _sys.executable
    try:
        # Nothing in sys.path may reveal the file: only sys.executable's
        # directory may. That is precisely the frozen constraint.
        _sys.path[:] = [p for p in real_path
                        if os.path.abspath(p or ".") != os.path.abspath(dist)]
        _sys.frozen = True
        _sys.executable = os.path.join(dist, "server.exe")
        module = loader()
    finally:
        _sys.path[:] = real_path
        _sys.frozen = real_frozen
        _sys.executable = real_exec

    return (module is not None
            and getattr(module, "DISCOGS_TOKEN", "") == "offpath-token")


def _status_reports_discogs():
    payload = store_cli._status_payload(None)
    discogs = payload.get("discogs")
    if not isinstance(discogs, dict):
        return None
    return "configured" in discogs


def _status_prints_discogs_line():
    import io
    import contextlib

    payload = {"providers_registered": [], "registry_supported": False,
               "discogs": {"configured": False, "token_length": 0}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        store_cli._print_status(payload, None)
    return "discogs" in buf.getvalue().lower()


def _status_output_omits_token():
    """The token must never be printed, only whether one exists."""
    import io
    import contextlib
    import importlib

    server = importlib.import_module("FAI Server")
    secret = getattr(server, "DISCOGS_TOKEN", "") or "s3cr3t-not-a-real-token"
    payload = {"providers_registered": [], "registry_supported": False,
               "discogs": {"configured": True, "token_length": len(secret)}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        store_cli._print_status(payload, None)
    return secret not in buf.getvalue()


# ---- a frozen build must find local_settings.py beside the EXE --------------
# This is the exact bug that made Discogs vanish from the Windows 7 build. The
# loader used a plain `import local_settings`, which only resolves when the file
# is on sys.path - true in a checkout, false in a PyInstaller onedir build,
# where __file__ lives in _internal\ and the settings file is a sibling of the
# .exe. With no token Discogs does not error, it returns empty, so the only
# symptom was a provider silently missing from the list.
check("settings loader: finds a file that is not on sys.path",
      _settings_loader_finds_offpath_file(),
      "could not load local_settings.py from a directory absent from sys.path")

# The status command must SAY that Discogs is unconfigured. Silence is what
# made this take hours to find: no error, no warning, just no Discogs rows.
_discogs_reported = _status_reports_discogs()
check("cli: status reports Discogs configuration state",
      _discogs_reported is not None,
      "status payload carried no discogs key")
if _discogs_reported is not None:
    check("cli: the discogs status line is actually printed",
          _status_prints_discogs_line(),
          "payload had the key but the printed output did not")
    check("cli: the discogs status never leaks the token",
          _status_output_omits_token(),
          "token text appeared in status output")

def _installer_post_install_block_parses():
    """The installer's real success text must be valid cmd.

    That text sits inside an if/else block and contains parentheses. In cmd, an
    unescaped ")" inside a parenthesised block CLOSES it, so a line reading
    "...(containing DISCOGS_TOKEN = '...') so it" terminates the block early and
    cmd aborts with "so was unexpected at this time". Verified on this machine -
    it is not a theoretical worry.

    Two things make this worth automating. It only runs on a REAL install, so
    /D never reaches it, and the text only exists inside the .bat, so a Python
    test could not see it at all before. This runs the REAL file, driven with
    the variables set so the success branch is taken.
    """
    import subprocess
    import tempfile

    installer = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "install-online-store-win7.bat")
    if not os.path.exists(installer):
        return False

    # Read the real file and force the success branch, then let it run far
    # enough to echo the block. The EXE is never invoked: the registry section
    # is skipped by jumping straight past it.
    with open(installer, encoding="utf-8", errors="replace") as fh:
        source = fh.read()

    # Anchor on text that must exist for this extraction to mean anything. If
    # the installer is restructured, return False loudly rather than slicing a
    # meaningless span and calling it a pass.
    if 'echo   Installed.' not in source:
        return False
    if 'call :pause_if_interactive' not in source:
        return False

    # Keep only the reporting section: the echo block and its closing braces.
    start = source.rindex("echo ============================================================",
                          0, source.index('echo   Installed.'))
    end = source.index('call :pause_if_interactive', start)
    body = source[start:end]

    harness = (
        '@echo off\r\n'
        'set "ACTION=install"\r\n'
        'set "DRYRUN=0"\r\n'
        '(\r\n'
        + body.replace("\n", "\r\n") +
        ')\r\n'
        'echo BLOCK PARSED AND COMPLETED\r\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".bat", delete=False,
                                     encoding="ascii", errors="replace",
                                     newline="") as fh:
        fh.write(harness)
        path = fh.name
    proc = subprocess.run(["cmd", "/c", path], capture_output=True, text=True)
    try:
        os.unlink(path)
    except OSError:
        pass
    return "BLOCK PARSED AND COMPLETED" in proc.stdout


check("installer: the post-install message is valid cmd",
      _installer_post_install_block_parses(),
      "the success block does not parse")

print("")
print("=" * 60)
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("Failures:")
    for name in FAIL:
        print(f"  - {name}")
print("=" * 60)
sys.exit(1 if FAIL else 0)








