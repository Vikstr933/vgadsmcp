"""Synthetic Search package tests: real protobufs, no credentials or ad writes."""

import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from pydantic import ValidationError

from ads_mcp.search_campaign_schema import SearchCampaign
from ads_mcp.search_campaigns import (
    compile_search,
    creation_receipt,
    configure_search_goals,
    verify_search,
    snapshot_search,
    COLLECTIONS,
)
from ads_mcp.search_campaign_review import render_search_review
from ads_mcp.write_gateway import GoogleAdsWriteGateway
from ads_mcp.write_policy import Change, WriteBlocked
from ads_mcp.write_workflow import WriteWorkflow
from tests.write_workflow_test import CID, MemoryStore, policy


def package():
    return dict(
        name="Synthetic Search",
        daily_budget_micros=200_000_000,
        bidding="MANUAL_CPC",
        start_date="2030-04-01",
        end_date="2030-04-30",
        geo_target_ids=["2752"],
        excluded_geo_target_ids=["20300"],
        language_ids=["1015"],
        conversion_action_ids=["11"],
        negative_keywords=[dict(text="jobb", match_type="PHRASE")],
        ad_groups=[
            dict(
                name="Tickets",
                cpc_bid_micros=5_000_000,
                keywords=[dict(text="synthetic tickets", match_type="EXACT")],
                negative_keywords=[dict(text="gratis", match_type="PHRASE")],
                ads=[
                    dict(
                        headlines=[
                            "Synthetic headline one",
                            "Synthetic headline two",
                            "Synthetic headline three",
                        ],
                        descriptions=[
                            "Synthetic description one.",
                            "Synthetic description two.",
                        ],
                        final_url="https://tickets.example.com/event",
                        path1="tickets",
                        path2="april",
                    )
                ],
            )
        ],
        sitelinks=[
            dict(
                text="More information",
                final_url="https://tickets.example.com/info",
                description1="Read about this event",
                description2="Find the venue",
            )
        ],
        callouts=["Book online"],
        final_url_suffix="utm_source=google&utm_medium=cpc",
    )


def change(**overrides):
    return Change(
        action="create_search_campaign",
        customer_id=CID,
        eu_political_advertising="does_not_contain",
        search_campaign=SearchCampaign.model_validate({**package(), **overrides}),
    )


def before():
    return dict(
        account=dict(
            id=CID,
            name="Synthetic account",
            currency="SEK",
            time_zone="Europe/Stockholm",
            status="ENABLED",
            manager=False,
            conversion_customer=f"customers/{CID}",
        ),
        conversions=[
            dict(
                id="11",
                resource_name=f"customers/{CID}/conversionActions/11",
                name="Purchase",
                primary_for_goal=False,
                category="PURCHASE",
                origin="WEBSITE",
            )
        ],
        customer_goals=[
            dict(category="PURCHASE", origin="WEBSITE", biddable=True),
            dict(category="PAGE_VIEW", origin="WEBSITE", biddable=True),
        ],
        locations=[
            dict(id="2752", name="Sweden", country="SE", status="ENABLED"),
            dict(id="20300", name="Excluded fixture", country="SE", status="ENABLED"),
        ],
        languages=[dict(id="1015", name="Swedish", targetable=True)],
    )


@pytest.fixture
def gateway():
    gateway = object.__new__(GoogleAdsWriteGateway)
    gateway.client = GoogleAdsClient(
        credentials=AnonymousCredentials(), use_proto_plus=True
    )
    gateway.service = Mock()
    return gateway


def response_for(gateway, builder):
    response = gateway.client.get_type("MutateGoogleAdsResponse")
    for i, expected in enumerate(builder.expected, 101):
        result = gateway.client.get_type("MutateOperationResponse")
        kind = expected["kind"]
        getattr(result, kind + "_result").resource_name = (
            f"customers/{CID}/{COLLECTIONS[kind]}/{i}"
        )
        response.mutate_operation_responses.append(result)
    return response


