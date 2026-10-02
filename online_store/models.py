# -*- coding: utf-8 -*-
"""Catalog model shared by every Online Store provider.

These are deliberately dumb value objects. A provider's job is to turn some
remote or local source into these; everything downstream - the storefront, the
ServiceInfo document, the purchase flow - is written against them and never
against a provider's native response shape.

That boundary is the whole point of the provider abstraction: when a real
7digital or Qobuz adapter lands, it produces these objects and nothing else in
the subsystem changes.
"""


def format_duration(ms):
    """Milliseconds -> ``M:SS`` (or ``H:MM:SS`` past an hour).

    Duplicated from ``FAI Server.py`` rather than imported: the server module
    is named ``FAI Server.py``, which is not a legal Python module name, so
    importing it needs the importlib dance that ``wsgi.py`` already performs.
    Importing it here would also mean the store subsystem could not be unit
    tested without booting the entire FAI server. Twenty lines of trivial
    formatting is a cheaper price than that coupling.
    """
    try:
        total = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    seconds = total // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


class Track(object):
    """One purchasable track."""

    def __init__(self, track_id, title, artist, duration_ms=0, track_number=0,
                 disc_number=1, price=None, currency="", artwork_url="",
                 audio_url="", preview_url="", album_id=""):
        self.track_id = str(track_id)
        self.title = title or ""
        self.artist = artist or ""
        self.duration_ms = int(duration_ms or 0)
        self.track_number = int(track_number or 0)
        self.disc_number = int(disc_number or 1)
        #: Price as a string, or None when the track is not for sale. A string
        #: avoids binary-float drift on money and serialises to JSON cleanly.
        self.price = None if price is None else str(price)
        self.currency = currency or ""
        self.artwork_url = artwork_url or ""
        #: Where the audio itself is fetched from, when the provider knows.
        self.audio_url = audio_url or ""
        #: Short, freely-playable excerpt, when the provider offers one.
        self.preview_url = preview_url or ""
        self.album_id = str(album_id or "")

    @property
    def is_purchasable(self):
        return self.price is not None

    def as_dict(self):
        return {
            "track_id": self.track_id,
            "album_id": self.album_id,
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
            "duration": format_duration(self.duration_ms),
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "price": self.price,
            "currency": self.currency,
            "artwork_url": self.artwork_url,
            "audio_url": self.audio_url,
            "preview_url": self.preview_url,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Track %s %r>" % (self.track_id, self.title)


class Album(object):
    """A purchasable album and its tracks."""

    def __init__(self, album_id, title, artist, provider="", year=None,
                 genre="", artwork_url="", tracks=None, price=None,
                 currency="", description=""):
        self.album_id = str(album_id)
        self.title = title or ""
        self.artist = artist or ""
        self.provider = provider or ""
        self.year = year
        self.genre = genre or ""
        self.artwork_url = artwork_url or ""
        self.tracks = list(tracks or [])
        #: Album price, or None when tracks are sold individually.
        self.price = None if price is None else str(price)
        self.currency = currency or ""
        self.description = description or ""

    @property
    def total_duration_ms(self):
        return sum(track.duration_ms for track in self.tracks)

    @property
    def track_count(self):
        return len(self.tracks)

    def track(self, track_id):
        for candidate in self.tracks:
            if candidate.track_id == str(track_id):
                return candidate
        return None

    def as_dict(self):
        return {
            "album_id": self.album_id,
            "title": self.title,
            "artist": self.artist,
            "provider": self.provider,
            "year": self.year,
            "genre": self.genre,
            "artwork_url": self.artwork_url,
            "description": self.description,
            "price": self.price,
            "currency": self.currency,
            "track_count": self.track_count,
            "duration": format_duration(self.total_duration_ms),
            "tracks": [track.as_dict() for track in self.tracks],
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Album %s %r by %r>" % (self.album_id, self.title, self.artist)
