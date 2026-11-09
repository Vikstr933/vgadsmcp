"""Standard PMax validation, approval and read-back using SDK messages."""

from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from ads_mcp.pmax_campaign_review import render_pmax_review
from ads_mcp.pmax_campaign_schema import PMaxCampaign
from ads_mcp.pmax_campaigns import AUTOMATION, compile_pmax, snapshot_pmax
from ads_mcp.search_campaigns import creation_receipt, verify_campaign_package
from ads_mcp.write_policy import Change, WriteBlocked
from ads_mcp.write_workflow import WriteWorkflow
from tests.search_campaigns_test import (
    CID,
    ProgressStore,
    before,
    gateway,
    install_readback,
    policy,
    response_for,
    snapshot_rows,
)


def proposal(**overrides):
    campaign = dict(
        name="Synthetic PMax",
        start_date="2030-04-01",
        end_date="2030-04-30",
        total_budget_micros=4_500_000_000,
        bidding="MAXIMIZE_CONVERSIONS",
        geo_target_ids=["2752"],
        excluded_geo_target_ids=["20300"],
        language_ids=["1015"],
        conversion_action_ids=["11"],
        asset_group_name="Visitors",
        final_url="https://tickets.example.com/event",
        business_name="Synthetic Event",
        logo_asset_id="101",
        landscape_image_asset_ids=["102"],
        square_image_asset_ids=["103"],
        video_asset_ids=["104"],
        allow_auto_video=False,
        headlines=["Buy tickets", "See the synthetic event", "Meet the exhibitors"],
        long_headlines=["Find out what is happening at the synthetic event"],
        descriptions=["Explore the event.", "Book a ticket to attend."],
    )
    return Change(
        action="create_pmax_campaign",
        customer_id=CID,
        eu_political_advertising="does_not_contain",
        pmax_campaign=PMaxCampaign.model_validate({**campaign, **overrides}),
    )


def media_rows(gateway):
    rows = []
    for asset_id, width, height in ((101, 128, 128), (102, 1200, 628), (103, 300, 300)):
        asset = gateway.client.get_type("Asset")
        asset.id = asset_id
        asset.resource_name = f"customers/{CID}/assets/{asset_id}"
        asset.name = "Test <image>"
        asset.type_ = "IMAGE"
        asset.image_asset.full_size.width_pixels = width
        asset.image_asset.full_size.height_pixels = height
        rows.append(NS(asset=asset))
    video = gateway.client.get_type("Asset")
    video.id = 104
    video.resource_name = f"customers/{CID}/assets/104"
    video.type_ = "YOUTUBE_VIDEO"
    video.youtube_video_asset.youtube_video_id = "placeholder"
    video.youtube_video_asset.youtube_video_title = "Approved fixture"
    return rows + [NS(asset=video)]


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_asset_ids": []},
        {"headline": ["Unexpected field"]},
        {"bidding": "MANUAL_CPC"},
        {"headlines": ["Long synthetic headline"] * 3},
        {"daily_budget_micros": 300_000_000},
        {"landscape_image_asset_ids": []},
    ],
)
def test_schema_rejects_invalid_pmax_packages(overrides):
    with pytest.raises(ValidationError):
        proposal(**overrides)


def test_snapshot_checks_asset_account_ratio_and_conversion(gateway):
    change = proposal()
    assets = media_rows(gateway)
    rows = snapshot_rows(gateway) + [assets]
    gateway.rows = Mock(side_effect=rows)
    state = snapshot_pmax(gateway, change)
    assert state["assets"]["104"]["video_id"] == "placeholder"
    assert state["assets"]["102"]["width"] == 1200

    rendered = render_pmax_review(
        dict(change=change.model_dump(), snapshot=state, expires_at=2_000_000_000)
    )
    assert "4,500.00 SEK" in rendered and "Buy tickets" in rendered
    assert "Test &lt;image&gt;" in rendered and "<image>" not in rendered
    assert "Approved fixture" in rendered and "Purchase" in rendered

    assets[1].asset.image_asset.full_size.width_pixels = 500
    gateway.rows = Mock(side_effect=rows)
    with pytest.raises(WriteBlocked, match="size or ratio"):
        snapshot_pmax(gateway, change)
    rows[2][0].conversion_action.category = "OUTBOUND_CLICK"
    gateway.rows = Mock(side_effect=rows)
    with pytest.raises(WriteBlocked, match="completed purchase"):
        snapshot_pmax(gateway, change)


