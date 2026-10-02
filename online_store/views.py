# -*- coding: utf-8 -*-
"""The Online Store storefront, as a Flask Blueprint on the existing app.

This is a Blueprint, not a second server. The FAI application already owns
port 80, port 443, the hosts-file redirection, the TLS certificate and the
request log; standing up another process would mean a second thing to
administrate and a second thing to forget to start, for no benefit. Registering
on the existing ``app`` means the store is served by the same listener that
already answers WMP, and it is reachable at a plain ``/online-store/...``
prefix.

Routes
------
===========================  ======  =========================================
Route                        Method  Purpose
===========================  ======  =========================================
``/online-store/``           GET     Storefront home (ServiceTask1's URL)
``/online-store/album/<id>`` GET     One album and its track list
``/online-store/search``     GET     Search the active provider
``/online-store/serviceinfo`` GET    The ServiceInfo XML document
``/online-store/nav``        GET     Base for ``External.NavigateTaskPaneURL``
``/online-store/downloads``  GET     DownloadStatus target
``/online-store/art/<id>``   GET     Generated cover art (PNG)
``/online-store/buy``        POST    Resolve a track to a playable file
``/online-store/api/albums`` GET     JSON catalog
``/online-store/api/search`` GET     JSON search
``/online-store/api/status`` GET     JSON diagnostics
===========================  ======  =========================================

The HTML is rendered with ``render_template_string`` rather than Jinja files,
matching the rest of this project: ``build_exe.py`` documents that the frozen
build ships no template or data files, and introducing a template directory
would mean changing the packaging contract for a feature that does not need it.
"""
import json
import os

from flask import (Blueprint, Response, jsonify, redirect, render_template_string,
                   request, url_for)

from . import registry, serviceinfo
from .config import ConfigError, load_config
from .models import format_duration
from .providers import ProviderError, available_providers, create_provider

#: The URL prefix. Configurable would mean the ServiceInfo document, the
#: registry BASEURL and the FAI server's own routes would all need to agree on
#: a value that must be knowable before the server starts - so it is a constant
#: and config controls the *host* part of the URL instead.
URL_PREFIX = "/online-store"

#: Set by :func:`init_store` so every request handler reads the same live
#: config without re-parsing the INI on each request.
_state = {"config": None, "provider": None, "error": None}


