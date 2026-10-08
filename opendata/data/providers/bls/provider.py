"""Provider metadata and lazy bindings for BLS survey search and observations."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="bls",
    name="U.S. Bureau of Labor Statistics",
    description=(
        "Public bulk files and v1 observations need no key; an optional BLS_API_KEY selects "
        "v2, without asserting v2 permission or approved data use."
    ),
    website="https://www.bls.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.bls.models.search",
            "BlsSearchFetcher",
            canonical_model_ids=("BlsSearch",),
            scenario="BLS调查目录发现与原生维度查询",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.bls.models.series",
            "BlsSeriesFetcher",
            canonical_model_ids=("BlsSeries",),
            scenario="保留原生期间及脚注的劳工统计研究",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="BLS_API_KEY",
            required_for_health=False,
        ),
    ),
)
