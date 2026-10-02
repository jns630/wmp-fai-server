# -*- coding: utf-8 -*-
"""Install and remove the WMP Online Store registry entries, reversibly.

This is the only part of the subsystem that touches machine state, so it is
written to be conservative: it writes only the keys this store owns, it
refuses to clobber a key it did not create, and it can show you exactly what
it would do before doing it.

What is written, and why each value exists
------------------------------------------
All four locations below are taken from Microsoft's archived "Registry Keys and
Entries for a Type 2 Online Store" page. That page documents three of them
directly. The fourth - ``Services\\<keyName>`` with its ``BASEURL`` - is marked
below as inferred, because it is how this project points WMP at a ServiceInfo
document served by a local Flask process rather than by Microsoft's CDN.

1. ``HKLM\\SOFTWARE\\Microsoft\\MediaPlayer\\Subscriptions\\<keyName>``
   ``Capabilities`` (REG_DWORD), ``SubscriptionObjectGUID`` (REG_SZ),
   ``FriendlyName`` (REG_SZ).
   *Documented.* The store's identity. A commerce store has no plug-in, so
   ``Capabilities`` is written as 0: it is a bitmask of IWMPSubscriptionService
   callbacks, and we implement none of them. Writing a non-zero mask for
   interfaces we do not implement would have WMP call into nothing.

2. ``HKCU\\Software\\Microsoft\\MediaPlayer\\Services``
   ``TestParameter`` (REG_SZ) = our test key, appended to any existing
   semicolon-separated list.
   *Documented.* This is the documented development gate: a store not
   published by Microsoft is visible only while its test or production key is
   in this value.

3. ``HKCU\\Software\\Microsoft\\MediaPlayer\\Services\\<keyName>``
   ``BASEURL``, ``FriendlyName``, and the cosmetic values
   ``ColorPlayer``, ``ColorPlayerText``, ``ImageMenuURL``, ``ImageLargeURL``,
   ``ImageSmallURL``, ``Task1ButtonText``, ``Task1ButtonTip``, ``Type``.
   *Partly inferred.* Every one of these value names is present in
   ``setup_wm.exe`` on this machine (verified by reading its strings), and
   ``setup_wm.exe`` is Microsoft's own code for installing a store from a local
   ServiceInfo document via ``/DefaultService``. The page that documents
   ``BASEURL`` for a general store is not in the public SDK documentation, so
   the pairing of key to value names is an inference from the binary. It is
   isolated in this one function so it is easy to correct if WMP turns out to
   want something else.

4. ``HKCR\\CLSID\\<SubscriptionObjectGUID>`` with ``InprocServer32`` and
   ``ThreadingModel = Apartment``.
   *Documented but deliberately NOT written by default.* The documented layout
   includes it because a Type 2 *music* store has a COM plug-in. We are a
   commerce store with no plug-in, so registering a CLSID pointing at a
   non-existent DLL would be a lie that WMP might act on. The key is available
   behind ``register_plugin_dll=`` for a future music store that genuinely
   ships one.

Deliberately NOT written
------------------------
* Anything under ``HKLM\\SOFTWARE\\Policies`` - group policy is not a
  per-user feature toggle and must not be used as one.
* ``ActiveService``. Microsoft documents it as written *by WMP* when the user
  activates a store, and only in HKCU. Overwriting it would be the installer
  impersonating the user; the ``--set-active`` flag exists but is separate and
  says so.
* Any WMP setting that is not part of the store's own registration.
"""
import os
import sys

try:
    import winreg
except ImportError:                                   # pragma: no cover
    winreg = None                                     # non-Windows: see is_supported()

# Documented locations.
SUBSCRIPTIONS_ROOT = r"SOFTWARE\Microsoft\MediaPlayer\Subscriptions"
SERVICES_ROOT = r"Software\Microsoft\MediaPlayer\Services"

#: Where a plug-in DLL would be registered. Unused unless explicitly requested.
CLSID_ROOT = r"CLSID"

