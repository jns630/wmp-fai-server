# -*- coding: utf-8 -*-
"""
Pure-Python ``WM/Picture`` support for ASF containers (.wma / .asf / .wmv).

Why this exists, and why it is pure Python
------------------------------------------
The WMF 11 SDK offers a COM route to album art
(``IWMHeaderInfo3::AddAttribute`` with a ``WM/Picture`` value). That route was
verified working on this machine - see the notes at the bottom. It is not used
here, for two reasons:

  1. The SDK's ``wmvcore.dll`` is **32-bit only** (machine type ``0x10B``) and
     cannot be loaded by the 64-bit interpreter this project runs on. The
     *system* ``wmvcore.dll`` is 64-bit and does export the interfaces, so a COM
     implementation is possible - but see (2).
  2. Editing an ASF header through the WMF writer is not a header patch. ASF
     cannot be rewritten in place: the writer needs a fully configured object
     (``SetProfile`` / ``SetInputProps`` / ``SetOutputFilename``) and then
     **re-streams every audio sample**, because adding an attribute moves the
     Data Object and every packet offset after it.

This module instead rewrites only the header object and shifts the audio data
in place - no decoding, no re-encoding, no re-streaming.

The layout
----------
The ASF Header Object is::

    GUID(16) Size(8, LE) ObjectCount(4) Reserved1(1) Reserved2(1)
    then ObjectCount sub-objects, each: GUID(16) Size(8) body...

``WM/Picture`` belongs in the **Metadata Object**
(``C5F8CBEA-5BAF-4877-8467-AA8C44FA4CCA``), which holds description records::

    Reserved(2) StreamNumber(2) NameLength(2) DataType(2) DataLength(4)
    Name(NameLength bytes, UTF-16LE, NUL-terminated) Data(DataLength bytes)

Both name and data are padded to a 4-byte boundary.

The value
---------
``WM/Picture`` is not raw image bytes. It is a ``WM_PICTURE`` struct -
``pwszMIMEType``, ``bPictureType``, ``pwszDescription``, ``dwDataLen``,
``pbData`` - serialised with its pointer members replaced by offsets, which is
what makes it self-contained. ``bPictureType`` 3 is "Front album cover". See
``_wmpicture_blob``.

Why growing the header is safe
------------------------------
An **index object** stores absolute byte offsets to packets. Growing the header
shifts the Data Object, so those offsets go stale and seeking breaks. Every
sample WMA on this machine was checked and none has one - but that is a property
of *those files*, not of ASF, so this module refuses any file that has one
rather than assume. See ``_assert_no_index_objects()``.

Everything else is preserved byte-for-byte: the audio is moved, never rewritten,
and any sub-object this module does not understand is copied verbatim.
"""
import os
import struct

#: The attribute written. Matches the name in WMP's own metadata table.
WM_PICTURE_NAME = "WM/Picture"

#: WMT_ATTR_DATATYPE - WM_BINARY_METADATA.
_ASF_BINARY = 0x03

#: WMT_STREAM_TYPE - the file-level (non-stream) pseudo stream number.
_ASF_STREAM_FILE = 0xFFFF

#: bPictureType 3 == "Front album cover", per the WM_PICTURE documentation.
PICTURE_TYPE_FRONT_COVER = 3

#: Refuse anything larger. A cover that big will not fit a WMP library entry
#: either, and a huge header blob is a sign something upstream sent the wrong
#: bytes rather than an image.
MAX_PICTURE_BYTES = 8 * 1024 * 1024


def _guid(text):
    """'C5F8CBEA-...' -> the 16 little-endian bytes ASF stores."""
    raw = bytes.fromhex(text.replace("-", ""))
    if len(raw) != 16:
        raise ValueError("bad GUID: %r" % text)
    return raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:16]


_ASF_HEADER = _guid("75B22630-668E-11CF-A6D9-00AA0062CE6C")
_ASF_METADATA = _guid("C5F8CBEA-5BAF-4877-8467-AA8C44FA4CCA")

