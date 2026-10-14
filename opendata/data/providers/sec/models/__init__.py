"""Source-authored SEC fetcher modules and their bounded parsing helpers.

SEC's multi-stage and non-JSON endpoints use typed fetchers in this package. The declarative HTTP
engine remains separately measured by :mod:`opendata.data.providers.sec.specs`; its empty model
declaration set does not describe the package's source-owned bindings.
"""
