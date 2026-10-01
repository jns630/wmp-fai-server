# Release 1.1.1

Search-dialog rebuild, disc identification, and Windows Media Center support.

Since 1.1.0. Eight commits: `bfd0666`, `44f789c`, `e54cb2c`, `3afa6e2`, `f9f7506`,
`74d5f50`, `fa34945`, `3433fab`.

## The Find Album Information dialog

Rebuilt to the reference layout: 416px left column with a 1px divider, a 405x23
search field, a 405x280 result list, 56x56 covers, an 18px `#315D96` lead-in, a
48px footer and 72x24 buttons. Selection highlights in `#E8F4FC`, and Up / Down /
Enter / Escape now work in the results list.

**Laid out with floats, not grid or flexbox.** WMP 12 hosts this page in an IE7
WebBrowser control - real sessions log `MSIE 7.0; Trident/7.0` - where
`display:grid`, flexbox, `border-radius` and `linear-gradient()` do not exist. The
shared stylesheet was already leaning on all four, so this page no longer does,
and a test now holds it to that.

Styling for the search page lives in its own `FAI_UI_CSS`, injected only on
`/FAI/ui`. `/confirm` renders byte-identical output to 1.1.0.

## Identifying the disc (new)

When WMP opens the dialog for a CD it could not tag, it sends only `?cd=` and no
artist/album/track. Every field then read blank - `Searching for ""...`, an empty
box, and "Enter a search and press Enter."

The disc is not unidentified: it carries a TOC, and the server already resolved
that TOC for automatic delivery. The dialog was simply never told. It now asks
the same resolver and starts populated.

This runs **after** the page is on screen, not during the render. Measured on one
disc, the page render went from **6911 ms to 206 ms** - the earlier inline lookup
made the dialog block on MusicBrainz, which WMP presents as a hung window.

Resolving the identity itself still takes roughly 7 s on a cold disc: it is two
MusicBrainz calls behind a 1 req/sec limiter. The dialog is usable throughout and
reports progress instead of freezing.

## Windows Media Center

Media Center is served on its own hosts and paths (`/toc/getmdrcd.aspx`, the
`.asp` post-URL routes), with `CD=` handled alongside `cd=`, UA handling for WMC
and WMP 7-9, and certificate SANs for the WMC hostnames.

## Cover art

- The confirm page now renders the album cover. `proxy_art` was computed and
  never passed to the template, so the page showed no artwork at all.
- `WMP_ART_MODE` can be `direct` (default), `proxy` or `relative`.

**`direct` is still the default, and deliberately so.** `relative` was briefly
made the default and cost WMP its cover art. The reasoning behind that change was
circular: Media Center made no `/cover/` request, but under `direct` it could not
have whether artwork worked or failed, because a successful direct fetch happens
at the upstream CDN. Absence of a log line was evidence of the mode in use, never
of failure. Move defaults off an observed-working path only on a run test, not a
theory.

**Media Center artwork remains undiagnosed.** Nothing here is claimed to fix it.

## Known limitations

- **Media Center album art is not fixed.** WMC fetches metadata from this server
  and its cover art does not appear. The cause is not established and no claim is
  made about it here.
- **No screenshot comparison** of the rebuilt dialog against the reference image;
  headless capture would not run in the build environment. Geometry follows the
  measured reference, but it has not been visually diffed.
- The privacy-statement link in the footer is inert, as it was in 1.1.0 - there is
  no privacy route to point it at.
- Disc identification still costs about 7 s on a cold disc.

## Testing

523 passed, 0 failed.

Three assertions were passing for the wrong reason and were rewritten to match
the stylesheet and behaviour that actually govern the page - one of them matched a
literal that survived only inside a CSS comment.