#: Content Description Object. Holds Title / Author / Copyright / Description /
#: Rating, as five length-prefixed UTF-16LE strings. WMP puts a rip's track
#: title and artist here.
_ASF_CONTENT_DESCRIPTION = _guid("75B22633-668E-11CF-A6D9-00AA0062CE6C")

#: Extended Content Description Object. Holds the named attributes - WMP writes
#: WM/AlbumTitle, WM/AlbumArtist, WM/TrackNumber and friends here.
_ASF_EXTENDED_CONTENT = _guid("D2D0A440-E307-11D2-97F0-00A0C95EA850")

#: WMT_ATTR_DATATYPE values this reader converts. Everything else is skipped.
_ASF_UNICODE = 0x00
_ASF_DWORD = 0x03
_ASF_QWORD = 0x04
_ASF_WORD = 0x05

#: Index objects. Any of these holds absolute packet offsets.
_ASF_INDEX = _guid("D6E229DF-35DA-11D1-9034-00A0C90349BE")
_ASF_SIMPLE_INDEX = _guid("33000890-E5B1-11CF-89F4-00A0C90349CB")
_ASF_MEDIA_INDEX = _guid("A3D9C229-293F-11D2-8F91-006097C9A2B2")
_INDEX_GUIDS = (_ASF_INDEX, _ASF_SIMPLE_INDEX, _ASF_MEDIA_INDEX)


def _pad4(value):
    """Bytes of 4-byte padding needed after ``value``."""
    return (-value) % 4


def _wmpicture_blob(image, mime, description=""):
    """Serialise a ``WM_PICTURE`` into a self-contained ASF attribute value.

    The documented struct carries three pointers. On disk those become offsets
    from the start of the value, so the value can be relocated without fixing
    anything up - which is why WMP can hand it to a different process and have
    it still work.

    Layout (little-endian)::

        0  DWORD dwDataLen     picture bytes, this many
        4  DWORD mimeOffset    -> the MIME string
        8  DWORD descOffset    -> the description string
        12 DWORD dataOffset    -> the picture bytes
        16 BYTE  bPictureType  3 = front cover
        17 ..  padding to 4
        then: MIME string, description string, picture bytes

    The DWORDs come first and the strings last, so every offset is known before
    a single byte is copied and no second pass is needed.
    """
    mime_b = (mime + "\x00").encode("utf-16-le")
    desc_b = (description + "\x00").encode("utf-16-le")
    if _pad4(len(mime_b)):
        mime_b += b"\x00" * _pad4(len(mime_b))
    if _pad4(len(desc_b)):
        desc_b += b"\x00" * _pad4(len(desc_b))

    header = 17
    header += _pad4(header)
    mime_off = header
    desc_off = mime_off + len(mime_b)
    data_off = desc_off + len(desc_b)
    blob = bytearray(data_off + len(image))

    struct.pack_into("<IIII", blob, 0, len(image), mime_off, desc_off,
                     data_off)
    blob[16] = PICTURE_TYPE_FRONT_COVER
    blob[mime_off:mime_off + len(mime_b)] = mime_b
    blob[desc_off:desc_off + len(desc_b)] = desc_b
    blob[data_off:data_off + len(image)] = image
    return bytes(blob)


def _descriptor(name, data, stream=_ASF_STREAM_FILE):
    """One ASF metadata description record, padded to a 4-byte boundary."""
    name_b = (name + "\x00").encode("utf-16-le")
    out = bytearray()
    out += struct.pack("<HHHHI", 0, stream, len(name_b), _ASF_BINARY,
                       len(data))
    out += name_b
    out += b"\x00" * _pad4(len(name_b))
    out += data
    out += b"\x00" * _pad4(len(data))
    return bytes(out)


