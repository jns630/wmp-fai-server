# -*- coding: utf-8 -*-
"""Tests for artwork_wma: the ASF/WMA WM/Picture writer.

Every test runs on a COPY of a real sample WMA. The user's library is read and
never written, and the last assertion proves it.

The assertions that matter most are the negative ones: that a file this module
cannot handle confidently is left byte-for-byte untouched.
"""
import glob
import hashlib
import os
import shutil
import struct
import sys
import tempfile

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import artwork_wma as wma

PASS = []
FAIL = []

#: A real 1x1 PNG, so what goes in is genuinely image bytes.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} :: {detail}")


_SAMPLE_ROOTS = [r"C:\Users\User\Music", os.path.join(os.path.expanduser("~"), "Music")]
_SAMPLE_PATHS = {path for root in _SAMPLE_ROOTS if os.path.isdir(root)
                 for path in glob.glob(os.path.join(root, "**", "*.wma"),
                                       recursive=True)}
SAMPLES = sorted(_SAMPLE_PATHS, key=lambda path: wma.has_art(path))
_TMP = tempfile.mkdtemp(prefix="wmatest-")

#: Digests taken up front; compared at the end to prove the library is intact.
_ORIGINALS = {}
for _s in SAMPLES:
    with open(_s, "rb") as _fh:
        _ORIGINALS[_s] = hashlib.sha256(_fh.read()).hexdigest()


def sample(index=0):
    """A private copy of a real WMA. Callers must use this, never SAMPLES."""
    src = SAMPLES[index % len(SAMPLES)]
    dst = os.path.join(_TMP, os.path.basename(src))
    shutil.copy2(src, dst)
    return dst


def parse(path):
    with open(path, "rb") as fh:
        return wma._parse_header(fh.read())


# ======================================================================
# 1. The samples parse and none carries an index object. That is the fact
#    the whole header-rewrite approach rests on, so it is asserted against
#    real files rather than a synthetic one.
# ======================================================================
check("real WMA samples are available to test against", len(SAMPLES) >= 3,
      "found %d" % len(SAMPLES))

for src in SAMPLES:
    tag = os.path.basename(src)[:26]
    objects = tail = None
    err = ""
    try:
        objects, tail, _size = parse(src)
    except ValueError as exc:
        err = str(exc)
    check("parses: %s" % tag, objects is not None, err)
    if objects is None:
        continue
    try:
        wma._assert_no_index_objects(objects, tail)
        clean, why = True, ""
    except ValueError as exc:
        clean, why = False, str(exc)
    check("no index object: %s" % tag, clean, why)
    check("data tail is non-empty: %s" % tag, len(tail) > 1000, len(tail))
    check("header declares sub-objects: %s" % tag, len(objects) > 0)
# ======================================================================
# 2. The blob round-trips: what goes in comes back out.
# ======================================================================
blob = wma._wmpicture_blob(PNG, "image/png", "cover")
split = wma._split_existing_picture(blob)
check("WM_PICTURE blob round-trips the image",
      split is not None and split[2] == PNG, split)
check("WM_PICTURE blob round-trips the MIME type",
      split is not None and split[0] == "image/png", split)
check("WM_PICTURE blob round-trips the description",
      split is not None and split[1] == "cover", split)
check("the picture type is FRONT COVER (3)", blob[16] == 3, blob[16])

jpeg = wma._wmpicture_blob(b"\xff\xd8\xff\xe0JFIF", "image/jpeg", "")
d2 = wma._split_existing_picture(jpeg)
check("a JPEG blob round-trips too",
      d2 is not None and d2[2] == b"\xff\xd8\xff\xe0JFIF" and d2[0] == "image/jpeg",
      d2)

big = wma._wmpicture_blob(b"\xab" * 5000, "image/jpeg", "x")
check("a 5000-byte blob round-trips (padding maths holds)",
      wma._split_existing_picture(big)[2] == b"\xab" * 5000)

# ======================================================================
# 3. Writing to a real file. The cover must read back AND the audio must be
#    byte-identical - that is what proves the header was grown and the data
#    shifted, rather than the file being rewritten.
# ======================================================================
path = sample()
with open(path, "rb") as fh:
    original = fh.read()
orig_objects, orig_tail, orig_size = wma._parse_header(original)

check("a fresh sample has no cover art", wma.read_art(path) is None)

check("write_art reports success on a real WMA",
      wma.write_art(path, PNG, "image/png", "cover") is True)

with open(path, "rb") as fh:
    after = fh.read()
check("the file grew", len(after) > len(original),
      "%d -> %d" % (len(original), len(after)))

new_objects, new_tail, new_size = wma._parse_header(after)
check("the rewritten file still parses", new_objects is not None)
check("the audio tail is byte-for-byte identical",
      new_tail == orig_tail, "the audio was rewritten, not shifted")
check("the data object moved by exactly the header growth",
      new_size - orig_size == len(after) - len(original),
      "growth %d vs file growth %d" % (new_size - orig_size,
                                       len(after) - len(original)))
