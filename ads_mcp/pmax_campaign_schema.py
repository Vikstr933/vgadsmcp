"""Input contract for one standard Performance Max asset group."""

from typing import Literal

from pydantic import Field, model_validator

from ads_mcp.search_campaign_schema import (
    CampaignPackage,
    Description,
    Headline,
    ID,
    Name,
    ShortText,
    URL,
)


class PMaxCampaign(CampaignPackage):
    bidding: Literal["MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"]
    asset_group_name: Name
    final_url: URL
    business_name: ShortText
    logo_asset_id: ID
    landscape_image_asset_ids: list[ID] = Field(min_length=1, max_length=20)
    square_image_asset_ids: list[ID] = Field(min_length=1, max_length=20)
    video_asset_ids: list[ID] = Field(default_factory=list, max_length=15)
    allow_auto_video: bool
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    long_headlines: list[Description] = Field(min_length=1, max_length=5)
    descriptions: list[Description] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def bounded_assets(self):
        if not any(len(text) <= 15 for text in self.headlines):
            raise ValueError("Provide at least one headline of 15 characters or fewer.")
        if not self.video_asset_ids and not self.allow_auto_video:
            raise ValueError(
                "Select a YouTube video asset or explicitly accept Google-generated video."
            )
        for values in (
            self.headlines,
            self.long_headlines,
            self.descriptions,
            self.landscape_image_asset_ids,
            self.square_image_asset_ids,
            self.video_asset_ids,
        ):
            if len({v.strip().casefold() for v in values}) != len(values):
                raise ValueError("Duplicate assets within a role are not allowed.")
        return self
