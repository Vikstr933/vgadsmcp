"""Bounded input contract for complete, paused Search campaigns."""

import ipaddress
import json
import unicodedata
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_validator


def printable(value):
    if not value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Text must be non-empty and contain no control characters.")
    if "{" in value or "}" in value:
        raise ValueError("Dynamic text and URL placeholders are not supported.")
    return value


def public_url(value):
    printable(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Use a complete public HTTPS landing page without credentials."
        )
    host = parsed.hostname.lower()
    if (
        "." not in host
        or host.endswith((".local", ".localhost", ".internal", ".test", ".example"))
        or host in ("example.com", "example.org", "example.net")
    ):
        raise ValueError("A real public landing page is required, not a placeholder.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a public domain, not an IP address.")
    if any(c.isspace() for c in value):
        raise ValueError("URL must not contain whitespace.")
    return value


Name = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(printable)]
Headline = Annotated[str, Field(min_length=1, max_length=30), AfterValidator(printable)]
Description = Annotated[
    str, Field(min_length=1, max_length=90), AfterValidator(printable)
]
ShortText = Annotated[
    str, Field(min_length=1, max_length=25), AfterValidator(printable)
]
URL = Annotated[str, Field(max_length=2048), AfterValidator(public_url)]
ID = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
Micros = Annotated[int, Field(gt=0, le=10**12)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchKeyword(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=80), AfterValidator(printable)]
    match_type: Literal["EXACT", "PHRASE"]

    @model_validator(mode="after")
    def syntax(self):
        if len(self.text.split()) > 10 or any(c in self.text for c in '[]"'):
            raise ValueError(
                "Use at most 10 words; set match_type separately, without brackets or quotes."
            )
        return self


class SearchAd(StrictModel):
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    descriptions: list[Description] = Field(min_length=2, max_length=4)
    final_url: URL
    path1: (
        Annotated[str, Field(min_length=1, max_length=15), AfterValidator(printable)]
        | None
    ) = None
    path2: (
        Annotated[str, Field(min_length=1, max_length=15), AfterValidator(printable)]
        | None
    ) = None

    @model_validator(mode="after")
    def unique_assets(self):
        for values in (self.headlines, self.descriptions):
            if len({v.casefold().strip() for v in values}) != len(values):
                raise ValueError(
                    "Headlines and descriptions must be unique within each ad."
                )
        if self.path2 and not self.path1:
            raise ValueError("path2 requires path1.")
        return self


class SearchAdGroup(StrictModel):
    name: Name
    cpc_bid_micros: Micros | None = None
    keywords: list[SearchKeyword] = Field(min_length=1, max_length=100)
    negative_keywords: list[SearchKeyword] = Field(default_factory=list, max_length=100)
    ads: list[SearchAd] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def keyword_conflicts(self):
        positive = {k.text.casefold().strip() for k in self.keywords}
        negative = {k.text.casefold().strip() for k in self.negative_keywords}
        if positive & negative:
            raise ValueError(
                "An ad group cannot both target and exclude the same keyword text."
            )
        for values in (self.keywords, self.negative_keywords):
            if len({(k.text.casefold().strip(), k.match_type) for k in values}) != len(
                values
            ):
                raise ValueError("Duplicate keywords are not allowed.")
        if len({urlsplit(ad.final_url).hostname for ad in self.ads}) != 1:
            raise ValueError(
                "Ads in one ad group must use the same landing-page domain."
            )
        return self


class SearchSitelink(StrictModel):
    text: ShortText
    final_url: URL
    description1: (
        Annotated[str, Field(min_length=1, max_length=35), AfterValidator(printable)]
        | None
    ) = None
    description2: (
        Annotated[str, Field(min_length=1, max_length=35), AfterValidator(printable)]
        | None
    ) = None

    @model_validator(mode="after")
    def paired_descriptions(self):
        if bool(self.description1) != bool(self.description2):
            raise ValueError("Provide both sitelink descriptions or neither.")
        return self


