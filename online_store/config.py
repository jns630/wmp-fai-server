# -*- coding: utf-8 -*-
"""Configuration for the Windows Media Player Online Store subsystem.

Precedence, lowest to highest:

    1. The DEFAULTS below.
    2. ``online_store.ini`` next to the server file.
    3. ``local_settings.py`` (git-ignored) - the project's existing
       machine-specific override mechanism, reused rather than replaced.
    4. ``WMP_STORE_*`` environment variables.

Why an INI *and* the environment: the project already reads machine-specific
values from ``local_settings.py``/env (see DISCOGS_TOKEN in the server). This
subsystem follows the same rule so there is one override mechanism to learn,
but a committed INI is still useful because the store's *identity* - store id,
friendly name, GUID - belongs in version control, while the *deployment* - which
base URL this machine serves the storefront from - does not.

Every value is validated here rather than at use, so a typo in a config file
produces one clear message instead of a 500 from a route forty lines away.
"""
import os
import re
import sys

try:                                  # Python 3
    import configparser
except ImportError:                   # pragma: no cover - Python 2 never ran this
    import ConfigParser as configparser

#: The project-specific GUID generated for this project's development store.
#:
#: This is NOT a Microsoft GUID and NOT one copied from any commercial store
#: (7digital, Bandcamp, Qobuz, Juno, Napster, Rhapsody, ...). It was generated
#: for this repository and is safe to publish. It is written to
#: ``SubscriptionObjectGUID`` per the documented Type 2 registry layout.
DEFAULT_SUBSCRIPTION_OBJECT_GUID = "{b4867d0f-fb79-4efb-86d0-c2c94410ee15}"

#: The test key. Microsoft issues these to real store developers; a store that
#: was never submitted to Microsoft never gets one. ``TestParameter`` matches
#: against the store's keyName, and nothing validates the number against a
#: server - the store list is filtered locally, client side. A locally chosen
#: value is therefore sufficient to make a self-hosted store visible, and it
#: avoids pretending to be a real Microsoft-issued credential.
DEFAULT_TEST_KEY = "9100"

DEFAULTS = {
    "enabled": "false",
    "store_id": "legacy_music_store",
    "friendly_name": "Legacy Music Store",
    "base_url": "http://127.0.0.1/online-store",
    "subscription_object_guid": DEFAULT_SUBSCRIPTION_OBJECT_GUID,
    "test_key": DEFAULT_TEST_KEY,
    "capabilities": "0",
    "menu_image_url": "",
    "large_image_url": "",
    "button_color": "#1F5C99",
    "button_text_color": "#FFFFFF",
    "provider": "local_fake",
    "purchases_dir": "",
    "currency": "USD",
}

#: ``keyName`` is the identity WMP stores the store under. It must be a bare
#: registry key component: no backslashes, no path separators, and short. It is
#: validated rather than sanitised, because silently rewriting it would make a
#: store that installs under one name and un-installs under another, which is
#: exactly the kind of thing that leaves junk in the registry forever.
_FORBIDDEN_KEY_CHARS = set('\\/"\'?*|<>:. \t\r\n')

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off", "")

_GUID_RE = re.compile(r"^\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-"
                      r"[0-9A-F]{4}-[0-9A-F]{12}\}$")


class ConfigError(Exception):
    """Raised for a configuration value that cannot be used as written."""


def _as_bool(value, name):
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ConfigError(
        "[online_store] %s: expected a boolean (1/0, true/false, yes/no, on/off), "
        "got %r" % (name, value))


def _as_int(value, name, minimum=0):
    try:
        number = int(str(value).strip(), 0)
    except (TypeError, ValueError):
        raise ConfigError("[online_store] %s: expected an integer, got %r"
                          % (name, value))
    if number < minimum:
        raise ConfigError("[online_store] %s: must be >= %d, got %d"
                          % (name, minimum, number))
    return number


def validate_key_name(name):
    """Return ``name`` if it is usable as a bare registry key component."""
    text = str(name or "").strip()
    if not text:
        raise ConfigError("[online_store] store_id is empty")
    bad = sorted(set(text) & _FORBIDDEN_KEY_CHARS)
    if bad:
        raise ConfigError(
            "[online_store] store_id %r contains characters that are illegal in "
            "a registry key component: %s"
            % (text, " ".join(repr(c) for c in bad)))
    if len(text) > 64:
        raise ConfigError("[online_store] store_id must be 64 characters or fewer")
    return text


def validate_guid(value):
    """Normalise a GUID to the ``{xxxxxxxx-...}`` registry format.

    WMP is documented as requiring "registry format, complete with the curly
    braces". A bare GUID is accepted here and braced, because that is the
    single most likely way to get this wrong by hand and it is unambiguous to
    fix.
    """
    text = str(value or "").strip().upper()
    if not text:
        raise ConfigError("[online_store] subscription_object_guid is empty")
    if not text.startswith("{"):
        text = "{" + text
    if not text.endswith("}"):
        text = text + "}"
    if not _GUID_RE.match(text):
        raise ConfigError(
            "[online_store] subscription_object_guid must be a GUID, got %r"
            % (value,))
    return text


def validate_base_url(value):
    """Return an http(s) base URL with no trailing slash.

    A trailing slash is stripped rather than preserved because the callers
    concatenate paths onto it, and ``base_url + "/x"`` producing ``//x`` is a
    bug that only shows up inside WMP's browser, not in a test.
    """
    text = str(value or "").strip().rstrip("/")
    lowered = text.lower()
    if not lowered.startswith("http://") and not lowered.startswith("https://"):
        raise ConfigError(
            "[online_store] base_url must start with http:// or https://, "
            "got %r" % (value,))
    if " " in text:
        raise ConfigError("[online_store] base_url must not contain spaces")
    return text


