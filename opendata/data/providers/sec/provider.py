"""Provider metadata and lazy bindings for sec."""

from opendata.data.provider import Provider

#: sec needs no key, so no credential is declared. The binding tuple is empty by measurement, not
#: by omission: every one of the 24 upstream rows is recorded with its blocking capability in
#: :data:`opendata.data.providers.sec.specs.NOT_DECLARABLE`, and a row becomes a binding the moment
#: a ``ModelSpec`` for it is added to that module and generated with ``make_http_json_fetcher``.
PROVIDER = Provider(
    source="sec",
    name="SEC",
    description=(
        "EDGAR adapter measured against the declarative engine: no upstream model is expressible "
        "as a single-GET JSON declaration yet, and each row's blocker is recorded in the package."
    ),
    website="https://www.sec.gov/",
    fetcher_bindings=(),
    credentials=(),
)

__all__ = ["PROVIDER"]