#: REG_DWORD for the plug-in capability mask. Zero: see the module docstring.
#: (Microsoft's default for "no flags registered" is SUBSCRIPTION_V1_CAPS 0xF,
#: but that is the value to use when a v1 plug-in exists and is silent about
#: capabilities. A store with no plug-in at all must advertise none, or WMP
#: will call allowPlay/allowCDBurn/... on a class that does not implement them.)
CAPABILITIES_NONE = 0

#: The "Type" value setup_wm.exe writes for a Type 2 store. Read out of the
#: binary's string table; the value it expects for a Type 2 store is 2.
#: Inferred - the public documentation never states the number.
STORE_TYPE_TYPE2 = 2

REG_SZ = 1
REG_DWORD = 4


def is_supported():
    """True when this module can actually write registry keys."""
    return winreg is not None and os.name == "nt"


class RegistryError(Exception):
    """A registry operation failed, or was refused on purpose."""


def _hkey(root):
    """Map a hive name to a HKEY constant.

    Case-insensitive on purpose: the public helpers take the root as
    ``"HKLM"``/``"HKCU"``/``"HKCR"`` (that is how registry paths are written in
    every Microsoft document and in ``reg query`` output) but ``describe_*``
    upper-cases the value it echoes back. A lookup that cared about the case
    would fail on the second call and nowhere else, which is a miserable bug.
    """
    name = str(root).strip().lower()
    roots = {"hklm": winreg.HKEY_LOCAL_MACHINE,
             "hkcu": winreg.HKEY_CURRENT_USER,
             "hkcr": winreg.HKEY_CLASSES_ROOT}
    if name not in roots:
        raise RegistryError("unknown registry root %r (expected HKLM, HKCU or "
                            "HKCR)" % (root,))
    return roots[name]


def _open(root, path, access):
    return winreg.OpenKey(_hkey(root), path, 0, access)


def _create(root, path):
    return winreg.CreateKey(_hkey(root), path)


def read_values(root, path):
    """Return ``{name: (value, type)}`` for a key, or ``{}`` if it is absent."""
    if not is_supported():
        return {}
    try:
        with _open(root, path, winreg.KEY_READ) as key:
            out = {}
            index = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                out[name] = (value, kind)
                index += 1
            return out
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RegistryError("cannot read %s\\%s: %s" % (root.upper(), path, exc))


def describe_install(config):
    """Return the exact operations install would perform, without doing them.

    Each entry is ``{"action", "location", "name", "value", "type", "note"}``.
    Returned rather than printed so the CLI, the tests and ``--dry-run`` all
    see the same list.
    """
    store_id = config.store_id
    plans = []

    def plan(action, root, path, name, value, kind, note=""):
        plans.append({"action": action, "root": root.upper(),
                      "path": path, "name": name, "value": value,
                      "type": kind, "note": note})

    # 1. Store identity, HKLM.
    sub = "%s\\%s" % (SUBSCRIPTIONS_ROOT, store_id)
    plan("create", "HKLM", sub, "", "", None, "store identity (machine-wide)")
    plan("set", "HKLM", sub, "Capabilities", config.capabilities, REG_DWORD,
         "no IWMPSubscriptionService callbacks: commerce store, no plug-in")
    plan("set", "HKLM", sub, "SubscriptionObjectGUID",
         config.subscription_object_guid, REG_SZ, "documented CLSID value")
    plan("set", "HKLM", sub, "FriendlyName", config.friendly_name, REG_SZ, "")

    # 2. Self-hosted ServiceInfo location (inferred from setup_wm.exe strings).
    svc = "%s\\%s" % (SERVICES_ROOT, store_id)
    plan("create", "HKCU", svc, "", "", None,
         "points WMP at our own ServiceInfo.xml (inferred)")
    plan("set", "HKCU", svc, "BASEURL", config.base_url + "/", REG_SZ,
         "WMP appends 'serviceinfo.xml' to this")
    plan("set", "HKCU", svc, "FriendlyName", config.friendly_name, REG_SZ, "")
    plan("set", "HKCU", svc, "Type", STORE_TYPE_TYPE2, REG_DWORD,
         "Type 2 store (inferred constant)")
    plan("set", "HKCU", svc, "ColorPlayer", config.button_color, REG_SZ, "")
    plan("set", "HKCU", svc, "ColorPlayerText", config.button_text_color,
         REG_SZ, "")
    plan("set", "HKCU", svc, "Task1ButtonText", config.friendly_name, REG_SZ, "")
    plan("set", "HKCU", svc, "Task1ButtonTip",
         "%s - browse and buy" % config.friendly_name, REG_SZ, "")
    for value_name, configured in (("ImageMenuURL", config.menu_image_url),
                                   ("ImageLargeURL", config.large_image_url)):
        if configured:
            plan("set", "HKCU", svc, value_name, configured, REG_SZ, "")

    # 3. The documented development gate: append our key to TestParameter.
    existing = read_values("HKCU", SERVICES_ROOT).get("TestParameter", ("",))[0]
    keys = [part for part in str(existing).split(";") if part]
    keys.append(config.test_key)
    plan("set", "HKCU", SERVICES_ROOT, "TestParameter", ";".join(keys),
         REG_SZ, "makes an unpublished store visible to WMP (documented)")

    return plans