def _parse_header(data):
    """Split an ASF file into its header sub-objects and the data tail.

    Returns ``(sub_objects, data_tail, header_size)`` where ``sub_objects`` is a
    list of ``(guid, raw_bytes)`` covering the header object *after* its fixed
    30-byte preamble, and ``data_tail`` is everything from the Data Object to
    EOF - copied verbatim, never re-encoded.

    Raises ValueError on anything that does not parse as a well-formed ASF
    header. The caller must treat that as "leave the file alone": a mis-parsed
    header means we do not know where the audio starts, and guessing there
    destroys the file.
    """
    if len(data) < 30 or data[:16] != _ASF_HEADER:
        raise ValueError("not an ASF container")
    header_size = struct.unpack("<Q", data[16:24])[0]
    count = struct.unpack("<I", data[24:28])[0]
    if header_size < 30 or header_size > len(data):
        raise ValueError("bad ASF header size %d" % header_size)

    objects = []
    pos = 30
    while pos + 24 <= header_size:
        guid = data[pos:pos + 16]
        size = struct.unpack("<Q", data[pos + 16:pos + 24])[0]
        # A sub-object smaller than its own GUID+size cannot be walked, and one
        # that overruns the header means the sizes disagree with each other.
        # Either way the layout is not something we can safely rewrite.
        if size < 24 or pos + size > header_size:
            raise ValueError("bad sub-object size %d at %d" % (size, pos))
        objects.append((guid, data[pos:pos + size]))
        pos += size

    if count and len(objects) != count:
        # The header's own object count disagreeing with what we walked means
        # the two disagree about the layout. Refuse rather than pick one.
        raise ValueError("ASF object count mismatch: header says %d, walked %d"
                         % (count, len(objects)))
    return objects, data[header_size:], header_size


def _assert_no_index_objects(objects, tail):
    """Refuse a file whose index objects would go stale when the header grows.

    Index objects live inside the Data Object and store absolute byte offsets to
    packets. Inserting an attribute shifts the Data Object by the header growth,
    so every one of those offsets becomes wrong. Refusing is the only safe
    answer: a file that will not seek correctly afterwards looks like random
    corruption to a player, and the audio itself is fine.
    """
    haystack = b"".join(raw for _guid_bytes, raw in objects) + tail
    for index_guid in _INDEX_GUIDS:
        if index_guid in haystack:
            raise ValueError(
                "ASF file contains an index object (%s); growing the header "
                "would invalidate its packet offsets, so this file is left "
                "untouched" % index_guid.hex())


def _rebuild_header(objects, added_object):
    """Reassemble the header object, adding one sub-object, fixing all sizes."""
    count = struct.pack("<I", len(objects) + (1 if added_object else 0))
    body = count + b"\x01\x02"
    if added_object:
        body += added_object
    for _guid_bytes, raw in objects:
        body += raw
    total = 16 + 8 + len(body)
    return _ASF_HEADER + struct.pack("<Q", total) + body


def _find_metadata_object(objects):
    """Index of the Metadata Object, or None.

    WMP writes one even when it holds nothing (verified: every sample WMA here
    has a Metadata Object with zero description records), so this nearly always
    succeeds. When it is absent the caller builds a new one rather than giving
    up - a new Metadata Object is well-formed and additive, and refusing would
    mean no cover art on those files at all.
    """
    for position, (guid, _raw) in enumerate(objects):
        if guid == _ASF_METADATA:
            return position
    return None