def goal(gateway, campaign, category, enabled=False):
    row = gateway.client.get_type("GoogleAdsRow")
    g = row.campaign_conversion_goal
    g.resource_name = (
        campaign.replace("/campaigns/", "/campaignConversionGoals/")
        + "~"
        + category
        + "~WEBSITE"
    )
    g.campaign, g.category, g.origin, g.biddable = (
        campaign,
        category,
        "WEBSITE",
        enabled,
    )
    return row


def install_readback(gateway, builder, receipt):
    """Model Google assigning IDs/defaults and serving persisted protobuf resources."""
    objects = {}
    mapping = receipt["mapping"]
    for op, expected, resource in zip(
        builder.operations, builder.expected, receipt["resources"]
    ):
        kind = expected["kind"]
        obj = copy.deepcopy(getattr(op, kind + "_operation").create)
        obj.resource_name = resource["resource_name"]
        for path, value in expected["fields"].items():
            if isinstance(value, str) and value in mapping and path != "resource_name":
                target = obj
                parts = path.split(".")
                for p in parts[:-1]:
                    target = getattr(target, p)
                setattr(target, parts[-1], mapping[value])
        if kind == "campaign":
            obj.bidding_strategy_type = "MANUAL_CPC"
        objects.setdefault(kind, []).append(obj)
    campaign = receipt["campaign_resource"]
    goals = [goal(gateway, campaign, c) for c in ("PURCHASE", "PAGE_VIEW")]
    config = gateway.client.get_type("GoogleAdsRow")
    config.conversion_goal_campaign_config.goal_config_level = "CAMPAIGN"
    config.conversion_goal_campaign_config.custom_conversion_goal = receipt[
        "custom_goal_resource"
    ]

    def rows(customer, query):
        assert customer == CID
        kind = query.split(" FROM ")[1].split()[0]
        if kind == "campaign_conversion_goal":
            return goals
        if kind == "conversion_goal_campaign_config":
            return [config]
        return [NS(**{kind: obj}) for obj in objects[kind]]

    gateway.rows = Mock(side_effect=rows)
    return objects, goals, config


def test_complete_atomic_package_uses_real_protobufs(gateway):
    proposal = change()
    builder = compile_search(gateway, proposal, before())
    assert len(builder.operations) == 15
    campaign = builder.operations[1].campaign_operation.create
    assert campaign.status.name == "PAUSED"
    assert campaign.ai_max_setting.enable_ai_max is False
    assert campaign.network_settings.target_search_network is False
    assert campaign.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE"
    assert {v.name for v in campaign.excluded_parent_asset_field_types} == {
        "SITELINK",
        "CALLOUT",
    }
    assert all(
        s.asset_automation_status.name == "OPTED_OUT"
        for s in campaign.asset_automation_settings
    )
    assert campaign.start_date_time == "2030-04-01 00:00:00"
    assert campaign.end_date_time == "2030-04-30 23:59:59"
    kinds = [e["kind"] for e in builder.expected]
    assert kinds.count("ad_group_ad") == 1 and kinds.count("ad_group_criterion") == 2
    assert kinds.count("campaign_asset") == 2
    for validate in (True, False):
        gateway.mutate(proposal, before(), validate_only=validate)
        kwargs = gateway.service.mutate.call_args.kwargs
        assert kwargs["request"].validate_only == validate
        assert not kwargs["request"].partial_failure
        assert kwargs["retry"] is None


@pytest.mark.parametrize(
    "bidding,field",
    [
        ("MAXIMIZE_CONVERSIONS", "maximize_conversions"),
        ("MAXIMIZE_CONVERSION_VALUE", "maximize_conversion_value"),
    ],
)
def test_explicit_automated_bidding(gateway, bidding, field):
    spec = package()
    spec["bidding"] = bidding
    spec["ad_groups"][0].pop("cpc_bid_micros")
    builder = compile_search(gateway, change(**spec), before())
    campaign = builder.operations[1].campaign_operation.create
    assert campaign._pb.WhichOneof("campaign_bidding_strategy") == field


