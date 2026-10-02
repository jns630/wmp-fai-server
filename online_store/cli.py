# -*- coding: utf-8 -*-
"""``install-online-store`` / ``uninstall-online-store`` / ``online-store-status``.

Deliberately dependency-free (``argparse`` only) so it runs from the source
checkout, from the frozen EXE, and on a machine with nothing but Python.

The ``--dry-run`` flag on both mutating commands is not a nicety. Installing
writes to ``HKLM``, which needs Administrator, and a user who is not sure what
a command will do to their registry should be able to find out for free.
"""
import argparse
import importlib
import json
import sys

from . import registry
from .config import ConfigError, load_config
from .providers import available_providers

COMMANDS = ("install-online-store", "uninstall-online-store",
            "online-store-status")


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="online-store",
        description="Manage the WMP Online Store registration for this server.")
    parser.add_argument("--store-dir", default=None,
                        help="directory holding online_store.ini "
                             "(default: the server's own directory)")
    subs = parser.add_subparsers(dest="command")

    install = subs.add_parser(
        "install-online-store",
        help="write the registry entries WMP needs to find this store")
    install.add_argument("--dry-run", action="store_true",
                         help="print every change without writing anything")
    install.add_argument("--register-plugin-dll", default=None, metavar="DLL",
                         help="also register this COM in-process server under "
                              "the store CLSID. Only for a future Type 2 music "
                              "store that actually ships a plug-in; this "
                              "project ships none.")

    uninstall = subs.add_parser(
        "uninstall-online-store",
        help="remove the entries install-online-store added")
    uninstall.add_argument("--dry-run", action="store_true",
                           help="print every change without changing anything")
    uninstall.add_argument("--remove-clsid", action="store_true",
                           help="also delete the CLSID key. Off by default so "
                                "a CLSID shared with other software is never "
                                "destroyed.")

    status = subs.add_parser(
        "online-store-status",
        help="show the effective config and what is currently installed")
    status.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser


def _print_lines(lines, prefix="  "):
    for line in lines:
        print(prefix + line)


def _discogs_status():
    """Report whether Discogs credentials were found, never the token itself.

    Returns None when the server module cannot be found at all, so this stays
    usable as a library: the online_store package is importable on its own, in
    tests, and from a checkout where the server entry point is absent.

    __main__ is checked FIRST, and that is not a stylistic choice. In a frozen
    build the entry point is not importable as "FAI Server" - it runs as
    __main__, so import_module("FAI Server") raises and this returns None. That
    is precisely the case that matters, being the one where a user is told
    nothing. Checking __main__ first also avoids loading a SECOND copy of the
    server module inside a process that is already running it.
    """
    import sys as _sys

    server = _sys.modules.get("__main__")
    for name in ("FAI Server",):
        if server is None or not hasattr(server, "DISCOGS_TOKEN"):
            try:
                server = importlib.import_module(name)
            except Exception:
                server = None
    token = (getattr(server, "DISCOGS_TOKEN", "") or "").strip()
    if server is None and not hasattr(_sys.modules.get("__main__", object()),
                                      "DISCOGS_TOKEN"):
        # Nothing that looks like the server module: report nothing rather than
        # claiming Discogs is unconfigured, which would be a false accusation.
        return None
    return {"configured": bool(token), "token_length": len(token)}


def _status_payload(config):
    payload = {
        "providers_registered": available_providers(),
        "registry_supported": registry.is_supported(),
        # Imported lazily: online_store must stay importable on its own, and the
        # Discogs credentials live in the server module, not in this package.
        "discogs": _discogs_status(),
    }
    if config is None:
        return payload
    payload["config"] = config.as_dict()
    payload["config_sources"] = config.source
    payload["serviceinfo_url"] = config.url_for("serviceinfo.xml")
    payload["would_write"] = [
        "%s %s %s%s = %r" % (step["action"], step["root"], step["path"],
                             ("\\" + step["name"]) if step["name"] else "",
                             step["value"])
        for step in registry.describe_install(config)]
    if registry.is_supported():
        payload["installed"] = {
            "subscriptions": registry.read_values(
                "HKLM", "%s\\%s" % (registry.SUBSCRIPTIONS_ROOT, config.store_id)),
            "services": registry.read_values(
                "HKCU", "%s\\%s" % (registry.SERVICES_ROOT, config.store_id)),
            "test_parameter": registry.read_values(
                "HKCU", registry.SERVICES_ROOT).get("TestParameter", ("",))[0],
        }
    return payload