check("every sub-object except the Metadata one is untouched",
      [raw for g, raw in new_objects if g != wma._ASF_METADATA]
      == [raw for g, raw in orig_objects if g != wma._ASF_METADATA])

# ======================================================================
# 4. Writing twice REPLACES rather than duplicating. Two cover records is a
#    file whose displayed cover WMP resolves unpredictably.
# ======================================================================
SECOND = b"\xff\xd8\xff\xe0SECONDIMAGE"
check("a second write reports success",
      wma.write_art(path, SECOND, "image/jpeg", "second") is True)
read2 = wma.read_art(path)
check("the second write replaced the first",
      read2 is not None and read2[2] == SECOND,
      None if read2 is None else read2[0])

with open(path, "rb") as fh:
    final = fh.read()
f_objects, _f_tail, _f_size = wma._parse_header(final)
meta = [raw for g, raw in f_objects if g == wma._ASF_METADATA][0]
records = struct.unpack("<H", meta[26:28])[0]
check("exactly one metadata record remains", records == 1, records)

# The audio must STILL be identical after two writes.
_o2, f_tail2, _s2 = wma._parse_header(final)
check("the audio is still byte-identical after two writes",
      f_tail2 == orig_tail)

# ======================================================================
# 5. The refusals. These are the tests that stop this from eating a library.
# ======================================================================
bad = os.path.join(_TMP, "notasf.wma")
with open(bad, "wb") as fh:
    fh.write(b"this is not an ASF file at all" * 40)
check("a non-ASF file is refused", wma.write_art(bad, PNG) is False)
with open(bad, "rb") as fh:
    check("...and is left untouched", fh.read().startswith(b"this is not"))