def test_policy_cap_and_cross_action_fields():
    policy().check(change(), before())
    with pytest.raises(WriteBlocked, match="limit"):
        policy().check(change(daily_budget_micros=501_000_000), before())
    with pytest.raises(WriteBlocked):
        change().model_copy(update={"campaign_id": "42"}).check_shape()
    with pytest.raises(WriteBlocked):
        Change(
            action="pause",
            customer_id=CID,
            campaign_id="42",
            search_campaign=change().search_campaign,
        ).check_shape()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(start_date="2030-02-30"),
        lambda s: s.update(end_date="2029-01-01"),
        lambda s: s.update(status="ENABLED"),
        lambda s: s.update(geo_target_ids=[]),
        lambda s: s.update(conversion_action_ids=[]),
        lambda s: s["ad_groups"][0]["keywords"][0].update(match_type="BROAD"),
        lambda s: s["ad_groups"][0]["ads"][0].update(
            headlines=["x" * 31, "two", "three"]
        ),
        lambda s: s["ad_groups"][0]["ads"][0].update(final_url="http://localhost/page"),
        lambda s: s["ad_groups"][0]["ads"][0].update(
            final_url="https://169.254.169.254/a"
        ),
        lambda s: s["ad_groups"][0]["ads"][0].update(final_url="https://example.com/a"),
        lambda s: s["ad_groups"][0].pop("cpc_bid_micros"),
        lambda s: s.update(bidding="MAXIMIZE_CONVERSIONS"),
        lambda s: s.update(
            negative_keywords=[dict(text="synthetic tickets", match_type="PHRASE")]
        ),
    ],
)
def test_invalid_packages_fail_before_any_api(mutation):
    spec = package()
    mutation(spec)
    with pytest.raises(ValidationError):
        SearchCampaign.model_validate(spec)


def test_receipt_validates_count_and_customer(gateway):
    builder = compile_search(gateway, change(), before())
    response = response_for(gateway, builder)
    receipt = creation_receipt(change(), builder, response)
    assert receipt["campaign_resource"].endswith("/102")
    response.mutate_operation_responses[0].campaign_budget_result.resource_name = (
        "customers/9999999999/campaignBudgets/101"
    )
    with pytest.raises(WriteBlocked, match="outside"):
        creation_receipt(change(), builder, response)
    response.mutate_operation_responses.pop()
    with pytest.raises(WriteBlocked, match="count"):
        creation_receipt(change(), builder, response)


def test_second_stage_changes_only_new_campaign_goals(gateway):
    proposal = change()
    builder = compile_search(gateway, proposal, before())
    receipt = creation_receipt(proposal, builder, response_for(gateway, builder))
    objects, _, _ = install_readback(gateway, builder, receipt)
    check = Mock()
    configure_search_goals(gateway, proposal, before(), receipt, before_write=check)
    assert check.call_count == 2
    calls = gateway.service.mutate.call_args_list
    assert [c.kwargs["request"].validate_only for c in calls] == [True, False]
    for call in calls:
        request = call.kwargs["request"]
        assert len(request.mutate_operations) == 3
        assert not request.partial_failure and call.kwargs["retry"] is None
        for op in request.mutate_operations[:-1]:
            assert list(op.campaign_conversion_goal_operation.update_mask.paths) == [
                "biddable"
            ]
            assert op.campaign_conversion_goal_operation.update.biddable is False
        config = request.mutate_operations[-1].conversion_goal_campaign_config_operation
        assert config.update.custom_conversion_goal == receipt["custom_goal_resource"]
    objects["campaign"][0].status = "ENABLED"
    gateway.service.mutate.reset_mock()
    with pytest.raises(WriteBlocked, match="no longer paused"):
        configure_search_goals(gateway, proposal, before(), receipt)
    gateway.service.mutate.assert_not_called()