class StoreConfig(object):
    """Validated Online Store configuration."""

    def __init__(self, values, source=None):
        get = values.get

        self.enabled = _as_bool(get("enabled"), "enabled")
        self.store_id = validate_key_name(get("store_id"))
        self.friendly_name = (get("friendly_name") or "").strip() or self.store_id
        self.base_url = validate_base_url(get("base_url"))
        self.subscription_object_guid = validate_guid(
            get("subscription_object_guid"))
        self.test_key = str(get("test_key") or "").strip()
        self.capabilities = _as_int(get("capabilities"), "capabilities")
        self.menu_image_url = (get("menu_image_url") or "").strip()
        self.large_image_url = (get("large_image_url") or "").strip()
        self.button_color = (get("button_color") or "").strip() or "#1F5C99"
        self.button_text_color = ((get("button_text_color") or "").strip()
                                  or "#FFFFFF")
        self.provider = (get("provider") or "").strip() or "local_fake"
        self.purchases_dir = (get("purchases_dir") or "").strip()
        self.currency = ((get("currency") or "").strip() or "USD").upper()

        # Where each value came from, so `online-store-status` can explain why
        # the store is configured the way it is instead of just stating it.
        self.source = dict(source or {})

    def url_for(self, path=""):
        """Absolute URL for a storefront path, e.g. ``serviceinfo.xml``."""
        if not path:
            return self.base_url
        return self.base_url + "/" + str(path).lstrip("/")

    def as_dict(self):
        return {
            "enabled": self.enabled,
            "store_id": self.store_id,
            "friendly_name": self.friendly_name,
            "base_url": self.base_url,
            "subscription_object_guid": self.subscription_object_guid,
            "test_key": self.test_key,
            "capabilities": self.capabilities,
            "menu_image_url": self.menu_image_url,
            "large_image_url": self.large_image_url,
            "button_color": self.button_color,
            "button_text_color": self.button_text_color,
            "provider": self.provider,
            "purchases_dir": self.purchases_dir,
            "currency": self.currency,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<StoreConfig %s enabled=%s base_url=%s>" % (
            self.store_id, self.enabled, self.base_url)


def _ini_path(server_dir):
    return os.path.join(server_dir, "online_store.ini")


def candidate_dirs(server_dir=None):
    """Ordered list of directories that may hold ``online_store.ini``.

    The frozen build is why this is a search rather than a single path. In a
    PyInstaller bundle ``__file__`` for this module points into ``_internal``,
    so the "one directory up from config.py" answer that is correct for a
    source checkout resolves to somewhere the user cannot see or edit. The
    directory holding the EXE comes first for that reason: it is the one place
    a user can actually open Notepad against.
    """
    if server_dir is not None:
        return [server_dir]

    dirs = []

    def add(path):
        if path and path not in dirs and os.path.isdir(path):
            dirs.append(path)

    # Frozen: the folder the user double-clicked. First, so an edited copy
    # there wins over the one baked into _internal at build time.
    if getattr(sys, "frozen", False):
        add(os.path.dirname(os.path.abspath(sys.executable)))
    # Frozen: PyInstaller's data root, where --add-data put the bundled copy.
    add(getattr(sys, "_MEIPASS", None))
    # Unfrozen: this file is <repo>/online_store/config.py, server one up.
    add(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return dirs


def resolve_config_path(server_dir=None):
    """Return the path of the INI that will actually be read, or ``None``."""
    for directory in candidate_dirs(server_dir):
        path = _ini_path(directory)
        if os.path.exists(path):
            return path
    return None


def load_config(server_dir=None, environ=None):
    """Build a :class:`StoreConfig`.

    Never raises for a *missing* file - the store is off by default, so a
    checkout without ``online_store.ini`` behaves exactly as before this
    subsystem existed. A file that exists but is malformed does raise, because
    silently ignoring a config the user just wrote is how you get an hour of
    wondering why their store did not appear.
    """
    if environ is None:
        environ = os.environ

    values = dict(DEFAULTS)
    source = {}

    # Searched rather than assumed: see candidate_dirs() for why a frozen
    # build cannot use a single hard-coded parent directory.
    for directory in candidate_dirs(server_dir):
        path = _ini_path(directory)
        if not os.path.exists(path):
            continue
        parser = configparser.ConfigParser()
        # The INI is UTF-8; a friendly name with an accent must not blow up on
        # a machine whose ANSI codepage is not UTF-8.
        try:
            with open(path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (configparser.Error, UnicodeDecodeError) as exc:
            raise ConfigError("cannot parse %s: %s" % (path, exc))
        if parser.has_section("online_store"):
            for key, value in parser.items("online_store"):
                values[key] = value
                source[key] = path
        break   # first match wins; do not merge two different files

    # local_settings.py - the project's existing per-machine override.
    try:
        import local_settings  # git-ignored
    except Exception:
        local_settings = None
    if local_settings is not None:
        for key in list(values):
            value = getattr(local_settings, "WMP_STORE_" + key.upper(), None)
            if value is not None:
                values[key] = value
                source[key] = "local_settings.py"

    for key in list(values):
        env_name = "WMP_STORE_" + key.upper()
        if env_name in environ:
            values[key] = environ[env_name]
            source[key] = "env:" + env_name

    return StoreConfig(values, source)
