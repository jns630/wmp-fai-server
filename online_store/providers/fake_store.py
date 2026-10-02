# -*- coding: utf-8 -*-
"""The Fake Digital Music Store: a completely local, offline catalog.

This is the first provider, and it is the one that makes the whole subsystem
testable with no network, no account, no provider credentials and nothing
installed but Python.

Content
-------
The catalog is **generated at runtime as synthesised tones**, not shipped as
audio files. That is a deliberate choice with three consequences worth stating:

1. The repository stays a few hundred kilobytes instead of a few hundred
   megabytes, and the store is diffable in code review.
2. Nothing in it is anyone's copyrighted music. Every track is a sine tone
   built from a formula, so there is no rights question at all - which is what
   makes it safe to put in a public repository under MIT.
3. It is honest: a real provider replaces :meth:`FakeStore.purchase` with an API
   call, and the storefront does not change.

The artist and album names are invented. Several are deliberately *not* real
release titles, so that nobody can mistake a generated tone for a real
recording of a real song.

Thread safety
-------------
The catalog is built once in ``__init__`` and only read afterwards, so the
shared instance is safe to use from Flask's request threads without a lock.
"""
import hashlib
import math
import os
import struct
import threading
import wave

from ..models import Album, Track
from .base import Purchase, StoreProvider, register_provider

#: (album_id, title, artist, year, genre, price, [(title, seconds, price)])
#:
#: Prices are strings on purpose - see the note in models.Track about money and
#: binary floats.
_CATALOG_SPEC = [
    ("lms-0001", "Signals From The Quiet Room",
     "The Modulation Set", 2019, "Ambient", "6.99", [
         ("First Light On The Dial", 214, "0.99"),
         ("Carrier Wave", 187, "0.99"),
         ("Standing Wave", 245, "1.29"),
         ("Null And Void", 168, "0.99"),
     ]),
    ("lms-0002", "Twelve Bar Buffer",
     "Rust Belt Radio", 2021, "Blues", "7.99", [
         ("Two-Four Time", 233, "0.99"),
         ("Barleycorn Stomp", 198, "0.99"),
         ("Slide Position Nine", 256, "1.29"),
         ("Closing Time Shuffle", 289, "0.99"),
         ("Last Set Of The Night", 312, "1.29"),
     ]),
    ("lms-0003", "Cathode Sunrise",
     "Valve And Vine", 2018, "Electronic", "5.99", [
         ("Heater Warmup", 201, "0.99"),
         ("Rectifier Dream", 276, "0.99"),
         ("Mains Hum", 189, "0.99"),
         ("Cooling Down", 143, "1.29"),
     ]),
    ("lms-0004", "Field Recordings From The Flooded Line",
     "Anthea Brackish", 2022, "Modern Classical", "8.99", [
         ("Levee", 341, "1.29"),
         ("Sluice", 297, "1.29"),
         ("Spillway", 405, "1.49"),
         ("Dry Season", 366, "1.29"),
     ]),
    ("lms-0005", "Northbound, Minor Key",
     "The Gravel Atlas", 2020, "Folk", "6.49", [
         ("County Road", 224, "0.99"),
         ("Passing Lane", 208, "0.99"),
         ("Hairpin", 262, "1.29"),
         ("Summit", 198, "0.99"),
     ]),
    ("lms-0006", "Paper Architecture",
     "Marguerite Fold", 2017, "Indie", "7.49", [
         ("Origami Heart", 231, "0.99"),
         ("Crease Pattern", 249, "0.99"),
         ("Unfold", 275, "1.29"),
         ("Flat Pack", 194, "0.99"),
     ]),
]

#: Sample rate for the generated WAVs. 22050 Hz mono 16-bit is roughly 44 KB
#: per second, so a five-minute track is about 13 MB. That is deliberate: a
#: real file size makes the download path testable, and 22050 is more than
#: enough for a sine tone.
SAMPLE_RATE = 22050


def _tone_seed(track_id, title):
    """A stable integer seed derived from the track's identity.

    Stable matters: the same track must produce byte-identical audio on every
    run, otherwise a user who "purchases" a track, notes its hash, and
    restarts the server would see the hash change for no reason.
    """
    digest = hashlib.sha256(("%s|%s" % (track_id, title)).encode("utf-8"))
    return int(digest.hexdigest()[:8], 16)


def write_tone_wav(path, track_id, title, seconds, frequency_hz=440.0):
    """Write a real, playable WAV of a decaying sine tone.

    Generated with the stdlib ``wave`` module: no numpy, no encoder, no
    dependency, and the result is a file WMP plays without complaint.
    """
    frames = max(1, int(SAMPLE_RATE * float(seconds)))
    seed = _tone_seed(track_id, title)
    # Derive a stable, pleasant-ish frequency rather than using the default
    # 440 Hz for everything, so a user comparing two tracks can hear a
    # difference. Kept in a musical range on purpose.
    frequency = 196.0 + (seed % 24) * 22.0

    samples = bytearray()
    two_pi_over_rate = 2.0 * math.pi / SAMPLE_RATE
    for index in range(frames):
        # A simple decay envelope, so the tone fades out instead of clicking.
        envelope = 1.0 - (float(index) / frames)
        value = math.sin(two_pi_over_rate * frequency * index) * envelope * 0.4
        # int16 is signed, so clamp into range before packing.
        packed = int(max(-1.0, min(1.0, value)) * 32767)
        samples += struct.pack("<h", packed)

    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    handle = wave.open(path, "wb")
    try:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(samples))
    finally:
        handle.close()
    return path


