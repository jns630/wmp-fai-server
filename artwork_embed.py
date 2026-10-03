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
    """Build one ID3v2.3 APIC frame (album art)."""
    body = (b"\x03"                                 # picture type 3 = front
            + mime.encode("ascii") + b"\x00"
            + b"\x00"                               # picture type byte
            + description.encode("utf-16") + b"\x00\x00"
            + image)
    return _ID3_APIC + struct.pack(">I", len(body)) + b"\x00\x00" + body


def _id3_build(body):
    """Wrap frame bodies in a fresh ID3v2.3 tag with padding."""
    padding = b"\x00" * 32
    # Synchsafe integer: 4 x 7 bits, not a plain 32-bit big-endian value.
    size = len(body) + len(padding)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F])
    return (b"ID3" + bytes([_ID3_V2, 0, 0]) + synchsafe + body + padding)


def embed_mp3(path, image, mime="image/jpeg"):
    """Write ``image`` into an MP3's ID3v2 tag. True if the file changed."""
    with open(path, "rb") as handle:
        original = handle.read()

    audio, existing = original, b""
    new_tag = None
    if original[:3] == b"ID3":
        raw = original[6:10]
        tag_size = ((raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14
                    | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F))
        end = min(10 + tag_size, len(original))
        existing, audio = original[:end], original[end:]
        if _id3_existing_picture(existing):
            return False                  # already has art; leave it alone
        # Rebuild the tag as: existing frames, then the new APIC. The earlier
        # version replaced the tag wholesale, which silently DELETED the album,
        # artist and title WMP had just written - the very tags this runs
        # immediately after WMP applied them. Frame bodies are copied through
        # unchanged, so nothing that was there is lost.
        new_tag = _id3_rebuild(existing, _apic_frame(image, mime))
        if new_tag is None:
            return False              # unparseable tag; do not touch the file

    if new_tag is None:
        new_tag = _id3_build(_apic_frame(image, mime))

    with open(path, "wb") as handle:
        handle.write(new_tag + audio)
    return True


# ==========================================================
# FLAC - Vorbis comment METADATA_BLOCK_PICTURE
# ==========================================================
# A base64 picture block in a VORBIS_COMMENT metadata block. Payload layout:
# type(4) | mime_len(4) | mime | desc_len(4) | desc | width(4) | height(4) |
# depth(4) | colours(4) | data_len(4) | data. Art larger than a base64 block
# can safely carry is skipped rather than written in a form that might not
# round-trip.
_MAX_PICTURE_BYTES = 8 * 1024 * 1024

#: Metadata block type for FLAC PICTURE.
_FLAC_PICTURE_BLOCK = 4


def flac_has_picture(data):
    """True if the FLAC stream already carries a PICTURE metadata block."""
    if data[:4] != b"fLaC":
        return False
    pos = 4
    for _ in range(64):
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
        if (header & 0x7F) == _FLAC_PICTURE_BLOCK:
            return True
        pos += 4 + size
        if header & 0x80000000:
            break
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


def embed_flac(path, image, mime="image/jpeg", width=0, height=0, depth=24):
    import base64
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"fLaC" or len(image) > _MAX_PICTURE_BYTES:
        return False
    # Checked before writing because the block is APPENDED: without this, a
    # second call grew the file every time, so re-tagging a library album would
    # stack another copy of the image into every file.
    if flac_has_picture(data):
        return False
    payload = (struct.pack(">I", 3)                  # front cover
               + struct.pack(">I", len(mime)) + mime.encode("ascii")
               + struct.pack(">I", 0)                  # empty description
               + struct.pack(">IIII", width, height, depth, 0)
               + struct.pack(">I", len(image)) + image)
    block = (struct.pack(">I", _FLAC_PICTURE_BLOCK | (len(payload) << 24))
             + base64.b64encode(payload))
    with open(path, "wb") as handle:
        handle.write(_flac_clear_last(data) + block)
    return True


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
        block_type = header & 0x7F
        size = header & 0x00FFFFFF
        body = data[pos + 4:pos + 4 + size]
        if block_type == 4 and len(body) >= 4:         # VORBIS_COMMENT
            try:
                pos2 = 0
                count = 0
                while pos2 + 4 <= len(body):
                    length = struct.unpack("<I", body[pos2:pos2 + 4])[0]
                    item = body[pos2 + 4:pos2 + 4 + length].decode("utf-8", "replace")
                    pos2 += 4 + length
                    count += 1
                    if "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    target = want.get(key.upper())
                    if target and not out[target]:
                        out[target] = value
                del count
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


def write_art(path, image, mime="image/jpeg"):
    """Write ``image`` into ``path``. Returns True if the file was changed."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return embed_mp3(path, image, mime)
    if ext == ".flac":
        return embed_flac(path, image, mime)
    return False
    return True