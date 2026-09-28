# WMP Find Album Info (FAI) Metadata Server

A local, self-hosted reimplementation of the **Find Album Information** service that
Windows Media Player 12 (and, partially, Zune) used to call when you ripped a CD or
wanted to fix the tags on a library album.

Microsoft retired the original service
(`fai.music.metadataservices.microsoft.com`). This project answers the same
requests from a local Flask server, searching **iTunes** and **MusicBrainz** for real
album metadata and cover art, and writing it back into WMP through the player's own
`IWMPCDDVDWizardExternal` COM interface.

Everything runs on your own machine. No Microsoft endpoint is contacted.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [How to use](#how-to-use)
- [How it works](#how-it-works)
- [Route reference](#route-reference)
- [What works](#what-works)
- [What does not work](#what-does-not-work)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Deploying to the cloud](#deploying-to-the-cloud)
- [Project layout](#project-layout)
- [License](#license)

---

## What it does

When WMP wants album information, it normally contacts a Microsoft service. This
server stands in for that service and provides:

- **Concurrent hybrid search** across the iTunes Search API and the MusicBrainz
  Web Service, merged into a single ranked result list.
- **A Find Album Information dialog** rendered in the player's own browser host,
  restyled to match the authentic Microsoft dialog: the blue *"Found 500+ Album(s)
  containing …"* lead-in, the **Existing Information** / **Search** column pair, the
  `Artists | Albums | Tracks` filter strip, and the *Read the privacy statement.* /
  **Next** / **Cancel** command strip.
- **Cover art** proxied and cached locally, so WMP can fetch it without reaching
  out to a CDN.
- **WMP-format XML** (`MDR-CD`) delivered over the exact endpoints WMP polls,
  keyed to the specific disc or library collection it asked about.
- **Direct COM writes** so the tags land even when WMP's own HTTP fetch path is
  unreliable.
- **A diagnostics dashboard** at `/fai_status` plus a self-rotating log file.



---

## Requirements

| | |
|---|---|
| **OS** | Windows 7 / 10 / 11 (the FAI dialog is an IE7-era MSHTML host) |
| **Player** | Windows Media Player 12 |
| **Python** | 3.9+ (developed on 3.13) |
| **Packages** | `flask`, `requests`, `cryptography` — see `requirements.txt` |
| **Privileges** | Administrator, for the hosts file and the machine trust store |
| **Ports** | 80 and 443 must be free |

```powershell
pip install -r requirements.txt
```

---

## Quick start

### 1. Point the hostname at your machine

WMP talks to `musicmatch-ssl.xboxlive.com`. Open an **elevated** Notepad (or run
`notepad C:\Windows\System32\drivers\etc\hosts` as Administrator) and add:

```
127.0.0.1 musicmatch-ssl.xboxlive.com
```

If you do not do this, WMP will never reach the local server and the dialog will
never open.

### 2. Run the server

```powershell
python "FAI Server.py"
```

On first run it will:

1. Generate a self-signed RSA-2048 certificate for
   `musicmatch-ssl.xboxlive.com` (with a proper `SubjectAlternativeName` and
   `basicConstraints CA:TRUE`) into
   `%APPDATA%\WMP_FAIServer\cert.pem`.
2. Install that certificate into **both** the machine and user *Trusted Root*
   stores via `certutil`.
3. Start an HTTP listener on port 80 and an HTTPS listener on port 443.

> **Why the certificate matters.** WMP fetches metadata over **HTTPS**. If the
> server presents an untrusted certificate, the TLS prompt appears inside WMP's own
> modal host window where it cannot be seen or dismissed — WMP appears to freeze and
> Windows kills it (`AppHangB1`). The trust install is what prevents that.

Set `WMP_FAI_NO_TRUST=1` to skip the automatic trust-store install (you will then
need to trust the certificate yourself, and the dialog will likely hang).

### 3. Use it

Just rip a CD, or right-click an album in your library and choose
**Update album info**. WMP will call the local server instead of Microsoft's.

You can also open the dialog directly in a normal browser to check it works:

```
http://127.0.0.1/FAI/ui?artist=The+Beatles&album=Abbey+Road
```

---

## How to use

### Ripping a CD

1. Insert the CD and start the rip in WMP as usual.
2. WMP contacts `musicmatch-ssl.xboxlive.com`, which now resolves to your machine.
3. The **Find album information** dialog opens with the rip's existing tags shown
   under *Existing Information* and a populated search box.
4. Press **Enter** in the search box, or press **Next**, to run the search.
5. Click the correct album in the results list. The track list loads so you can
   review it.
6. Tick the tracks you want to retag, then press **Finish & Apply**.
7. The dialog hands control back to WMP and your tags (and cover art) are applied.

### Fixing a library album

1. Select the album in the WMP library.
2. Right-click → **Update album info**.
3. WMP passes the collection's `WMID` in the URL, and the server tags the returned
   XML with that same GUID so it lines up with your existing entry.
4. Search, pick, and press **Finish & Apply**. In library mode the tags are written
   **without** asking WMP to rename or regroup files on disk.

### Choosing which tracks to write

On the confirmation page:

---

## How it works

### The identifier problem

WMP uses a **different identifier for every flow**, and getting this wrong is what
made earlier versions tag every CD with the same album:

| Flow | Query parameter | Meaning |
|---|---|---|
| Physical disc | `cd=` | Content IDs, e.g. `5+96+554B+83B5` |
| Library collection | `wmid=` | The collection GUID WMP is tracking |
| Dialog session | `requestid=` | Identifies one dialog session |
| Legacy TOC routing | `toc=` | CD TOC signature, always starts with a literal `+` |

All of these are read with `raw_query_arg()` rather than Flask's `request.args`,
because Werkzeug decodes a literal `+` as a space. A TOC like `+hAhAAQBAAMAA...`
would arrive as ` hAhAAQBAAMAA...`, fail to match the disc, and WMP would silently
apply nothing.

### Metadata delivery

When the dialog finishes, the client POSTs the generated XML to `/store_staged_xml`.
The server stores it **keyed by the identifier** (requestid / CD / WMID / TOC) and
WMP's background fetches are answered from that per-identifier store.

Critically, there is **no fallback to the most recently staged document**. An
identifier with nothing staged for it receives `EMPTY_METADATA_XML`, a valid
document carrying `<status>NOTFOUND</status>` and no album and no tracks — so an
unrelated disc is left exactly as it was instead of inheriting someone else's
album.

### Write paths

WMP's own HTTP fetch is not always reliable, so the dialog writes directly through
COM, trying these in order and falling back until one succeeds:

| Order | Call | Used for |
|---|---|---|
| 1 | `WriteNamesEx(1, WMP_CD, xml, true)` | Physical disc, by content ID |
| 2 | `WriteNamesEx(2, mdq, xml, true)` | Real disc MDQ (rename/regroup allowed) |
| 3 | `WriteNamesEx(1, WMP_WMID, xml, false)` | Library album, tags only |
| 4 | `WriteNamesEx(2, mdq, xml, false)` | Library fallback, tags only |
| 5 | `WriteNamesEx(0, WMP_TOC, xml, true)` | Legacy TOC routing |

The MDQ is obtained from `window.external.GetMDQByRequestID(REQUEST_ID)` and its
**real per-track `WMContentID` values** are used. Generated GUIDs match nothing in
WMP, which is why earlier versions appeared to write successfully while changing
nothing.

For library flows the XML is *retargeted*: `WMCollectionID`, `WMCollectionGroupID`,
`ZuneAlbumMediaID` and `mdr-id` are rewritten to the `wmid` WMP actually asked
about, so the document describes the collection WMP is tracking.

> **Never truthiness-test a COM member.** Inside the WMP dialog, host objects
### Keeping the dialog responsive

WMP's dialog host is single-threaded. Two mistakes reliably hang `wmplayer`:

- **Synchronous XHR.** Staging and beacon requests are all `async` (third argument
  `true`). A sync request wedges the UI thread and the dialog sits on
  "Applying…" forever.
- **Duplicate COM teardown.** Firing `ReturnToMainTask` twice — e.g. from a timer
  *and* a click — can deadlock the host. The completion page uses a single-shot
  `CLOSING` guard instead of a timer.

The dialog also installs `window.onerror`, which beacons failures to
`/client_error` so JS errors are visible in `fai_server.log` rather than lost in a
browser console that does not exist in the player.

### The dialog styling

The real FAI window is not a themed application. The Aero glass is only the **host
frame** — the title bar and close box drawn by the player. The page underneath is
a plain white IE7 content area. Styling it to match therefore means reproducing
that content area exactly, not painting a Windows 7 theme over it.

Because the host is IE7, the stylesheet avoids anything IE7 cannot parse:

- Both columns and every list row are laid out with **floats**, not flexbox.
- The only flexbox is the outer flex *column* on `body`, which the conditional
  flips to `display: block`.
- Every `linear-gradient()` has a `progid:DXImageTransform.Microsoft.gradient`
  equivalent in the `<!--[if lt IE 8]>` block.
- No `calc()`, no `gap:`, no CSS custom properties, no `backdrop-filter`.

### Caching

Three thread-safe TTL caches, all in memory:

| Cache | TTL |
|---|---|
| `search_cache` | 2 hours |
| `album_cache` | 24 hours |
| `image_cache` | 24 hours |

Artwork is served through `/cover/album.jpg?url=…`, which keeps slashes in
upstream URLs readable (double-encoding them was why album art silently failed),
and also recovers URLs that arrive double-encoded from older staged documents.

---

## Route reference

### WMP / Zune protocol

| Route | Method | Purpose |
|---|---|---|
| `/cdinfo/GetMDRCDPOSTURL.aspx` | GET | Discovery — tells WMP where to fetch metadata |
| `/redir/getmdrcdposturl/` | GET | Same, legacy path |
| `/redir/getmdrcdposturlbackground/` | GET | Same, background variant |
| `/redir/getmdrcdposturlbackgroundzune/` | GET | Zune variant |
| `/cdinfo/GetMDRCD.aspx` | GET/POST | Metadata delivery (`MDR-CD` XML) |
| `/redir/getmdrcdbackground/` | GET/POST | Metadata delivery, legacy |
| `/redir/getmdrcdbackgroundzune/` | GET/POST | Metadata delivery, Zune |
| `/redir/getmdrcd/` | GET/POST | Metadata delivery, legacy |
| `/cdinfo/submittoc.aspx` and friends | GET/POST | Legacy TOC submission → dialog |

The legacy `submittoc` / `GetMDRCD.asp` / `QueryTOC.asp` routes are
**content-negotiated**: a browser `User-Agent` gets a redirect to the dialog, while
`WindowsMediaPlayer/12` gets raw XML.

### User interface

| Route | Method | Purpose |
|---|---|---|
| `/FAI/ui` | GET | The Find Album Information dialog |
| `/confirm`, `/confirm_musicbrainz` | GET | Track selection + apply |
| `/done` | GET | Completion page, calls `ReturnToMainTask` |
| `/api_search?q=` | GET | Concurrent iTunes + MusicBrainz search |

### Internals

| Route | Method | Purpose |
|---|---|---|
| `/store_staged_xml` | POST | Stage generated XML, keyed by identifier |
| `/store_single_track_xml` | POST | Backwards-compatible alias |
| `/track_navigation` | POST | Wizard step telemetry |
| `/client_error` | POST/GET | Client beacon for JS/COM outcomes |
| `/fai_status`, `/status` | GET | Diagnostics (`?format=json` for JSON) |
| `/get_image`, `/cover/album.jpg` | GET | Cached artwork proxy |
| `/static/noart.png` | GET | Generated "no artwork" placeholder |

---

## What works

- **Physical CD rips** — tags and cover art are applied to the correct disc.
- **Library album updates** — existing collections are retagged in place by `WMID`,
  without renaming or regrouping files on disk.
- **Concurrent hybrid search** — iTunes and MusicBrainz are queried in parallel and
  merged, with per-result provenance (`iTunes` / `MusicBrainz`).
- **Real per-track `WMContentID`s** from the disc MDQ, which is what makes WMP
  accept the write at all.
- **Correct disc isolation** — an unrelated disc that was never staged receives
  `NOTFOUND` and is left untouched.
- **Cover art** — proxied, cached, and successfully fetched by WMP as JPEG.
- **No hangs** — asynchronous staging, asynchronous beacons, no timer-driven
  teardown, and a single-shot close guard.
- **Multi-disc albums** — grouped by disc number on the confirmation page.
- **TLS trust** — a properly formed SAN + `CA:TRUE` certificate that Windows
  accepts, installed automatically into both trust stores.
- **Dialog styling** reproduces the authentic Microsoft FAI layout and renders
  correctly in IE7.

## What does not work

Please read this section before assuming something is broken.

### Not implemented

- **Automatic disc identification.** There is no fingerprinting (AcoustID,
  MusicBrainz recording IDs). You must search and pick the album yourself.
  Automatic TOC-based matching is **deliberately disabled** — a TOC match is
  logged and otherwise ignored, because a wrong automatic match silently
  mis-tags a disc.
- **Track-level art or acoustic matching.** The whole album's cover is applied.
- **Lyrics, release-group selection, or "best match" ranking.** Results are ordered
  by provider score; there is no re-ranking across sources.
- **Zune flows are only partially wired.** The Zune redirect routes exist and
  respond, but the Zune client has not been tested end to end.
- **Windows Media Player 11 and earlier.** Only WMP 12 is supported; the COM
  surface and the dialog host differ on older players.
- **No multi-user or remote access.** The server binds `0.0.0.0` but has **no
  authentication whatsoever**. See [Security notes](#security-notes).

### Known rough edges

- **MusicBrainz rate limits.** Their API allows roughly one request per second and
  returns `503` under bursts. Searches occasionally come back short.
- **Result quality depends on the upstream APIs.** iTunes is fast with
  high-resolution art but is storefront-dependent; MusicBrainz is comprehensive
  but frequently has no cover art.
- **The search box is the only input.** The filter strip
  (`Artists | Albums | Tracks`) is decorative — it mirrors the original dialog but
  does not change the query. The `Edit`, `Buy`, `More…` and *Read the privacy
  statement.* links are likewise presentational and intentionally non-navigating,
  because navigating away inside the player's modal host traps the wizard.
- **WMP must be the browser that opens the dialog.** The layout is tuned for the
  player's IE7 host. Modern browsers render it correctly but will never look
  pixel-identical to the original, which was IE7.
- **Cover art URLs must be reachable from your machine.** iTunes' CDN serves
  regional variants; a URL that 404s for you will fall back to the generated
  placeholder.
- **A stale certificate is regenerated automatically**, but if you previously
  installed an old one into the trust store it may linger. Delete entries named
  `WMP FAI Server` from `certmgr.msc` if you see TLS trouble.
- **Ports 80 and 443 must be free.** Another web server or a previous instance of
  this one will block startup; the server then keeps running HTTP-only and logs a
  warning.
- **Single process, in-memory state.** Staged XML and the caches live in the
  process and are lost on restart. Do not run multiple instances behind a load
  balancer.

---

## Testing

The suite runs the Flask app in-process via `test_client()`, so **no port 80 and no
running server is required**. Some checks hit the live iTunes/MusicBrainz APIs and
will be skipped or fall back if you are offline.

```powershell
python test_fai_v2.py
```

Expected result:

```
==== 139 passed, 0 failed ====
```

The suite covers, among other things:

- Identifier handling, including that a literal `+` in a TOC survives
- All five COM write paths and their ordering
- MDQ `WMContentID` parsing, and that `applyMetadata` reads a shared `RESOLVED_MDQ`
- That no synchronous XHR exists anywhere in the source
- That an unrelated disc gets `NOTFOUND` while the staged disc still gets its album
- `WMID` retargeting and per-identifier staging stability
- Artwork proxy URL encoding and double-decoding recovery
- The FAI dialog's visual contract and IE7 fallbacks
- The generated `noart.png` placeholder's PNG structure and CRCs

Useful during development:

```powershell
python -m py_compile "FAI Server.py"        # syntax
python -W error::SyntaxWarning -m py_compile "FAI Server.py"   # also fail on bad escapes
```

If you change the served JavaScript, extract each `<script>` block and run
`node --check` on it — the test client writes them to disk for this purpose.

---

## Troubleshooting

**WMP never opens the dialog.**
WMP is not reaching the server. Check the hosts file entry for
`musicmatch-ssl.xboxlive.com`, that nothing else holds port 80, and that
`fai_server.log` records any incoming request at all.

**WMP hangs and Windows kills it (`AppHangB1`).**
Almost always TLS trust. The certificate is not in the Trusted Root store. Re-run
the server as Administrator so `certutil` can install it, or check
`certmgr.msc` for `WMP FAI Server` under *Trusted Root Certification Authorities*.

**The dialog opens but tags are not applied.**
Read `fai_server.log`. Look for the `[CLIENT]` beacon lines — they record exactly
which write path succeeded or which one threw. A `WriteNamesEx-cdid-ok` means the
write landed. If you see `RETURN_TO_MAIN` without a preceding write, the
`finishSync` path did not complete.

**Every CD gets the same album.**
This was a real bug, fixed by removing the `LAST_XML` fallback: the delivery
endpoint used to serve the most recently staged document for any unmatched
identifier. If you see this behaviour, confirm the server is running this version
and that there is only one instance bound to the port.

**Album art does not appear.**
Check `[REQ]` and `[CLIENT]` lines for the cover fetch. A 404 from
`/cover/album.jpg` means the upstream URL expired or is region-blocked; the
placeholder is shown instead.

**Search returns nothing.**
Try a plain artist or album name. iTunes' API is storefront-dependent and will
return nothing for some regions; MusicBrainz is stricter about formatting.

**"Address already in use" / no HTTPS.**
Another process holds the port, or a previous instance is still running. Find it
with `netstat -ano | findstr :443` and stop it, or change `HTTPS_PORT` at the top
of `FAI Server.py`.

**To remove it completely**

1. Stop the server.
2. Remove the hosts entry.
3. Delete `WMP FAI Server` from `certmgr.msc` (Trusted Root).
4. Delete `%APPDATA%\WMP_FAIServer\`.

---

## Security notes

Read this before exposing the server to a network.

- **There is no authentication or authorization of any kind.** Any client that can
  reach the port can read staged metadata, write arbitrary XML that WMP will
  apply to your library, and exhaust memory through the caches. The default
  `HOST = "0.0.0.0"` listens on every interface.
- **Change `HOST` to `127.0.0.1`** unless you have a specific reason not to.
- **A self-signed root CA is installed into your machine's Trusted Root store.**
  A trusted root can issue certificates for any host. It is only as trustworthy as
  the private key on disk, which is stored unencrypted in `%APPDATA%\WMP_FAIServer`.
  Delete both when you are done.
- **`MUSICBRAINZ_USER_AGENT` contains a placeholder contact address.** MusicBrainz
  asks for a real contact. Change it to a real one before making sustained use of
  their API.
- Metadata and artwork are fetched from third-party APIs; only public catalog data
  is requested and nothing about your library is sent to them.
- Use this on your own machine. Microsoft retired the original service; this
  project does not interact with, or represent itself as, any Microsoft system.

---

## Deploying to the cloud

> **Read this before assuming a cloud deploy can replace your local server. It
> cannot.** This section exists so the Render deployment is useful for what it
> genuinely does, and so nobody loses an evening expecting it to tag a CD.

A `render.yaml` is included. The app is Flask, which is **WSGI**, so it is served
by **gunicorn** — *not* uvicorn, which is an ASGI server and cannot serve Flask at
all. The entry point is `wsgi:app` rather than `"FAI Server.py":app`, because
gunicorn imports its target and Python module paths cannot contain spaces.

### What works on Render

Everything that is just HTTP:

| Feature | URL |
|---|---|
| Diagnostics dashboard | `/fai_status` |
| The FAI dialog, in a real browser | `/FAI/ui?artist=…&album=…` |
| Hybrid search | `/api_search?q=…` |
| Artwork proxy | `/cover/album.jpg?url=…` |
| XML delivery + staging endpoints | `/cdinfo/…`, `/redir/…`, `/store_staged_xml` |

### What cannot work on Render

The actual purpose — getting WMP to apply tags to your discs — depends on three
things a cloud host cannot provide:

1. **WMP resolves `musicmatch-ssl.xboxlive.com` to `127.0.0.1`** via your hosts
   file. Your player is never at the same machine as a Render dyno, so it will
   never issue a request to one.
2. **`window.external.WriteNamesEx(…)` only exists inside the WMP dialog host.**
   It is a COM object injected by the player. In an ordinary browser it is
   `undefined`, so the dialog loads and looks correct but cannot write anything.
3. **The TLS trust step is Windows-only.** The server generates a self-signed
   certificate for `musicmatch-ssl.xboxlive.com` and installs it with `certutil`
   into the Windows Trusted Root store. A public cloud host cannot present a
   certificate for that hostname that Windows will trust, and `certutil` does not
   exist on Linux.

On Linux, `ensure_ssl_certificates()` and the port 80/443 listeners are never
reached anyway: they live inside `if __name__ == "__main__":`, which importing
the module does not run.

**So: use Render to host a demo and to develop the UI. Run `python "FAI Server.py"`
on your own Windows machine to actually tag discs.**

### Single worker, on purpose

`WEB_CONCURRENCY=1` is set deliberately. Staged XML, `LAST_WMID` and the three
TTL caches all live in process memory; a second worker is a separate universe that
can never see the first worker's staged document. Render already defaults to 1 on
the free plan, and the value is pinned so it cannot drift.

The free plan also sleeps after inactivity, so the first request after an idle
period will be slow while the dyno cold-starts.

---

## Project layout

```
FAI Server.py     the entire server (single file by design)
wsgi.py           WSGI entry point for Linux/cloud hosts (gunicorn wsgi:app)
test_fai_v2.py    the test suite
render.yaml       Render blueprint (demo only - see "Deploying to the cloud")
requirements.txt  runtime dependencies
fai_server.log    runtime log, self-rotating at 5 MB (git-ignored)
```

---

## License

MIT — see [LICENSE](LICENSE).

This project is not affiliated with or endorsed by Microsoft.


> evaluate falsy, so `if (window.external.ReturnToMainTask) { … }` silently skips
> the call. Every COM call in this project is made directly, unguarded.



| Action | Effect |
|---|---|
| Click a row | Toggle that single track |
| **Select All** | Stage every track on the album |
| **Clear All** | Stage nothing |

If WMP's own disc data does not match the album you picked, a warning banner
appears at the top of the confirmation page so you can back out before writing the
wrong tags.