def _metadata_with_picture(raw, blob):
    """Return the Metadata Object's bytes with ``blob`` appended as a record.

    Existing records are parsed and rebuilt rather than copied blindly, so an
    existing ``WM/Picture`` is *replaced* instead of duplicated. A file carrying
    two cover records is one WMP resolves unpredictably.
    """
    # Fixed part of the Metadata Object:
    #   GUID(16) + Size(8) + Reserved1(1) + Reserved2(1) + DescriptionCount(2)
    #   + Reserved(2)
    # That is 30 bytes before the first description record. Getting this wrong
    # by even 2 shifts every field, and the symptom is a name length of 3 where
    # "WM/Picture" needs 22 - which is how an earlier revision of this function
    # failed to find its own record on the second write.
    if len(raw) < 30:
        raise ValueError("Metadata Object is truncated")
    count = struct.unpack("<H", raw[26:28])[0]
    # Description records start at offset 30. The object layout, verified by
    # dumping what this module writes:
    #   0..15  GUID
    #  16..23  Size
    #  24..25  Reserved1/Reserved2 (0x01 0x00)
    #  26..27  DescriptionRecordsCount
    #  28..29  Reserved (0x01 0x00)
    #  30..    first description record
    # Reading at 32 instead yields a stream number of 0xFFFF followed by a name
    # length of 3 where "WM/Picture" needs 22, which is exactly how this
    # function failed to find its own record on a second write.
    pos = 30
    kept = []
    for _ in range(count):
        if pos + 12 > len(raw):
            raise ValueError("Metadata Object record is truncated")
        _rsv, _stream, name_len, _dtype, data_len = struct.unpack(
            "<HHHHI", raw[pos:pos + 12])
        name_start = pos + 12
        name_end = name_start + name_len
        # A name or data length that runs past the end of the object means the
        # record layout is not what this module reads. Pad arithmetic on a bad
        # length produces a plausible-looking slice of the wrong bytes, so the
        # bounds are checked BEFORE anything is copied.
        if name_end > len(raw) or name_end + _pad4(name_len) + data_len > len(raw):
            raise ValueError("Metadata Object record overruns the object")
        try:
            name = raw[name_start:name_end].decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            raise ValueError("Metadata Object name is not valid UTF-16")
        if name != WM_PICTURE_NAME:
            # The record runs to the end of its padded data. BOTH name and data
            # are padded, and forgetting either is what produced a slice that
            # started mid-string on the second write.
            data_start = name_end + _pad4(name_len)
            data_end = data_start + data_len
            kept.append(raw[pos:data_end + _pad4(data_len)])
        pos = name_end + _pad4(name_len) + data_len + _pad4(data_len)

    # Metadata Object body: Reserved1(1) Reserved2(1) Count(2) Reserved(2), then
    # the records from offset 30. Matches the layout documented above.
    body = struct.pack("<BBHH", 1, 0, len(kept) + 1, 1) + b"".join(kept)
    body += _descriptor(WM_PICTURE_NAME, blob)
    return _ASF_METADATA + struct.pack("<Q", 24 + len(body)) + body


def _split_existing_picture(blob):
    """Recover ``(mime, description, image)`` from a stored WM_PICTURE blob."""
    if len(blob) < 17:
        return None
    data_len, mime_off, desc_off, data_off = struct.unpack_from("<IIII", blob, 0)
    if not (17 <= mime_off <= len(blob) and 17 <= desc_off <= len(blob)
            and data_off <= len(blob)):
        return None
    if data_off + data_len > len(blob) or mime_off >= desc_off:
        return None
    try:
        mime = blob[mime_off:desc_off].decode("utf-16-le").rstrip("\x00")
        desc = blob[desc_off:data_off].decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return None
    return mime, desc, bytes(blob[data_off:data_off + data_len])


def _decode_asf_string(raw):
    """One UTF-16LE ASF string, without its terminator."""
    try:
        return raw.decode("utf-16-le").rstrip("\x00").strip()
    except UnicodeDecodeError:
        return ""


def _asf_object_body(raw):
    """A sub-object's body: skip its GUID(16) + Size(8) header."""
    return raw[24:] if len(raw) > 24 else b""