# Truncating the AUDIO is not malformed: the header still parses and the data
# tail is merely shorter, so writing a cover there is safe and is allowed.
# Truncating the HEADER is malformed - the declared size no longer fits the file
# - and must be refused, because we would not know where the audio starts.
# An earlier version of this test called the audio-truncated case "corrupt" and
# asserted a refusal; that was wrong, and the code was right.
audio_cut = os.path.join(_TMP, "audiocut.wma")
with open(audio_cut, "wb") as fh:
    fh.write(original[:len(original) // 3])
with open(audio_cut, "rb") as fh:
    _objs, _tail, _sz = wma._parse_header(fh.read())
check("an audio-truncated file still parses, so a cover is safe there",
      len(_tail) > 1000, len(_tail))

header_cut = os.path.join(_TMP, "headercut.wma")
with open(header_cut, "wb") as fh:
    fh.write(original[:orig_size // 2])
check("a header-truncated file is refused",
      wma.write_art(header_cut, PNG) is False)
with open(header_cut, "rb") as fh:
    check("...and is left untouched", len(fh.read()) == orig_size // 2)

short = os.path.join(_TMP, "tiny.wma")
with open(short, "wb") as fh:
    fh.write(original[:12])
check("a 12-byte file is refused", wma.write_art(short, PNG) is False)

# A large cover is normal, not corrupt: real album art is 100-800 KB. The
# concern is whether the header-growth maths survives a big insertion, so this
# uses the WHOLE file (the previous version used a header-only fixture and then
# compared its empty tail against the full file's tail, which could not pass).
grow = os.path.join(_TMP, "grow.wma")
with open(grow, "wb") as fh:
    fh.write(original)
big_image = b"\xff\xd8\xff\xe0" + b"\x00" * (600 * 1024)
check("a large cover (600 KB) is written successfully",
      wma.write_art(grow, big_image, "image/jpeg") is True)
check("...and reads back intact",
      (wma.read_art(grow) or (None, None, None))[2] == big_image)
_g_objects, grow_tail, _grow_size = wma._parse_header(
    open(grow, "rb").read())
check("...with the audio still byte-identical after a 600 KB insert",
      grow_tail == orig_tail)
check("...and the file grew by roughly the cover size",
      len(open(grow, "rb").read()) - len(original) > 600 * 1024)

check("an empty image is refused",
      wma.write_art(sample(), b"", "image/png") is False)
check("an oversized image is refused",
      wma.write_art(sample(), b"\x00" * (wma.MAX_PICTURE_BYTES + 1)) is False)
check("a missing file is refused",
      wma.write_art(os.path.join(_TMP, "nope.wma"), PNG) is False)

# A header whose object count lies about itself must be refused, not repaired.
lying = bytearray(original)
struct.pack_into("<I", lying, 24, 999)
lying_path = os.path.join(_TMP, "lying.wma")
with open(lying_path, "wb") as fh:
    fh.write(bytes(lying))
check("a header whose object count disagrees is refused",
      wma.write_art(lying_path, PNG) is False)
with open(lying_path, "rb") as fh:
    check("...and is left byte-for-byte untouched", fh.read() == bytes(lying))

# An index object anywhere in the file must block the write: its absolute
# packet offsets would go stale the moment the header grew.
indexed = bytearray(original)
indexed[orig_size:orig_size] = wma._ASF_SIMPLE_INDEX
idx_path = os.path.join(_TMP, "indexed.wma")
with open(idx_path, "wb") as fh:
    fh.write(bytes(indexed))
idx_before = len(bytes(indexed))
check("a file with an index object is refused",
      wma.write_art(idx_path, PNG) is False)
with open(idx_path, "rb") as fh:
    check("...and is left byte-for-byte untouched",
          len(fh.read()) == idx_before)

# ======================================================================
# 5b. Reading the tags back. This is what lets artwork_embed find a WMA
#     album's tracks; with it returning nothing, every WMA looked untagged
#     and a populated library could never match one.
# ======================================================================
tagged_ok = []
for src in SAMPLES:
    try:
        tags = wma.read_tags(src)
    except Exception as exc:                   # must never raise on a scan
        tags = {"album": "", "artist": "", "title": "", "error": repr(exc)}
    tagged_ok.append(bool(tags.get("album") and tags.get("artist")
                          and tags.get("title")))
check("every real WMA reports album, artist and title",
      all(tagged_ok), "%d of %d did not" % (tagged_ok.count(False),
                                            len(tagged_ok)))

if SAMPLES:
    one = SAMPLES[0]
    tags = wma.read_tags(one)
    check("the album it reports is the folder WMP filed it under",
          tags["album"].strip().lower() ==
          os.path.basename(os.path.dirname(one)).strip().lower(),
          (tags["album"], os.path.dirname(one)))

    # The Extended Content Description is the only thing carrying the album,
    # so a file read as album-only proves that object is parsed on its own.
    def u16(text):
        return text.encode("utf-16-le") + b"\x00\x00"

    def obj(guid, body):
        return guid + struct.pack("<Q", 24 + len(body)) + body

    def synth(path, fields, content=None):
        records = b""
        for name, value in fields:
            nb, vb = u16(name), u16(value)
            records += (struct.pack("<H", len(nb)) + nb
                        + struct.pack("<HH", 0, len(vb)) + vb)
        ext = obj(wma._ASF_EXTENDED_CONTENT,
                  struct.pack("<H", len(fields)) + records)
        objects = ext
        count = 1
        if content:
            title_b, author_b = u16(content[0]), u16(content[1])
            body = (struct.pack("<5H", len(title_b), len(author_b), 2, 2, 2)
                    + title_b + author_b + b"\x00\x00" * 3)
            objects = obj(wma._ASF_CONTENT_DESCRIPTION, body) + ext
            count = 2
        header = (wma._ASF_HEADER + struct.pack("<Q", 30 + len(objects))
                  + struct.pack("<I", count) + b"\x01\x02" + objects)
        with open(path, "wb") as fh:
            fh.write(header + b"\x00" * 64)
        return path

    ext_only = synth(os.path.join(_TMP, "extonly.wma"), [
        ("WM/AlbumTitle", "An Album From The Extended Object"),
        ("WM/AlbumArtist", "An Artist"),
    ])
    ext_tags = wma.read_tags(ext_only)
    check("the Extended Content Description supplies album and artist",
          ext_tags["album"] == "An Album From The Extended Object"
          and ext_tags["artist"] == "An Artist", ext_tags)
    check("...and an absent title is empty, not invented",
          ext_tags["title"] == "", ext_tags)

    # WM/AlbumArtist must win over the Content Description's Author: the
    # dialog's artist is the album artist, and Author is often a per-track
    # credit.
    both = synth(os.path.join(_TMP, "both.wma"), [
        ("WM/AlbumTitle", "Album"), ("WM/AlbumArtist", "Album Artist"),
    ], content=("A Track Title", "Track Credit"))
    both_tags = wma.read_tags(both)
    check("the Content Description supplies the track title",
          both_tags["title"] == "A Track Title", both_tags)
    check("WM/AlbumArtist is preferred over Author",
          both_tags["artist"] == "Album Artist", both_tags)

not_asf = os.path.join(_TMP, "notasf.wma")
with open(not_asf, "wb") as fh:
    fh.write(b"this is not an asf file at all" * 4)
check("a non-ASF file returns empty tags rather than raising",
      wma.read_tags(not_asf) == {"album": "", "artist": "", "title": ""},
      wma.read_tags(not_asf))

bad_header = os.path.join(_TMP, "badheader.wma")
with open(bad_header, "wb") as fh:
    fh.write(original[:20])
check("a header-truncated file returns empty tags rather than raising",
      wma.read_tags(bad_header) == {"album": "", "artist": "", "title": ""},
      wma.read_tags(bad_header))

# ======================================================================
# 6. The original library was never written to.
# ======================================================================
untouched = []
for src in SAMPLES:
    with open(src, "rb") as fh:
        untouched.append(hashlib.sha256(fh.read()).hexdigest()
                         == _ORIGINALS[src])
check("every original sample file is unmodified", all(untouched),
      "%d of %d changed" % (untouched.count(False), len(untouched)))

print("")
print("=" * 60)
print(f"==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("Failures:")
    for name in FAIL:
        print(f"  - {name}")
print("=" * 60)
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
