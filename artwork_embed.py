# -*- coding: utf-8 -*-
"""Write cover art into audio FILES, so the art survives outside WMP.

Scope, deliberately narrow: LIBRARY tagging only
------------------------------------------------
WMP's own Find Album Info applies metadata to a physical CD through COM, and
that path already writes both tags and artwork to every ripped track - the
report this was written for says so explicitly. Overwriting those files here
would be redundant at best and destructive at worst, because the CD flow writes
into files it is simultaneously creating.

So this is used ONLY for a LIBRARY operation - the user picking an album that
is already in their library and choosing "Update album info". There, WMP writes
the text tags through WriteNamesEx but the cover never lands in the file, so it
lives only in WMP's library database and disappears with it.

There is no COM route
---------------------
Checked, not assumed. IWMMetadataWriter (the interface that would do this)
returns E_NOINTERFACE from the editor object WMCreateEditor hands out, and is
not even declared in the Windows Media Format SDK 11 headers - SDK 11 replaced
it with IWMMetadataEditor, whose entire surface is Open/Close/Flush with no
method that writes a value at all. WM/Picture also only covers ASF/WMA; WMP rips
to MP3 by default, which is ID3v2 APIC, for which Windows ships no COM API.
The tags are therefore written directly here, in pure Python.

Why matching files is reliable
------------------------------
WMP never sends a file path. A library dialog carries ?wmid= (the collection
GUID) and per-track WMContentIDs, and nothing else - so the files have to be
found here. That is not guesswork, because of the ordering: WMP applies the text
tags BEFORE the dialog reports success, so by the time this runs the files
already carry the album title and artist we just applied. Reading the tags back
is therefore matching on a value we ourselves chose moments earlier.
"""
import os
import struct

#: Extensions this module will touch. Anything else is left alone: a wrong
#: rewrite of an unfamiliar container is how a tagging bug eats a music library.
SUPPORTED = (".mp3", ".flac", ".m4a", ".mp4", ".wma", ".asf")