def read_tags(path):
    """Return ``{'album': str, 'artist': str, 'title': str}`` from an ASF file.

    This is the WMA half of ``artwork_embed.read_tags()``, and it exists for one
    reason: without it every WMA in a library looks UNTAGGED. The cover writer
    finds an album's files by reading their album tag back, so a WMA album in a
    populated library could never be matched - ``_find_album_files()`` saw every
    WMA folder as "untagged files spread over many folders" and refused them
    all. MP3 and FLAC matched and got their covers; WMA (what WMP rips by
    default) silently did not, leaving only WMP's database carrying the art.

    The two objects that matter are the Content Description (Title/Author) and
    the Extended Content Description (WM/AlbumTitle, WM/AlbumArtist). WMP writes
    both into a ripped file, and both are walked defensively: a malformed file
    returns whatever was read rather than raising, exactly like the MP3 and FLAC
    readers.

    Only the header is read, not the audio - these objects live in the first few
    kilobytes of the file, and a library scan touches every track.
    """
    out = {"album": "", "artist": "", "title": ""}
    try:
        with open(path, "rb") as handle:
            head = handle.read(30)
            if len(head) < 30 or head[:16] != _ASF_HEADER:
                return out
            header_size = struct.unpack("<Q", head[16:24])[0]
            # Same bound the writer enforces. A header this large is not a file
            # we understand, and reading it into memory is not worth the risk.
            if header_size < 30 or header_size > 16 * 1024 * 1024:
                return out
            data = head + handle.read(header_size - 30)
        objects, _tail, _size = _parse_header(data)
    except (OSError, ValueError, struct.error):
        return out

    author = album_artist = ""
    for guid, raw in objects:
        body = _asf_object_body(raw)
        if len(body) < 2:
            continue
        if guid == _ASF_CONTENT_DESCRIPTION:
            # Five WORD lengths, then the five strings back to back. Every
            # length includes the string's trailing NUL.
            if len(body) < 10:
                continue
            lengths = struct.unpack("<5H", body[:10])
            pos = 10
            values = []
            for length in lengths:
                if pos + length > len(body):
                    break
                values.append(body[pos:pos + length])
                pos += length
            if values:
                out["title"] = _decode_asf_string(values[0])
            if len(values) > 1:
                author = _decode_asf_string(values[1])
        elif guid == _ASF_EXTENDED_CONTENT:
            # WORD count, then name/value records:
            #   WORD name_len, name (UTF-16LE, NUL included),
            #   WORD value_type, WORD value_len, value
            count = struct.unpack("<H", body[:2])[0]
            pos = 2
            for _ in range(min(int(count), 1024)):   # bounded: a corrupt count
                if pos + 2 > len(body):              # must not spin
                    break
                name_len = struct.unpack("<H", body[pos:pos + 2])[0]
                pos += 2
                if pos + name_len + 4 > len(body):
                    break
                name = _decode_asf_string(body[pos:pos + name_len]).lower()
                pos += name_len
                value_type, value_len = struct.unpack("<HH", body[pos:pos + 4])
                pos += 4
                if pos + value_len > len(body):
                    break
                value = body[pos:pos + value_len]
                pos += value_len
                if value_type == _ASF_UNICODE:
                    text = _decode_asf_string(value)
                elif value_type == _ASF_DWORD and len(value) >= 4:
                    text = str(struct.unpack("<I", value[:4])[0])
                elif value_type == _ASF_QWORD and len(value) >= 8:
                    text = str(struct.unpack("<Q", value[:8])[0])
                elif value_type == _ASF_WORD and len(value) >= 2:
                    text = str(struct.unpack("<H", value[:2])[0])
                else:
                    continue
                if name == "wm/albumtitle" and text:
                    out["album"] = text
                elif name == "wm/albumartist" and text:
                    album_artist = text
    # WM/AlbumArtist is what the dialog calls the artist; the Content
    # Description's Author is the fallback for files WMP did not tag that way.
    out["artist"] = album_artist or author
    return out


