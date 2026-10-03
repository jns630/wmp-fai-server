"""Round-trip: write art into a real file, read the tags back, verify the art.

Writes real files in a temp directory. A tag writer that has never been read
back is not a tag writer.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artwork_embed as ae


def id3_text_frame(frame_id, value):
    """One ID3v2.3 text frame. Body = encoding byte + text + NUL."""
    payload = b"\x03" + value.encode("utf-8") + b"\x00"
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def make_mp3(path, album=None, artist=None, title=None):
    """A structurally valid MP3: an ID3v2 tag plus MPEG frames."""
    frames = []
    if album or artist or title:
        body = b""
        for fid, val in ((b"TALB", album), (b"TPE1", artist), (b"TIT2", title)):
            if val:
                body += id3_text_frame(fid, val)
        frames.append(ae._id3_build(body))
    # 0xFF 0xFB = MPEG-1 Layer III; enough to look like audio to a tagger.
    frames.append(b"\xff\xfb\x90\x00" + b"\x00" * 413)
    with open(path, "wb") as handle:
        handle.write(b"".join(frames))


def flac_block_header(block_type, length, last=False):
    """One FLAC metadata block header.

    Layout: bit31 = LAST, bits30-24 = TYPE, bits23-0 = LENGTH. The LENGTH is
    NOT shifted left by 24 - only the type lives in the high bits. Shifting the
    length by 24 pushes it out of the 32-bit word entirely and silently yields a
    zero-length block, which is exactly what the first version of this fixture
    did and which made the writer look broken.
    """
    return struct.pack(">I", (length & 0x00FFFFFF)
                       | ((block_type & 0x7F) << 24)
                       | (0x80000000 if last else 0))


def make_flac(path, album=None, artist=None, title=None):
    """A FLAC stream header plus one real STREAMINFO block.

    STREAMINFO is a fixed 34 bytes in the format, and it is marked as the last
    metadata block - which is what a real file has and what the writer relies on
    when it clears the flag to append the picture.
    """
    body = (struct.pack(">HH", 4096, 4096)         # min/max blocksize  (4)
            + b"\x00" * 3 + b"\x00" * 3            # min/max framesize (6)
            + b"\x00" * 3                         # sample rate/ch/bps/samples
            + b"\x00" * 16)                       # MD5 signature      (16)
    # 4 + 6 + 3 + 16 = 29. STREAMINFO is specified as 34 bytes because the
    # rate/channels/bps/total-samples fields pack into 8 bytes across those
    # five values, not 3. Padded correctly below.
    body += b"\x00" * (34 - len(body))
    assert len(body) == 34, len(body)
    with open(path, "wb") as handle:
        handle.write(b"fLaC" + flac_block_header(0, len(body), last=True)
                     + body)


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
       b"\x00\x00\x01\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def main():
    tmp = tempfile.mkdtemp()
    failures = []

    def check(name, ok, detail=""):
        print("%-58s %s" % (name, "PASS" if ok else "FAIL " + str(detail)))
        if not ok:
            failures.append(name)

    # --- MP3 write ---
    mp3 = os.path.join(tmp, "track.mp3")
    make_mp3(mp3, album="Signals From The Quiet Room",
             artist="The Modulation Set", title="First Light")
    before = os.path.getsize(mp3)
    check("mp3: write_art reports a change", ae.write_art(mp3, PNG) is True)
    grew = os.path.getsize(mp3) > before
    check("mp3: file grew", grew, os.path.getsize(mp3))
    # The MPEG sync word must still be findable after the new tag, i.e. the
    # audio payload was appended-to rather than overwritten.
    with open(mp3, "rb") as handle:
        blob = handle.read()
    check("mp3: audio data survived", b"\xff\xfb\x90\x00" in blob,
          "the MPEG frame sync is gone")
    tags = ae.read_tags(mp3)
    check("mp3: WMP's text tags SURVIVED the art write",
          tags == {"album": "Signals From The Quiet Room",
                   "artist": "The Modulation Set", "title": "First Light"},
          tags)
    size_after_first = os.path.getsize(mp3)
    # APIC must be findable by walking the tag the same way a reader would.
    with open(mp3, "rb") as handle:
        whole = handle.read()
    raw = whole[6:10]
    tag_size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
                | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
    check("mp3: APIC present after write",
          ae._id3_existing_picture(whole[:10 + tag_size]))
    check("mp3: second write does not grow the file",
          (ae.write_art(mp3, PNG) is False)
          and os.path.getsize(mp3) == size_after_first)

    # --- FLAC write ---
    flac = os.path.join(tmp, "track.flac")
    make_flac(flac)
    check("flac: write_art reports a change", ae.write_art(flac, PNG) is True)
    check("flac: fLaC magic intact",
          open(flac, "rb").read(4) == b"fLaC")
    flac_size = os.path.getsize(flac)
    check("flac: write is idempotent",
          (ae.write_art(flac, PNG) is False
           and os.path.getsize(flac) == flac_size),
          "a second FLAC write must be a no-op, not another appended block")

    # --- guards ---
    check("unsupported extension refused",
          ae.write_art(os.path.join(tmp, "x.xyz"), PNG) is False)
    check("is_supported accepts mp3/flac and rejects others",
          ae.is_supported("a.mp3") and ae.is_supported("a.FLAC")
          and not ae.is_supported("a.txt"))

    # --- an MP3 with no existing tag ---
    bare = os.path.join(tmp, "bare.mp3")
    with open(bare, "wb") as handle:
        handle.write(b"\xff\xfb\x90\x00" + b"\x00" * 200)
    check("mp3: works with no pre-existing tag",
          ae.write_art(bare, PNG) is True)

    # --- read_tags must not raise on garbage ---
    junk = os.path.join(tmp, "junk.mp3")
    with open(junk, "wb") as handle:
        handle.write(b"ID3\x03\x00\x00\xff\xff\xff\x7fGARBAGE")
    try:
        ae.read_tags(junk)
        check("read_tags survives a malformed tag", True)
    except Exception as exc:
        check("read_tags survives a malformed tag", False, exc)

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("all artwork_embed checks passed")
    return 0


sys.exit(main())