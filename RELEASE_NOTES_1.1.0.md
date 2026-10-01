# Release 1.1.0

Automatic disc identification, plus a Zune protocol fix. Both come from the same
root cause: the CD TOC this server received was never being converted before it
was used, so every lookup it ever attempted failed.

## Automatic TOC lookup (WMP only)

When WMP names a disc (`?cd=` / `?toc=`) and **nothing is staged for it**, the
server now identifies the disc from its TOC and applies the match without
opening the dialog.

A TOC is a physical fingerprint — it identifies the exact pressing — so a match
is evidence rather than a guess. The four guards around it:

| Guard | Why |
|---|---|
| **WMP only**, never Zune | Zune has no dialog to correct a wrong match with, so one would be silent and unfixable. |
| **The request names a real disc** | A `?wmid=`-only library update is you editing an album you can see; never retag that on a hunch. |
| **Not a browser User-Agent** | A browser UA means WMP is driving the dialog; auto-answering would take the choice away from the UI you are looking at. |
| **Nothing was staged** | Runs only on the empty-document path. |

**The dialog remains the escape hatch and outranks all of it.** Anything you pick
in the FAI dialog wins. Wrong album, or none at all? Open the dialog for that
disc, pick, and it is served from then on.

Discs with no MusicBrainz release are left untouched, and misses are cached so
repeat fetches cost no request. Disable with `AUTO_TOC_LOOKUP=0`.

## TOC conversion fix

Every TOC lookup this server ever made was a guaranteed `400 Invalid TOC` — for
WMP as well as Zune. The clients send hex:

```
B+96+43DA+71A4+105D1+15498+19A64+1F0B3+23C14+29CD4+2EB21+33B0F+37106
```

MusicBrainz wants decimal, reordered:

```
1+11+225542+150+17370+29092+67025+87192+105060+127155+146452+171220+191265+211727
```

A straight hex→decimal pass still fails — the lead-out has to move to position
two and a leading `1` is added. All three encodings the clients use are handled
(`+` from Zune, `-` and space-separated from WMP).

## Zune

Zune's FAI protocol was read out of the installed client
(`ZuneNativeLib.dll` / `ZuneNss.exe`) rather than guessed from network traffic,
which corrected three things:

- **`/redir/getmdrcdzune/` is metadata *delivery*, not the dialog.** It was
  being served HTML, which Zune could not parse — that was the "Can't connect to
  the server" it reported over a `200`.
- **`/redir/getmdrcdposturlzune/` was missing entirely.** Zune's first handshake
  ("where do I POST the disc?") fell through to the catch-all, which answers with
  a sentence rather than a URL.
- **`/redir/zunesearch/` was invented.** That path exists in no Zune binary.

Zune still has no dialog: it consumes the MDR-CD XML directly, so it cannot offer
album selection, and it is deliberately excluded from automatic TOC matching.

## Build

The version was hardcoded as `1.0.0` in four places in `build_exe.py`, so the
1.0.1–1.0.3 EXEs all reported 1.0.0 in their Windows file properties. It is now
a single `VERSION` constant. Verified as `1.1.0` in the shipped binary.

493 passed, 0 failed.