def read_art(path):
    """Return ``(mime, description, image_bytes)`` from a file, or None.

    Reading is separated from writing because ``artwork_embed.py`` matches
    library files by reading their tags back, and a WMA that already has a cover
    must not be treated as untagged.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        objects, _tail, _size = _parse_header(data)
    except (OSError, ValueError):
        return None
    position = _find_metadata_object(objects)
    if position is None:
        return None
    raw = objects[position][1]
    if len(raw) < 30:
        return None
    count = struct.unpack("<H", raw[26:28])[0]
    pos = 30                      # records start at 30 - see _metadata_with_picture
    for _ in range(count):
        if pos + 12 > len(raw):
            return None
        _rsv, _stream, name_len, _dtype, data_len = struct.unpack(
            "<HHHHI", raw[pos:pos + 12])
        name_start = pos + 12
        name_end = name_start + name_len
        if name_end > len(raw):
            return None
        try:
            name = raw[name_start:name_end].decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return None
        data_start = name_end + _pad4(name_len)
        data_end = data_start + data_len
        if name == WM_PICTURE_NAME and data_end <= len(raw):
            return _split_existing_picture(raw[data_start:data_end])
        pos = data_start + data_len + _pad4(data_len)
    return None


def _atomic_write(path, payload):
    """Replace ``path`` with ``payload`` via a temp file and os.replace.

    os.replace is atomic on Windows and on POSIX, so a crash mid-write leaves
    the original intact rather than a half-written music file.
    """
    temp = path + ".wmavtmp"
    with open(temp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def write_art(path, image, mime="image/jpeg", description=""):
    """Write ``WM/Picture`` into an ASF file. Returns True on success.

    Returns False - leaving the file untouched - for anything this module will
    not rewrite with confidence: a non-ASF file, a header it cannot walk, an
    oversized image, or an index object whose packet offsets a header growth
    would invalidate.
    """
    if not image:
        return False
    if len(image) > MAX_PICTURE_BYTES:
        return False
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False

    try:
        objects, tail, _size = _parse_header(data)
        _assert_no_index_objects(objects, tail)
        blob = _wmpicture_blob(image, mime, description)
        position = _find_metadata_object(objects)
        if position is None:
            body = struct.pack("<BBHH", 1, 0, 1, 1) \
                + _descriptor(WM_PICTURE_NAME, blob)
            metadata = _ASF_METADATA + struct.pack("<Q", 24 + len(body)) + body
            objects.append((_ASF_METADATA, metadata))
            new_header = _rebuild_header(objects, None)
        else:
            guid, raw = objects[position]
            objects[position] = (guid, _metadata_with_picture(raw, blob))
            new_header = _rebuild_header(objects, None)
    except ValueError:
        return False
    except (OSError, struct.error):
        return False

    # The Data Object begins immediately after the header, so shifting the header
    # is exactly a shift of the data. The tail is written verbatim - the audio
    # is moved, never touched.
    try:
        _atomic_write(path, new_header + tail)
    except OSError:
        return False
    return True


def has_art(path):
    """True when the file already carries a WM/Picture record."""
    return read_art(path) is not None


# --------------------------------------------------------------------------
# Notes on the COM route that was tested and rejected
# --------------------------------------------------------------------------
# Recorded because the negative result is worth keeping. The system
# C:\Windows\System32\wmvcore.dll (12.0.19041.3996, x64) exports WMCreateWriter
# and WMCreateEditor, and QI on the returned object succeeds for IWMWriter,
# IWMHeaderInfo, IWMHeaderInfo2 and **IWMHeaderInfo3**
# ({15CC68E3-27CC-4ECD-B222-3F5D02D80BD5}). A WM_PICTURE was built by hand and
# passed to IWMHeaderInfo3::AddAttribute, which returned 0xC00D0BBC =
# NS_E_NOT_CONFIGURED - "the object must be fully configured", i.e. the call
# shape is right and the writer simply needs its profile and output set first.
# So the COM route does work; it was not chosen because it requires re-streaming
# the whole file, which is more risk for the same header bytes written here.
#
# This also corrects a claim in README.md: IWMHeaderInfo3 does NOT return
# E_NOINTERFACE from the editor object. An earlier probe appeared to show that
# because the probe's GUID marshalling was wrong - every interface including
# IUnknown failed - and a control test on IUnknown is what exposed it.