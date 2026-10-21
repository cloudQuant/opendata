"""Provider metadata and lazy binding for Biztoc world news."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider
from opendata.data.providers.biztoc._source import CREDENTIAL_ENV_VAR

PROVIDER = Provider(
    source="biztoc",
    name="Biztoc",
    description=(
        "Clean-room Biztoc world-news adapter over the public RapidAPI feed; the key is read "
        "from BIZTOC_API_KEY and sent only in the X-RapidAPI-Key header."
    ),
    website="https://api.biztoc.com",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.biztoc.models.world_news",
            "BiztocWorldNewsFetcher",
            canonical_model_ids=("WorldNews",),
            scenario="Biztoc 世界新闻：按搜索词、发布源或最新 feed 返回文章行",
        ),
    ),
    credentials=(
        CredentialSpec(name="api_key", environment_variable=CREDENTIAL_ENV_VAR),
    ),
)
