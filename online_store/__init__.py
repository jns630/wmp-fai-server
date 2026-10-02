# -*- coding: utf-8 -*-
"""Windows Media Player Online Store integration for the FAI server.

A Type 2 **commerce** store: WMP shows a tab, the tab hosts a webpage served by
this project, and the webpage is a working (fake) music storefront.

No COM component is required. Microsoft's own feature matrix marks the
IWMPSubscriptionService plug-in "No" for a Type 2 commerce store - the plug-in
exists to vend DRM licences, and a commerce store has none. Everything a
commerce store needs is a ServiceInfo XML document plus four registry entries,
and this package provides all of it.

Layout
------
``config``      validated ``[online_store]`` configuration
``models``      Album/Track/Purchase value objects
``providers``   the pluggable catalog backends (``local_fake`` ships)
``serviceinfo`` the ServiceInfo XML document WMP fetches
``registry``    install / uninstall of the WMP registry entries
``views``       the Flask Blueprint, mounted on the existing FAI app

See ``docs/ONLINE_STORE.md`` for the research notes, including which parts are
documented, which were verified against the WMP 12 binaries on Windows 11, and
which are inferred.
"""

__version__ = "1.0.0"

__all__ = ["__version__", "init_store", "main"]


def init_store(app, **kwargs):
    """Attach the Online Store to an existing Flask ``app``.

    Thin re-export of :func:`online_store.views.init_store` so the FAI server
    needs one import, not three. Returns True when the store is enabled.
    """
    from .views import init_store as _init
    return _init(app, **kwargs)


def main(argv=None):
    """Entry point for ``python "FAI Server.py" install-online-store``.

    Re-exported here so the console entry point is one stable path even as the
    internals move between modules.
    """
    from .cli import main as _main
    return _main(argv)