def test_readback_checks_all_resources_and_goals(gateway):
    proposal = change()
    builder = compile_search(gateway, proposal, before())
    receipt = creation_receipt(proposal, builder, response_for(gateway, builder))
    objects, goals, config = install_readback(gateway, builder, receipt)
    result = verify_search(gateway, proposal, before(), receipt)
    assert result["status"] == "PAUSED"
    assert result["resource_count"] == len(builder.operations)
    objects["ad_group_ad"][0].ad.responsive_search_ad.headlines[
        0
    ].text = "Changed externally"
    with pytest.raises(WriteBlocked, match="headlines"):
        verify_search(gateway, proposal, before(), receipt)
    objects, goals, config = install_readback(gateway, builder, receipt)
    goals[0].campaign_conversion_goal.biddable = True
    with pytest.raises(WriteBlocked, match="unintended"):
        verify_search(gateway, proposal, before(), receipt)
    goals[0].campaign_conversion_goal.biddable = False
    config.conversion_goal_campaign_config.goal_config_level = "CUSTOMER"
    with pytest.raises(WriteBlocked, match="selection"):
        verify_search(gateway, proposal, before(), receipt)


def test_review_contains_all_inputs_and_escapes_text():
    proposal = change(name="<script>alert(1)</script>")
    rendered = render_search_review(
        dict(change=proposal.model_dump(), snapshot=before(), expires_at=2_000_000_000)
    )
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    for text in [
        "200.00 SEK",
        "Synthetic headline three",
        "Synthetic description two.",
        "synthetic tickets",
        "More information",
        "Book online",
        "utm_source=google&amp;utm_medium=cpc",
        "Sweden",
        "sekundär",
        "PAUSAD",
    ]:
        assert text in rendered


class ProgressStore(MemoryStore):
    def record_progress(self, plan_id, owner, receipt):
        assert self.lock == plan_id and self.plans[plan_id]["owner"] == owner
        self.plans[plan_id]["progress"] = copy.deepcopy(receipt)


def snapshot_rows(gateway):
    state = before()
    customer = gateway.client.get_type("Customer")
    customer.id = int(CID)
    customer.descriptive_name = "Synthetic account"
    customer.currency_code = "SEK"
    customer.time_zone = "Europe/Stockholm"
    customer.status = "ENABLED"
    customer.conversion_tracking_setting.google_ads_conversion_customer = (
        f"customers/{CID}"
    )
    conversion = gateway.client.get_type("ConversionAction")
    for k, v in state["conversions"][0].items():
        setattr(conversion, k, int(v) if k == "id" else v)
    conversion.owner_customer = f"customers/{CID}"
    conversion.status = "ENABLED"
    goals = []
    for g in state["customer_goals"]:
        row = gateway.client.get_type("CustomerConversionGoal")
        for k, v in g.items():
            setattr(row, k, v)
        goals.append(NS(customer_conversion_goal=row))
    geos = []
    for g in state["locations"]:
        row = gateway.client.get_type("GeoTargetConstant")
        row.id = int(g["id"])
        row.name = g["name"]
        row.country_code = g["country"]
        row.status = g["status"]
        geos.append(NS(geo_target_constant=row))
    language = gateway.client.get_type("LanguageConstant")
    language.id = 1015
    language.name = "Swedish"
    language.targetable = True
    return [
        [NS(customer=customer)],
        [],
        [NS(conversion_action=conversion)],
        goals,
        geos,
        [NS(language_constant=language)],
    ]


def test_snapshot_resolves_real_names_and_goals(gateway):
    gateway.rows = Mock(side_effect=snapshot_rows(gateway))
    state = snapshot_search(gateway, change())
    assert state["account"] == before()["account"]
    assert state["conversions"] == before()["conversions"]
    assert len(state["locations"]) == 2
    assert state["languages"] == before()["languages"]


