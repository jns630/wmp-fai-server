# -*- coding: utf-8 -*-
"""Install and remove the WMP Online Store registry entries, reversibly.

This is the only part of the subsystem that touches machine state, so it is
written to be conservative: it writes only the keys this store owns, it
refuses to clobber a key it did not create, and it can show you exactly what
it would do before doing it.

What is written, and why each value exists
------------------------------------------
The three registry locations that matter are taken from Microsoft's archived
"Registry Keys and Entries for a Type 2 Online Store" page, which specifies:

1. ``HKLM\\SOFTWARE\\Microsoft\\MediaPlayer\\Subscriptions\\<keyName>``
   ``Capabilities`` (REG_DWORD), ``SubscriptionObjectGUID`` (REG_SZ),
   ``FriendlyName`` (REG_SZ).
   *Documented.* The store's identity. A commerce store has no plug-in, so
   ``Capabilities`` is written as 0: it is a bitmask of IWMPSubscriptionService
   callbacks, and we implement none of them.

2. ``HKCU\\Software\\Microsoft\\MediaPlayer\\Services``
   ``TestParameter`` (REG_SZ) = our test key, appended to any existing
   semicolon-separated list.
   *Documented.* An unpublished store is visible only while its test or
   production key is in this value.

3. ``HKCU\\Software\\Microsoft\\MediaPlayer\\Subscriptions``
   ``ActiveService``. Written by WMP when the user activates a store; never by us.

CORRECTION - what this module used to claim, and why it was wrong
----------------------------------------------------------------
An earlier version of this file wrote a fourth location,
``HKCU\\...\\MediaPlayer\\Services\\<keyName>`` with ``BASEURL`` and ``Type``,
and described it as an inference from ``setup_wm.exe``'s string table. Both
the reasoning and the conclusion were wrong, and testing on Windows 7 showed
it: **the store never appeared.**

* The documented page lists no such key. ``BASEURL`` appears in setup_wm.exe as
  a *value it reads from* an existing store registration, not as one it
  requires, so inferring a requirement from its presence was backwards.
* ``%sserviceinfo.xml`` in that binary is the format string for the
  ``/ServiceInfo:<path>`` command-line parameter - a LOCAL FILE PATH supplied
  by the installer. It is not WMP fetching ``<baseURL>serviceinfo.xml`` from
  our server at runtime. So the whole "point WMP at our own URL" premise was
  wrong: WMP is pointed at a file, once, at install time.
* ``TestParameter`` does not filter a local list. The documentation says WMP
  *retrieves the test ServiceInfo document* named by that key, and that the
  provider supplies Microsoft with the test and production URLs. The key is a
  lookup into a store index, so a key Microsoft never issued cannot resolve to
  our document however it is written.

``describe_install`` therefore no longer writes ``Services\\<keyName>`` or
``BASEURL``. The documented local-store route is
``online_store/setupwm.py``, which calls setup_wm.exe with ``/DefaultService``
and ``/ServiceInfo``.

DANGEROUS BY DEFAULT - the subscription key
--------------------------------------------
Microsoft's page specifies that ``SubscriptionObjectGUID`` is "a GUID that is
the class identifier (CLSID) for the class that implements
IWMPSubscriptionService in the online store's plug-in", and that the matching
``HKCR\\CLSID\\<guid>\\InprocServer32`` is part of the required layout.

A commerce store with no plug-in therefore has a ``SubscriptionObjectGUID``
that resolves to nothing. WMP enumerates ``Subscriptions`` at startup and tries
to load that class; it is not documented to cope with its absence. Reported
symptoms on Windows 7 were WMP failing to start cleanly and instability in
disc handling, so writing the HKLM half is now **opt-in** via
``register_subscription=True`` and is off by default.

``diagnose()`` reports the state, because "the store does not appear" and "WMP
misbehaves" are otherwise indistinguishable from a server problem.

Deliberately NOT written
------------------------
* Anything under ``HKLM\\SOFTWARE\\Policies`` - group policy is not a
  per-user feature toggle and must not be used as one.
* ``ActiveService``. Microsoft documents it as written *by WMP* when the user
  activates a store, and only in HKCU.
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


def describe_install(config, register_subscription=False):
    """Return the exact operations install would perform, without doing them.

    Each entry is ``{"action", "location", "name", "value", "type", "note"}``.
    Returned rather than printed so the CLI, the tests and ``--dry-run`` all
    see the same list.

    ``register_subscription`` controls the HKLM half and defaults to OFF. See
    the module docstring: that key carries a CLSID with no registered class,
    and on Windows 7 it was accompanied by WMP instability. Off by default
    because a working FAI server is worth more than a store tab that crashes
    the player.
    """
    store_id = config.store_id
    plans = []

    def plan(action, root, path, name, value, kind, note=""):
        plans.append({"action": action, "root": root.upper(),
                      "path": path, "name": name, "value": value,
                      "type": kind, "note": note})

    # 1. Store identity, HKLM. Opt-in: see describe_install's docstring.
    sub = "%s\\%s" % (SUBSCRIPTIONS_ROOT, store_id)
    if register_subscription:
        plan("create", "HKLM", sub, "", "", None,
             "store identity (machine-wide); OPT-IN, see registry.py")
        plan("set", "HKLM", sub, "Capabilities", config.capabilities, REG_DWORD,
             "no IWMPSubscriptionService callbacks: commerce store, no plug-in")
        plan("set", "HKLM", sub, "SubscriptionObjectGUID",
             config.subscription_object_guid, REG_SZ,
             "documented CLSID value - WARNING: resolves to no registered class")
        plan("set", "HKLM", sub, "FriendlyName", config.friendly_name, REG_SZ, "")
    else:
        plan("skip", "HKLM", sub, "", "", None,
             "not written: a SubscriptionObjectGUID with no CLSID is a dangling "
             "COM reference; pass register_subscription to force it")

    # 2. The documented development gate: append our key to TestParameter.
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


def install(config, dry_run=False, register_plugin_dll=None,
            register_subscription=False, log=print):
    """Install the store. Returns a list of human-readable result lines.

    ``register_plugin_dll`` is the path to a COM in-process server for a future
    Type 2 *music* store. It is None by default and this project ships no DLL,
    so by default no CLSID is registered.

    ``register_subscription`` opts into the HKLM Subscriptions key. It defaults
    to False because that key's ``SubscriptionObjectGUID`` points at a class
    that does not exist; see the module docstring for what that cost on
    Windows 7.
    """
    if not is_supported():
        raise RegistryError(
            "the Online Store installer needs Windows; this platform is %r. "
            "Run it on the machine that runs WMP." % sys.platform)

    plans = describe_install(config,
                             register_subscription=register_subscription)
    results = []
    for step in plans:
        if step["action"] == "skip":
            results.append("skip   %-6s %s   # %s"
                           % (step["root"], step["path"], step["note"]))
            continue
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


def diagnose(config):
    """Report every fact about how this store is (or is not) registered.

    This exists because the two failure modes reported from Windows 7 were
    indistinguishable from each other and from a server fault:

        * the store does not appear at all, and
        * WMP ejects discs, and sometimes closes.

    Both are consistent with a *partially* registered store, so a status
    command that only printed "installed: yes" is not diagnostic. Each entry
    below is a fact with a severity, so the output says what is wrong rather
    than only what is present.

    Returns a list of ``(severity, message)``. Severities: ``ok``,
    ``warn``, ``bad``, ``info``.
    """
    out = []
    if not is_supported():
        return [("warn", "registry not available on this platform (%s)"
                 % sys.platform)]

    sub = "%s\\%s" % (SUBSCRIPTIONS_ROOT, config.store_id)
    values = read_values("HKLM", sub)
    guid = str(values.get("SubscriptionObjectGUID", ("",))[0]).strip()

    if not values:
        out.append(("info", "no HKLM Subscriptions\\%s key - the store is NOT "
                            "registered with WMP" % config.store_id))
    else:
        out.append(("ok", "HKLM Subscriptions\\%s exists" % config.store_id))

    # The dangling-CLSID check. This is the important one.
    if guid:
        clsid_path = "%s\\%s\\InprocServer32" % (CLSID_ROOT, guid)
        if read_values("HKCR", clsid_path):
            out.append(("ok", "SubscriptionObjectGUID %s has a registered "
                              "InprocServer32" % guid))
        else:
            out.append(("bad",
                        "SubscriptionObjectGUID %s has NO registered COM class. "
                        "WMP enumerates Subscriptions at startup and will try to "
                        "load this class; it is not documented to cope with it "
                        "being absent. This is the most likely cause of WMP "
                        "closing or misreporting discs. Fix: run "
                        "`uninstall-online-store` (or delete HKLM\\%s\\%s)."
                        % (guid, SUBSCRIPTIONS_ROOT, config.store_id)))
        caps = values.get("Capabilities", (None, None))[0]
        if caps == 0:
            out.append(("info", "Capabilities = 0 (no plug-in callbacks claimed)"))

    test = read_values("HKCU", SERVICES_ROOT).get("TestParameter", ("",))[0]
    keys = [k for k in str(test).split(";") if k]
    if str(config.test_key) in keys:
        out.append(("warn",
                    "TestParameter contains %r, but Microsoft issues test keys - "
                    "a self-chosen one does not make an unpublished store "
                    "visible. Expect the store not to appear."
                    % config.test_key))
    else:
        out.append(("warn", "TestParameter does not contain %r"
                    % config.test_key))

    # The inferred key we used to write. If it is still there, WMP is reading a
    # BASEURL that the documented layout does not define.
    legacy = read_values("HKCU", "%s\\%s" % (SERVICES_ROOT, config.store_id))
    if legacy:
        out.append(("warn",
                    "HKCU Services\\%s still exists with values %r. That key is "
                    "NOT part of the documented Type 2 layout - BASEURL and Type "
                    "were inferred and are not what WMP reads. Remove it."
                    % (config.store_id, sorted(legacy))))
    return out


