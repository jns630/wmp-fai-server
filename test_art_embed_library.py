"""End-to-end: a real library folder, a library-tag beacon, and a CD beacon.

Builds actual MP3 files on disk, fires the same /client_error beacon the FAI
dialog fires, and checks what happened to the bytes.
"""
import importlib
import os
import struct
import sys
import tempfile
import time

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
    """Write an MP3. A falsy value omits that frame entirely, which is exactly
    how a file WMP has not tagged yet looks - see the CD-shape check below."""
    body = b""
    for fid, val in ((b"TALB", album), (b"TPE1", artist), (b"TIT2", title)):
        if not val:
            continue
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
# The real deferral is 30s. Pushed out of reach here so a queued thread cannot
# wake up and write files partway through the assertions below - the queueing
# itself is what this file checks, and it is checked synchronously.
fai.ART_EMBED_RIP_DEFER = 3600

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
# A rip IS written now, but never from inside this request - the beacon fires in
# the same JS turn as WriteNamesEx and WMP is still writing these files. So the
# file must be untouched the instant the beacon returns, which is exactly what
# this asserts.
check("CD: nothing written synchronously (deferred, not refused)",
      apic_count(cdrip) == 0,
      "the rip write must be deferred; a synchronous write races WMP and can "
      "leave a half-written file")
check("CD: the deferred write is actually queued",
      (ALBUM, ARTIST) in fai.RIP_EMBED_PENDING,
      "deferring without queueing would silently mean never writing")

# ---------------------------------------------------------------------------
# The REAL disc shape, which the check above does NOT cover.
#
# A rip arrives with ?cd=... and an EMPTY toc, so WMP_TOC is "". Guarding on
# `toc` alone therefore did not recognise a disc at all. Taken from an actual
# rip of a real CD:
#     GET /FAI/default.aspx?...&cd=B+96+1970+523A+...
#     [STAGED] album='Tiny Cities' tracks=11 req_id='' toc=''
# The file below is left UNTAGGED on purpose, so the untagged-folder fallback
# would eagerly match it - this check fails loudly if the guard regresses to
# `toc` alone, which is what let a rip be written to mid-write.
print()
print("the dialog's tags, written into a library album:")
tags_dir = tempfile.mkdtemp(prefix="tags_")
TALB, TARTIST = "Signals From The Quiet Room", "The Modulation Set"
tagged = []
for i, t in enumerate(("First Light On The Dial", "Carrier Wave"), 1):
    p = os.path.join(tags_dir, "t%d.mp3" % i)
    make_mp3(p, TALB, TARTIST, t)          # titles already correct, as in a
    tagged.append(p)                       # real library album being updated
fai.ART_EMBED_FOLDERS = [tags_dir]
fai.ART_EMBED_TAGS_ENABLED = True
fai._fetch_artwork_bytes = lambda url: PNG


def id3_frames(path):
    """{frame_id: text} for an MP3 - the reader a tagger would use."""
    with open(path, "rb") as handle:
        blob = handle.read()
    raw = blob[6:10]
    size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
            | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
    out = {}
    for f in ae._id3_frames(blob[:10 + size]) or []:
        out.setdefault(f[:4], ae._decode_id3_text(f[10:]))
    return out


fai.PENDING_WRITE["xml"] = (
    "<METADATA><MDR-CD>"
    "<albumTitle>%s</albumTitle><albumArtist>%s</albumArtist>"
    "<genre>Ambient</genre><releaseDate>2019/01/01</releaseDate>"
    "<largeCoverParams>http://example.invalid/c.jpg</largeCoverParams>"
    "<track><trackTitle>First Light On The Dial</trackTitle>"
    "<trackNumber>1</trackNumber><discNumber>1</discNumber>"
    "<trackArtist>%s</trackArtist></track>"
    "<track><trackTitle>Carrier Wave</trackTitle>"
    "<trackNumber>2</trackNumber><discNumber>1</discNumber>"
    "<trackArtist>%s</trackArtist></track>"
    "</MDR-CD></METADATA>" % (TALB, TARTIST, TARTIST, TARTIST))
r = client.post("/client_error", json={"page": "finish", "applied": True,
                                      "write": "WriteNamesEx-wmid-ok"})
