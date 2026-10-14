"""Provider metadata and lazy bindings for Congress.gov API v3."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="congress_gov",
    name="Congress.gov",
    description=(
        "Clean-room typed adapters for the eight Congress.gov API v3 bill, amendment, committee, "
        "text, and committee-document models. Live source verification and data-rights approval "
        "remain separate from the local contract implementation."
    ),
    website="https://api.congress.gov",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressBillsFetcher",
            canonical_model_ids=("CongressBills",),
            scenario="按国会届次和法案类型查询官方分页法案列表",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressBillInfoFetcher",
            canonical_model_ids=("CongressBillInfo",),
            scenario="查询单项法案详情及明确选择的官方子资源",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressBillTextFetcher",
            canonical_model_ids=("CongressBillText",),
            scenario="按API公布的文本版本下载法案原始文档",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressAmendmentsFetcher",
            canonical_model_ids=("CongressAmendments",),
            scenario="按届次和修正案类型查询官方分页修正案列表",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressAmendmentInfoFetcher",
            canonical_model_ids=("CongressAmendmentInfo",),
            scenario="查询单项修正案详情及明确选择的官方子资源",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressAmendmentTextFetcher",
            canonical_model_ids=("CongressAmendmentText",),
            scenario="按API公布的文本版本下载修正案原始文档",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressCommitteeInfoFetcher",
            canonical_model_ids=("CongressCommitteeInfo",),
            scenario="查询官方委员会身份、历史、分委会及活动计数",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.congress_gov.models.iter2",
            "CongressCommitteeDocumentsFetcher",
            canonical_model_ids=("CongressCommitteeDocuments",),
            scenario="查询官方委员会法案、报告、提名、通信或会议文档元数据",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="CONGRESS_GOV_API_KEY",
            required_for_health=False,
        ),
    ),
)

__all__ = ["PROVIDER"]
