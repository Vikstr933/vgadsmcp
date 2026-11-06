"""Read-only, bounded Google Keyword Planner requests; no persistent plans."""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from ads_mcp import utils
from ads_mcp.search_campaign_schema import (
    StrictModel,
    ID,
    URL,
    SearchKeyword,
    printable,
)

Keyword = Annotated[str, Field(min_length=1, max_length=80)]


class PlanningInputError(ValueError):
    """Only static, application-owned errors are safe to show to the user."""


class Targeting(StrictModel):
    customer_id: str
    login_customer_id: str | None = None
    geo_target_ids: list[ID] = Field(min_length=1, max_length=10)
    language_id: ID

    @field_validator("customer_id", "login_customer_id")
    @classmethod
    def account(cls, value):
        from ads_mcp.write_policy import Change

        return Change.account_id(value)

    @field_validator("geo_target_ids")
    @classmethod
    def unique_geos(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Locations must be unique.")
        return value


class HistoricalOptions(Targeting):
    start_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def months(self):
        if bool(self.start_month) != bool(self.end_month):
            raise ValueError("Specify both historical months or neither.")
        if self.start_month and self.start_month > self.end_month:
            raise ValueError("Historical end month must follow start month.")
        return self


def keyword_list(values):
    for value in values:
        printable(value)
        if len(value.split()) > 10:
            raise ValueError("Use at most 10 words per keyword.")
    if len({v.strip().casefold() for v in values}) != len(values):
        raise ValueError("Remove duplicate keywords.")
    return values


class IdeasRequest(HistoricalOptions):
    keywords: list[Keyword] = Field(default_factory=list, max_length=20)
    url: URL | None = None
    limit: int = Field(default=100, ge=1, le=500)

    _keywords = field_validator("keywords")(keyword_list)

    @model_validator(mode="after")
    def seed(self):
        if not self.keywords and not self.url:
            raise ValueError("Provide keyword seeds, a public URL, or both.")
        return self


class HistoryRequest(HistoricalOptions):
    keywords: list[Keyword] = Field(min_length=1, max_length=500)
    _keywords = field_validator("keywords")(keyword_list)


class ForecastRequest(Targeting):
    keywords: list[SearchKeyword] = Field(min_length=1, max_length=500)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bidding: Literal["MANUAL_CPC", "MAXIMIZE_CONVERSIONS"]
    daily_budget_micros: int = Field(gt=0, le=10**12)
    max_cpc_bid_micros: int | None = Field(default=None, gt=0, le=10**10)

    @model_validator(mode="after")
    def scenario(self):
        if date.fromisoformat(self.end_date) < date.fromisoformat(self.start_date):
            raise ValueError("Forecast end date must follow start date.")
        if (self.bidding == "MANUAL_CPC") != (self.max_cpc_bid_micros is not None):
            raise ValueError(
                "Manual CPC requires max_cpc_bid_micros; omit it for maximize conversions."
            )
        if len(
            {(k.text.strip().casefold(), k.match_type) for k in self.keywords}
        ) != len(self.keywords):
            raise ValueError("Remove duplicate forecast keywords.")
        return self


def optional(message, field):
    """Preserve missing metrics as null; a present zero remains zero."""
    return getattr(message, field) if message._pb.HasField(field) else None


def metrics(message):
    return {
        **{
            f: optional(message, f)
            for f in (
                "avg_monthly_searches",
                "competition_index",
                "low_top_of_page_bid_micros",
                "high_top_of_page_bid_micros",
                "average_cpc_micros",
            )
        },
        "competition": (
            message.competition.name
            if message.competition.name not in ("UNKNOWN", "UNSPECIFIED")
            else None
        ),
        "monthly_search_volumes": [
            dict(
                year=v.year,
                month=v.month.name,
                searches=optional(v, "monthly_searches"),
            )
            for v in message.monthly_search_volumes
        ],
    }


class Planner:
    def __init__(self, targeting):
        self.client = utils.get_googleads_client(
            login_customer_id=targeting.login_customer_id
        )
        self.service = utils.get_googleads_service(
            "KeywordPlanIdeaService", login_customer_id=targeting.login_customer_id
        )
        self.ads = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=targeting.login_customer_id
        )

    def context(self, target):
        rows = list(
            self.ads.search(
                customer_id=target.customer_id,
                query="SELECT customer.id, customer.currency_code, customer.time_zone, customer.manager FROM customer",
                retry=None,
                timeout=30,
            )
        )
        if (
            len(rows) != 1
            or str(rows[0].customer.id) != target.customer_id
            or rows[0].customer.manager
        ):
            raise PlanningInputError(
                "Select an accessible advertising client account, not the MCC itself."
            )
        c = rows[0].customer
        return dict(
            customer_id=target.customer_id,
            login_customer_id=target.login_customer_id,
            currency=c.currency_code,
            time_zone=c.time_zone,
            geo_target_ids=target.geo_target_ids,
            language_id=target.language_id,
            network="GOOGLE_SEARCH",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            source="Google Ads KeywordPlanIdeaService",
            google_ads_changed=False,
        )

    def historical_request(self, target, name, context):
        request = self.client.get_type(name)
        request.customer_id = target.customer_id
        request.language = f"languageConstants/{target.language_id}"
        request.geo_target_constants.extend(
            f"geoTargetConstants/{g}" for g in target.geo_target_ids
        )
        request.keyword_plan_network = "GOOGLE_SEARCH"
        request.include_adult_keywords = False
        request.historical_metrics_options.include_average_cpc = True
        if target.start_month:
            today = datetime.now(ZoneInfo(context["time_zone"])).date()
            earliest = f"{today.year-4:04d}-{today.month:02d}"
            current = f"{today.year:04d}-{today.month:02d}"
            if target.start_month < earliest or target.end_month >= current:
                raise PlanningInputError(
                    "Request completed historical months within the past four years."
                )
            for field, value in (
                ("start", target.start_month),
                ("end", target.end_month),
            ):
                year, month = map(int, value.split("-"))
                ym = getattr(request.historical_metrics_options.year_month_range, field)
                ym.year = year
                ym.month = month + 1  # MonthOfYear starts JANUARY at enum value 2.
        context["requested_history"] = {
            "start_month": target.start_month,
            "end_month": target.end_month,
            "default": "past 12 months" if not target.start_month else None,
        }
        return request

    def ideas(self, target):
        context = self.context(target)
        request = self.historical_request(
            target, "GenerateKeywordIdeasRequest", context
        )
        request.page_size = target.limit
        if target.url and target.keywords:
            request.keyword_and_url_seed.url = target.url
            request.keyword_and_url_seed.keywords.extend(target.keywords)
        elif target.url:
            request.url_seed.url = target.url
        else:
            request.keyword_seed.keywords.extend(target.keywords)
        pager = self.service.generate_keyword_ideas(
            request=request, retry=None, timeout=45
        )
        page = next(
            iter(pager.pages)
        )  # Exactly one API page; never enumerate the full pager.
        rows = list(page.results)
        return {
            **context,
            "seed_keywords": target.keywords,
            "seed_url": target.url,
            "results": [
                dict(text=r.text, metrics=metrics(r.keyword_idea_metrics))
                for r in rows[: target.limit]
            ],
            "has_more": bool(page.next_page_token) or len(rows) > target.limit,
            "notes": [
                "Ideas are suggestions, not approved campaign keywords. Review relevance and city/event intent.",
                "Historical volume is approximate; null means unavailable, not zero. Inspect monthly values for seasonality.",
                "Google may return only the available subset of the requested historical months; inspect the returned months.",
            ],
        }

    def history(self, target):
        context = self.context(target)
        request = self.historical_request(
            target, "GenerateKeywordHistoricalMetricsRequest", context
        )
        request.keywords.extend(target.keywords)
        response = self.service.generate_keyword_historical_metrics(
            request=request, retry=None, timeout=45
        )
        results = [
            dict(
                text=r.text,
                close_variants=list(r.close_variants),
                metrics=metrics(r.keyword_metrics),
            )
            for r in response.results
        ]
        represented = {
            v.casefold().strip()
            for r in results
            for v in [r["text"], *r["close_variants"]]
        }
        return {
            **context,
            "requested_keywords": target.keywords,
            "results": results,
            "unmatched_input_keywords": [
                k for k in target.keywords if k.casefold().strip() not in represented
            ],
            "notes": [
                "Google may merge close variants; do not sum duplicate/overlapping volumes.",
                "Missing results and null metrics do not imply zero demand. Monthly values are historical estimates, not future forecasts.",
                "Bid ranges are historical top-of-page estimates, not suggested campaign bids.",
                "Google may return only the available subset of the requested historical months; inspect the returned months.",
            ],
        }

    def forecast(self, target):
        context = self.context(target)
        today = datetime.now(ZoneInfo(context["time_zone"])).date()
        if date.fromisoformat(target.start_date) <= today or date.fromisoformat(
            target.end_date
        ) > today + timedelta(days=365):
            raise PlanningInputError(
                "Forecast must start in the future and end within one year in the account time zone."
            )
        request = self.client.get_type("GenerateKeywordForecastMetricsRequest")
        request.customer_id = target.customer_id
        request.forecast_period.start_date = target.start_date
        request.forecast_period.end_date = target.end_date
        campaign = request.campaign
        campaign.language_constants.append(f"languageConstants/{target.language_id}")
        campaign.geo_target_constants.extend(
            f"geoTargetConstants/{g}" for g in target.geo_target_ids
        )
        if target.bidding == "MANUAL_CPC":
            strategy = campaign.bidding_strategy.manual_cpc_bidding_strategy
            strategy.daily_budget_micros = target.daily_budget_micros
            strategy.max_cpc_bid_micros = target.max_cpc_bid_micros
        else:
            campaign.bidding_strategy.maximize_conversions_bidding_strategy.daily_target_spend_micros = (
                target.daily_budget_micros
            )
        group = self.client.get_type("ForecastAdGroup")
        for keyword in target.keywords:
            item = self.client.get_type("KeywordInfo")
            item.text = keyword.text
            item.match_type = keyword.match_type
            group.keywords.append(item)
        campaign.ad_groups.append(group)
        response = self.service.generate_keyword_forecast_metrics(
            request=request, retry=None, timeout=45
        )
        # v25 planless forecasts expose no network selector or impression metric.
        context["network"] = "API_DEFINED_NO_NETWORK_SELECTOR"
        result = None
        if response._pb.HasField("campaign_forecast_metrics"):
            result = {
                f: optional(response.campaign_forecast_metrics, f)
                for f in (
                    "average_cpc_micros",
                    "clicks",
                    "cost_micros",
                    "conversions",
                    "average_cpa_micros",
                )
            }
        return {
            **context,
            "scenario": target.model_dump(),
            "metrics": result,
            "notes": [
                "This is a forecast scenario, not a campaign or budget commitment. No resources are created.",
                "v25 forecast has no network selector, negative keyword inputs, ads, or campaign-specific conversion selection; it is not an exact simulation of the campaign builder.",
                "Only supplied API metrics are returned. Clicks/conversions availability depends on bidding. Predictions are not guarantees.",
            ],
        }


def safe_error(error):
    from google.ads.googleads.errors import GoogleAdsException

    if isinstance(error, GoogleAdsException):
        return (
            "Google Keyword Planner rejected the request: "
            + utils.google_ads_error_codes(error)
            + ". Check client/MCC access, targeting, dates and quotas. KeywordPlanIdeaService requires Basic or Standard API access; Explorer is insufficient. No campaign changes were made; no fallback statistics are invented."
        )
    if isinstance(error, PlanningInputError):
        return str(error)
    return "Keyword planning failed. Check connection, account access and service availability. No campaign changes or substitute statistics were produced."
