# -*- coding: utf-8 -*-
"""Generate the ``ServiceInfo.xml`` document WMP reads to configure the store.

What this is
------------
WMP does not read a store's settings out of the registry when it draws the
Online Stores tab. It reads a *ServiceInfo XML document* whose URL it learns at
startup. The registry entries documented for a Type 2 store (``FriendlyName``,
``SubscriptionObjectGUID``, ``Capabilities``) identify the store and its
plug-in; the ServiceInfo document is what actually describes the pages, button
text, colours and images.

Evidence for that split, and for every field used below:

*Documented* - Microsoft's archived WMP SDK pages ("ServiceInfo Document",
"Example ServiceInfo Document for a Type 2 Online Store", "Service Task
Panes") give the element list and a complete worked example.

*Verified on this machine* - ``wmp.dll`` (Windows 11 build 10.0.26100) still
contains the literals ``ServiceInfo``, ``ServiceInfoRefresh``,
``ServiceInfoTimeout``, ``\\ServiceTask1`` and ``eAllServicesUrl_Win7``, and
``setup_wm.exe`` contains ``%sserviceinfo.xml`` and ``BASEURL``. So the
mechanism is still compiled into WMP 12 on Windows 11, not merely documented.

*Inferred* - that WMP resolves ``<baseURL>serviceinfo.xml``. ``setup_wm.exe``
holds the format string ``%sserviceinfo.xml`` next to a ``BASEURL`` value, and
``BASEURL`` is what the ``Services\\<keyName>`` registry key is documented to
carry. That is a strong inference from two adjacent literals, not a
specification. It is the single most important thing to confirm empirically
when first testing, which is why ``/online-store/serviceinfo.xml`` is served
at a predictable path and the installer writes ``BASEURL`` explicitly.

Element choice for a Type 2 *commerce* store
-------------------------------------------
Microsoft's feature matrix marks ``AlbumInfo``, ``BuyCD`` and ``InfoCenter`` as
"ignored" for a Type 2 commerce store, and ``Install`` as ignored too. They are
omitted rather than emitted-and-hoped-for, because a document containing
elements a consumer is documented to ignore is a document nobody has verified
against a real client. What remains is the documented commerce-store minimum:
``FriendlyName``, ``ServiceTask1`` with ``ButtonText``, plus the optional
``Color``, ``Image``, ``Navigate`` and ``DownloadStatus``.
"""
from xml.sax.saxutils import escape, quoteattr

#: The ``Version`` attribute of the root element. This is the value in every
#: Microsoft example and is what WMP 10/11 expect; it is not a schema version we
#: get to choose freely.
SERVICE_INFO_VERSION = "1.00"


def _attrs(attributes):
    """Render an attribute list, skipping empty values.

    quoteattr picks the quoting style and escapes correctly. Hand-rolled XML
    string building is how an ampersand in a friendly name turns into a parse
    failure that WMP reports only as "store not found".
    """
    out = ""
    for key in sorted(attributes or {}):
        value = (attributes or {})[key]
        if value is None or value == "":
            continue
        out += " %s=%s" % (key, quoteattr(str(value)))
    return out


def _leaf(name, attributes=None, indent=1):
    return "%s<%s%s/>" % ("  " * indent, name, _attrs(attributes))


def _text(name, value, indent=1):
    return "%s<%s>%s</%s>" % ("  " * indent, name, escape(str(value)), name)


def build_service_info(config, provider=None):
    """Return the ServiceInfo XML document for ``config`` as a string.

    ``config`` is an :class:`~online_store.config.StoreConfig`. ``provider`` is
    optional and unused today; it is accepted so a future Type 2 *music* store
    can add the BuyCD/AlbumInfo/InfoCenter elements without changing the
    signature every caller already uses.
    """
    base = config.base_url
    store_url = base + "/"
    out = ['<?xml version="1.0" encoding="utf-8" ?>']

    # The Key attribute must match the keyName the store is registered under.
    # Microsoft states this pairing explicitly for the /DefaultService setup
    # parameter ("This value must match the key name in the Key attribute of
    # the ServiceInfo element"), and the same pairing governs discovery from
    # the registry.
    out.append("<ServiceInfo%s>" % _attrs({
        "Version": SERVICE_INFO_VERSION,
        "Key": config.store_id,
    }))

    out.append(_text("FriendlyName", config.friendly_name, indent=1))

    # Image. Only emitted when configured: an empty URL attribute is worse than
    # no element, because a consumer that tries to fetch "" requests the
    # current page rather than falling back to a placeholder.
    if config.menu_image_url or config.large_image_url:
        out.append(_leaf("Image", {
            "MenuURL": config.menu_image_url,
            "ServiceLargeURL": config.large_image_url,
        }))

    out.append(_leaf("Color", {
        "MediaPlayer": config.button_color,
        "MediaPlayerText": config.button_text_color,
    }))

    # ServiceTask1 is the store's main page. Documented as REQUIRED for a Type
    # 2 commerce store; ServiceTask2/3 are optional and, in WMP 11 and later,
    # ignored entirely. Only one is emitted, deliberately.
    out.append("  <ServiceTask1%s>" % _attrs({"URL": store_url}))
    out.append(_text("ButtonText", config.friendly_name, indent=2))
    out.append(_text("ButtonTip", "%s - browse and buy" % config.friendly_name,
                     indent=2))
    out.append("  </ServiceTask1>")

    # Navigate is the base URL used by External.NavigateTaskPaneURL() from
    # inside the hosted page, so it is a base, not a full page URL.
    out.append(_leaf("Navigate", {"BaseURL": base + "/nav"}))

    # DownloadStatus is the "view download status" link. Pointed at a real
    # storefront page rather than omitted, so the documented element is
    # exercised rather than left unverified.
    out.append(_leaf("DownloadStatus", {"URL": base + "/downloads"}))

    out.append("</ServiceInfo>")
    return "\n".join(out) + "\n"
