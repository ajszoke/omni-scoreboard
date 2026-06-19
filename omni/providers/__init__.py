"""Provider boundary: the only place raw API JSON is allowed.

Providers fetch from an upstream source (MLB StatsAPI, ESPN, ...) and return a
:class:`~omni.providers.base.ProviderUpdate` of typed domain objects. Nothing
downstream (queue, cards, renderers) ever sees raw provider JSON.
"""

from __future__ import annotations

from omni.providers.base import Provider, ProviderError, ProviderUpdate

__all__ = ["Provider", "ProviderError", "ProviderUpdate"]