check("tags: beacon accepted", r.status_code == 200, r.status_code)

got0, got1 = id3_frames(tagged[0]), id3_frames(tagged[1])
check("tags: album written into the files",
      got0.get(b"TALB") == TALB, got0.get(b"TALB"))
check("tags: artist written into the files",
      got0.get(b"TPE1") == TARTIST, got0.get(b"TPE1"))
check("tags: genre written into the files",
      got0.get(b"TCON") == "Ambient", got0.get(b"TCON"))
check("tags: year written into the files",
      got0.get(b"TYER") == "2019", got0.get(b"TYER"))
check("tags: track 1 numbered 1", got0.get(b"TRCK") == "1", got0.get(b"TRCK"))
check("tags: track 2 numbered 2 - per-file, not per-album",
      got1.get(b"TRCK") == "2", got1.get(b"TRCK"))
check("tags: titles preserved",
      got0.get(b"TIT2") == "First Light On The Dial"
      and got1.get(b"TIT2") == "Carrier Wave",
      (got0.get(b"TIT2"), got1.get(b"TIT2")))
check("tags: the cover is written in the SAME pass",
      all(apic_count(p) == 1 for p in tagged),
      [apic_count(p) for p in tagged])
check("tags: no XML escaping leaked into the files",
      "&amp;" not in (got0.get(b"TPE1") or "") + (got0.get(b"TALB") or ""),
      "the staged document is escaped; writing it raw would double-escape")

# A file whose title is NOT in the document must keep its own title and number.
# Numbering it would be a guess, and a player shows a guess as fact.
stray = os.path.join(tags_dir, "unknown.mp3")
make_mp3(stray, TALB, TARTIST, "A Song The Document Does Not List")
client.post("/client_error", json={"page": "finish", "applied": True})
gs = id3_frames(stray)
check("tags: an unlisted track keeps its OWN title and number",
      gs.get(b"TIT2") == "A Song The Document Does Not List"
      and not gs.get(b"TRCK"),
      (gs.get(b"TIT2"), gs.get(b"TRCK")))
check("tags: it still gets the album-level tags",
      gs.get(b"TALB") == TALB and gs.get(b"TCON") == "Ambient",
      (gs.get(b"TALB"), gs.get(b"TCON")))

# Re-applying must not rewrite anything: the values already agree, so this is
# the property that stops a repeated apply churning the user's library.
touched = tagged + [stray]
before = [(os.path.getmtime(p), os.path.getsize(p)) for p in touched]
client.post("/client_error", json={"page": "finish", "applied": True})
check("tags: re-applying the same album rewrites NOTHING",
      before == [(os.path.getmtime(p), os.path.getsize(p)) for p in touched],
      "an idempotent write is what makes this safe to run on every apply")

fai.ART_EMBED_TAGS_ENABLED = False
make_mp3(tagged[1], TALB, TARTIST, "Carrier Wave")
client.post("/client_error", json={"page": "finish", "applied": True})
check("tags: embed_tags_in_library = false writes no tags at all",
      not id3_frames(tagged[1]).get(b"TCON"),
      id3_frames(tagged[1]).get(b"TCON"))
fai.ART_EMBED_TAGS_ENABLED = True

# ---------------------------------------------------------------------------
# A CD RIP IS NEVER TAGGED. WMP applies a disc's tags itself over COM, so
# writing them from here would race WMP's own write.
print()
print("a CD rip is never tagged from here:")
rip_dir = tempfile.mkdtemp(prefix="riptags_")
rip_file = os.path.join(rip_dir, "disc.mp3")
make_mp3(rip_file, None, None, "Whatever The Rip Had")
fai.ART_EMBED_FOLDERS = [rip_dir]
fai.ART_EMBED_RIP_DEFER = 3600
fai.RIP_EMBED_PENDING.clear()
fai.PENDING_WRITE["xml"] = (
    "<METADATA><MDR-CD><albumTitle>%s</albumTitle>"
    "<albumArtist>%s</albumArtist><genre>Ambient</genre>"
    "<releaseDate>2019/01/01</releaseDate>"
    "<largeCoverParams>http://example.invalid/c.jpg</largeCoverParams>"
    "</MDR-CD></METADATA>" % (TALB, TARTIST))
