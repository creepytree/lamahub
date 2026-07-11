"""Ollama endpoint registry parsed from the OLLAMA_URL environment variable.

OLLAMA_URL accepts either a single URL (backward compatible) or a
comma-separated list of endpoints, each optionally labeled as ``Name=URL``::

    OLLAMA_URL=http://localhost:11434
    OLLAMA_URL=Dev=http://dev.ollama,Prod=http://prod.ollama

The first endpoint is the default: it is badged ``[Default]`` in the UI and is
the only endpoint where FIXED_MODELS are managed (pulled at startup, protected
from deletion). Unlabeled entries fall back to a label derived from the host.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from lamahub.env import env


@dataclass(frozen=True)
class Endpoint:
    """A single Ollama server the UI can target."""

    name: str
    url: str


def _host_label(url: str) -> str:
    """Derive a human label from a URL's host when none was provided."""
    return urlparse(url).hostname or url


def _parse(raw: str) -> list[Endpoint]:
    """Parse the OLLAMA_URL string into an ordered, de-duplicated endpoint list."""
    endpoints: list[Endpoint] = []
    seen: set[str] = set()

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue

        # The first "=" separates an optional label from the URL. Base URLs do
        # not contain "=", so a single split is unambiguous.
        if "=" in item:
            name, _, url = item.partition("=")
            name, url = name.strip(), url.strip()
        else:
            name, url = "", item

        url = url.rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        endpoints.append(Endpoint(name=name or _host_label(url), url=url))

    if not endpoints:
        endpoints.append(Endpoint(name="default", url="http://localhost:11434"))

    return endpoints


class EndpointRegistry:
    """Holds the configured endpoints and resolves requested URLs safely."""

    def __init__(self, endpoints: list[Endpoint]):
        self._endpoints = endpoints
        self._by_url = {endpoint.url: endpoint for endpoint in endpoints}

    @property
    def endpoints(self) -> list[Endpoint]:
        return list(self._endpoints)

    @property
    def default(self) -> Endpoint:
        return self._endpoints[0]

    def resolve(self, url: str | None) -> Endpoint:
        """Return the configured endpoint matching ``url``, or the default.

        Only URLs present in the configured allowlist are honored; anything
        unknown (or missing) falls back to the default endpoint.
        """
        if url:
            match = self._by_url.get(url.rstrip("/"))
            if match:
                return match
        return self.default

    def is_default(self, url: str | None) -> bool:
        return self.resolve(url).url == self.default.url


registry = EndpointRegistry(_parse(env.ollama_url))