def _print_status(payload, config):
    print("Online Store status")
    print("=" * 60)
    if config is None:
        print("  no usable configuration (check online_store.ini)")
    else:
        print("  store id       : %s" % config.store_id)
        print("  friendly name  : %s" % config.friendly_name)
        print("  enabled        : %s" % config.enabled)
        print("  base url       : %s" % config.base_url)
        print("  serviceinfo    : %s" % config.url_for("serviceinfo.xml"))
        print("  provider       : %s" % config.provider)
        print("  object guid    : %s" % config.subscription_object_guid)
        print("  test key       : %s" % config.test_key)
    print("  providers      : %s" % ", ".join(payload["providers_registered"]))
    print("  registry write : %s"
          % ("available" if payload["registry_supported"]
             else "NOT available on this platform"))
    # Discogs is reported here because its failure mode is a provider that is
    # simply MISSING rather than one that errors: with no token every Discogs
    # code path returns empty, so the album dialog just shows no Discogs
    # results. That is indistinguishable from "Discogs has nothing for this
    # album", and it is how a frozen build without local_settings.py beside the
    # EXE shipped with Discogs silently switched off. Saying it out loud turns
    # an invisible absence into a one-line check. The token itself is never
    # printed, only whether one was found.
    discogs = payload.get("discogs")
    if discogs:
        print("  discogs        : %s" % ("configured"
                                        if discogs["configured"]
                                        else "NO TOKEN - not configured"))
        if not discogs["configured"]:
            print("                    set DISCOGS_TOKEN, or put local_settings.py "
                  "with DISCOGS_TOKEN = '...' next to the .exe")
    installed = payload.get("installed")
    if installed:
        print("")
        print("  Currently registered:")
        for label in ("subscriptions", "services"):
            values = installed[label]
            print("    %-13s %s" % (label + ":",
                                    ("; ".join("%s=%r" % (k, v[0])
                                               for k, v in values.items())
                                     or "(key absent)")))
        print("    %-13s %r" % ("TestParameter:", installed["test_parameter"]))
    print("")
    print("  Install would do:")
    _print_lines(payload.get("would_write", []))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print("")
        print("Available commands: %s" % ", ".join(COMMANDS))
        return 2

    try:
        config = load_config(server_dir=args.store_dir)
    except ConfigError as exc:
        print("Configuration error: %s" % exc)
        return 2

    if args.command == "online-store-status":
        payload = _status_payload(config)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            _print_status(payload, config)
        return 0

    if args.command == "install-online-store":
        if not config.enabled:
            print("Refusing to install: enabled = false in the config.")
            print("Set 'enabled = true' in online_store.ini first - the store "
                  "must be running before WMP is pointed at it.")
            return 2
        verb = "Would install" if args.dry_run else "Installing"
        print("%s %r at %s" % (verb, config.friendly_name, config.base_url))
        try:
            _print_lines(registry.install(
                config, dry_run=args.dry_run,
                register_plugin_dll=args.register_plugin_dll))
        except registry.RegistryError as exc:
            print("\n%s" % exc)
            return 1
        if not args.dry_run:
            print("")
            print("Done. Start the server, then open WMP's Online Stores tab.")
            print("Check /online-store/api/status to confirm WMP can reach the "
                  "ServiceInfo document.")
        return 0

    if args.command == "uninstall-online-store":
        print("%s %r"
              % ("Would uninstall" if args.dry_run else "Uninstalling",
                 config.friendly_name))
        try:
            _print_lines(registry.uninstall(
                config, dry_run=args.dry_run, remove_clsid=args.remove_clsid))
        except registry.RegistryError as exc:
            print("\n%s" % exc)
            return 1
        if not args.dry_run:
            print("")
            print("Done. Unrelated Windows Media Player settings were not "
                  "touched.")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