r = client.post("/client_error", json={"page": "finish", "applied": True,
                                      "toc": "+hAhAAQBAAMAAwADAAQAAAAA",
                                      "write": "WriteNamesEx-toc-ok"})
check("rip: beacon accepted", r.status_code == 200, r.status_code)
# The rip path passes no document; drive it directly to prove that is what
# keeps the tags out, rather than relying on the beacon's routing alone.
fai._embed_into_files(TALB, TARTIST, "http://example.invalid/c.jpg",
                      settle=0, recent_seconds=fai.ART_EMBED_RIP_WINDOW)
check("rip: the deferred rip write carries NO document, so no tags are written",
      not id3_frames(rip_file).get(b"TALB")
      and not id3_frames(rip_file).get(b"TCON"),
      id3_frames(rip_file))
fai.ART_EMBED_FOLDERS = [tags_dir]

print()
print("the real disc shape (cd set, toc empty):")
cdrip_dir = tempfile.mkdtemp(prefix="cdrip_")
cdrip2 = os.path.join(cdrip_dir, "track01.mp3")
make_mp3(cdrip2, None, None, "Untagged, mid-rip")
fai.ART_EMBED_FOLDERS = [cdrip_dir]
check("precondition: the fallback WOULD have matched this file",
      len(fai._find_album_files(ALBUM, ARTIST, [cdrip_dir])) == 1,
      "so it is the cd/toc guard, not the matching, that protects the rip")
fai.PENDING_WRITE["xml"] = (
      "<METADATA><MDR-CD><albumTitle>%s</albumTitle>"
      "<albumArtist>%s</albumArtist>"
      "<largeCoverParams>http://example.invalid/c.jpg</largeCoverParams>"
      "<track><trackTitle>Untagged, mid-rip</trackTitle></track>"
      "</MDR-CD></METADATA>" % (ALBUM, ARTIST))
scheduled_titles = []
schedule_rip = fai._schedule_rip_art_embed


def capture_rip_schedule(album_title, artist, cover_url, track_titles=None):
      scheduled_titles.append(track_titles)
      schedule_rip(album_title, artist, cover_url, track_titles)


fai._schedule_rip_art_embed = capture_rip_schedule
r = client.post("/client_error", json={"page": "finish", "applied": True,
                                       "toc": "",
                                       "cd": "B+96+1970+523A+1+150+200",
                                       "library_mode": False,
                                       "write": "WriteNamesEx-cdid-ok"})
fai._schedule_rip_art_embed = schedule_rip
check("real CD shape: beacon accepted", r.status_code == 200, r.status_code)
check("real CD beacon passes staged track titles to the retry worker",
        scheduled_titles == [["Untagged, mid-rip"]], scheduled_titles)
check("real CD shape: nothing written synchronously",
      apic_count(cdrip2) == 0,
      "identified by ?cd=, not by toc - and deferred, so the file is untouched "
      "the moment the beacon returns")
check("real CD shape: the deferred write is queued too",
      (ALBUM, ARTIST) in fai.RIP_EMBED_PENDING,
      "a rip identified only by ?cd= must reach the same deferred write as one "
      "identified by toc - this is the shape that used to slip through entirely")

# The settle check, which is the actual safety mechanism. A file WMP is still
# writing must be skipped rather than raced, and a file WMP has finished with
# must be written. Driven directly so it does not need a 30-second sleep.
fai.ART_EMBED_FOLDERS = [cdrip_dir]
os.utime(cdrip2, (time.time(), time.time()))       # "WMP is writing it right now"
fai._embed_into_files(ALBUM, ARTIST, "http://example.invalid/c.jpg",
                          settle=fai.ART_EMBED_RIP_SETTLE)
check("settle: a file still being written is SKIPPED, not raced",
      apic_count(cdrip2) == 0,
      "writing into a file WMP has not finished with is the corruption case the "
      "original guard existed to prevent")
_old = time.time() - (fai.ART_EMBED_RIP_SETTLE + 60)
os.utime(cdrip2, (_old, _old))                     # WMP finished long ago
fai._embed_into_files(ALBUM, ARTIST, "http://example.invalid/c.jpg",
                          settle=fai.ART_EMBED_RIP_SETTLE)
