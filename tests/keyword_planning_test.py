"""Keyword planning uses real protobufs and mocked read-only API transports."""

from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from ads_mcp.keyword_planning import (
    IdeasRequest,
    HistoryRequest,
    ForecastRequest,
    Planner,
    metrics,
    safe_error,
)

BASE = dict(
    customer_id="1234567890",
    login_customer_id="2222222222",
    geo_target_ids=["2752"],
    language_id="1015",
)


@pytest.fixture
def planner():
    p = object.__new__(Planner)
    p.client = GoogleAdsClient(credentials=AnonymousCredentials(), use_proto_plus=True)
    p.service = Mock()
    p.ads = Mock()
    customer = p.client.get_type("Customer")
    customer.id = 1234567890
    customer.currency_code = "SEK"
    customer.time_zone = "Europe/Stockholm"
    p.ads.search.return_value = [NS(customer=customer)]
    return p


@pytest.mark.parametrize(
    "extra,seed",
    [
        ({"keywords": ["grillmässa"]}, "keyword_seed"),
        ({"url": "https://grillatmassan.se/"}, "url_seed"),
        (
            {"keywords": ["grillmässa"], "url": "https://grillatmassan.se/"},
            "keyword_and_url_seed",
        ),
    ],
)
def test_ideas_seed_and_bounded_page(planner, extra, seed):
    response = planner.client.get_type("GenerateKeywordIdeaResponse")
    response.next_page_token = "opaque"
    row = planner.client.get_type("GenerateKeywordIdeaResult")
    row.text = "grillmässa göteborg"
    row.keyword_idea_metrics.avg_monthly_searches = 0
    response.results.append(row)
    planner.service.generate_keyword_ideas.return_value = NS(pages=iter([response]))
    result = planner.ideas(IdeasRequest(**BASE, **extra, limit=1))
    call = planner.service.generate_keyword_ideas.call_args.kwargs
    req = call["request"]
    assert req._pb.WhichOneof("seed") == seed
    assert req.keyword_plan_network.name == "GOOGLE_SEARCH"
    assert req.page_size == 1 and not req.include_adult_keywords
    assert list(req.geo_target_constants) == ["geoTargetConstants/2752"]
    assert req.language == "languageConstants/1015"
    assert call["retry"] is None
    assert result["has_more"] and result["currency"] == "SEK"
    assert result["results"][0]["metrics"]["avg_monthly_searches"] == 0
    assert result["results"][0]["metrics"]["competition_index"] is None


def test_history_variants_and_missing_metrics(planner):
    response = planner.client.get_type("GenerateKeywordHistoricalMetricsResponse")
    row = planner.client.get_type("GenerateKeywordHistoricalMetricsResult")
    row.text = "grillmässa"
    row.close_variants.append("grillmässor")
    month = planner.client.get_type("MonthlySearchVolume")
    month.year = 2025
    month.month = "MAY"
    month.monthly_searches = 0
    row.keyword_metrics.monthly_search_volumes.append(month)
    response.results.append(row)
    planner.service.generate_keyword_historical_metrics.return_value = response
    result = planner.history(
        HistoryRequest(
            **BASE,
            keywords=["grillmässa", "grillmässor", "missing"],
            start_month="2025-01",
            end_month="2025-12",
        )
    )
    assert result["unmatched_input_keywords"] == ["missing"]
    assert result["results"][0]["metrics"]["avg_monthly_searches"] is None
    assert result["results"][0]["metrics"]["monthly_search_volumes"] == [
        dict(year=2025, month="MAY", searches=0)
    ]
    req = planner.service.generate_keyword_historical_metrics.call_args.kwargs[
        "request"
    ]
    assert req.historical_metrics_options.year_month_range.start.month.name == "JANUARY"
    assert req.historical_metrics_options.year_month_range.end.month.name == "DECEMBER"
    assert req.keyword_plan_network.name == "GOOGLE_SEARCH"