class SearchCampaign(StrictModel):
    name: Name
    daily_budget_micros: Micros | None = None
    total_budget_micros: Micros | None = None
    bidding: Literal["MANUAL_CPC", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"]
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    geo_target_ids: list[ID] = Field(min_length=1, max_length=30)
    excluded_geo_target_ids: list[ID] = Field(default_factory=list, max_length=30)
    language_ids: list[ID] = Field(min_length=1, max_length=10)
    conversion_action_ids: list[ID] = Field(min_length=1, max_length=20)
    ad_groups: list[SearchAdGroup] = Field(min_length=1, max_length=20)
    negative_keywords: list[SearchKeyword] = Field(default_factory=list, max_length=200)
    sitelinks: list[SearchSitelink] = Field(default_factory=list, max_length=20)
    callouts: list[ShortText] = Field(default_factory=list, max_length=20)
    final_url_suffix: (
        Annotated[str, Field(max_length=1024), AfterValidator(printable)] | None
    ) = None

    def duration_days(self):
        return (
            date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)
        ).days + 1

    @model_validator(mode="after")
    def bounded_campaign(self):
        days = self.duration_days()
        if days <= 0:
            raise ValueError("End date must not precede start date.")
        if (self.daily_budget_micros is None) == (self.total_budget_micros is None):
            raise ValueError(
                "Supply exactly one of daily_budget_micros or total_budget_micros."
            )
        if self.total_budget_micros is not None and not 3 <= days <= 90:
            raise ValueError(
                "Search campaign total budgets require a period of 3 to 90 calendar days."
            )
        for values in (
            self.geo_target_ids,
            self.excluded_geo_target_ids,
            self.language_ids,
            self.conversion_action_ids,
            self.callouts,
        ):
            if len(set(values)) != len(values):
                raise ValueError(
                    "Duplicate targeting, conversion IDs or callouts are not allowed."
                )
        if set(self.geo_target_ids) & set(self.excluded_geo_target_ids):
            raise ValueError("A location cannot be both included and excluded.")
        if len({g.name.casefold().strip() for g in self.ad_groups}) != len(
            self.ad_groups
        ):
            raise ValueError("Ad group names must be unique.")
        if len({s.text.casefold().strip() for s in self.sitelinks}) != len(
            self.sitelinks
        ):
            raise ValueError("Sitelink texts must be unique.")
        if len(
            {(k.text.casefold().strip(), k.match_type) for k in self.negative_keywords}
        ) != len(self.negative_keywords):
            raise ValueError("Duplicate campaign negative keywords.")
        negatives = {k.text.casefold().strip() for k in self.negative_keywords}
        for group in self.ad_groups:
            if self.bidding == "MANUAL_CPC" and group.cpc_bid_micros is None:
                raise ValueError("Manual CPC requires a CPC bid for every ad group.")
            if self.bidding != "MANUAL_CPC" and group.cpc_bid_micros is not None:
                raise ValueError(
                    "Do not supply manual CPC bids with automated bidding."
                )
            if negatives & {k.text.casefold().strip() for k in group.keywords}:
                raise ValueError(
                    "Campaign negatives conflict with targeted keyword text."
                )
        if self.final_url_suffix and (
            self.final_url_suffix.startswith("?") or "://" in self.final_url_suffix
        ):
            raise ValueError(
                "Supply query parameters only for final_url_suffix, without ? or a URL."
            )
        if len(json.dumps(self.model_dump()).encode()) > 100_000:
            raise ValueError(
                "Campaign is too large for one approval. Split into smaller campaigns."
            )
        if (
            sum(len(g.keywords) + len(g.negative_keywords) for g in self.ad_groups)
            + len(self.negative_keywords)
            > 500
        ):
            raise ValueError(
                "Maximum 500 positive and negative keywords per campaign package."
            )
        return self