def _html_page(title, body, config, nav_active=""):
    """Wrap a page in the storefront chrome.

    Deliberately plain, self-contained HTML with no external CSS, JS or fonts.
    WMP hosts this page inside a browser control whose document is created
    from a local URL, and a page that reaches out to a CDN can be slow, blocked
    or render differently depending on the machine's security zone.

    The placeholders are Jinja (``{{ }}``) because this goes through
    ``render_template_string``, which is Jinja and NOT Python's ``%``-formatting.
    That distinction is not academic: written as ``%(title)s`` the template
    renders literally, so every page ships with ``%(body)s`` in it and looks
    empty in WMP's task pane while still returning HTTP 200. Hence the test
    that asserts the store name is actually in the served bytes.
    """
    accent = config.button_color
    return render_template_string("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
 :root { --accent: {{ accent }}; --text: #FFFFFF; }
 * { box-sizing: border-box; }
 body { margin:0; font: 13px/1.5 "Segoe UI", Tahoma, sans-serif;
        background:#1E1E24; color:#E8E8EC; }
 header { background:var(--accent); color:var(--text); padding:10px 14px; }
 header h1 { margin:0; font-size:15px; font-weight:600; }
 header .sub { opacity:.85; font-size:11px; margin-top:2px; }
 nav { background:#2A2A33; padding:0 8px; border-bottom:1px solid #3A3A46; }
 nav a { display:inline-block; padding:8px 12px; color:#BFC0C8;
         text-decoration:none; font-size:12px; }
 nav a.on, nav a:hover { color:#FFF; background:#3A3A46; }
 main { padding:14px; }
 a { color:#7AB7FF; }
 input[type=text], input[type=search] { background:#17171C; color:#E8E8EC;
   border:1px solid #3A3A46; padding:6px 8px; border-radius:3px; width:220px; }
 button { background:var(--accent); color:var(--text); border:0;
   padding:6px 12px; border-radius:3px; cursor:pointer; font-size:12px; }
 button:disabled { background:#45454F; cursor:default; }
 .grid { display:flex; flex-wrap:wrap; gap:14px; }
 .card { background:#26262E; border:1px solid #33333D; border-radius:4px;
   padding:10px; width:200px; }
 .card img { width:100%; height:200px; object-fit:cover; border-radius:3px;
   display:block; background:#17171C; }
 .card .t { font-weight:600; margin:8px 0 2px; }
 .card .a { color:#9A9AA6; font-size:12px; }
 .card .p { color:#9FD39A; font-size:12px; margin-top:6px; }
 table { width:100%; border-collapse:collapse; }
 th, td { text-align:left; padding:7px 8px; border-bottom:1px solid #33333D;
   font-size:12px; }
 th { color:#9A9AA6; font-weight:600; }
 .dur { color:#9A9AA6; }
 .note { color:#9A9AA6; font-size:11px; margin-top:10px; }
 .err { background:#3A2020; border:1px solid #6A3030; color:#FFB0B0;
   padding:10px; border-radius:3px; }
 .flash { background:#1F3320; border:1px solid #2F5A30; color:#B0E8B0;
   padding:8px 10px; border-radius:3px; margin-bottom:12px; }
</style>
</head>
<body>
<header>
  <h1>{{ store }}</h1>
  <div class="sub">Type 2 commerce store &middot; {{ provider_label }}</div>
</header>
<nav>
  <a href="{{ prefix }}/" class="{{ on_home }}">Browse</a>
  <a href="{{ prefix }}/downloads" class="{{ on_dl }}">Downloads</a>
  <a href="{{ prefix }}/api/status">Status</a>
</nav>
<main>{{ body|safe }}</main>
</body>
</html>""",
        title=title, accent=accent, store=config.friendly_name,
        provider_label=(_state.get("provider").display_name
                        if _state.get("provider") else "no provider"),
        prefix=URL_PREFIX, body=body,
        on_home="on" if nav_active == "home" else "",
        on_dl="on" if nav_active == "downloads" else "")


def _get_state():
    """Return ``(config, provider)``, or raise a clear error if unconfigured."""
    config = _state.get("config")
    if config is None or not config.enabled:
        raise ConfigError(_state.get("error")
                          or "the online store is disabled "
                             "(set enabled = true in online_store.ini)")
    return config, _state.get("provider")


def _art_src(album_id):
    return "%s%s/art/%s" % (_state["config"].base_url, URL_PREFIX, album_id)


def _money(value, currency):
    if value is None:
        return ""
    return "%s %s" % (value, currency or "")


def _album_card(album):
    return """<div class="card">
  <a href="%(prefix)s/album/%(id)s"><img src="%(art)s" alt="%(title)s"></a>
  <div class="t"><a href="%(prefix)s/album/%(id)s">%(title)s</a></div>
  <div class="a">%(artist)s</div>
  <div class="p">%(price)s</div>
</div>""" % {
        "prefix": URL_PREFIX,
        "id": album.album_id,
        "art": _art_src(album.album_id),
        "title": album.title,
        "artist": album.artist,
        "price": _money(album.price, album.currency),
    }


def _purchases_dir():
    configured = _state["config"].purchases_dir
    if configured:
        return configured
    # Same location the FAI server already uses for its certificate and log, so
    # the store adds no new place for a user to look.
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WMP_FAIServer", "online_store_purchases")


blueprint = Blueprint("online_store", __name__)


@blueprint.route(URL_PREFIX + "/")
def storefront_home():
    """The store's main page - the URL advertised as ServiceTask1."""
    config, provider = _get_state()
    albums = provider.list_albums(limit=60)
    body = ["""<form method="get" action="%s/search" style="margin-bottom:12px">
  <input type="search" name="q" placeholder="Search the catalog" value="%s">
  <button type="submit">Search</button>
</form>""" % (URL_PREFIX, request.args.get("q", ""))]
    if albums:
        body.append('<div class="grid">%s</div>'
                    % "".join(_album_card(album) for album in albums))
    else:
        body.append('<p class="note">The catalog is empty.</p>')
    body.append('<p class="note">%d album(s). Content is synthesised locally '
                'for testing; no commercial store is connected.</p>'
                % len(albums))
    return _html_page(config.friendly_name, "".join(body), config, "home")


@blueprint.route(URL_PREFIX + "/album/<album_id>")
def storefront_album(album_id):
    config, provider = _get_state()
    album = provider.get_album(album_id)
    if album is None:
        return _html_page("Not found",
                          '<p class="err">No album with id %s.</p>' % album_id,
                          config), 404
    rows = []
    for track in album.tracks:
        price = _money(track.price, track.currency)
        rows.append(
            "<tr><td>%d</td><td>%s</td><td class='dur'>%s</td>"
            "<td class='dur'>%s</td>"
            "<td><form method='post' action='%s/buy' style='display:inline'>"
            "<input type='hidden' name='track_id' value='%s'>"
            "<button type='submit'>Buy %s</button></form></td></tr>"
            % (track.track_number, track.title,
               format_duration(track.duration_ms), price,
               URL_PREFIX, track.track_id, price))
    body = """<p><a href="%s/">&larr; Back</a></p>
<h2 style="margin:4px 0 0">%s</h2>
<div style="color:#9A9AA6; margin-bottom:10px">%s%s%s</div>
<img src="%s" alt="" style="width:150px;height:150px;object-fit:cover;
     border-radius:3px;float:right;margin:0 0 10px 14px">
<table>
 <tr><th>#</th><th>Title</th><th>Length</th><th>Price</th><th></th></tr>
 %s
</table>
<div style="clear:both"></div>
<p class="note">%s</p>""" % (
        URL_PREFIX, album.title, album.artist,
        " &middot; %s" % album.genre if album.genre else "",
        " &middot; %s" % album.year if album.year else "",
        _art_src(album.album_id), "".join(rows), album.description)
    return _html_page(album.title, body, config, "home")


@blueprint.route(URL_PREFIX + "/search")
def storefront_search():
    config, provider = _get_state()
    query = (request.args.get("q") or "").strip()
    if query:
        albums = provider.search(query, limit=60)
        body = ["<h2>Results for &ldquo;%s&rdquo; (%d)</h2>"
                % (query, len(albums))]
        body.append(('<div class="grid">%s</div>'
                     % "".join(_album_card(album) for album in albums))
                    if albums else '<p class="note">Nothing matched.</p>')
    else:
        body = ['<p class="note">Type something in the search box.</p>']
    body.append('<p class="note"><a href="%s/">Back to browse</a></p>'
                % URL_PREFIX)
    return _html_page("Search", "".join(body), config, "home")


@blueprint.route(URL_PREFIX + "/serviceinfo.xml")
@blueprint.route(URL_PREFIX + "/serviceinfo")
def store_serviceinfo():
    """Serve the ServiceInfo document WMP is documented to fetch.

    ``.xml`` is the documented extension; the bare name is accepted too
    because ``BASEURL`` + ``serviceinfo.xml`` is the inferred fetch shape, and
    serving both halves makes that guess cheap to test.
    """
    config, provider = _get_state()
    return Response(serviceinfo.build_service_info(config, provider),
                    mimetype="text/xml")


@blueprint.route(URL_PREFIX + "/nav")
@blueprint.route(URL_PREFIX + "/nav/<path:rest>")
def store_navigate(rest=""):
    """The ``Navigate`` base URL, and the pages it can point at.

    ``External.NavigateTaskPaneURL()`` is documented as taking a URL relative
    to this base, so this is where a hosted page sends the user.
    """
    _get_state()
    return redirect(URL_PREFIX + "/")


@blueprint.route(URL_PREFIX + "/downloads")
def store_downloads():
    """The ``DownloadStatus`` target: what has been bought, and where it went."""
    config, _ = _get_state()
    purchases_dir = _purchases_dir()
    if not os.path.isdir(purchases_dir):
        listing = ('<p class="note">Purchases directory not created yet. Set '
                   '<code>purchases_dir</code> in online_store.ini to change '
                   'where bought files land.</p>')
    else:
        found = []
        for root, _dirs, files in os.walk(purchases_dir):
            for name in files:
                if name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                    found.append(os.path.join(root, name))
        found.sort()
        listing = ("<ul>%s</ul>" % "".join("<li>%s</li>" % path
                                          for path in found[:200])
                   if found else '<p class="note">Nothing purchased yet.</p>')
    body = ('<h2>Downloads</h2><p class="note">Everything here was produced '
            'locally by the fake provider.</p>%s' % listing)
    return _html_page("Downloads", body, config, "downloads")


@blueprint.route(URL_PREFIX + "/art/<album_id>")
def store_art(album_id):
    """Generated cover art.

    Falls back to a 1x1 transparent PNG rather than a 404, because a broken
    image in a hosted page is visible in WMP's task pane and a 404 there looks
    like a server fault rather than "this album has no cover".
    """
    config, provider = _get_state()
    art = provider.artwork(album_id)
    if art is None or not hasattr(art, "png_bytes"):
        return Response(_TRANSPARENT_PNG, mimetype="image/png")
    return Response(art.png_bytes(), mimetype="image/png")


#: 1x1 fully transparent PNG, base64. Used only as the no-cover fallback.
_TRANSPARENT_PNG = (
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    b"+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@blueprint.route(URL_PREFIX + "/buy", methods=["POST"])
def store_buy():
    """Resolve a track to a playable file.

    There is no payment step and no account, because this is a fake store: the
    button exists so the delivery path - provider to disk to user - is real and
    testable end to end. A real provider replaces
    :meth:`StoreProvider.purchase` with its own fulfilment and nothing else in
    this route changes.
    """
    config, provider = _get_state()
    track_id = (request.form.get("track_id")
                or request.args.get("track_id") or "").strip()
    if not track_id:
        return _html_page("No track", '<p class="err">No track_id given.</p>',
                          config), 400
    if not getattr(provider, "supports_purchase", False):
        return _html_page(
            "Not for sale",
            '<p class="err">Provider %r cannot deliver purchases.</p>'
            % provider.provider_id, config), 501
    purchase = provider.purchase(track_id)
    if purchase is None:
        return _html_page(
            "Not found",
            '<p class="err">Provider %r has no track %s.</p>'
            % (provider.provider_id, track_id), config), 404
    body = """<div class="flash">Purchased &ldquo;%s&rdquo; by %s.</div>
<p>File written to:</p>
<p><code>%s</code></p>
<p class="note">%s bytes, %s. Generated locally - this is not a DRM-protected
file and not a real recording. Open it in WMP with
<em>File &rarr; Open</em>, or browse the
<a href="%s/downloads">downloads page</a>.</p>""" % (
        purchase.title, purchase.artist, purchase.local_path,
        "{:,}".format(purchase.size_bytes), purchase.content_type, URL_PREFIX)
    return _html_page("Purchased", body, config, "downloads")


@blueprint.route(URL_PREFIX + "/api/albums")
def api_albums():
    _config, provider = _get_state()
    limit = _int_arg("limit", 50, 1, 200)
    offset = _int_arg("offset", 0, 0, 100000)
    albums = provider.list_albums(limit=limit, offset=offset)
    return jsonify({"count": len(albums), "offset": offset, "limit": limit,
                    "albums": [album.as_dict() for album in albums]})


@blueprint.route(URL_PREFIX + "/api/album/<album_id>")
def api_album(album_id):
    _config, provider = _get_state()
    album = provider.get_album(album_id)
    if album is None:
        return jsonify({"error": "no such album", "album_id": album_id}), 404
    return jsonify(album.as_dict())


@blueprint.route(URL_PREFIX + "/api/search")
def api_search():
    _config, provider = _get_state()
    query = (request.args.get("q") or "").strip()
    limit = _int_arg("limit", 25, 1, 200)
    albums = provider.search(query, limit=limit)
    return jsonify({"query": query, "count": len(albums),
                    "albums": [album.as_dict() for album in albums]})


def _int_arg(name, default, minimum, maximum):
    """Read a bounded integer query argument.

    Bounded rather than trusted: an unbounded ``int()`` here would let a
    request ask for ten million albums and tie the thread up generating them.
    """
    raw = request.args.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


@blueprint.route(URL_PREFIX + "/api/status")
def api_status():
    """Diagnostics: is the store configured, which provider, what is installed.

    Written for someone debugging "WMP does not show my store", which is the
    failure this whole subsystem is most likely to produce.
    """
    config = _state.get("config")
    provider = _state.get("provider")
    payload = {
        "enabled": bool(config and config.enabled),
        "url_prefix": URL_PREFIX,
        "providers_registered": available_providers(),
        "error": _state.get("error"),
        "registry_supported": registry.is_supported(),
    }
    if config is not None:
        payload["config"] = config.as_dict()
        payload["config_sources"] = config.source
        payload["serviceinfo_url"] = config.url_for("serviceinfo.xml")
    if provider is not None:
        payload["provider"] = provider.describe()
    if config is not None and registry.is_supported():
        payload["registry"] = {
            "subscriptions_key": _probe(
                "HKLM", "%s\\%s" % (registry.SUBSCRIPTIONS_ROOT, config.store_id)),
            "services_key": _probe(
                "HKCU", "%s\\%s" % (registry.SERVICES_ROOT, config.store_id)),
            "test_parameter": registry.read_values(
                "HKCU", registry.SERVICES_ROOT).get("TestParameter", ("",))[0],
        }
    return jsonify(payload)


def _probe(root, path):
    """Summarise a key's presence and values for the status endpoint."""
    try:
        values = registry.read_values(root, path)
    except registry.RegistryError as exc:
        return {"present": False, "error": str(exc)}
    return {"present": bool(values),
            "values": {name: value for name, (value, _kind) in values.items()}}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
# These are registered on the Blueprint, not the app, so a misconfigured store
# cannot change the error behaviour of any existing FAI route. That isolation is
# the whole reason this is a Blueprint rather than app-level handlers.


@blueprint.app_errorhandler(ConfigError)
def _handle_config_error(exc):
    """The store is off, or its config is broken.

    503 rather than 500: nothing has crashed, the subsystem is deliberately not
    running. The body says exactly which file to edit, because the most common
    cause is a config the user has not written yet.
    """
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "store_not_configured",
                        "detail": str(exc)}), 503
    config = _state.get("config")
    if config is None:
        # Nothing to render the chrome with, so return a bare minimal page.
        return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>Store unavailable</title></head><body>"
                "<h1>Online Store unavailable</h1><p>%s</p></body></html>"
                % str(exc).replace("<", "&lt;")), 503
    return _html_page("Store unavailable",
                      '<p class="err">%s</p>'
                      '<p class="note">The Find Album Info server is still '
                      'running normally; only the store is disabled.</p>'
                      % str(exc), config), 503


@blueprint.app_errorhandler(ProviderError)
def _handle_provider_error(exc):
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "provider_error", "detail": str(exc)}), 502
    config = _state.get("config")
    body = '<p class="err">%s</p>' % str(exc)
    if config is None:
        return body, 502
    return _html_page("Store error", body, config), 502


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def init_store(app, server_dir=None, environ=None, log=print):
    """Attach the Online Store to an existing Flask ``app``.

    Returns True when the store is enabled, False when it is not. It never
    raises for a store that is simply switched off: a checkout with no
    ``online_store.ini`` must behave exactly as it did before this subsystem
    existed, and an FAI server that refuses to start because an optional
    feature is unconfigured would be a regression for every existing user.

    It *does* swallow a config error, storing it for ``/api/status`` to report,
    because the same argument applies: a typo in the store's INI must not stop
    the FAI server from starting.
    """
    app.register_blueprint(blueprint)

    # The FAI server hands user-supplied strings to a page that calls
    # window.external.*, so the store's templates stay plain HTML with no
    # script. Still, the store pages are HTML documents from a local server and
    # get a frame-ancestors-free policy explicitly rather than by accident.
    @app.after_request
    def _store_headers(response):
        try:
            if request.path.startswith(URL_PREFIX):
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("Referrer-Policy", "no-referrer")
        except Exception:
            pass
        return response

    try:
        config = load_config(server_dir=server_dir, environ=environ)
    except ConfigError as exc:
        _state["error"] = str(exc)
        _state["config"] = None
        _state["provider"] = None
        log("[STORE] online store disabled: %s" % exc)
        return False

    _state["config"] = config
    if not config.enabled:
        _state["error"] = ("online store is disabled "
                           "(set enabled = true in online_store.ini)")
        _state["provider"] = None
        log("[STORE] online store present but disabled (store_id=%s)"
            % config.store_id)
        return False

    try:
        provider = create_provider(
            config.provider,
            purchases_dir=config.purchases_dir or _default_purchases_dir(),
            currency=config.currency)
    except ProviderError as exc:
        _state["error"] = str(exc)
        _state["provider"] = None
        log("[STORE] online store enabled but no provider: %s" % exc)
        return False

    _state["provider"] = provider
    _state["error"] = None
    log("[STORE] online store ready: %s (%s) at %s"
        % (config.friendly_name, config.provider, config.base_url))
    return True


def _default_purchases_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WMP_FAIServer", "online_store_purchases")