class _Artwork(object):
    """A tiny deterministic PNG cover, generated rather than shipped.

    The FAI server already has a precedent here: it generates its ``noart.png``
    placeholder at request time and the test suite checks the PNG structure and
    CRCs. This follows that pattern instead of adding binary files.
    """

    def __init__(self, album_id, title, artist, size=240):
        self.album_id = album_id
        self.title = title
        self.artist = artist
        self.size = size

    def png_bytes(self):
        """Build a small RGB PNG whose colour is derived from the album id."""
        seed = _tone_seed(self.album_id, self.title)
        # Muted, mid-brightness colours. Deliberately not garish.
        red = 60 + (seed % 120)
        green = 60 + ((seed >> 8) % 120)
        blue = 60 + ((seed >> 16) % 120)
        size = self.size
        row = b""
        for x in range(size):
            # A simple diagonal gradient so the cover is not a flat block.
            shade = (x * 40) // max(1, size)
            row += bytes((
                min(255, red + shade),
                min(255, green + shade // 2),
                min(255, blue + shade // 3),
            ))
        # Filter type 0 (None) at the start of every scanline.
        raw = b"".join(b"\x00" + row for _ in range(size))
        return _png(self.size, self.size, raw)


def _png(width, height, raw_rows):
    """Wrap raw RGB scanlines in a valid PNG. Stdlib only (zlib + struct)."""
    import struct as _struct
    import zlib as _zlib

    def chunk(tag, payload):
        return (_struct.pack(">I", len(payload)) + tag + payload
                + _struct.pack(">I", _zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = _struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", _zlib.compress(raw_rows, 9))
            + chunk(b"IEND", b""))


def _safe_component(name):
    """Make ``name`` safe to use as a single filesystem path component."""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in str(name or "")).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned[:80] or "untitled"


@register_provider
class FakeStore(StoreProvider):
    """An entirely local catalog. No network, no credentials, no scraping.

    Construct with the directory purchases are written into::

        FakeStore(purchases_dir=r"C:\\path\\to\\purchases")
    """

    provider_id = "local_fake"
    display_name = "Legacy Music Store (local fake catalog)"
    supports_purchase = True

    def __init__(self, purchases_dir=None, currency="USD"):
        self.currency = currency
        self.purchases_dir = purchases_dir or ""
        # Built once in __init__, read-only afterwards, so the shared instance
        # is safe to use from Flask's request threads without a lock.
        self._albums = []
        self._tracks = {}
        self._lock = threading.Lock()
        self._build()

    def _build(self):
        for (album_id, title, artist, year, genre, album_price,
             track_specs) in _CATALOG_SPEC:
            tracks = []
            for index, (track_title, seconds, price) in enumerate(track_specs, 1):
                track = Track(
                    track_id="%s-t%02d" % (album_id, index),
                    title=track_title,
                    artist=artist,
                    duration_ms=int(seconds * 1000),
                    track_number=index,
                    price=price,
                    currency=self.currency,
                    album_id=album_id,
                )
                tracks.append(track)
                self._tracks[track.track_id] = track
            self._albums.append(Album(
                album_id=album_id,
                title=title,
                artist=artist,
                provider=self.provider_id,
                year=year,
                genre=genre,
                price=album_price,
                currency=self.currency,
                tracks=tracks,
                description="%s - %s (%s). Synthesised demo content; "
                            "not a real recording." % (artist, title, year),
            ))

    # -- StoreProvider ---------------------------------------------------
    def search(self, query, limit=25):
        needle = (query or "").strip().lower()
        if not needle:
            return self.list_albums(limit=limit)
        hits = []
        for album in self._albums:
            haystack = " ".join(
                [album.title, album.artist, album.genre, str(album.year or "")]
                + [track.title for track in album.tracks]).lower()
            if needle in haystack:
                hits.append(album)
        return hits[:max(0, int(limit))]

    def list_albums(self, limit=50, offset=0):
        limit = max(0, int(limit))
        start = max(0, int(offset))
        if limit == 0:
            return []
        return self._albums[start:start + limit]

    def get_album(self, album_id):
        for album in self._albums:
            if album.album_id == str(album_id):
                return album
        return None

    def get_track(self, track_id):
        return self._tracks.get(str(track_id))

    def artwork(self, album_id):
        album = self.get_album(album_id)
        if album is None:
            return None
        return _Artwork(album.album_id, album.title, album.artist)

    def purchase(self, track_id):
        """Materialise a real WAV on disk and describe it.

        The tone is generated once and then reused, so purchasing the same
        track twice is idempotent and returns the same path.
        """
        track = self.get_track(track_id)
        if track is None:
            return None
        album = self.get_album(track.album_id)
        safe_album = _safe_component(album.title if album else track.album_id)
        safe_track = _safe_component(track.title)
        directory = os.path.join(self.purchases_dir or os.getcwd(), safe_album)
        path = os.path.join(directory, "%02d - %s.wav"
                            % (track.track_number, safe_track))
        with self._lock:
            if not os.path.exists(path):
                write_tone_wav(path, track.track_id, track.title,
                               track.duration_ms / 1000.0)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        return Purchase(track_id=track.track_id, title=track.title,
                        artist=track.artist, local_path=path,
                        content_type="audio/wav", size_bytes=size)

    def describe(self):
        info = StoreProvider.describe(self)
        info.update({
            "albums": len(self._albums),
            "tracks": len(self._tracks),
            "purchases_dir": self.purchases_dir,
            "network_access": "none",
        })
        return info

