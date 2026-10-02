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
from xml.sax.saxutils import escape

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
    """Wrap a page in the Media Guide chrome.

    Deliberately plain, self-contained HTML with no external CSS, JS or fonts.
    WMP hosts this page inside a browser control whose document is created from
    a local URL, and a page that reaches out to a CDN can be slow, blocked or
    render differently depending on the machine's security zone.

    The placeholders are Jinja (``{{ }}``) because this goes through
    ``render_template_string``, which is Jinja and NOT Python's ``%``-formatting.
    That distinction is not academic: written as ``%(title)s`` the template
    renders literally, so every page ships with ``%(body)s`` in it and looks
    empty in WMP's task pane while still returning HTTP 200. Hence the test
    that asserts the store name is actually in the served bytes.
    """
    css = _MEDIA_GUIDE_CSS % {
        "accent": config.button_color,
        "accent_text": config.button_text_color,
    }
    provider = _state.get("provider")
    provider_label = provider.display_name if provider else "no provider"
    # The CSS is interpolated AFTER Jinja renders, not passed in as a variable.
    # Jinja only substitutes {{ name }}, so a bare __CSS__ placeholder is not a
    # template expression and would ship to the browser literally - the page
    # would render completely unstyled. Doing the substitution here, on the
    # rendered result, is the only ordering that works.
    page = render_template_string("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>__CSS__</style>
</head>
<body>
<div class="mg-top"><div class="mg-titlebar">
  <h1>{{ store }}</h1>
  <span class="mg-sub">{{ provider_label }}</span>
  <span class="mg-crumb">{{ title }}</span>
</div></div>
<div class="mg-body">
  <nav class="mg-rail">
    <div class="mg-railhead">Media Guide</div>
    <a href="{{ prefix }}/" class="{{ on_home }}">Browse Music</a>
    <a href="{{ prefix }}/search" class="{{ on_search }}">Search</a>
    <a href="{{ prefix }}/downloads" class="{{ on_dl }}">Downloads</a>
    <a href="{{ prefix }}/api/status">Status</a>
  </nav>
  <main class="mg-main">{{ body|safe }}</main>
</div>
</body>
</html>""",
                              title=title, store=config.friendly_name,
                              provider_label=provider_label, prefix=URL_PREFIX,
                              body=body,
                              on_home="on" if nav_active == "home" else "",
                              on_search="on" if nav_active == "search" else "",
                              on_dl="on" if nav_active == "downloads" else "")
    if "__CSS__" not in page:
        raise RuntimeError("storefront template lost its CSS placeholder")
    return page.replace("__CSS__", css)


def _get_state():
    """Return ``(config, provider)``, or raise a clear error if unconfigured."""
    config = _state.get("config")
    if config is None or not config.enabled:
        raise ConfigError(_state.get("error")
                          or "the online store is disabled "
                             "(set enabled = true in online_store.ini)")
    return config, _state.get("provider")


def _art_src(album_id):
    # base_url ALREADY ends in the mount prefix (it is
    # "http://127.0.0.1/online-store"), so appending URL_PREFIX again produced
    # ".../online-store/online-store/art/..." and every cover 404'd. Only the
    # /art/<id> suffix belongs here.
    return "%s/art/%s" % (_state["config"].base_url.rstrip("/"), album_id)


def _money(value, currency):
    if value is None:
        return ""
    return "%s %s" % (value, currency or "")


#: The Windows Media Player 11 / 12 "Media Guide" chrome.
#:
#: WMP hosts this page in a task pane whose width it chooses, so the layout is
#: built to survive being squeezed to ~300px as well as filling a window:
#: sections wrap, the grid reflows to a single column, and nothing relies on a
#: fixed pixel width. That is the same constraint the original Media Guide
#: worked under, which is why the proportions here mirror it rather than a
#: modern responsive site.
#:
#: Colours are WMP's own, not a designer's: #1F5C99 blue chrome, #0F1216 page
#: background, #E8E8EC body text. Sourced from the WMP 11 "Media Guide" era
#: screenshots and matched to the Color element the store advertises.
_MEDIA_GUIDE_CSS = """
:root { --accent: %(accent)s; --accent-text: %(accent_text)s;
        --page: #0F1216; --panel: #1A1F26; --panel2: #232A33;
        --line: #2E3742; --text: #E8E8EC; --muted: #98A2AE; }
* { box-sizing: border-box; }
html, body { height: 100%%; }
body { margin: 0; background: var(--page); color: var(--text);
       font: 12px/1.45 "Segoe UI", Tahoma, "Microsoft Sans Serif", sans-serif; }
a { color: #6FB2F2; text-decoration: none; }
a:hover { text-decoration: underline; }

/* Title bar: the solid accent strip the Media Guide used across the top. */
.mg-top { background: var(--accent); color: var(--accent-text); }
.mg-titlebar { padding: 9px 14px 8px; display: flex; align-items: baseline;
               gap: 10px; flex-wrap: wrap; }
.mg-titlebar h1 { margin: 0; font-size: 15px; font-weight: 600; letter-spacing: .2px; }
.mg-titlebar .mg-sub { font-size: 11px; opacity: .85; }
.mg-crumb { font-size: 11px; opacity: .9; margin-left: auto; }

/* Left rail, like the Media Guide's category column. Collapses to a row on
   narrow panes rather than disappearing, so navigation is never lost. */
.mg-body { display: flex; align-items: stretch; min-height: 0; }
.mg-rail { width: 168px; flex: 0 0 168px; background: var(--panel);
           border-right: 1px solid var(--line); padding: 10px 0; }
.mg-rail a { display: block; padding: 7px 14px; color: var(--text); font-size: 12px; }
.mg-rail a:hover { background: var(--panel2); text-decoration: none; }
.mg-rail a.on { background: var(--accent); color: var(--accent-text);
                font-weight: 600; }
.mg-rail .mg-railhead { padding: 4px 14px 7px; color: var(--muted);
                        font-size: 10px; text-transform: uppercase;
                        letter-spacing: .6px; }
.mg-main { flex: 1 1 auto; min-width: 0; padding: 12px 14px 24px; }

@media (max-width: 520px) {
  .mg-body { display: block; }
  .mg-rail { width: auto; border-right: none; border-bottom: 1px solid var(--line);
             padding: 6px 0; }
  .mg-rail a, .mg-rail .mg-railhead { display: inline-block; padding: 6px 10px; }
}

/* Search box, styled as the Media Guide's inset field. */
.mg-search { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
.mg-search input { flex: 1 1 150px; min-width: 120px; background: #0A0D11;
                   color: var(--text); border: 1px solid var(--line);
                   padding: 6px 8px; border-radius: 2px; font-size: 12px; }
.mg-search button, .mg-buy { background: var(--accent); color: var(--accent-text);
                             border: 0; padding: 6px 12px; border-radius: 2px;
                             cursor: pointer; font-size: 12px; }
.mg-buy:hover, .mg-search button:hover { filter: brightness(1.12); }
.mg-buy:disabled { background: #39424D; color: #8B949E; cursor: default; }

.mg-h2 { font-size: 13px; font-weight: 600; margin: 0 0 3px; }
.mg-note { color: var(--muted); font-size: 11px; margin: 3px 0 14px; }

/* Album grid. minmax rather than fixed widths so 200px works and 700px does
   not leave a ragged gap; the Media Guide reflowed the same way. */
.mg-grid { display: grid; gap: 14px;
           grid-template-columns: repeat(auto-fill, minmax(124px, 1fr)); }
.mg-card { background: var(--panel); border: 1px solid var(--line);
           border-radius: 3px; overflow: hidden; }
.mg-card img { width: 100%%; aspect-ratio: 1/1; object-fit: cover; display: block;
               background: #0A0D11; }
.mg-card .mg-t { font-weight: 600; font-size: 12px; padding: 7px 8px 0;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mg-card .mg-a { color: var(--muted); font-size: 11px; padding: 0 8px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mg-card .mg-p { color: #9FD39A; font-size: 11px; padding: 4px 8px 8px; }

table.mg-tbl { width: 100%%; border-collapse: collapse; }
table.mg-tbl th, table.mg-tbl td { text-align: left; padding: 6px 8px;
                                   border-bottom: 1px solid var(--line); font-size: 12px; }
table.mg-tbl th { color: var(--muted); font-weight: 600; }
table.mg-tbl td.mg-dur { color: var(--muted); white-space: nowrap; }

.mg-hero { float: right; width: 148px; height: 148px; object-fit: cover;
           border: 1px solid var(--line); border-radius: 3px;
           margin: 0 0 10px 14px; }
.mg-flash { background: #16301A; border: 1px solid #2F5A30; color: #B6E8B6;
            padding: 9px 11px; border-radius: 3px; margin-bottom: 12px; }
.mg-err { background: #33191B; border: 1px solid #6A3030; color: #FFB4B4;
          padding: 9px 11px; border-radius: 3px; }
.mg-foot { color: var(--muted); font-size: 10px; margin-top: 18px;
           border-top: 1px solid var(--line); padding-top: 9px; }
code { color: #9FD39A; }
"""


def _album_card(album):
    return """<div class="mg-card">
  <a href="%(prefix)s/album/%(id)s"><img src="%(art)s" alt="%(title)s"></a>
  <div class="mg-t"><a href="%(prefix)s/album/%(id)s">%(title)s</a></div>
  <div class="mg-a">%(artist)s</div>
  <div class="mg-p">%(price)s</div>
</div>""" % {
        "prefix": URL_PREFIX,
        "id": album.album_id,
        "art": _art_src(album.album_id),
        "title": escape(album.title),
        "artist": escape(album.artist),
        "price": _money(album.price, album.currency),
    }


def _album_grid(albums):
    return '<div class="mg-grid">%s</div>' % "".join(
        _album_card(album) for album in albums)


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
    body = ['<h2 class="mg-h2">Browse Music</h2>',
            '<form class="mg-search" method="get" action="%s/search">'
            '<input type="search" name="q" placeholder="Search the catalog" '
            'value="%s"><button type="submit">Search</button></form>'
            % (URL_PREFIX, escape(request.args.get("q", "")))]
    if albums:
        body.append(_album_grid(albums))
    else:
        body.append('<p class="mg-note">The catalog is empty.</p>')
    body.append('<div class="mg-foot">%d album(s). Content is synthesised '
                'locally for testing; no commercial store is connected.</div>'
                % len(albums))
    return _html_page(config.friendly_name, "".join(body), config, "home")


@blueprint.route(URL_PREFIX + "/album/<album_id>")
def storefront_album(album_id):
    config, provider = _get_state()
    album = provider.get_album(album_id)
    if album is None:
        return _html_page("Not found",
                          '<p class="mg-err">No album with id %s.</p>'
                          % escape(album_id), config), 404
    rows = []
    for track in album.tracks:
        price = _money(track.price, track.currency)
        rows.append(
            "<tr><td>%d</td><td>%s</td><td class='mg-dur'>%s</td>"
            "<td class='mg-dur'>%s</td>"
            "<td><form method='post' action='%s/buy' style='display:inline'>"
            "<input type='hidden' name='track_id' value='%s'>"
            "<button class='mg-buy' type='submit'>Buy %s</button></form></td></tr>"
            % (track.track_number, escape(track.title),
               format_duration(track.duration_ms), price,
               URL_PREFIX, escape(track.track_id), price))
    # str() before escape(): escape() is an XML helper and only accepts text.
    # album.year is an int in the model, which is what made this page a 500 -
    # the pre-existing code interpolated it with %s, which coerced it silently.
    facts = " &middot; ".join(part for part in (
        escape(str(album.artist or "")),
        escape(str(album.genre or "")),
        escape(str(album.year))) if part)
    body = """<img class="mg-hero" src="%s" alt="">
<h2 class="mg-h2">%s</h2>
<p class="mg-note">%s</p>
<table class="mg-tbl">
 <tr><th>#</th><th>Title</th><th>Length</th><th>Price</th><th></th></tr>
 %s
</table>
<div style="clear:both"></div>
<p class="mg-note">%s</p>""" % (
        _art_src(album.album_id), escape(album.title), facts,
        "".join(rows), escape(album.description or ""))
    return _html_page(album.title, body, config, "home")


@blueprint.route(URL_PREFIX + "/search")
def storefront_search():
    config, provider = _get_state()
    query = (request.args.get("q") or "").strip()
    body = ['<h2 class="mg-h2">Search</h2>',
            '<form class="mg-search" method="get" action="%s/search">'
            '<input type="search" name="q" value="%s">'
            '<button type="submit">Search</button></form>'
            % (URL_PREFIX, escape(query))]
    if query:
        albums = provider.search(query, limit=60)
        body.append('<p class="mg-note">%d result(s) for &ldquo;%s&rdquo;</p>'
                    % (len(albums), escape(query)))
        body.append(_album_grid(albums) if albums
                    else '<p class="mg-note">Nothing matched.</p>')
    else:
        body.append('<p class="mg-note">Type something in the search box.</p>')
    body.append('<div class="mg-foot"><a href="%s/">Back to Browse Music</a>'
                '</div>' % URL_PREFIX)
    return _html_page("Search", "".join(body), config, "search")


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
        listing = ('<p class="mg-note">Purchases directory not created yet. Set '
                   '<code>purchases_dir</code> in online_store.ini to change '
                   'where bought files land.</p>')
    else:
        found = []
        for root, _dirs, files in os.walk(purchases_dir):
            for name in files:
                if name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                    found.append(os.path.join(root, name))
        found.sort()
        listing = ("<ul>%s</ul>" % "".join("<li>%s</li>" % escape(path)
                                          for path in found[:200])
                   if found else '<p class="mg-note">Nothing purchased yet.</p>')
    body = ('<h2 class="mg-h2">Downloads</h2><p class="mg-note">Everything here '
            'was produced locally by the fake provider.</p>%s' % listing)
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
        return _html_page("No track",
                          '<p class="mg-err">No track_id given.</p>',
                          config), 400
    if not getattr(provider, "supports_purchase", False):
        return _html_page(
            "Not for sale",
            '<p class="mg-err">Provider %r cannot deliver purchases.</p>'
            % escape(provider.provider_id), config), 501
    purchase = provider.purchase(track_id)
    if purchase is None:
        return _html_page(
            "Not found",
            '<p class="mg-err">Provider %r has no track %s.</p>'
            % (escape(provider.provider_id), escape(track_id)), config), 404
    body = """<div class="mg-flash">Purchased &ldquo;%s&rdquo; by %s.</div>
<p>File written to:</p>
<p><code>%s</code></p>
<p class="mg-note">%s bytes, %s. Generated locally - this is not a
DRM-protected file and not a real recording. Open it in WMP with
<em>File &rarr; Open</em>, or browse the
<a href="%s/downloads">downloads page</a>.</p>""" % (
        escape(purchase.title), escape(purchase.artist),
        escape(purchase.local_path), "{:,}".format(purchase.size_bytes),
        escape(purchase.content_type or ""), URL_PREFIX)
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