def _set_value(root, path, name, value, kind):
    with _create(root, path) as key:
        if name:
            winreg.SetValueEx(key, name, 0, kind, value)


def install(config, dry_run=False, register_plugin_dll=None, log=print):
    """Install the store. Returns a list of human-readable result lines.

    ``register_plugin_dll`` is the path to a COM in-process server for a future
    Type 2 *music* store. It is None by default and this project ships no DLL,
    so by default no CLSID is registered.
    """
    if not is_supported():
        raise RegistryError(
            "the Online Store installer needs Windows; this platform is %r. "
            "Run it on the machine that runs WMP." % sys.platform)

    plans = describe_install(config)
    results = []
    for step in plans:
        line = "%-5s %-6s %s%s = %r" % (
            step["action"], step["root"], step["path"],
            ("\\" + step["name"]) if step["name"] else "", step["value"])
        if step["note"]:
            line += "   # %s" % step["note"]
        results.append(line)
        if dry_run or step["action"] != "set":
            continue
        try:
            _set_value(step["root"], step["path"], step["name"],
                       step["value"], step["type"])
        except OSError as exc:
            raise RegistryError(
                "failed writing %s\\%s\\%s: %s\n"
                "Installing the HKLM half needs an elevated (Administrator) "
                "console. Re-run from one, or use --dry-run to preview."
                % (step["root"], step["path"], step["name"], exc))

    if register_plugin_dll:
        clsid = config.subscription_object_guid
        path = "%s\\%s" % (CLSID_ROOT, clsid)
        results.append("create HKCR  %s" % path)
        results.append("set    HKCR  %s\\InprocServer32 = %r"
                       % (path, register_plugin_dll))
        if not dry_run:
            try:
                _set_value("HKCR", path, "", register_plugin_dll, REG_SZ)
                _set_value("HKCR", path + "\\InprocServer32", "",
                           register_plugin_dll, REG_SZ)
                _set_value("HKCR", path + "\\InprocServer32",
                           "ThreadingModel", "Apartment", REG_SZ)
            except OSError as exc:
                raise RegistryError("failed registering CLSID %s: %s"
                                    % (clsid, exc))
    return results