check("settle: a file WMP has finished with IS written",
      apic_count(cdrip2) == 1,
      "deferring must not turn into never writing - that was the bug")

# Two applies must not leave two threads racing over the same files.
fai.RIP_EMBED_PENDING.clear()
fai._schedule_rip_art_embed(ALBUM, ARTIST, "http://example.invalid/c.jpg")
_first = fai.RIP_EMBED_PENDING.get((ALBUM, ARTIST))
fai._schedule_rip_art_embed(ALBUM, ARTIST, "http://example.invalid/c.jpg")
_second = fai.RIP_EMBED_PENDING.get((ALBUM, ARTIST))
check("a later apply supersedes the queued one",
      _first is not None and _second is not None and _first is not _second,
      "two pending writes for one album would race each other")

# An unrelated untagged track anywhere in the library must not make the rip
# lookup refuse. This is a real shape: the fallback below only trusts the
# untagged files when they share one parent folder, so a single stray untagged
# file under Music used to silently mean "no art" on the disc.
stray_dir = tempfile.mkdtemp(prefix="stray_")
stray = os.path.join(stray_dir, "some_other_download.mp3")
make_mp3(stray, None, None, "Untagged, not this album")
# Backdated, because that is the real shape: the stray has been sitting there for
# months, the disc's own files were written seconds ago. A stray created NOW
# would be indistinguishable from the rip, and no time window could separate them.
_old_stray = time.time() - (fai.ART_EMBED_RIP_WINDOW * 3)
os.utime(stray, (_old_stray, _old_stray))
fai.ART_EMBED_FOLDERS = [cdrip_dir, stray_dir]
check("rip lookup is narrowed by the window, not defeated by a stray file",
      len(fai._find_album_files(ALBUM, ARTIST, fai.ART_EMBED_FOLDERS,
                                recent_seconds=fai.ART_EMBED_RIP_WINDOW)) == 1,
      "a stray untagged file elsewhere must not stop the disc's own file "
      "from being found")
check("the same lookup WITHOUT the window still refuses (unchanged safety)",
      fai._find_album_files(ALBUM, ARTIST, fai.ART_EMBED_FOLDERS) == [],
      "a library-wide guess is still refused; only the rip path is narrowed")

# The rip window is authoritative even when it finds nothing. Falling through
# would let an old untagged file masquerade as the just-ripped disc.
stale_rip = tempfile.mkdtemp(prefix="stale_rip_")
stale_file = os.path.join(stale_rip, "old_untagged.mp3")
make_mp3(stale_file, None, None, None)
_old = time.time() - (fai.ART_EMBED_RIP_WINDOW * 3)
os.utime(stale_file, (_old, _old))
check("a rip with no fresh files does not fall back to stale untagged files",
      fai._find_album_files(ALBUM, ARTIST, [stale_rip],
                            recent_seconds=fai.ART_EMBED_RIP_WINDOW) == [],
      "the rip window must not be bypassed when it finds no candidates")

# WMP's default WMA files have no title tags available to our matcher, so the
# staged track list must be checked against the numbered filename instead.
wma_rip = tempfile.mkdtemp(prefix="wma_rip_")
wma_match = os.path.join(wma_rip, "01 A Few Less Sour.wma")
wma_wrong = os.path.join(wma_rip, "02 Central Reservation.wma")
make_mp3(wma_match, None, None, "ignored by WMA tag dispatch")
make_mp3(wma_wrong, None, None, "also ignored by WMA tag dispatch")
check("a WMA rip matches the staged title in its numbered filename",
        fai._find_album_files(
              "For All Intents And Purposes", "The Evergreen Trio", [wma_rip],
              recent_seconds=fai.ART_EMBED_RIP_WINDOW,
              expected_track_titles=["A Few Less Sour"])
        == [wma_match], wma_rip)
check("unrelated recent WMA files are not selected for a rip",
        fai._find_album_files(
              "For All Intents And Purposes", "The Evergreen Trio", [wma_rip],
              recent_seconds=fai.ART_EMBED_RIP_WINDOW,
              expected_track_titles=["A Few Less Sour", "Another Song"])
        == [wma_match], wma_rip)

