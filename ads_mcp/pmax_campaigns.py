"""Build a paused, standard Performance Max campaign from reviewed assets."""

from ads_mcp.conversion_writes import blocked
from ads_mcp.search_campaigns import (
    TEXT_AUTOMATION,
    Compiler,
    add_custom_goal,
    add_targeting,
    campaign_fields,
    set_campaign_bidding,
    snapshot_campaign_package,
)

AUTOMATION = TEXT_AUTOMATION + (
    "GENERATE_IMAGE_EXTRACTION",
    "GENERATE_IMAGE_ENHANCEMENT",
    "GENERATE_ENHANCED_YOUTUBE_VIDEOS",
)


def snapshot_pmax(gateway, change):
    spec = change.pmax_campaign
    state = snapshot_campaign_package(gateway, change)
    if any(
        conversion["category"] in ("PAGE_VIEW", "OUTBOUND_CLICK", "ENGAGEMENT")
        for conversion in state["conversions"]
    ):
        blocked("Select a completed purchase, registration or lead action for PMax.")
    asset_ids = {
        spec.logo_asset_id,
        *spec.landscape_image_asset_ids,
        *spec.square_image_asset_ids,
        *spec.video_asset_ids,
    }
    rows = gateway.rows(
        change.customer_id,
        "SELECT asset.id, asset.resource_name, asset.name, asset.type, "
        "asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels, "
        "asset.youtube_video_asset.youtube_video_id, asset.youtube_video_asset.youtube_video_title "
        "FROM asset WHERE asset.id IN (" + ",".join(sorted(asset_ids)) + ")",
    )
    assets = {}
    for row in rows:
        asset = row.asset
        asset_id = str(asset.id)
        if asset.resource_name != f"customers/{change.customer_id}/assets/{asset_id}":
            blocked("All selected media assets must belong to this Ads account.")
        details = dict(id=asset_id, name=asset.name, type=asset.type_.name)
        if details["type"] == "IMAGE":
            size = asset.image_asset.full_size
            details.update(width=size.width_pixels, height=size.height_pixels)
        if details["type"] == "YOUTUBE_VIDEO":
            video = asset.youtube_video_asset
            details.update(
                video_id=video.youtube_video_id, title=video.youtube_video_title
            )
        assets[asset_id] = details
    if assets.keys() != asset_ids:
        blocked("One or more selected media assets are unavailable in this account.")
    for ids, min_width, min_height, ratio in (
        ([spec.logo_asset_id], 128, 128, 1),
        (spec.landscape_image_asset_ids, 600, 314, 1.91),
        (spec.square_image_asset_ids, 300, 300, 1),
    ):
        for asset_id in ids:
            asset = assets[asset_id]
            if (
                asset["type"] != "IMAGE"
                or asset["width"] < min_width
                or asset["height"] < min_height
                or abs(asset["width"] / asset["height"] - ratio) > 0.01
            ):
                blocked(f"Asset {asset_id} does not meet its PMax image size or ratio.")
    for asset_id in spec.video_asset_ids:
        asset = assets[asset_id]
        if asset["type"] != "YOUTUBE_VIDEO" or not asset["video_id"]:
            blocked(f"Asset {asset_id} must be an existing YouTube video asset.")
    state["assets"] = assets
    return state


def compile_pmax(gateway, change, before):
    spec = change.pmax_campaign
    builder = Compiler(gateway.client, change.customer_id)
    fields = campaign_fields(builder, change, "PERFORMANCE_MAX")
    fields["brand_guidelines_enabled"] = True
    obj = builder.create("campaign", fields)
    set_campaign_bidding(builder, obj, spec.bidding, AUTOMATION)
    builder.expected[-1]["fields"]["shopping_setting.merchant_id"] = 0
    campaign = fields["resource_name"]
    group = builder.resource("assetGroups")
    add_targeting(builder, spec, campaign)

    texts = {}
    for text in dict.fromkeys(
        [spec.business_name, *spec.headlines, *spec.long_headlines, *spec.descriptions]
    ):
        resource = builder.resource("assets")
        builder.create("asset", {"resource_name": resource, "text_asset.text": text})
        texts[text] = resource
    for field_type, resource in (
        ("BUSINESS_NAME", texts[spec.business_name]),
        ("LOGO", f"{builder.root}/assets/{spec.logo_asset_id}"),
    ):
        builder.create(
            "campaign_asset",
            dict(
                campaign=campaign,
                asset=resource,
                field_type=field_type,
                status="ENABLED",
            ),
        )
    builder.create(
        "asset_group",
        dict(
            resource_name=group,
            campaign=campaign,
            name=spec.asset_group_name,
            final_urls=[spec.final_url],
            status="ENABLED",
        ),
    )
    for field_type, resources in (
        ("HEADLINE", [texts[text] for text in spec.headlines]),
        ("LONG_HEADLINE", [texts[text] for text in spec.long_headlines]),
        ("DESCRIPTION", [texts[text] for text in spec.descriptions]),
        (
            "MARKETING_IMAGE",
            [f"{builder.root}/assets/{i}" for i in spec.landscape_image_asset_ids],
        ),
        (
            "SQUARE_MARKETING_IMAGE",
            [f"{builder.root}/assets/{i}" for i in spec.square_image_asset_ids],
        ),
        ("YOUTUBE_VIDEO", [f"{builder.root}/assets/{i}" for i in spec.video_asset_ids]),
    ):
        for resource in resources:
            builder.create(
                "asset_group_asset",
                dict(asset_group=group, asset=resource, field_type=field_type),
            )
            builder.expected[-1]["fields"]["status"] = "ENABLED"
    add_custom_goal(builder, spec, before)
    return builder


def describe_pmax(change, before):
    return {
        "before": "No campaign created by this plan.",
        "after": change.pmax_campaign.model_dump(),
        "status": "PAUSED",
        "automation_opt_outs": list(AUTOMATION),
        "warnings": [
            "Google still optimizes bidding and delivery across eligible inventory.",
            "Google may generate video if suitable video assets are missing.",
            "Selected custom goal actions can be used for bidding even if marked secondary.",
            "Verify the completed conversion and its value before activation.",
        ],
        "existing_custom_goal": before.get("existing_custom_goal"),
        "rollback": "Keep the campaign paused. Activation requires a separate decision.",
    }
