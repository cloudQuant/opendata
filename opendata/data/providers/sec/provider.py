"""Provider metadata and lazy bindings for sec."""

from opendata.data.provider import Provider

#: sec's public JSON endpoints need no key, so no credential is declared. The bindings are filled
#: by the declarations in :mod:`opendata.data.providers.sec.specs`.
PROVIDER = Provider(
    source="sec",
    name="SEC",
    description="Declarative adapter for the SEC EDGAR public JSON endpoints.",
    website="https://www.sec.gov/",
    fetcher_bindings=(),
    credentials=(),
)

__all__ = ["PROVIDER"]
