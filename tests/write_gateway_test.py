"""Check real Google protobufs and mutation flags using mocked transport."""

import unittest
from unittest.mock import Mock

from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_mcp.write_gateway import GoogleAdsWriteGateway
from ads_mcp.write_policy import Change
from tests.write_workflow_test import CID, snapshot


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.gateway = object.__new__(GoogleAdsWriteGateway)
        self.gateway.client = GoogleAdsClient(
            credentials=AnonymousCredentials(), use_proto_plus=True
        )
        self.gateway.service = Mock()

    def test_campaign_create_is_always_paused_and_atomic(self):
        change = Change(
            action="create_paused_search",
            customer_id=CID,
            name="Fixture",
            amount_micros=200_000_000,
            eu_political_advertising="does_not_contain",
        )
        for validate in (True, False):
            self.gateway.mutate(
                change,
                {"account": snapshot()["account"]},
                validate_only=validate,
            )
            call = self.gateway.service.mutate.call_args.kwargs
            request = call["request"]
            self.assertEqual(request.validate_only, validate)
            self.assertFalse(request.partial_failure)
            self.assertIsNone(call["retry"])
            self.assertEqual(len(request.mutate_operations), 2)
            budget, campaign = request.mutate_operations
            self.assertEqual(
                campaign.campaign_operation.create.status.name, "PAUSED"
            )
            self.assertEqual(
                campaign.campaign_operation.create.advertising_channel_type.name,
                "SEARCH",
            )
            self.assertEqual(
                campaign.campaign_operation.create.contains_eu_political_advertising.name,
                "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
            )
            self.assertEqual(
                campaign.campaign_operation.create.campaign_budget,
                budget.campaign_budget_operation.create.resource_name,
            )
            self.assertFalse(
                budget.campaign_budget_operation.create.explicitly_shared
            )

    def test_status_changes_have_narrow_mask(self):
        for action, status in (("pause", "PAUSED"), ("activate", "ENABLED")):
            change = Change(action=action, customer_id=CID, campaign_id="42")
            op = self.gateway.operations(change, snapshot())[
                0
            ].campaign_operation
            self.assertEqual(op.update.status.name, status)
            self.assertEqual(list(op.update_mask.paths), ["status"])
            self.assertEqual(
                op.update.resource_name, f"customers/{CID}/campaigns/42"
            )

    def test_budget_cannot_change_status_or_other_fields(self):
        change = Change(
            action="set_budget",
            customer_id=CID,
            campaign_id="42",
            amount_micros=200_000_000,
        )
        op = self.gateway.operations(change, snapshot())[
            0
        ].campaign_budget_operation
        self.assertEqual(list(op.update_mask.paths), ["amount_micros"])
        self.assertEqual(op.update.amount_micros, 200_000_000)
