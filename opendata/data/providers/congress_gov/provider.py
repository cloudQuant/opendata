"""Provider metadata and lazy bindings for congress_gov."""

from opendata.data.provider import CredentialSpec, Provider

#: The credential face is declared because the upstream dictionary declares it
#: (``providers/congress_gov/openbb_congress_gov/__init__.py:22``, ``credentials=["api_key"]``), and
#: the environment variable name is the one ``catalog._credential`` spelled for this source. It is
#: not marked ``required_for_health``: with no fetcher bound, health answers ``not_implemented``
#: before any key is looked at, and a key would buy no request here today.
#:
#: The binding tuple is empty by measurement, not by omission: all 8 upstream rows are recorded with
#: their blocking capability in :data:`opendata.data.providers.congress_gov.specs.NOT_DECLARABLE`,
#: and a row becomes a binding the moment a ``ModelSpec`` for it is added to that module and
#: generated with ``make_http_json_fetcher``.
PROVIDER = Provider(
    source="congress_gov",
    name="Congress.gov",
    description=(
        "US Congress.gov legislative JSON adapter measured against the declarative engine: no "
        "upstream model is expressible as a single-GET JSON declaration yet, and each row's "
        "blocking capability is recorded in the package."
    ),
    website="https://api.congress.gov",
    fetcher_bindings=(),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="CONGRESS_GOV_API_KEY",
        ),
    ),
)

__all__ = ["PROVIDER"]