@pytest.mark.parametrize("bidding", ["MANUAL_CPC", "MAXIMIZE_CONVERSIONS"])
def test_planless_forecast_available_fields_and_explicit_scenario(planner, bidding):
    response = planner.client.get_type("GenerateKeywordForecastMetricsResponse")
    response.campaign_forecast_metrics.cost_micros = 0
    planner.service.generate_keyword_forecast_metrics.return_value = response
    target = ForecastRequest(
        **BASE,
        keywords=[dict(text="grillmässa", match_type="EXACT")],
        start_date=(date.today() + timedelta(days=2)).isoformat(),
        end_date=(date.today() + timedelta(days=10)).isoformat(),
        bidding=bidding,
        daily_budget_micros=100_000_000,
        max_cpc_bid_micros=2_000_000 if bidding == "MANUAL_CPC" else None,
    )
    result = planner.forecast(target)
    assert result["metrics"]["cost_micros"] == 0
    assert result["metrics"]["clicks"] is None
    assert result["network"] == "API_DEFINED_NO_NETWORK_SELECTOR"
    call = planner.service.generate_keyword_forecast_metrics.call_args.kwargs
    req = call["request"]
    assert call["retry"] is None
    assert req.forecast_period.start_date == target.start_date
    assert req.campaign.ad_groups[0].keywords[0].match_type.name == "EXACT"
    assert req.campaign.bidding_strategy._pb.WhichOneof("bidding_strategy") == (
        "manual_cpc_bidding_strategy"
        if bidding == "MANUAL_CPC"
        else "maximize_conversions_bidding_strategy"
    )
    assert not req._pb.HasField(
        "currency_code"
    )  # Native account currency, no guessed conversion.


def test_forecast_missing_result_is_null(planner):
    planner.service.generate_keyword_forecast_metrics.return_value = (
        planner.client.get_type("GenerateKeywordForecastMetricsResponse")
    )
    result = planner.forecast(
        ForecastRequest(
            **BASE,
            keywords=[dict(text="boat fair", match_type="PHRASE")],
            start_date=(date.today() + timedelta(days=2)).isoformat(),
            end_date=(date.today() + timedelta(days=3)).isoformat(),
            bidding="MAXIMIZE_CONVERSIONS",
            daily_budget_micros=100_000_000,
        )
    )
    assert result["metrics"] is None


@pytest.mark.parametrize(
    "bad",
    [
        dict(customer_id="1 OR 1=1"),
        dict(geo_target_ids=[]),
        dict(geo_target_ids=["1", "1"]),
        dict(language_id="x"),
        dict(url="http://localhost/"),
        dict(keywords=["a", "A"]),
        dict(start_month="2025-01"),
        dict(limit=501),
    ],
)
def test_invalid_planning_input(bad):
    data = {**BASE, "keywords": ["grillmässa"], **bad}
    with pytest.raises(ValidationError):
        IdeasRequest(**data)


def test_forecast_past_dates_block_before_planning_call(planner):
    target = ForecastRequest(
        **BASE,
        keywords=[dict(text="tickets", match_type="EXACT")],
        start_date="2000-01-01",
        end_date="2000-01-02",
        bidding="MANUAL_CPC",
        daily_budget_micros=100_000_000,
        max_cpc_bid_micros=1_000_000,
    )
    with pytest.raises(ValueError, match="future"):
        planner.forecast(target)
    planner.service.generate_keyword_forecast_metrics.assert_not_called()


def test_mcc_is_not_planning_client(planner):
    planner.ads.search.return_value[0].customer.manager = True
    with pytest.raises(ValueError, match="client"):
        planner.history(HistoryRequest(**BASE, keywords=["tickets"]))
    planner.service.generate_keyword_historical_metrics.assert_not_called()


def test_generic_failure_does_not_echo_secrets():
    assert "secret-token" not in safe_error(RuntimeError("secret-token"))
    assert "secret-token" not in safe_error(ValueError("secret-token"))


@pytest.mark.parametrize("raw_protobuf", [False, True])
def test_google_error_exposes_code_not_backend_message(planner, raw_protobuf):
    from google.ads.googleads.errors import GoogleAdsException

    failure = planner.client.get_type("GoogleAdsFailure")
    error = planner.client.get_type("GoogleAdsError")
    error.error_code.authorization_error = "USER_PERMISSION_DENIED"
    error.message = "secret-token"
    failure.errors.append(error)
    if raw_protobuf:
        failure = type(failure).pb(failure)
    result = safe_error(GoogleAdsException(Exception(), None, failure, "test-request"))
    assert "USER_PERMISSION_DENIED" in result
    assert "secret-token" not in result
    assert "Basic or Standard" in result
