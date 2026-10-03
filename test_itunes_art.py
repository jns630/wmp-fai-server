"""The iTunes cover URL rewrite, and the 404 it caused.

The real 404, captured from a live session:
    GET /cover/https://is1-ssl.mzstatic.com/.../859727388959_cover.jpg/600x600bb.jpg
That URL is malformed. It came from art_url.replace("100x100bb", "600x600bb"),
which only works when the size segment is the LAST one. iTunes artwork URLs also
end in the original filename, so for many albums the size is not last and the
substitution produced a path that does not exist.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fai = importlib.import_module("FAI Server")
hires = fai._itunes_art_hires

failures = []


def check(name, ok, detail=""):
    print("%-62s %s" % (name, "PASS" if ok else "FAIL " + str(detail)))
    if not ok:
        failures.append(name)


# The exact shape from the logged 404: size segment is last, but the path also
# ends in "_cover.jpg", which is what made the old replace produce garbage.
NESTED = ("https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/90/81/27/"
          "908127e4-acd6-8538-b8ab-0b8d1f1cd18c/859727388959_cover.jpg"
          "/100x100bb.jpg")
out = hires(NESTED)
check("the exact logged URL upgrades to a real 600px URL",
      out.endswith("/600x600bb.jpg") and "_cover.jpg/600x600bb.jpg" in out,
      out)
check("  and the nested filename is preserved, not mangled",
      "/859727388959_cover.jpg/600x600bb.jpg" in out, out)

# The ordinary shape.
PLAIN = ("https://is1-ssl.mzstatic.com/image/thumb/Music116/v4/07/60/ba/"
         "0760ba0f-148c-b18f-d0ff-169ee96f3af5/634904078164.png/100x100bb.jpg")
check("an ordinary URL upgrades 100x100 -> 600x600",
      hires(PLAIN).endswith("/600x600bb.jpg"), hires(PLAIN))
# Only the size segment changes. The upstream path ends in ".png", and that is
# left exactly as it was - the iTunes service serves whatever extension the
# artwork really is, so rewriting .png to .jpg would request a file that may not
# exist. This is what the earlier fix got wrong too.
check("  the original .png segment is preserved",
      hires(PLAIN).endswith("/634904078164.png/600x600bb.jpg"), hires(PLAIN))

check("no accidental 100x600x600 duplication anywhere",
      all("100x600" not in hires(u) for u in (NESTED, PLAIN)))

# bn suffix (some iTunes artwork uses it).
BN = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100bn.jpg"
check("the bn suffix is handled", hires(BN).endswith("600x600bn.jpg"), hires(BN))

# Already 600: must not be mangled.
ALREADY = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/600x600bb.jpg"
check("an already-large URL is left alone", hires(ALREADY) == ALREADY,
      hires(ALREADY))

# No size segment at all: return something usable rather than nothing.
NOSIZE = "https://is1-ssl.mzstatic.com/image/thumb/Music/cover.jpg"
check("a URL with no size segment is returned unchanged",
      hires(NOSIZE) == NOSIZE, hires(NOSIZE))

check("an empty URL stays empty", hires("") == "")

# The old behaviour, for contrast. On this particular nested URL the plain
# replace happens to produce the same string, which means the logged 404 was NOT
# produced here - it was produced upstream, when iTunes served a URL whose size
# segment was not the one we assumed. The rewrite is still the right fix: it
# only ever touches a genuine trailing size segment, so it cannot mangle a path
# that contains "100x100bb" anywhere else.
OLD = NESTED.replace("100x100bb", "600x600bb")
check("the OLD replace left the nested filename in place (no corruption)",
      OLD == out,
      "old=%r" % OLD)

# Where the old code actually went wrong: the size token appearing mid-path.
MIDPATH = ("https://is1-ssl.mzstatic.com/image/thumb/Music/100x100bb"
           "/real_cover.jpg")
OLD_MID = MIDPATH.replace("100x100bb", "600x600bb")
NEW_MID = hires(MIDPATH)
check("the OLD replace corrupted a mid-path size token",
      OLD_MID != MIDPATH and MIDPATH not in OLD_MID, OLD_MID)
check("  the new version leaves such a URL untouched",
      NEW_MID == MIDPATH, NEW_MID)

# ---- No silent DOWNGRADE to the 100px original --------------------------
# The rewrite above required the size token to be followed by ".jpg"/".png".
# iTunes also serves two other real shapes - a bare size segment with NO
# extension, and a .webp - and those silently stayed at 100px. That is a
# regression against the plain replace, which upgraded both. Every check below
# asserts the URL is genuinely upgraded, never merely "not mangled".
BARE = "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/44/06/fd/44.rgb.jpg/100x100bb"
check("a bare size segment with no extension is still upgraded",
      hires(BARE).endswith("/600x600bb"), hires(BARE))

WEBP = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100bb.webp"
check("a .webp artwork URL is still upgraded",
      hires(WEBP).endswith("/600x600bb.webp"), hires(WEBP))

# The 60px thumbnail is artworkUrl60, the fallback field. Upgrading it is a
# strict improvement - the old replace left it at 60px.
SMALL = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/60x60bb.jpg"
check("a 60px thumbnail is upgraded rather than left tiny",
      hires(SMALL).endswith("/600x600bb.jpg"), hires(SMALL))

# iTunes returns uppercase occasionally; the old literal replace missed these.
UPPER = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100BB.JPG"
check("an uppercase URL is upgraded (the old replace was case-sensitive)",
      hires(UPPER).endswith("/600x600BB.JPG"), hires(UPPER))

# Regression guard, stated as the invariant rather than as per-shape examples:
# for every shape the plain replace upgraded, so must this.
ALL_SHAPES = [NESTED, PLAIN, BN, BARE, WEBP, SMALL, UPPER,
              BARE + ".jpg", "https://is1-ssl.mzstatic.com/image/thumb/M/x/"
              "859727388959_cover.jpg/100x100bb.jpg",
              "https://is1-ssl.mzstatic.com/image/thumb/M/x/cover_100x100bb.jpg"]
downgraded = [u for u in ALL_SHAPES
              if "100x100bb" in u.lower()
              and "600x600" not in hires(u).lower()]
check("no shape is left sitting at 100px", not downgraded, downgraded)

# And the safety property that motivated the rewrite must still hold.
check("a size token that is not a real segment is never touched",
      hires(MIDPATH) == MIDPATH, hires(MIDPATH))
check("Discogs-style URLs are untouched",
      hires("https://i.discogs.com/abc-123.jpeg")
      == "https://i.discogs.com/abc-123.jpeg")

print()
if failures:
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("all iTunes artwork URL checks passed")
sys.exit(0)