def test_compile_and_verify_standard_pmax_with_explicit_goals(gateway):
    change, state = proposal(), before()
    builder = compile_pmax(gateway, change, state)
    budget = builder.operations[0].campaign_budget_operation.create
    campaign = builder.operations[1].campaign_operation.create
    assert (
        budget.period.name == "CUSTOM_PERIOD"
        and budget.total_amount_micros == 4_500_000_000
    )
    assert campaign.status.name == "PAUSED"
    assert campaign.advertising_channel_type.name == "PERFORMANCE_MAX"
    assert (
        campaign.brand_guidelines_enabled and campaign.shopping_setting.merchant_id == 0
    )
    assert {
        a.asset_automation_type.name
        for a in campaign.asset_automation_settings
        if a.asset_automation_status.name == "OPTED_OUT"
    } == set(AUTOMATION)
    brand_links = [
        x.campaign_asset_operation.create
        for x in builder.operations
        if "campaign_asset_operation" in x
    ]
    assert [x.field_type.name for x in brand_links] == ["BUSINESS_NAME", "LOGO"]
    group_links = [
        x.asset_group_asset_operation.create
        for x in builder.operations
        if "asset_group_asset_operation" in x
    ]
    assert len(group_links) == 9
    assert {x.field_type.name for x in group_links} == {
        "HEADLINE",
        "LONG_HEADLINE",
        "DESCRIPTION",
        "MARKETING_IMAGE",
        "SQUARE_MARKETING_IMAGE",
        "YOUTUBE_VIDEO",
    }

    receipt = creation_receipt(change, builder, response_for(gateway, builder))
    objects, _, _ = install_readback(gateway, builder, receipt)
    objects["campaign"][0].bidding_strategy_type = "MAXIMIZE_CONVERSIONS"
    for link in objects["asset_group_asset"]:
        link.status = "ENABLED"
    assert (
        verify_campaign_package(gateway, change, state, receipt)["status"] == "PAUSED"
    )
    objects["asset_group"][0].final_urls[0] = "https://different.example.com/"
    with pytest.raises(WriteBlocked, match="asset_group.final_urls"):
        verify_campaign_package(gateway, change, state, receipt)


def test_pmax_workflow_applies_only_after_approval(gateway):
    change, state = proposal(), before()
    builder = compile_pmax(gateway, change, state)
    response = response_for(gateway, builder)
    store = ProgressStore()
    workflow = WriteWorkflow(policy(), store, lambda _: gateway)
    gateway.snapshot = Mock(return_value=state)
    gateway.mutate = Mock(
        side_effect=lambda *args, validate_only: None if validate_only else response
    )
    gateway.verify = Mock(return_value={"status": "PAUSED"})
    plan = workflow.preview(change, "owner")
    with pytest.raises(WriteBlocked, match="approve"):
        workflow.execute(plan["plan_id"], "owner")
    assert all(call.kwargs["validate_only"] for call in gateway.mutate.call_args_list)
    store.transition(plan["plan_id"], "pending_approval", "approved")
    with patch("ads_mcp.search_campaigns.configure_campaign_goals") as configure:
        result = workflow.execute(plan["plan_id"], "owner")
    assert configure.call_count == 1 and result["status"] == "verified"
    assert (
        sum(not call.kwargs["validate_only"] for call in gateway.mutate.call_args_list)
        == 1
    )


def test_pmax_reconciliation_reads_the_saved_campaign_without_writing(gateway):
    change, state = proposal(), before()
    builder = compile_pmax(gateway, change, state)
    response = response_for(gateway, builder)
    receipt = creation_receipt(change, builder, response)
    store = ProgressStore()
    workflow = WriteWorkflow(policy(), store, lambda _: gateway)
    gateway.snapshot = Mock(return_value=state)
    gateway.mutate = Mock(
        side_effect=lambda *args, validate_only: None if validate_only else response
    )
    gateway.verify = Mock(side_effect=TimeoutError)
    plan_id = workflow.preview(change, "owner")["plan_id"]
    store.transition(plan_id, "pending_approval", "approved")
    with patch("ads_mcp.search_campaigns.configure_campaign_goals"):
        with pytest.raises(WriteBlocked, match="uncertain"):
            workflow.execute(plan_id, "owner")
    assert store.lock == plan_id
    with pytest.raises(WriteBlocked, match="campaign type"):
        workflow.reconcile_search_plan(plan_id, "owner")

    objects, _, _ = install_readback(gateway, builder, receipt)
    objects["campaign"][0].bidding_strategy_type = "MAXIMIZE_CONVERSIONS"
    for link in objects["asset_group_asset"]:
        link.status = "ENABLED"
    outcome = workflow.reconcile_campaign_plan(plan_id, "owner", "create_pmax_campaign")
    assert outcome["result"]["reconciled_by_readback"] and store.lock is None
    assert (
        sum(not call.kwargs["validate_only"] for call in gateway.mutate.call_args_list)
        == 1
    )