def describe_uninstall(config):
    """Return the exact operations uninstall would perform, without doing them."""
    store_id = config.store_id
    plans = [
        {"action": "remove-key", "root": "HKCU",
         "path": "%s\\%s" % (SERVICES_ROOT, store_id), "name": "", "value": None,
         "type": None,
         "note": "self-hosted store settings; only ours is deleted"},
        {"action": "remove-key", "root": "HKLM",
         "path": "%s\\%s" % (SUBSCRIPTIONS_ROOT, store_id), "name": "",
         "value": None, "type": None, "note": "store identity"},
        {"action": "remove-key", "root": "HKCR",
         "path": "%s\\%s" % (CLSID_ROOT, config.subscription_object_guid),
         "name": "", "value": None, "type": None,
         "note": "only if it is empty of other values (see uninstall)"},
        {"action": "edit", "root": "HKCU", "path": SERVICES_ROOT,
         "name": "TestParameter", "value": "<our key removed>", "type": REG_SZ,
         "note": "other stores' keys are preserved"},
    ]
    return plans


def _delete_tree(root, path):
    """Delete a key and its subkeys. Returns True if anything was removed."""
    if not is_supported():
        return False
    try:
        key = _open(root, path, winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RegistryError("cannot open %s\\%s: %s" % (root.upper(), path, exc))
    with key:
        subkeys = []
        index = 0
        while True:
            try:
                subkeys.append(winreg.EnumKey(key, index))
            except OSError:
                break
            index += 1
        for name in subkeys:
            _delete_tree(root, path + "\\" + name)
    try:
        winreg.DeleteKey(_hkey(root), path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RegistryError("cannot delete %s\\%s: %s" % (root.upper(), path, exc))


def _remove_test_key(store_key):
    """Remove only our key from TestParameter, preserving any others.

    This is the value most likely to be shared: another store's test key may
    already be in the list, and blanking the whole value would silently hide
    that store. So the list is re-joined minus our entry, and the value is only
    deleted outright when nothing but our key was in it.
    """
    current = read_values("HKCU", SERVICES_ROOT).get("TestParameter", ("",))[0]
    keys = [part for part in str(current).split(";") if part]
    remaining = [part for part in keys if part != str(store_key)]
    if remaining == keys:
        return "unchanged (our key %r was not present)" % store_key
    if not remaining:
        # We were the only key in the list, so the value itself goes. The
        # Services key is left in place: it may hold values we never wrote.
        try:
            with _open("HKCU", SERVICES_ROOT, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "TestParameter")
        except FileNotFoundError:
            return "unchanged (Services key absent)"
        except OSError as exc:
            raise RegistryError("cannot delete TestParameter: %s" % exc)
        return "removed TestParameter entirely (we were the only key)"
    _set_value("HKCU", SERVICES_ROOT, "TestParameter", ";".join(remaining),
               REG_SZ)
    return "TestParameter now %r (kept %d other store key(s))" % (
        ";".join(remaining), len(remaining))


def uninstall(config, dry_run=False, remove_clsid=False, log=print):
    """Remove everything install added, leaving unrelated WMP settings alone.

    ``remove_clsid`` defaults to False. The CLSID key is only deleted when it
    is asked for *and* it holds nothing but our own plug-in values - a CLSID
    that some other software also registered is left alone rather than
    destroyed.
    """
    if not is_supported():
        raise RegistryError(
            "the Online Store uninstaller needs Windows; this platform is %r."
            % sys.platform)

    results = []
    store_id = config.store_id

    for step in describe_uninstall(config):
        if step["action"] == "remove-key" and step["root"] == "HKCR" \
                and not remove_clsid:
            results.append("skip   HKCR  %s  # not requested; a shared CLSID is "
                           "never deleted by default" % step["path"])
            continue
        line = "%-6s %-5s %s" % (step["action"], step["root"], step["path"])
        results.append(line)
        if dry_run:
            continue
        if step["action"] == "remove-key":
            removed = _delete_tree(step["root"], step["path"])
            results.append("       -> %s"
                           % ("deleted" if removed else "was not present"))

    if not dry_run:
        results.append("       -> %s" % _remove_test_key(config.test_key))
    else:
        results.append("       -> would remove %r from TestParameter"
                       % config.test_key)
    return results