def is_supported(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED


# ==========================================================
# MP3 - ID3v2 APIC
# ==========================================================
# A minimal ID3v2 reader is all that is needed: do not duplicate an existing
# APIC frame, and find the frame size so the tag can be rebuilt. Frames are
# stored sequentially with 32-bit big-endian sizes; an optional extended header
# follows the 10-byte ID3 header when the extended-header flag is set.
_ID3_V2 = 3
_ID3_APIC = b"APIC"


def _id3_existing_picture(tag):
    """True if an APIC frame is already present in an ID3v2 tag blob."""
    pos = 10
    size = len(tag)
    if tag[:3] != b"ID3" or size < 10:
        return False
    if tag[5] & 0x40:                      # extended header
        if pos + 4 > size:
            return False
        pos += 4 + struct.unpack(">I", tag[pos:pos + 4])[0]
    while pos + 10 <= size:
        frame_id = tag[pos:pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        frame_size = struct.unpack(">I", tag[pos + 4:pos + 8])[0]
        frame_flags = struct.unpack(">H", tag[pos + 8:pos + 10])[0]
        if frame_id == _ID3_APIC:
            return True
        # A frame flagged data-length or unsynchronised is not something to
        # reinterpret. Bail rather than compute a bogus offset: a wrong offset
        # here rebuilds a corrupt tag over the user's music.
        if frame_flags & 0x0003:
            return False
        pos += 10 + frame_size
    return False
def _id3_frames(tag):
    """Return [(frame_id, whole_frame_bytes)] for every frame in an ID3v2 tag.

    Returns None if the layout cannot be walked confidently. A frame flagged
    unsynchronised or carrying an explicit data length stores its body
    differently, and misreading that would corrupt the tag - so the caller skips
    the file instead of guessing.
    """
    pos = 10
    size = len(tag)
    if size < 10:
        return []
    if tag[5] & 0x40:                       # extended header
        if pos + 4 > size:
            return None
        pos += 4 + struct.unpack(">I", tag[pos:pos + 4])[0]
    frames = []
    while pos + 10 <= size:
        frame_id = tag[pos:pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        frame_size = struct.unpack(">I", tag[pos + 4:pos + 8])[0]
        frame_flags = struct.unpack(">H", tag[pos + 8:pos + 10])[0]
        if frame_flags & 0x0003:            # data length / unsynchronised
            return None
        end = pos + 10 + frame_size
        if end > size:
            return None
        frames.append(tag[pos:end])
        pos = end
    return frames


def _id3_rebuild(existing_tag, apic_frame):
    """Return a new ID3v2.3 tag: every existing frame, then ``apic_frame``.

    None means the existing tag could not be parsed safely, and the caller must
    leave the file untouched.
    """
    frames = _id3_frames(existing_tag)
    if frames is None:
        return None
    return _id3_build(b"".join(frames) + apic_frame)


def _apic_frame(image, mime="image/jpeg", description=""):
    """Build one ID3v2.3 APIC frame (album art).

    ID3v2.3.0 defines the frame body, in this order::

        text encoding $xx           1 = UTF-16 with BOM, matching the
                                    description below
        MIME type <text string> $00
        picture type $xx            3 = front cover
        description <text string> $00 ($00 $00 for UTF-16)
        picture data <binary data>

    This used to be wrong in a way no strict reader could survive: the frame
    opened with 0x03 where the TEXT ENCODING goes (0x03 is UTF-8, which does
    not exist in a v2.3 tag at all), the picture-type byte landed in the
    description's encoding slot as 0x00 - so the picture type a reader saw
    was 0 "Other" - and the UTF-16 description disagreed with the byte that
    declared its encoding, leaving an extra NUL in front of the picture data.
    A reader either dropped the frame outright or started the image one byte
    late; either way it could not decode a cover, which is how a library ends
    up with black placeholders where the album art should be.
    """
    body = (b"\x01"                                 # encoding 1: UTF-16 with BOM
            + mime.encode("ascii") + b"\x00"
            + b"\x03"                               # picture type 3 = front cover
            + description.encode("utf-16") + b"\x00\x00"   # BOM + text + NUL NUL
            + image)
    return _ID3_APIC + struct.pack(">I", len(body)) + b"\x00\x00" + body


def _id3_picture_ok(frame_body):
    """True if ``frame_body`` is an APIC body a v2.3 reader can actually decode.

    Presence is not enough. This is what decides whether an existing cover is
    kept or replaced: a file already stamped by the old, malformed writer must
    have that frame REPLACED - treating it as "already has art" is what would
    keep the black placeholder forever.
    """
    if not frame_body or frame_body[0] > 0x02:
        # v2.3 defines text encodings 0..2 only. 3 (UTF-8) is a v2.4 value,
        # and a tag declaring it is one a strict reader refuses.
        return False
    try:
        mime_end = frame_body.index(b"\x00", 1)      # end of the MIME type
    except ValueError:
        return False
    if mime_end <= 1:
        return False                                # empty MIME type
    rest = frame_body[mime_end + 2:]                # past MIME NUL + type byte
    if not rest:
        return False
    if frame_body[0] in (1, 2):                     # UTF-16: pairs, ended NUL NUL
        cut = -1
        for i in range(0, len(rest) - 1, 2):
            if rest[i] == 0 and rest[i + 1] == 0:
                cut = i
                break
        if cut < 0:
            return False
        picture = rest[cut + 2:]
    else:                                           # Latin-1/UTF-8: single NUL
        cut = rest.find(b"\x00")
        if cut < 0:
            return False
        picture = rest[cut + 1:]
    return bool(picture)


def _id3_build(body):
    """Wrap frame bodies in a fresh ID3v2.3 tag with padding."""
    padding = b"\x00" * 32
    # Synchsafe integer: 4 x 7 bits, not a plain 32-bit big-endian value.
    size = len(body) + len(padding)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F])
    return (b"ID3" + bytes([_ID3_V2, 0, 0]) + synchsafe + body + padding)


def _mp3_apply(path, tags, image, mime="image/jpeg"):
    """Write ``tags`` and/or ``image`` into an MP3. Returns (tags, art) changed.

    One read and one write for both, because these are written together: the
    beacon that calls this has just applied the album to a library album, and
    a file should be rewritten once or not at all.
    """
    with open(path, "rb") as handle:
        original = handle.read()

    audio, frames = original, []
    if original[:3] == b"ID3":
        raw = original[6:10]
        tag_size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
                    | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
        end = min(10 + tag_size, len(original))
        existing, audio = original[:end], original[end:]
        # Rebuild the tag from the frames that were already there rather than
        # replacing it wholesale, which is what silently deleted the album,
        # artist and title WMP had just written.
        frames = _id3_frames(existing)
        if frames is None:
            return (False, False)       # unparseable tag; do not touch the file

    tags_changed = False
    if tags:
        frames, tags_changed = _id3_frames_with_tags(frames, tags)

    art_changed = False
    if image is not None:
        kept = []
        has_readable_picture = False
        for frame in frames:
            if frame[:4] == _ID3_APIC:
                if _id3_picture_ok(frame[10:]):
                    # A cover a reader can decode is already present; keep it
                    # rather than stack a second picture on the file.
                    has_readable_picture = True
                    kept.append(frame)
                    continue
                # An UNREADABLE picture - the malformed frame this project
                # used to write - is dropped so it can be replaced. Presence
                # alone is not enough: keeping unreadable frames is how a
                # library stayed full of black placeholders.
                continue
            kept.append(frame)
        if not has_readable_picture:
            kept.append(_apic_frame(image, mime))
            art_changed = True
        frames = kept

    if not tags_changed and not art_changed:
        return (False, False)
    with open(path, "wb") as handle:
        handle.write(_id3_build(b"".join(frames)) + audio)
    return (tags_changed, art_changed)


def embed_mp3(path, image, mime="image/jpeg"):
    """Write ``image`` into an MP3's ID3v2 tag. True if the file changed."""
    return _mp3_apply(path, {}, image, mime)[1]


# ==========================================================
# FLAC - METADATA_BLOCK_PICTURE
# ==========================================================
# A standalone PICTURE metadata block (type 6) carrying the raw image bytes.
# Payload layout: type(4) | mime_len(4) | mime | desc_len(4) | desc |
# width(4) | height(4) | depth(4) | colours(4) | data_len(4) | data - all
# big-endian, picture data NEVER base64.
#
# Three separate things were wrong here before, and each on its own was enough
# to make the cover invisible to every reader:
#
#   * the block was appended at the END OF THE FILE - after the audio. A
#     reader only walks the metadata chain that sits BEFORE the first audio
#     frame, so the picture was never seen at all;
#   * the header packed the type into the low bits and the length across the
#     high ones, the exact reverse of the spec (bit31 LAST, bits30-24 TYPE,
#     bits23-0 LENGTH), and the type was 4 - which is VORBIS_COMMENT, not
#     PICTURE (6). The chain past that point was unparseable;
#   * the payload was base64. Base64 belongs to the metadata_block_picture
#     ENTRY inside a VORBIS_COMMENT block, which this code does not write.
#
# Art larger than a PICTURE block's 24-bit length field can carry is skipped
# rather than written in a form that might not round-trip.
_MAX_PICTURE_BYTES = 8 * 1024 * 1024

#: Metadata block type for FLAC PICTURE. 4 is VORBIS_COMMENT - exactly what
#: read_flac_tags looks for, and the value this constant used to hold.
_FLAC_PICTURE_BLOCK = 6


def _flac_blocks(data):
    """[(type, body)] for a FLAC's metadata chain, or None if unwalkable.

    None is the answer that matters: it means "not a FLAC stream" or "this
    chain cannot be walked with confidence", and every caller must then leave
    the file alone rather than insert something at an offset that is really
    audio.
    """
    if data[:4] != b"fLaC":
        return None
    blocks = []
    pos = 4
    for _ in range(256):
        if pos + 4 > len(data):
            return None
        header = struct.unpack(">I", data[pos:pos + 4])[0]
        block_type = (header >> 24) & 0x7F
        if block_type > 6:
            # Only types 0-6 are defined; anything else means the walk has
            # drifted into garbage (audio, or a block an older broken writer
            # left behind) and the true metadata end is not known.
            return None
        end = pos + 4 + (header & 0x00FFFFFF)
        if end > len(data):
            return None
        blocks.append((block_type, data[pos + 4:end]))
        if header & 0x80000000:
            return blocks
        pos = end
    return None


def _flac_metadata_end(data):
    """Offset just past the last metadata block, or None.

    None means "not a FLAC stream" or "the block chain cannot be walked
    confidently". The caller must then leave the file alone rather than
    insert a picture block at an offset that is really audio - which is the
    mistake that used to strand it after the music where nobody looks.
    """
    blocks = _flac_blocks(data)
    if blocks is None:
        return None
    return 4 + sum(4 + len(body) for _block_type, body in blocks)


def flac_has_picture(data):
    """True if the FLAC stream already carries a PICTURE metadata block."""
    if data[:4] != b"fLaC":
        return False
    pos = 4
    for _ in range(256):
        if pos + 4 > len(data):
            break
        header = struct.unpack(">I", data[pos:pos + 4])[0]
        size = header & 0x00FFFFFF
        if size == 0:
            # Real FLAC files carry zero-length PADDING blocks, so a walk that
            # does not survive one either stalls or never finds the picture.
            pos += 4
            continue
        if pos + 4 + size > len(data):
            return False               # truncated: do not claim anything
        # The type lives in bits 30-24. The old test looked at the low 7
        # bits, which are the tail of the LENGTH - noise, never the type.
        if ((header >> 24) & 0x7F) == _FLAC_PICTURE_BLOCK:
            return True
        pos += 4 + size
        if header & 0x80000000:
            break                      # end of the metadata chain
    return False


def _flac_clear_last(data):
    """Return the stream with the final block's "last" bit cleared.

    A PICTURE block is APPENDED, so the block it follows must stop claiming to
    be the last one. Leaving both set gives the stream two "last" blocks, and
    readers that stop at the first one never see the picture at all.
    """
    pos = 4
    for _ in range(64):
        if pos + 4 > len(data):
            break
        header = struct.unpack(">I", data[pos:pos + 4])[0]
        size = header & 0x00FFFFFF
        if header & 0x80000000:
            return (data[:pos] + struct.pack(">I", header & 0x7FFFFFFF)
                    + data[pos + 4:])
        pos += 4 + size
    return data


def _flac_picture_payload(image, mime="image/jpeg", width=0, height=0,
                          depth=24):
    """The PICTURE block payload: raw picture data, never base64."""
    return (struct.pack(">I", 3)                  # front cover
            + struct.pack(">I", len(mime)) + mime.encode("ascii")
            + struct.pack(">I", 0)                  # empty description
            + struct.pack(">IIII", width, height, depth, 0)
            + struct.pack(">I", len(image)) + image)


def _flac_apply(path, tags, image, mime="image/jpeg", width=0, height=0,
                depth=24):
    """Write ``tags`` and/or ``image`` into a FLAC. Returns (tags, art) changed.

    The metadata chain is rebuilt in place and the audio that follows it is
    copied through byte for byte - the picture goes at the END OF THE METADATA,
    after the last block and before the first audio frame. Appending at EOF,
    which is what this used to do, strands it where no reader looks.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"fLaC" or (image is not None
                               and len(image) > _MAX_PICTURE_BYTES):
        return (False, False)
    blocks = _flac_blocks(data)
    if blocks is None:
        return (False, False)          # unwalkable chain; do not corrupt it

    tags_changed = False
    if tags:
        vendor, comments = b"", []
        for block_type, body in blocks:
            if block_type == 4:        # VORBIS_COMMENT
                vendor, comments = _flac_comments(body)
                break
        vendor, comments, tags_changed = _flac_comments_with_tags(
            vendor, comments, tags)
        if tags_changed:
            body = _flac_comments_body(vendor, comments)
            for index, (block_type, _old) in enumerate(blocks):
                if block_type == 4:
                    blocks[index] = (4, body)
                    break
            else:
                # No comment block yet: add one directly after STREAMINFO,
                # which is where every reader expects to find it.
                blocks.insert(1 if blocks else 0, (4, body))

    art_changed = False
    if image is not None and not any(t == _FLAC_PICTURE_BLOCK
                                     for t, _body in blocks):
        # Checked here, not before, so a second call cannot stack another copy
        # of the image into every file.
        blocks.append((_FLAC_PICTURE_BLOCK,
                       _flac_picture_payload(image, mime, width, height, depth)))
        art_changed = True

    if not tags_changed and not art_changed:
        return (False, False)
    head = b"".join(
        # Header per spec: bit31 LAST (the final block ends the chain),
        # bits30-24 TYPE, bits23-0 LENGTH.
        struct.pack(">I", (0x80000000 if index == len(blocks) - 1 else 0)
                    | (block_type << 24) | len(body)) + body
        for index, (block_type, body) in enumerate(blocks))
    with open(path, "wb") as handle:
        # data[:4] is the "fLaC" magic: the chain itself starts at offset 4.
        handle.write(data[:4] + head + data[_flac_metadata_end(data):])
    return (tags_changed, art_changed)


def embed_flac(path, image, mime="image/jpeg", width=0, height=0, depth=24):
    """Append a PICTURE block to the file's metadata. True if it changed."""
    return _flac_apply(path, {}, image, mime, width, height, depth)[1]


# ==========================================================
# Reading tags back
# ==========================================================
def read_mp3_tags(path):
    """Return {'album': str, 'artist': str, 'title': str} from an MP3's ID3v2.

    Reads only the three text fields this module matches on. Returns empty
    strings when the file has no usable tag rather than raising: a library
    folder will contain files this cannot parse, and that must not stop the
    scan.
    """
    out = {"album": "", "artist": "", "title": ""}
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return out
    if data[:3] != b"ID3":
        return out
    raw = data[6:10]
    tag_size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
                | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
    pos = 10
    end = min(10 + tag_size, len(data))
    if data[5] & 0x40 and pos + 4 <= end:
        pos += 4 + struct.unpack(">I", data[pos:pos + 4])[0]
    keys = {b"TALB": "album", b"TPE1": "artist", b"TIT2": "title"}
    while pos + 10 <= end:
        frame_id = data[pos:pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        frame_size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 10:pos + 10 + frame_size]
        target = keys.get(frame_id)
        if target and not out[target]:
            out[target] = _decode_id3_text(body)
        pos += 10 + frame_size
    return out


def _decode_id3_text(body):
    """Decode one ID3 text frame, honouring the encoding byte."""
    if not body:
        return ""
    encoding = body[0]
    payload = body[1:].split(b"\x00")[0]
    try:
        if encoding == 1:                       # UTF-16 with BOM
            return payload.decode("utf-16", "replace").strip("\x00").strip()
        if encoding == 2:                       # UTF-16BE
            return payload.decode("utf-16-be", "replace").strip("\x00").strip()
        if encoding == 3:                       # UTF-8
            return payload.decode("utf-8", "replace").strip("\x00").strip()
    except (UnicodeDecodeError, LookupError):
        return ""
    # Latin-1 is the ID3v2.3 default; errors="replace" keeps a stray byte from
    # aborting the whole scan.
    return payload.decode("latin-1", "replace").strip("\x00").strip()


def read_flac_tags(path):
    """Return album/artist/title from a FLAC's VORBIS_COMMENT block."""
    out = {"album": "", "artist": "", "title": ""}
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return out
    if data[:4] != b"fLaC":
        return out
    pos = 4
    want = {"ALBUM": "album", "ARTIST": "artist", "TITLE": "title"}
    for _ in range(64):                       # bounded: a malformed file can
        if pos + 4 > len(data):               # otherwise loop on a zero length
            break
        header = struct.unpack(">I", data[pos:pos + 4])[0]
        last = bool(header & 0x80000000)
        # The type is bits 30-24. Testing the low 7 bits - the tail of the
        # LENGTH - meant a real VORBIS_COMMENT block was found only when its
        # length happened to end in 4, so FLAC albums never matched their tags
        # here and the cover embed silently skipped them.
        block_type = (header >> 24) & 0x7F
        size = header & 0x00FFFFFF
        body = data[pos + 4:pos + 4 + size]
        if block_type == 4 and len(body) >= 8:         # VORBIS_COMMENT
            try:
                # Layout: vendor_len, vendor string, user_comment_count, then
                # one length-prefixed string per comment. The count is NOT a
                # comment length - reading the block as a flat sequence of
                # [len][bytes] pairs (which is what this did) desynchronised
                # on the very first real file, so no FLAC's tags were ever
                # matched here and its album silently never got a cover.
                vendor_len = struct.unpack("<I", body[0:4])[0]
                pos2 = 4 + vendor_len
                count = struct.unpack("<I", body[pos2:pos2 + 4])[0]
                pos2 += 4
                for _ in range(min(int(count), 1024)):   # bounded: a corrupt
                    if pos2 + 4 > len(body):             # count must not spin
                        break
                    length = struct.unpack("<I", body[pos2:pos2 + 4])[0]
                    pos2 += 4
                    if pos2 + length > len(body):
                        break
                    item = body[pos2:pos2 + length].decode("utf-8", "replace")
                    pos2 += length
                    if "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    target = want.get(key.upper())
                    if target and not out[target]:
                        out[target] = value
            except (struct.error, ValueError):
                pass
        pos += 4 + size
        if last:
            break
    return out


# ==========================================================
# Dispatch
# ==========================================================
def read_tags(path):
    """Read album/artist/title from any supported file, whatever its format.

    Only the containers whose tags this module can both read and write are
    handled. WMA is deliberately read as nothing rather than guessed at: an ASF
    Extended Content Description parser is a lot of surface for no gain here,
    and returning empty tags makes the file simply not match.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return read_mp3_tags(path)
    if ext == ".flac":
        return read_flac_tags(path)
    return {"album": "", "artist": "", "title": ""}


# ==========================================================
# WRITING THE TEXT TAGS
# ==========================================================
# The values WMP applied to its own library database, written into the files
# as well. Every mapping below is a REPLACE, never a delete: a field the
# caller's document does not mention is left exactly as it was found, because
# this module is handed a document and must not decide what else a file says.
#
# Writing is IDEMPOTENT by construction - a value that already matches is not
# rewritten. That matters more than it sounds: WMP applies these same tags
# through WriteNamesEx before this runs, so where that worked the files already
# agree and this must be a no-op that touches nothing.
_ID3_FIELDS = {
    "album": b"TALB", "artist": b"TPE1", "title": b"TIT2",
    "track": b"TRCK", "disc": b"TPOS", "genre": b"TCON", "year": b"TYER",
}
_ID3_FIELD_BY_FRAME = dict((frame, key) for key, frame in _ID3_FIELDS.items())

_FLAC_FIELDS = {
    "album": "ALBUM", "artist": "ARTIST", "title": "TITLE",
    "track": "TRACKNUMBER", "disc": "DISCNUMBER", "genre": "GENRE",
    "year": "DATE",
}


def _clean_tags(tags):
    """Drop empty/absent fields, so "not in the document" never means "erase"."""
    out = {}
    for key, value in (tags or {}).items():
        if key not in _ID3_FIELDS:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def _id3_text_frame(frame_id, text):
    """One ID3v2.3 text frame: encoding byte, text, NUL.

    Latin-1 first, because that is v2.3's default encoding and what WMP itself
    writes. UTF-16 (with its BOM) only when the value cannot be represented as
    Latin-1 - a name with an accent in it must survive, and the encoding byte
    is what tells a reader how to read the rest of the frame.
    """
    try:
        payload = b"\x00" + text.encode("latin-1") + b"\x00"
    except UnicodeEncodeError:
        payload = b"\x01" + text.encode("utf-16") + b"\x00\x00"
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _id3_frames_with_tags(frames, tags):
    """Return (frames, changed) with the managed text frames set to ``tags``.

    Frames this module does not manage - the picture above all - are copied
    through untouched.
    """
    changed = False
    out = []
    seen = set()
    for frame in frames:
        key = _ID3_FIELD_BY_FRAME.get(frame[:4])
        value = tags.get(key) if key else None
        if value is None:
            out.append(frame)          # not in this document: leave it alone
            continue
        seen.add(key)
        if _decode_id3_text(frame[10:]) == value:
            out.append(frame)          # already correct; do not rewrite
            continue
        out.append(_id3_text_frame(frame[:4], value))
        changed = True
    for key, frame_id in _ID3_FIELDS.items():
        if key in seen or key not in tags:
            continue
        out.append(_id3_text_frame(frame_id, tags[key]))
        changed = True
    return out, changed


def _flac_comments(body):
    """(vendor_bytes, [(key, value)]) from a VORBIS_COMMENT block body."""
    vendor, comments = b"", []
    try:
        vendor_len = struct.unpack("<I", body[0:4])[0]
        vendor = body[4:4 + vendor_len]
        pos = 4 + vendor_len
        count = struct.unpack("<I", body[pos:pos + 4])[0]
        pos += 4
        for _ in range(min(int(count), 4096)):     # bounded: a corrupt count
            if pos + 4 > len(body):                # must not spin or overrun
                break
            length = struct.unpack("<I", body[pos:pos + 4])[0]
            pos += 4
            if pos + length > len(body):
                break
            item = body[pos:pos + length].decode("utf-8", "replace")
            pos += length
            key, _, value = item.partition("=")
            comments.append((key.upper(), value))
    except (struct.error, ValueError, IndexError):
        pass
    return vendor, comments


def _flac_comments_body(vendor, comments):
    """Rebuild a VORBIS_COMMENT body, keeping the file's own vendor string."""
    body = struct.pack("<I", len(vendor)) + vendor
    body += struct.pack("<I", len(comments))
    for key, value in comments:
        item = ("%s=%s" % (key, value)).encode("utf-8", "replace")
        body += struct.pack("<I", len(item)) + item
    return body


def _flac_comments_with_tags(vendor, comments, tags):
    """Return (vendor, comments, changed) with the managed keys set.

    Keys this module does not manage - including ``metadata_block_picture``,
    which is how some taggers store artwork - are carried through untouched.
    """
    changed = False
    out = list(comments)
    for key, flac_key in _FLAC_FIELDS.items():
        if key not in tags:
            continue
        text = tags[key]
        for index, (existing_key, existing_value) in enumerate(out):
            if existing_key != flac_key:
                continue
            if existing_value == text:
                break                   # already correct
            out[index] = (existing_key, text)
            changed = True
            break
        else:
            out.append((flac_key, text))
            changed = True
    return vendor, out, changed


def sniff_mime(image):
    """The MIME type that matches ``image``'s actual bytes, or None.

    The declared type and the data have to agree: a PNG stamped
    "image/jpeg" is decoded as JPEG by any reader that trusts the tag, and a
    cover that cannot be decoded is a placeholder. Magic bytes do not lie,
    so they win over whatever the caller guessed.
    """
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image[:2] == b"BM":
        return "image/bmp"
    return None


def write_tags_and_art(path, tags=None, image=None, mime="image/jpeg"):
    """Write ``tags`` and/or ``image`` into ``path``.

    Returns ``(tags_written, art_written)``. Either half is skipped when the
    file already says what it is being asked to say, so calling this twice -
    which is what happens whenever an album is re-applied - touches nothing
    the second time. A container this module cannot write (WMA, M4A) is left
    alone and both halves report False.
    """
    tags = _clean_tags(tags)
    if image is not None:
        detected = sniff_mime(image)
        if detected:
            mime = detected
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return _mp3_apply(path, tags, image, mime)
    if ext == ".flac":
        return _flac_apply(path, tags, image, mime)
    return (False, False)


def write_tags(path, tags):
    """Write ``tags`` into ``path``. True if the file was changed."""
    return write_tags_and_art(path, tags)[0]


def write_art(path, image, mime="image/jpeg"):
    """Write ``image`` into ``path``. Returns True if the file was changed."""
    return write_tags_and_art(path, None, image, mime)[1]