# -*- coding: utf-8 -*-
"""The provider abstraction: one seam between WMP and a real music backend.

Why this exists
---------------
The first milestone is a *local* fake store, but the point of the subsystem is
that the fake can later be swapped for a real provider (7digital, Bandcamp,
Qobuz, Juno Download, ...) **without touching the storefront, the ServiceInfo
document, the registry installer, or the WMP-facing URLs.**

The contract is intentionally small - five read operations plus one delivery
operation - because every method here is something a real, licensed store API
can actually answer. Anything a real provider could *not* implement (DRM
license vending, a synchronous cart on Microsoft's behalf, burning to CD on
WMP's initiative) is deliberately absent from this interface, so a future
adapter is not tempted to fake it.

A provider MUST NOT
-------------------
* scrape a website, or work around an API's authentication or rate limits
* implement or strip DRM
* download anything the user is not entitled to

Returning "I cannot do that" from one of these methods is a correct, supported
outcome. The FakeStore returns honest ``None``s rather than pretending.

Thread safety
-------------
Flask's dev server and the project's threaded listener can call a provider
from more than one thread, so an implementation must either be stateless or
guard its own mutable state.
"""


class ProviderError(Exception):
    """A provider could not answer.

    Carries a message intended for the log and the ``/api`` error body. It must
    never leak a credential, a token, or a full provider URL with a query
    string - those are logged verbatim by the caller.
    """


class StoreProvider(object):
    """Base class for a catalog backend.

    Subclasses must implement :meth:`search`, :meth:`list_albums` and
    :meth:`get_album`; the rest have honest defaults.
    """

    #: Short, stable identifier used in config and in URLs. Lowercase, no
    #: spaces. This is NOT a display name - the friendly name comes from config.
    provider_id = "base"

    #: What the storefront shows on the provider's own pages.
    display_name = "Unnamed provider"

    #: True when the provider can deliver purchased audio. A storefront hides
    #: the Buy button when this is False rather than letting a user click
    #: through to an error.
    supports_purchase = False

    # -- the catalog -----------------------------------------------------
    def search(self, query, limit=25):
        """Return a list of :class:`~online_store.models.Album` for ``query``.

        Must never raise for an empty result - an empty list is the answer.
        May raise :class:`ProviderError` for a genuine upstream failure.
        """
        raise NotImplementedError

    def list_albums(self, limit=50, offset=0):
        """Return a page of albums for the storefront's browse view."""
        raise NotImplementedError

    def get_album(self, album_id):
        """Return one :class:`Album`, or ``None`` if the id is unknown."""
        raise NotImplementedError

    def get_track(self, track_id):
        """Return one :class:`Track`, or ``None`` if the id is unknown."""
        raise NotImplementedError

    # -- delivery --------------------------------------------------------
    def purchase(self, track_id):
        """Resolve a paid track to something playable.

        Returns a :class:`Purchase` describing what the user gets. Must not
        return a URL to DRM-protected or unauthorised content. Returning
        ``None`` means "this provider cannot deliver that track", which the
        storefront reports as a 404 rather than a crash.
        """
        return None

    def artwork(self, album_id):
        """Return a URL or local path for cover art, or ``None``."""
        return None

    # -- diagnostics -----------------------------------------------------
    def describe(self):
        """Return a JSON-serialisable dict for ``/api/store/status``."""
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "supports_purchase": bool(self.supports_purchase),
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<%s %s>" % (type(self).__name__, self.provider_id)


class Purchase(object):
    """The result of a successful :meth:`StoreProvider.purchase`.

    ``local_path`` and ``url`` are alternatives: a local file is preferred,
    because the server can then hand WMP a real path and the user ends up with
    the file. ``url`` is for a provider that streams.
    """

    def __init__(self, track_id, title, artist="", local_path="", url="",
                 content_type="audio/mpeg", size_bytes=0, sha256=""):
        self.track_id = str(track_id)
        self.title = title or ""
        self.artist = artist or ""
        self.local_path = local_path or ""
        self.url = url or ""
        self.content_type = content_type or "audio/mpeg"
        self.size_bytes = int(size_bytes or 0)
        self.sha256 = sha256 or ""

    def as_dict(self):
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "local_path": self.local_path,
            "url": self.url,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Purchase %s %r>" % (self.track_id, self.title)


#: Registry of provider classes by id. A real provider is added here and
#: nothing else in the subsystem changes.
_PROVIDERS = {}


def register_provider(cls):
    """Class decorator: register a provider under its ``provider_id``."""
    pid = getattr(cls, "provider_id", "")
    if not pid or pid == "base":
        raise ProviderError("provider must define a provider_id")
    _PROVIDERS[pid] = cls
    return cls


def available_providers():
    return sorted(_PROVIDERS)


def create_provider(provider_id, **kwargs):
    """Instantiate a registered provider by id.

    Raises :class:`ProviderError` listing what *is* available, because "unknown
    provider: typo" with the available list is the error message that saves a
    user a restart.
    """
    cls = _PROVIDERS.get(provider_id)
    if cls is None:
        raise ProviderError(
            "unknown provider %r (available: %s)"
            % (provider_id, ", ".join(available_providers()) or "none"))
    return cls(**kwargs)
