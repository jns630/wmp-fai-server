# -*- coding: utf-8 -*-
"""Provider registry for the Online Store subsystem.

Importing this package registers every bundled provider. A future real store
(7digital, Bandcamp, Qobuz, Juno Download) is added by writing one module here
that subclasses :class:`StoreProvider` and applying ``@register_provider`` -
no change is needed anywhere else in the subsystem.
"""
from .base import (Purchase, ProviderError, StoreProvider,
                   available_providers, create_provider, register_provider)
# Import for the registration side effect, not for its name.
from . import fake_store  # noqa: F401

__all__ = ["Purchase", "ProviderError", "StoreProvider",
           "available_providers", "create_provider", "register_provider"]
