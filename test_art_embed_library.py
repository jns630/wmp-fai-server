"""End-to-end: a real library folder, a library-tag beacon, and a CD beacon.

Builds actual MP3 files on disk, fires the same /client_error beacon the FAI
dialog fires, and checks what happened to the bytes.
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
    print("%-56s %s" % (name, "PASS" if ok else "FAIL " + str(detail)))
    if not ok:
        failures.append(name)


def make_mp3(path, album, artist, title):
    body = b""
    for fid, val in ((b"TALB", album), (b"TPE1", artist), (b"TIT2", title)):
        payload = b"\x03" + val.encode("utf-8") + b"\x00"
        body += fid + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    with open(path, "wb") as handle:
        handle.write(ae._id3_build(body)
                     + b"\xff\xfb\x90\x00" + b"\x00" * 413)


music = tempfile.mkdtemp(prefix="lib_")
ALBUM, ARTIST = "Signals From The Quiet Room", "The Modulation Set"
targets = []
for i, t in enumerate(("First Light", "Carrier Wave", "Standing Wave"), 1):
    p = os.path.join(music, "t%d.mp3" % i)
    make_mp3(p, ALBUM, ARTIST, t)
    targets.append(p)

other = os.path.join(music, "other.mp3")
make_mp3(other, "A Completely Different Record", "Someone Else", "Untitled")

fai.ART_EMBED_ENABLED = True
fai.ART_EMBED_FOLDERS = [music]
fai.ART_EMBED_MAX_FILES = 200
fai._fetch_artwork_bytes = lambda url: PNG

fai.PENDING_WRITE["xml"] = (
    "<METADATA><version>5.0</version><status>OK</status>"
    "<MDR-CD><albumTitle>%s</albumTitle><albumArtist>%s</albumArtist>"
    "<largeCoverParams>http://example.invalid/cover.jpg</largeCoverParams>"
    "</MDR-CD></METADATA>" % (ALBUM, ARTIST))


def apic_count(path):
    with open(path, "rb") as handle:
        blob = handle.read()
    raw = blob[6:10]
    size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
            | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
    frames = ae._id3_frames(blob[:10 + size]) or []
    return sum(1 for f in frames if f[:4] == b"APIC")


r = client.post("/client_error", json={"page": "finish", "applied": True,
                                      "toc": "", "write": "WriteNamesEx-wmid-ok"})
check("beacon accepted", r.status_code == 200, r.status_code)
check("library: art written to every track of the album",
      all(apic_count(p) == 1 for p in targets),
      [apic_count(p) for p in targets])
check("library: a DIFFERENT album was not touched", apic_count(other) == 0)
check("library: tags survived", ae.read_tags(targets[0])["album"] == ALBUM,
      ae.read_tags(targets[0]))

cdrip = os.path.join(music, "cdrip.mp3")
make_mp3(cdrip, ALBUM, ARTIST, "From The Disc")
r = client.post("/client_error", json={"page": "finish", "applied": True,
                                      "toc": "+hAhAAQBAAMAAwADAAQAAAAA",
                                      "write": "WriteNamesEx-toc-ok"})
check("CD: beacon accepted", r.status_code == 200, r.status_code)
check("CD: art NOT written even though the album matches",
      apic_count(cdrip) == 0,
      "a CD rip must never be touched - WMP already wrote the artwork")

r = client.post("/client_error", json={"page": "finish", "applied": False})
check("guard: applied=false does nothing", r.status_code == 200)
r = client.post("/client_error", json={"page": "search", "applied": True})
check("guard: non-finish page does nothing", r.status_code == 200)

fai.ART_EMBED_ENABLED = False
r = client.post("/client_error", json={"page": "finish", "applied": True})
check("guard: disabled by default does nothing", r.status_code == 200)

amb = tempfile.mkdtemp(prefix="amb_")
for who in ("Artist One", "Artist Two"):
    p = os.path.join(amb, "%s.mp3" % who.replace(" ", "_"))
    make_mp3(p, "Greatest Hits", who, "Track")
fai.ART_EMBED_ENABLED = True
fai.ART_EMBED_FOLDERS = [amb]
fai.PENDING_WRITE["xml"] = (
    "<METADATA><albumTitle>Greatest Hits</albumTitle>"
    "<albumArtist></albumArtist>"
    "<largeCoverParams>http://example.invalid/c.jpg</largeCoverParams>"
    "</METADATA>")
r = client.post("/client_error", json={"page": "finish", "applied": True})
check("guard: ambiguous album title writes nothing",
      all(apic_count(os.path.join(amb, n)) == 0 for n in os.listdir(amb)),
      "two artists share this title; guessing would cross over")

print()
if failures:
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("all library-art checks passed")
sys.exit(0)