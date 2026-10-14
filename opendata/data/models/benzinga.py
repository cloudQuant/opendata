"""Typed public contracts for Benzinga analyst ratings and news."""

from __future__ import annotations

from datetime import date, datetime, time

from pydantic import StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class BenzingaPriceTarget(ContractModel):
    """One analyst rating action on one ticker, from Benzinga's Ratings API."""

    symbol: str | None = None
    published_date: date | None = None
    # The source publishes a bare clock time; no timezone is implied by this value.
    published_time: time | None = None
    exchange: str | None = None
    analyst_name: str | None = None
    analyst_firm: str | None = None
    analyst_id: str | None = None
    company_name: str | None = None
    currency: str | None = None
    action: str | None = None
    action_change: str | None = None
    rating_current: str | None = None
    rating_previous: str | None = None
    importance: int | None = None
    price_target: float | None = None
    price_target_previous: float | None = None
    adj_price_target: float | None = None
    previous_adj_price_target: float | None = None
    price_when_posted: float | None = None
    notes: str | None = None
    url_news: str | None = None
    url_analyst: str | None = None
    id: str | None = None
    last_updated: datetime | None = None


class BenzingaAnalystAccuracy(ContractModel):
    """Typed subset of the documented ratings_accuracy object."""

    one_month_average_return: StrictFloat | None = None
    one_month_smart_score: StrictFloat | None = None
    one_month_stdev: StrictFloat | None = None
    one_month_success_rate: StrictFloat | None = None
    one_month_loss_count: StrictInt | None = None
    three_month_average_return: StrictFloat | None = None
    three_month_smart_score: StrictFloat | None = None
    three_month_stdev: StrictFloat | None = None
    three_month_success_rate: StrictFloat | None = None
    three_month_loss_count: StrictInt | None = None
    nine_month_average_return: StrictFloat | None = None
    nine_month_smart_score: StrictFloat | None = None
    nine_month_stdev: StrictFloat | None = None
    nine_month_success_rate: StrictFloat | None = None
    nine_month_loss_count: StrictInt | None = None
    one_year_average_return: StrictFloat | None = None
    one_year_smart_score: StrictFloat | None = None
    one_year_stdev: StrictFloat | None = None
    one_year_success_rate: StrictFloat | None = None
    one_year_loss_count: StrictInt | None = None
    two_year_average_return: StrictFloat | None = None
    two_year_smart_score: StrictFloat | None = None
    two_year_stdev: StrictFloat | None = None
    two_year_success_rate: StrictFloat | None = None
    two_year_loss_count: StrictInt | None = None
    three_year_average_return: StrictFloat | None = None
    three_year_smart_score: StrictFloat | None = None
    three_year_stdev: StrictFloat | None = None
    three_year_success_rate: StrictFloat | None = None
    three_year_loss_count: StrictInt | None = None
    overall_average_return: StrictFloat | None = None
    overall_average_return_percentile: StrictFloat | None = None
    overall_smart_score: StrictFloat | None = None
    overall_stdev: StrictFloat | None = None
    overall_success_rate: StrictFloat | None = None
    overall_loss_count: StrictInt | None = None
    smart_score: StrictFloat | None = None
    total_ratings: StrictInt | None = None
    total_ratings_percentile: StrictFloat | None = None


class BenzingaAnalystSearch(ContractModel):
    """One analyst record and its documented accuracy summary."""

    firm_id: StrictStr
    firm_name: StrictStr
    analyst_id: StrictStr
    name_first: StrictStr | None = None
    name_full: StrictStr | None = None
    name_last: StrictStr | None = None
    ratings_accuracy: BenzingaAnalystAccuracy
    updated: datetime


class BenzingaNewsItem(ContractModel):
    """One structured Benzinga news item."""

    id: StrictInt
    author: StrictStr | None = None
    created: datetime
    updated: datetime
    title: StrictStr
    teaser: StrictStr | None = None
    body: StrictStr | None = None
    url: StrictStr
    channels: list[StrictStr]
    stocks: list[StrictStr]
    tags: list[StrictStr]


class BenzingaCompanyNews(BenzingaNewsItem):
    """One Benzinga news record filtered to the requested ticker."""

    symbol: StrictStr


class BenzingaWorldNews(BenzingaNewsItem):
    """One Benzinga news record from the unfiltered feed."""
