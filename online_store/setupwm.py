# -*- coding: utf-8 -*-
"""Install the store the documented way: ``setup_wm.exe /DefaultService``.

Why this module exists
----------------------
The first version of this subsystem registered the store by writing a
``BASEURL`` into ``HKCU\\...\\MediaPlayer\\Services\\<keyName>`` and expecting
WMP to fetch ``<baseURL>serviceinfo.xml`` from the running server. Tested on
Windows 7, the store never appeared. The documentation says why:

* Microsoft's "Registry Keys and Entries for a Type 2 Online Store" page lists
  no such key. ``BASEURL`` appears in ``setup_wm.exe`` as a value it *reads*
  from an existing registration, not one it requires.
* ``%sserviceinfo.xml`` in that binary is the format string behind the
  ``/ServiceInfo:<path>`` switch, where ``<path>`` is a **local file path**.
  WMP is handed a file once, at install time. It is not a runtime URL fetch.
* ``TestParameter`` is not a client-side filter. The documentation says WMP
  *retrieves the test ServiceInfo document* named by the key, and that the
  provider registers test and production URLs with Microsoft. A key Microsoft
  never issued cannot resolve to our document.

The documented switch, from "Setup Command-line Parameters for Online Stores":

    setup_wm.exe /Q /R:N /DefaultService:serviceKey /ServiceInfo:documentPath

    DefaultService  required. The store's keyName. "This value must match the
                    key name in the Key attribute of the ServiceInfo element."
    ServiceInfo     required. "The fully qualified path to a ServiceInfo
                    document installed on the user's computer."

So this module writes the document to disk and invokes exactly that. It is
still not guaranteed to make an *unpublished* store visible - the test-key
gate is Microsoft's to open - but it is the documented mechanism rather than
an invention, and it is what should be tried before concluding the store
cannot appear on a given player.

The document must exist before setup_wm.exe reads it, and its ``Key``
attribute must equal ``/DefaultService:``.
"""
import os
import subprocess

from . import serviceinfo

#: Where Microsoft's setup_wm.exe lives. Both documented locations are checked
#: because the file is absent on a machine where WMP was never installed.
SETUP_WM_PATHS = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Windows Media Player", "setup_wm.exe"),
    os.path.join(os.environ.get("ProgramW6432",
                                os.environ.get("ProgramFiles", r"C:\Program Files")),
                 "Windows Media Player", "setup_wm.exe"),
)


class SetupError(Exception):
    """setup_wm.exe could not be found, or refused the registration."""


def write_service_info(config, provider=None, directory=None):
    """Write ServiceInfo.xml to disk and return its full path.

    WMP's ``/ServiceInfo`` takes a path to a document "installed on the user's
    computer", so this has to be a real file. A temp directory would work but
    would be cleared unpredictably; the same %APPDATA% location the server
    already uses for its log and certificate is used instead, so there is one
    place to look.
    """
    if directory is None:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        directory = os.path.join(base, "WMP_FAIServer", "online_store")
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = os.path.join(directory, "ServiceInfo.xml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(serviceinfo.build_service_info(config, provider))
    return path


def build_command(config, service_info_path, setup_wm=None):
    """Return the exact argument list, without running anything.

    Exposed separately so it can be asserted in tests: passing a directory name
    like "New folder (12)" is exactly what breaks a hand-built command string,
    and a list makes that impossible - each argument stays one argument and cmd
    never gets to re-parse it.
    """
    setup_wm = setup_wm or find_setup_wm()
    if not setup_wm:
        raise SetupError(
            "setup_wm.exe not found. It ships with Windows Media Player; "
            "expected it in one of: %s" % ", ".join(SETUP_WM_PATHS))
    return [setup_wm, "/Q", "/R:N",
            "/DefaultService:%s" % config.store_id,
            "/ServiceInfo:%s" % service_info_path]


def register(config, provider=None, dry_run=False, setup_wm=None):
    """Register the store via setup_wm.exe. Returns a list of result lines.

    ``dry_run`` writes the document and prints the command without executing
    it, so the whole thing can be inspected before it touches WMP's
    configuration.
    """
    lines = []
    path = write_service_info(config, provider)
    lines.append("wrote %s" % path)
    cmd = build_command(config, path, setup_wm=setup_wm)
    lines.append(("would run: " if dry_run else "running:  ") + " ".join(cmd))
    if dry_run:
        return lines
    try:
        # CREATE_NO_WINDOW: a GUI-era binary would otherwise flash a console on
        # the user's desktop in the middle of an install.
        code = subprocess.call(cmd, timeout=120,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise SetupError("could not run setup_wm.exe: %s" % exc)
    except subprocess.TimeoutExpired:
        raise SetupError("setup_wm.exe did not finish within 120s")
    lines.append("setup_wm.exe exited with %d" % code)
    if code != 0:
        raise SetupError(
            "setup_wm.exe returned %d. A non-zero exit usually means it rejected "
            "the document - check that the ServiceInfo Key attribute is %r and "
            "that the file exists at the path shown above."
            % (code, config.store_id))
    return lines


def unregister(config, dry_run=False, setup_wm=None):
    """Ask setup_wm.exe to drop the store. Returns a list of result lines.

    ``/NoService`` is setup_wm.exe's counterpart to ``/DefaultService``, read
    out of the same binary's string table. Where it does not do the whole job
    the registry entries still have to go, so this says so rather than
    implying the store is fully removed.
    """
    exe = setup_wm or find_setup_wm()
    if not exe:
        raise SetupError(
            "setup_wm.exe not found; cannot unregister through it. Delete the "
            "registry entries directly or run `uninstall-online-store`.")
    cmd = [exe, "/Q", "/R:N", "/NoService:%s" % config.store_id]
    lines = [("would run: " if dry_run else "running:  ") + " ".join(cmd)]
    if dry_run:
        return lines
    try:
        code = subprocess.call(cmd, timeout=120,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise SetupError("could not run setup_wm.exe: %s" % exc)
    lines.append("setup_wm.exe exited with %d" % code)
    lines.append("also run `uninstall-online-store` to clear the registry "
                 "entries this project wrote")
    return lines
