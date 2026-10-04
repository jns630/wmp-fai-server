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


def apic_present(path):
    """True if the file's ID3 tag holds an APIC frame."""
    with open(path, "rb") as handle:
        blob = handle.read()
    raw = blob[6:10]
    size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
            | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
    return ae._id3_existing_picture(blob[:10 + size])


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

    # The writer's own reader agreeing with the writer proves nothing - both
    # were built from the same layout. Parse the APIC exactly the way
    # ID3v2.3.0 defines it (this is what a player does) and demand the
    # original image back, byte for byte. This is the check that catches a
    # frame no player can decode - the black placeholder in the library.
    def strict_apic(path):
        blob = open(path, "rb").read()
        if blob[:3] != b"ID3":
            return "no ID3 tag"
        raw = blob[6:10]
        tag_size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
                    | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
        pos, end = 10, 10 + tag_size
        while pos + 10 <= end:
            fid = blob[pos:pos + 4]
            if not fid.strip(b"\x00"):
                break
            size = struct.unpack(">I", blob[pos + 4:pos + 8])[0]
            if fid == b"APIC":
                body = blob[pos + 10:pos + 10 + size]
                if body[0] > 0x02:
                    return "text encoding %d is not valid in ID3v2.3" % body[0]
                mime_end = body.index(b"\x00", 1)
                mime = body[1:mime_end]
                picture_type = body[mime_end + 1]
                rest = body[mime_end + 2:]
                if body[0] in (1, 2):
                    cut = -1
                    for i in range(0, len(rest) - 1, 2):
                        if rest[i] == 0 and rest[i + 1] == 0:
                            cut = i
                            break
                    if cut < 0:
                        return "UTF-16 description not terminated with NUL NUL"
                    description = rest[:cut].decode("utf-16", "replace")
                    picture = rest[cut + 2:]
                else:
                    cut = rest.find(b"\x00")
                    if cut < 0:
                        return "description not NUL terminated"
                    description = rest[:cut].decode("latin-1", "replace")
                    picture = rest[cut + 1:]
                return mime, picture_type, description, picture
            pos += 10 + size
        return "no APIC frame"

    parsed = strict_apic(mp3)
    check("mp3: a spec reader parses the APIC frame",
          isinstance(parsed, tuple), parsed)
    if isinstance(parsed, tuple):
        mime, picture_type, description, picture = parsed
        check("mp3: picture type is 3 (front cover)",
              picture_type == 3, picture_type)
        check("mp3: declared MIME matches the image bytes",
              mime == b"image/png", mime)
        check("mp3: description decodes cleanly",
              description == "", repr(description))
        check("mp3: picture data is byte-identical to the image",
              picture == PNG, "len %d vs %d" % (len(picture), len(PNG)))

    # A file the OLD writer already stamped must be REPAIRED, not preserved:
    # "the tag contains an APIC" used to be enough to skip the file, which is
    # how a library kept its black placeholders forever. The malformed shape
    # is reproduced here byte for byte: encoding 0x03 (not valid in v2.3),
    # picture type 0x00, and a UTF-16 description under a UTF-8 encoding byte.
    legacy = os.path.join(tmp, "legacy.mp3")
    legacy_body = b"\x03image/jpeg\x00\x00\xff\xfe\x00\x00" + PNG
    with open(legacy, "wb") as handle:
        handle.write(ae._id3_build(
            b"APIC" + struct.pack(">I", len(legacy_body)) + b"\x00\x00"
            + legacy_body)
            + b"\xff\xfb\x90\x00" + b"\x00" * 413)
    check("mp3: the old malformed frame is detected as unreadable",
          ae._id3_picture_ok(legacy_body) is False)
    check("mp3: the unreadable frame is REPLACED, not kept",
          ae.write_art(legacy, PNG) is True)
    repaired = strict_apic(legacy)
    check("mp3: the repaired file parses strict and carries the image",
          isinstance(repaired, tuple) and repaired[0] == b"image/png"
          and repaired[1] == 3 and repaired[3] == PNG, repaired)

    # ...and a cover another tool wrote CORRECTLY must not be touched.
    good = os.path.join(tmp, "good.mp3")
    good_body = b"\x00image/png\x00\x03\x00" + PNG   # enc 0, front cover, no desc
    with open(good, "wb") as handle:
        handle.write(ae._id3_build(
            b"APIC" + struct.pack(">I", len(good_body)) + b"\x00\x00"
            + good_body)
            + b"\xff\xfb\x90\x00" + b"\x00" * 413)
    good_size = os.path.getsize(good)
    check("mp3: a readable foreign cover is kept untouched",
          ae.write_art(good, PNG) is False
          and os.path.getsize(good) == good_size)

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

    # The picture must live INSIDE the metadata chain - after the last
    # existing block, before the first audio frame. The old writer appended
    # at end-of-file, i.e. AFTER the audio, where no reader ever looks, and
    # with a header no reader could parse. Both are pinned here against a
    # file that actually has audio after its metadata.
    AUDIO = b"\xff\xf8" + b"\x12\x34" + b"\x00" * 60
    streaminfo = b"\x00" * 34
    flac2 = os.path.join(tmp, "with_audio.flac")
    with open(flac2, "wb") as handle:
        handle.write(b"fLaC"
                     + flac_block_header(0, len(streaminfo), last=True)
                     + streaminfo + AUDIO)
    check("flac: write into a file WITH audio reports a change",
          ae.write_art(flac2, PNG) is True)
    audio_size = os.path.getsize(flac2)

    def strict_flac(path):
        """Walk the chain the way the spec lays it out.

        Returns (picture_type, mime, picture_bytes, remainder_after_block)
        for the first PICTURE block, or an error string.
        """
        data = open(path, "rb").read()
        if data[:4] != b"fLaC":
            return "no fLaC magic"
        pos, saw_last, types = 4, False, []
        while not saw_last:
            if pos + 4 > len(data):
                return "metadata chain is not terminated (no LAST block)"
            header = struct.unpack(">I", data[pos:pos + 4])[0]
            size = header & 0x00FFFFFF
            if pos + 4 + size > len(data):
                return "block runs past the end of the file"
            block_type = (header >> 24) & 0x7F
            body = data[pos + 4:pos + 4 + size]
            types.append(block_type)
            if block_type == 6:
                # payload: type(4) | mime_len(4) | mime | desc_len(4) |
                # desc | w/h/depth/colours (16) | data_len(4) | data
                ptype = struct.unpack(">I", body[0:4])[0]
                offset = 4
                mlen = struct.unpack(">I", body[offset:offset + 4])[0]
                offset += 4
                mime = body[offset:offset + mlen]
                offset += mlen
                dlen = struct.unpack(">I", body[offset:offset + 4])[0]
                # description, then width/height/depth/colours: 4 x 4 bytes
                offset += 4 + dlen + 16
                ilen = struct.unpack(">I", body[offset:offset + 4])[0]
                offset += 4
                return (ptype, mime, body[offset:offset + ilen],
                        data[pos + 4 + size:])
            saw_last = bool(header & 0x80000000)
            pos += 4 + size
        return "no PICTURE block (types seen: %r)" % types

    parsed = strict_flac(flac2)
    check("flac: a spec reader finds the PICTURE block",
          isinstance(parsed, tuple), parsed)
    if isinstance(parsed, tuple):
        ptype, mime, picture, remainder = parsed
        check("flac: PICTURE is front cover with matching MIME",
              ptype == 3 and mime == b"image/png", (ptype, mime))
        check("flac: picture data is byte-identical to the image",
              picture == PNG, "len %d vs %d" % (len(picture), len(PNG)))
        check("flac: the audio still follows the metadata untouched",
              remainder == AUDIO,
              "len %d vs %d" % (len(remainder), len(AUDIO)))
    check("flac: writing again over a file WITH audio is a no-op",
          ae.write_art(flac2, PNG) is False
          and os.path.getsize(flac2) == audio_size)

    # read_flac_tags must find VORBIS_COMMENT by its spec bit field. Its type
    # check used to read the low 7 bits of the LENGTH, so a real FLAC matched
    # only when its comment block length happened to end in 4 - and a FLAC
    # album that does not match is silently never found for embedding.
    tagged = os.path.join(tmp, "tagged.flac")
    entries = [b"ALBUM=Indoor Weather", b"ARTIST=Sable Coast",
               b"TITLE=Grey Light"]
    vc_body = (struct.pack("<I", 0)                  # empty vendor string
               + struct.pack("<I", len(entries))
               + b"".join(struct.pack("<I", len(e)) + e for e in entries))
    with open(tagged, "wb") as handle:
        handle.write(b"fLaC"
                     + flac_block_header(0, len(streaminfo), last=False)
                     + streaminfo
                     + flac_block_header(4, len(vc_body), last=True)
                     + vc_body)
    check("flac: read_tags finds the VORBIS_COMMENT block",
          ae.read_tags(tagged) == {"album": "Indoor Weather",
                                   "artist": "Sable Coast",
                                   "title": "Grey Light"},
          ae.read_tags(tagged))

    # --- the TEXT TAGS, into the same file in the same pass ---
    tagged_flac2 = os.path.join(tmp, "tags.flac")
    with open(tagged_flac2, "wb") as handle:
        handle.write(b"fLaC" + flac_block_header(0, len(streaminfo), last=True)
                     + streaminfo + AUDIO)
    check("flac: write_tags reports a change",
          ae.write_tags(tagged_flac2, {"album": "Homogenic", "artist": "Björk",
                                       "title": "Jóga", "track": "3",
                                       "genre": "Art Pop", "year": "1997"})
          is True)
    check("flac: the tags are readable back",
          ae.read_tags(tagged_flac2) == {"album": "Homogenic",
                                         "artist": "Björk",
                                         "title": "Jóga"},
          ae.read_tags(tagged_flac2))
    vendor, comments = ae._flac_comments(
        [b for t, b in ae._flac_blocks(open(tagged_flac2, "rb").read())
         if t == 4][0])
    check("flac: genre, year and track number are written too",
          ("GENRE", "Art Pop") in comments
          and ("DATE", "1997") in comments
          and ("TRACKNUMBER", "3") in comments, comments)
    check("flac: audio after the metadata is untouched by a tag write",
          open(tagged_flac2, "rb").read()[-len(AUDIO):] == AUDIO)
    size_after_tags = os.path.getsize(tagged_flac2)
    check("flac: writing the same tags again is a no-op",
          ae.write_tags(tagged_flac2, {"album": "Homogenic", "artist": "Björk",
                                       "title": "Jóga", "track": "3",
                                       "genre": "Art Pop", "year": "1997"})
          is False and os.path.getsize(tagged_flac2) == size_after_tags)

    # A comment this module does not manage must survive - including the
    # metadata_block_picture entry some taggers use to store artwork.
    other = os.path.join(tmp, "other_comments.flac")
    entries = [b"ALBUM=Old Record", b"ARTIST=Old Artist",
               b"REPLAYGAIN_TRACK_GAIN=-3.21 dB",
               b"METADATA_BLOCK_PICTURE=" + b"aGVsbG8="]
    vc_body = (struct.pack("<I", 0)
               + struct.pack("<I", len(entries))
               + b"".join(struct.pack("<I", len(e)) + e for e in entries))
    with open(other, "wb") as handle:
        handle.write(b"fLaC"
                     + flac_block_header(0, len(streaminfo), last=False)
                     + streaminfo
                     + flac_block_header(4, len(vc_body), last=True)
                     + vc_body + AUDIO)
    ae.write_tags(other, {"album": "New Record", "artist": "New Artist"})
    _v, kept = ae._flac_comments(
        [b for t, b in ae._flac_blocks(open(other, "rb").read())
         if t == 4][0])
    check("flac: an unmanaged comment (replaygain) is preserved",
          ("REPLAYGAIN_TRACK_GAIN", "-3.21 dB") in kept, kept)
    check("flac: an embedded metadata_block_picture entry is preserved",
          any(k == "METADATA_BLOCK_PICTURE" for k, _v2 in kept), kept)
    check("flac: the managed keys were replaced",
          ae.read_tags(other)["album"] == "New Record"
          and ae.read_tags(other)["artist"] == "New Artist",
          ae.read_tags(other))

    # FLAC tags and picture together, and a tag write keeping the picture.
    both_flac = os.path.join(tmp, "both.flac")
    with open(both_flac, "wb") as handle:
        handle.write(b"fLaC" + flac_block_header(0, len(streaminfo), last=True)
                     + streaminfo + AUDIO)
    written = ae.write_tags_and_art(both_flac, {"album": "Homogenic"}, PNG)
    check("flac: tags and picture are written together",
          written == (True, True)
          and ae.flac_has_picture(open(both_flac, "rb").read()), written)
    check("flac: writing them again is a no-op",
          ae.write_tags_and_art(both_flac, {"album": "Homogenic"}, PNG)
          == (False, False))

    # --- the TEXT TAGS, written into the same file in the same pass ---
    artist = "Björk"
    tagged_mp3 = os.path.join(tmp, "tags.mp3")
    make_mp3(tagged_mp3)
    check("mp3: write_tags reports a change",
          ae.write_tags(tagged_mp3, {"album": "Homogenic", "artist": artist,
                                     "title": "Jóga", "track": "5",
                                     "disc": "1", "genre": "Art Pop",
                                     "year": "1997"}) is True)
    with open(tagged_mp3, "rb") as handle:
        tagged_blob = handle.read()
    tagged_raw = tagged_blob[6:10]
    tagged_size = ((tagged_raw[0] & 0x7F) << 21 | (tagged_raw[1] & 0x7F) << 14
                   | (tagged_raw[2] & 0x7F) << 7 | (tagged_raw[3] & 0x7F))
    tagged_frames = dict((f[:4], ae._decode_id3_text(f[10:]))
                         for f in ae._id3_frames(
                             tagged_blob[:10 + tagged_size]) or [])
    check("mp3: every requested tag is in the file",
          tagged_frames.get(b"TALB") == "Homogenic"
          and tagged_frames.get(b"TPE1") == artist
          and tagged_frames.get(b"TIT2") == "Jóga"
          and tagged_frames.get(b"TRCK") == "5"
          and tagged_frames.get(b"TPOS") == "1"
          and tagged_frames.get(b"TCON") == "Art Pop"
          and tagged_frames.get(b"TYER") == "1997", tagged_frames)
    check("mp3: a non-Latin-1 value is written, not dropped",
          tagged_frames.get(b"TPE1") == artist,
          "the accent must survive the encoding byte")
    check("mp3: audio still intact after a tag write",
          b"\xff\xfb\x90\x00" in tagged_blob, "the audio was overwritten")
    size_after_tags = os.path.getsize(tagged_mp3)
    check("mp3: writing the same tags again is a no-op",
          ae.write_tags(tagged_mp3, {"album": "Homogenic", "artist": artist,
                                     "title": "Jóga", "track": "5",
                                     "disc": "1", "genre": "Art Pop",
                                     "year": "1997"}) is False
          and os.path.getsize(tagged_mp3) == size_after_tags,
          "re-applying an album must not churn the file")

    # Tags and picture in one pass, which is how the server calls it.
    both = os.path.join(tmp, "both.mp3")
    make_mp3(both)
    written = ae.write_tags_and_art(both, {"album": "Homogenic"}, PNG)
    check("mp3: tags and picture are written together",
          written == (True, True) and apic_present(both), written)
    check("mp3: writing them again is a no-op",
          ae.write_tags_and_art(both, {"album": "Homogenic"}, PNG)
          == (False, False),
          "the second apply of the same album must touch nothing")

    # A tag write must not disturb a cover that is already there.
    art_then_tags = os.path.join(tmp, "art_then_tags.mp3")
    make_mp3(art_then_tags)
    ae.write_art(art_then_tags, PNG)
    before_art = os.path.getsize(art_then_tags)
    ae.write_tags(art_then_tags, {"album": "Homogenic", "artist": "Björk"})
    check("mp3: a tag write keeps the existing cover",
          apic_present(art_then_tags)
          and os.path.getsize(art_then_tags) > before_art,
          "the APIC frame must survive a text-tag rebuild")
    check("mp3: an untouched tag is not erased",
          ae.write_tags(art_then_tags, {"album": "Homogenic"})
          is False
          and ae.read_tags(art_then_tags)["artist"] == "Björk",
          "a field the caller does not mention must be left exactly as found")

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