wrong_album_rip = tempfile.mkdtemp(prefix="wrong_album_rip_")
for index, title in enumerate(("Stolen Car", "Sweetest Decline",
                                             "So Much More"), 1):
      make_mp3(os.path.join(wrong_album_rip, "%02d %s.wma" % (index, title)),
                   None, None, title)
check("a recent unrelated album is refused even in one folder",
        fai._find_album_files(
              "For All Intents And Purposes", "The Evergreen Trio",
              [wrong_album_rip], recent_seconds=fai.ART_EMBED_RIP_WINDOW,
              expected_track_titles=["A Few Less Sour", "The Evergreen Trio"])
        == [], wrong_album_rip)

# ======================================================================
# REGRESSION, from a real log. A RIP into a folder that already holds other
# albums' TAGGED files was refused outright:
#
#   [ART-EMBED] 'Tiny Cities': C:\...\Sample Music also holds 16 other tagged
#               album(s), so the untagged files there are not identifiable as
#               this album; skipped
#
# The guard collected "other albums" across the WHOLE library, so it was
# non-empty for anyone owning more than one album - and the rip narrowing that
# was added for exactly this case was computed, logged, and then thrown away by
# it. Every rip in a populated library lost its cover.
#
# The fix: when the rip window actually narrowed the pool, that window is
# authoritative. The tests below pin that, and pin that the LIBRARY path is
# still refused.
# ======================================================================
shared = tempfile.mkdtemp(prefix="shared_")
for i in range(3):
    make_mp3(os.path.join(shared, "old_%d.mp3" % i),
             "Some Other Album", "Some Other Artist", "Track %d" % i)
_old = time.time() - (fai.ART_EMBED_RIP_WINDOW * 3)
for _f in os.listdir(shared):
    os.utime(os.path.join(shared, _f), (_old, _old))

RIP_ALBUM, RIP_ARTIST = "Tiny Cities", "Sun Kil Moon"
ripped = []
for i in range(1, 12):                     # a real album's worth of tracks
    p = os.path.join(shared, "11_%02d.mp3" % i)
    make_mp3(p, None, None, None)          # WMP has not written tags yet
    ripped.append(p)

_found = fai._find_album_files(RIP_ALBUM, RIP_ARTIST, [shared],
                               recent_seconds=fai.ART_EMBED_RIP_WINDOW)
check("a rip into a folder holding other albums is FOUND",
      sorted(_found) == sorted(ripped),
      "found %d of %d; the rip window must beat the shared-folder guard"
      % (len(_found), len(ripped)))
check("...and it is not refused for the folder holding other albums",
      len(_found) > 0, "this is the exact refusal in the log")

check("the same rip lookup WITHOUT the window is still refused",
      fai._find_album_files(RIP_ALBUM, RIP_ARTIST, [shared]) == [],
      "a library-wide guess must stay refused; only a narrowed rip may pass")

# The rip window must still respect the hard ceiling. An unbounded write is
# the one outcome that must never happen.
_many = tempfile.mkdtemp(prefix="many_")
for i in range(fai.ART_EMBED_MAX_FILES + 5):
    make_mp3(os.path.join(_many, "%03d.mp3" % i), None, None, None)
check("the rip path still caps at ART_EMBED_MAX_FILES",
      len(fai._find_album_files("Capped", "Nobody", [_many],
                                recent_seconds=fai.ART_EMBED_RIP_WINDOW))
      == fai.ART_EMBED_MAX_FILES,
      "an unbounded rip write is the one thing that must never happen")

# A rip whose files are spread over several folders: the mtime evidence is
# strong, but "all of them" is not one album and the folder guard still applies
# rather than trusting a spread.
spread_root = tempfile.mkdtemp(prefix="spread_")
for sub in ("disc1", "disc2"):
    d = os.path.join(spread_root, sub)
    os.makedirs(d)
    make_mp3(os.path.join(d, "a.mp3"), None, None, None)
check("a rip spread over 2 folders is NOT trusted as one album",
      fai._find_album_files("Spread", "Nobody", [spread_root],
                            recent_seconds=fai.ART_EMBED_RIP_WINDOW) == [],
      "two folders of fresh files is not one disc")

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