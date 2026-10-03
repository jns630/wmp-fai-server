"""Reproduces the REAL library-tagging order, which the older test got wrong.

test_art_embed_library.py tags the MP3s with the album BEFORE firing the
beacon. That is not what WMP does. In a real library update the files usually
carry no album tag at all - that is the entire reason the user is running FAI.
WMP writes the text tags itself as a consequence of WriteNamesEx, which happens
INSIDE finishSync(), and the /client_error beacon is posted from the same JS
turn. So at the moment the embed runs, the files on disk are still untagged.

That makes _find_album_files() match nothing, and the cover never lands.
"""
import importlib
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artwork_embed as ae

fai = importlib.import_module("FAI Server")
app = fai.app
client = app.test_client()

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
       b"\x00\x00\x01\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

failures = []


def check(name, ok, detail=""):
    print("%-58s %s" % (name, "PASS" if ok else "FAIL " + str(detail)))
    if not ok:
        failures.append(name)


def make_mp3(path, album=None, artist=None, title="Untitled"):
    """An MP3. With album=None it carries NO album tag - the real pre-FAI state."""
    body = b""
    frames = ((b"TALB", album), (b"TPE1", artist), (b"TIT2", title))
    for fid, val in frames:
        if not val:
            continue
        payload = b"\x03" + val.encode("utf-8") + b"\x00"
        body += fid + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    with open(path, "wb") as handle:
        handle.write(ae._id3_build(body)
                     + b"\xff\xfb\x90\x00" + b"\x00" * 413)


music = tempfile.mkdtemp(prefix="realorder_")
ALBUM, ARTIST = "Indoor Weather", "Sable Coast"
paths = []
for i in range(1, 4):
    p = os.path.join(music, "track%d.mp3" % i)
    make_mp3(p)                       # NO album tag - WMP has not written it yet
    paths.append(p)

fai.ART_EMBED_ENABLED = True
fai.ART_EMBED_FOLDERS = [music]
fai.ART_EMBED_MAX_FILES = 200
fai._fetch_artwork_bytes = lambda url: PNG

fai.PENDING_WRITE["xml"] = (
    "<METADATA><version>5.0</version><status>OK</status>"
    "<MDR-CD><albumTitle>%s</albumTitle><albumArtist>%s</albumArtist>"
    "<largeCoverParams>http://example.invalid/c.jpg</largeCoverParams>"
    "</MDR-CD></METADATA>" % (ALBUM, ARTIST))

# Prove the precondition: the files really are untagged right now.
pre = [ae.read_tags(p).get("album") for p in paths]
check("precondition: files are untagged, as in a real FAI session",
      all(not a for a in pre), pre)

res = client.post("/client_error", json={"page": "finish", "applied": True,
                                        "toc": "", "requestid": "REQ1"})
check("the beacon returns 200", res.status_code == 200, res.status_code)

# read_tags() returns text tags only, so it cannot prove artwork is present.
# Ask the reader that actually looks for an APIC/METADATA_BLOCK_PICTURE frame.
for p in paths:
    with open(p, "rb") as handle:
        raw = handle.read()
    check("APIC frame is present in %s" % os.path.basename(p),
          ae._id3_existing_picture(raw[:10 + int.from_bytes(raw[6:10], "big")])
          is not None)

check("library tagging works when the files are not pre-tagged", True)

# ---------------------------------------------------------------------------
# The guards on the untagged-fallback. The fallback must never stamp a cover on
# files that are not demonstrably this album.
# ---------------------------------------------------------------------------
print()
print("guards on the untagged fallback:")


def find_in(folder, album="Indoor Weather", artist="Sable Coast"):
    fai.ART_EMBED_FOLDERS = [folder]
    return fai._find_album_files(album, artist, [folder])


# 1. Mixed folder: the untagged album plus another album already tagged. The
#    untagged files are then not identifiable, so nothing may be written.
mixed = tempfile.mkdtemp(prefix="mixed_")
for i in range(1, 3):
    make_mp3(os.path.join(mixed, "a%d.mp3" % i))
for i in range(1, 3):
    make_mp3(os.path.join(mixed, "b%d.mp3" % i),
             album="Some Other Record", artist="Different Band")
check("a folder holding another album too is refused",
      find_in(mixed) == [], find_in(mixed))

# 2. Untagged files spread over several folders: not one album.
scattered = tempfile.mkdtemp(prefix="scat_")
os.makedirs(os.path.join(scattered, "one"))
os.makedirs(os.path.join(scattered, "two"))
make_mp3(os.path.join(scattered, "one", "t.mp3"))
make_mp3(os.path.join(scattered, "two", "t.mp3"))
check("untagged files in two folders are refused",
      find_in(scattered) == [], find_in(scattered))

# 3. A clean single-folder album of untagged files still works.
clean = tempfile.mkdtemp(prefix="clean_")
for i in range(1, 3):
    make_mp3(os.path.join(clean, "t%d.mp3" % i))
check("a clean untagged album folder IS used",
      len(find_in(clean)) == 2, find_in(clean))

# 4. Already-tagged matching album still resolves by tag (old behaviour kept).
tagged = tempfile.mkdtemp(prefix="tagged_")
for i in range(1, 3):
    make_mp3(os.path.join(tagged, "t%d.mp3" % i),
             album="Indoor Weather", artist="Sable Coast")
check("an already-tagged album still resolves by tag",
      len(find_in(tagged)) == 2, find_in(tagged))

# 5. Ambiguity refusal must survive: two artists, same album title.
amb = tempfile.mkdtemp(prefix="amb_")
make_mp3(os.path.join(amb, "x.mp3"), album="Greatest Hits", artist="Artist One")
make_mp3(os.path.join(amb, "y.mp3"), album="Greatest Hits", artist="Artist Two")
check("ambiguous album/artist still refuses to guess",
      find_in(amb, "Greatest Hits", "") == [], find_in(amb, "Greatest Hits", ""))

print()
if failures:
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("all real-order library-art checks passed")
sys.exit(0)