@pytest.mark.parametrize(
    "case",
    [
        "manager",
        "shared",
        "missing_conversion",
        "removed_conversion",
        "foreign_conversion",
        "duplicate_campaign",
        "bad_geo",
        "bad_language",
        "past",
    ],
)
def test_snapshot_failures_stop_before_validation(gateway, case):
    rows = snapshot_rows(gateway)
    proposal = change()
    if case == "manager":
        rows[0][0].customer.manager = True
    elif case == "shared":
        rows[0][
            0
        ].customer.conversion_tracking_setting.google_ads_conversion_customer = (
            "customers/9999999999"
        )
    elif case == "missing_conversion":
        rows[2] = []
    elif case == "removed_conversion":
        rows[2][0].conversion_action.status = "REMOVED"
    elif case == "foreign_conversion":
        rows[2][0].conversion_action.owner_customer = "customers/9999999999"
    elif case == "duplicate_campaign":
        rows[1] = [NS(campaign=NS(name="  SYNTHETIC SEARCH "))]
    elif case == "bad_geo":
        rows[4].pop()
    elif case == "bad_language":
        rows[5][0].language_constant.targetable = False
    else:
        proposal = change(start_date="2000-01-01")
    gateway.rows = Mock(side_effect=rows)
    with pytest.raises(WriteBlocked):
        gateway.snapshot(proposal)
    gateway.service.mutate.assert_not_called()


def test_dry_run_package_never_saves_or_writes(gateway):
    gateway.snapshot = Mock(return_value=before())
    store = ProgressStore()
    workflow = WriteWorkflow(policy("dry_run"), store, lambda _: gateway)
    result = workflow.preview(change(), "owner")
    assert result["status"] == "validated_only"
    assert not store.plans
    assert gateway.service.mutate.call_count == 1
    assert gateway.service.mutate.call_args.kwargs["request"].validate_only


def test_goal_drift_requires_new_approval(gateway):
    current = before()
    gateway.snapshot = Mock(side_effect=lambda _: copy.deepcopy(current))
    store = ProgressStore()
    workflow = WriteWorkflow(policy(), store, lambda _: gateway)
    plan = workflow.preview(change(), "owner")
    store.transition(plan["plan_id"], "pending_approval", "approved")
    current["conversions"][0]["primary_for_goal"] = True
    with pytest.raises(WriteBlocked, match="No write sent"):
        workflow.execute(plan["plan_id"], "owner")
    assert all(
        c.kwargs["request"].validate_only for c in gateway.service.mutate.call_args_list
    )
    assert store.lock is None


@pytest.mark.parametrize("failure", [None, "goals", "readback", "lost_response"])
def test_workflow_partial_failure_retains_receipt_and_lock(gateway, failure):
    proposal = change()
    builder = compile_search(gateway, proposal, before())
    response = response_for(gateway, builder)
    store = ProgressStore()
    workflow = WriteWorkflow(policy(), store, lambda _: gateway)
    gateway.snapshot = Mock(side_effect=lambda _: copy.deepcopy(before()))
    gateway.mutate = Mock(
        side_effect=lambda *a, validate_only: response if not validate_only else None
    )
    gateway.verify = Mock(return_value={"status": "PAUSED"})
    plan_id = workflow.preview(proposal, "owner")["plan_id"]
    store.transition(plan_id, "pending_approval", "approved")
    if failure == "lost_response":
        gateway.mutate.side_effect = lambda *a, validate_only: (
            None if validate_only else (_ for _ in ()).throw(TimeoutError())
        )
    if failure == "readback":
        gateway.verify.side_effect = RuntimeError()
    with patch(
        "ads_mcp.search_campaigns.configure_search_goals",
        side_effect=RuntimeError() if failure == "goals" else None,
    ) as configure:
        if failure:
            with pytest.raises(WriteBlocked, match="uncertain"):
                workflow.execute(plan_id, "owner")
            assert store.get(plan_id)["status"] == "unknown"
            assert store.lock == plan_id
            assert ("progress" in store.get(plan_id)) == (failure != "lost_response")
            with pytest.raises(WriteBlocked):
                workflow.execute(plan_id, "owner")
        else:
            assert workflow.execute(plan_id, "owner")["status"] == "verified"
            assert configure.call_count == 1 and store.lock is None
            assert workflow.execute(plan_id, "owner")["status"] == "already_verified"
    assert (
        sum(not c.kwargs["validate_only"] for c in gateway.mutate.call_args_list) == 1
    )
