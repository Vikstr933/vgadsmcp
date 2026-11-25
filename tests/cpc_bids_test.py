"""Test real Ads protobuf operations against a synthetic transport."""

from unittest.mock import Mock
import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from ads_mcp.write_gateway import GoogleAdsWriteGateway
from ads_mcp.write_policy import Change, WriteBlocked
from ads_mcp.write_workflow import WriteWorkflow
from ads_mcp.write_approval import render_review
from tests.write_workflow_test import CID, policy, MemoryStore


@pytest.mark.parametrize("keyword", [False, True])
def test_personally_approved_bid(keyword):
    gateway = object.__new__(GoogleAdsWriteGateway)
    gateway.client = GoogleAdsClient(
        credentials=AnonymousCredentials(), use_proto_plus=True
    )
    row = gateway.client.get_type("GoogleAdsRow")
    row.customer.id = int(CID)
    row.customer.currency_code = "SEK"
    c, g, k = row.campaign, row.ad_group, row.ad_group_criterion
    c.id, c.status, c.advertising_channel_type, c.bidding_strategy_type = (
        42,
        "ENABLED",
        "SEARCH",
        "MANUAL_CPC",
    )
    g.id, g.name, g.status, g.cpc_bid_micros = 11, "Fixture group", "ENABLED", 4000000
    g.resource_name = f"customers/{CID}/adGroups/11"
    k.criterion_id, k.type_, k.status = 17, "KEYWORD", "ENABLED"
    k.keyword.text, k.keyword.match_type = "grillat göteborg", "PHRASE"
    k.resource_name = f"customers/{CID}/adGroupCriteria/11~17"
    gateway.service = Mock()
    gateway.service.search.return_value = [row]

    def mutate(request, **kwargs):
        (op,) = request.mutate_operations
        update = op.ad_group_criterion_operation if keyword else op.ad_group_operation
        target = k if keyword else g
        assert update.update.resource_name == target.resource_name
        assert list(update.update_mask.paths) == ["cpc_bid_micros"]
        if not request.validate_only:
            target.cpc_bid_micros = update.update.cpc_bid_micros
        return object()

    gateway.service.mutate.side_effect = mutate
    store = MemoryStore()
    flow = WriteWorkflow(policy(), store, lambda _: gateway)
    change = Change(
        action="set_cpc_bid",
        customer_id=CID,
        campaign_id="42",
        ad_group_id="11",
        criterion_id="17" if keyword else None,
        cpc_bid_micros=12000000 if keyword else 10000000,
    )
    c.bidding_strategy_type = "MAXIMIZE_CONVERSIONS"
    with pytest.raises(WriteBlocked):
        flow.preview(change, "user")
    c.bidding_strategy_type = "MANUAL_CPC"
    result = flow.preview(change, "user")
    pid = result["plan_id"]
    review = render_review(store.get(pid))
    assert "Fixture group" in review and "4 SEK" in review
    if keyword:
        assert "Ärver" in review and "grillat göteborg" in review
    assert k.cpc_bid_micros == 0 and g.cpc_bid_micros == 4000000
    with pytest.raises(WriteBlocked):
        flow.execute(pid, "user")
    store.transition(pid, "pending_approval", "approved", owner="user")
    assert flow.execute(pid, "user")["status"] == "verified"
    assert (k if keyword else g).cpc_bid_micros == change.cpc_bid_micros
    assert flow.execute(pid, "user")["status"] == "already_verified"
    assert [
        c.kwargs["request"].validate_only for c in gateway.service.mutate.call_args_list
    ] == [True, True, False]
    # A lost response can be reconciled by reading the explicit bid, without a replay.
    store.plans[pid]["status"], store.lock = "unknown", pid
    previous_calls = gateway.service.mutate.call_count
    assert (
        flow.reconcile_campaign_plan(pid, "user", "set_cpc_bid")["status"] == "verified"
    )
    assert store.lock is None and gateway.service.mutate.call_count == previous_calls
    if keyword:
        k.negative = True
        with pytest.raises(WriteBlocked):
            flow.preview(change, "user")
