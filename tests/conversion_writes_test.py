"""Synthetic conversions only: guardrails, protobufs, replay and read-back."""

import copy
import unittest
from unittest.mock import Mock
from types import SimpleNamespace as NS
from pydantic import ValidationError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from ads_mcp.conversion_writes import (
    ConversionSettings,
    snapshot_conversion,
    verify_conversion,
)
from ads_mcp.conversion_review import render_conversion_review
from ads_mcp.write_policy import Change, WriteBlocked
from ads_mcp.write_gateway import GoogleAdsWriteGateway
from ads_mcp.write_workflow import WriteWorkflow
from tests.write_workflow_test import policy, MemoryStore, CID


def state():
    action = dict(
        id="42",
        resource_name=f"customers/{CID}/conversionActions/42",
        name="Existing",
        type="WEBPAGE",
        status="ENABLED",
        owner_customer=f"customers/{CID}",
        category="PURCHASE",
        primary_for_goal=True,
        counting_type="MANY_PER_CLICK",
        click_through_lookback_window_days=30,
        view_through_lookback_window_days=1,
        default_value=10.0,
        default_currency_code="SEK",
        always_use_default_value=False,
    )
    return dict(
        account=dict(
            id=CID,
            currency="SEK",
            time_zone="Europe/Stockholm",
            manager=False,
            conversion_customer=f"customers/{CID}",
        ),
        conversion=action,
        conversion_inventory=[action],
        potentially_affected_campaigns=[
            dict(id="8", name="Campaign", status="ENABLED")
        ],
        custom_goals=[],
    )


def create():
    return Change(
        action="create_conversion",
        customer_id=CID,
        conversion_settings=ConversionSettings(
            name="New purchase", category="PURCHASE", counting_type="MANY_PER_CLICK"
        ),
    )


class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.gateway = object.__new__(GoogleAdsWriteGateway)
        self.gateway.client = GoogleAdsClient(
            credentials=AnonymousCredentials(), use_proto_plus=True
        )
        self.gateway.service = Mock()

    def test_creation_always_secondary_and_validate_only(self):
        self.gateway.mutate(create(), state(), validate_only=True)
        args = self.gateway.service.mutate.call_args.kwargs
        request = args["request"]
        self.assertTrue(request.validate_only)
        self.assertFalse(request.partial_failure)
        self.assertIsNone(args["retry"])
        action = request.mutate_operations[0].conversion_action_operation.create
        self.assertEqual(action.type_.name, "WEBPAGE")
        self.assertEqual(action.status.name, "ENABLED")
        self.assertFalse(action.primary_for_goal)
        self.assertEqual(action.value_settings.default_currency_code, "SEK")

    def test_update_mask_preserves_explicit_false_and_zero(self):
        change = Change(
            action="update_conversion",
            customer_id=CID,
            conversion_action_id="42",
            conversion_settings=ConversionSettings(
                default_value=0.0, always_use_default_value=False
            ),
        )
        op = self.gateway.operations(change, state())[0].conversion_action_operation
        self.assertEqual(
            set(op.update_mask.paths),
            {"value_settings.default_value", "value_settings.always_use_default_value"},
        )
        self.assertNotIn("primary_for_goal", op.update_mask.paths)
        self.assertEqual(
            op.update.resource_name, f"customers/{CID}/conversionActions/42"
        )

    def test_primary_is_separate_single_field(self):
        change = Change(
            action="set_conversion_primary",
            customer_id=CID,
            conversion_action_id="42",
            primary_for_goal=False,
        )
        op = self.gateway.operations(change, state())[0].conversion_action_operation
        self.assertEqual(list(op.update_mask.paths), ["primary_for_goal"])
        self.assertFalse(op.update.primary_for_goal)

    def test_invalid_inputs_rejected(self):
        for data in (
            {"type": "UPLOAD_CLICKS"},
            {"status": "REMOVED"},
            {"primary_for_goal": True},
            {"default_value": float("nan")},
            {"default_value": -1.0},
            {"name": "\n"},
            {"click_through_lookback_window_days": 91},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                ConversionSettings(**data)
        for change in (
            create().model_copy(update={"primary_for_goal": True}),
            create().model_copy(update={"campaign_id": "1"}),
            create().model_copy(update={"conversion_action_id": "42"}),
            create().model_copy(update={"conversion_settings": ConversionSettings()}),
        ):
            with self.assertRaises(WriteBlocked):
                change.check_shape()

    def test_shared_owner_wrong_currency_and_duplicates_blocked(self):
        for key, value in (
            ("conversion_customer", "customers/9999999999"),
            ("manager", True),
            ("currency", "USD"),
        ):
            snapshot = state()
            snapshot["account"][key] = value
            with self.subTest(key=key), self.assertRaises(WriteBlocked):
                policy().check(create(), snapshot)
        duplicate = create().model_copy(
            update={
                "conversion_settings": ConversionSettings(
                    name="existing", category="PURCHASE", counting_type="MANY_PER_CLICK"
                )
            }
        )
        with self.assertRaises(WriteBlocked):
            policy().check(duplicate, state())

    def test_ga4_removed_foreign_conversions_blocked(self):
        change = Change(
            action="set_conversion_primary",
            customer_id=CID,
            conversion_action_id="42",
            primary_for_goal=False,
        )
        for key, value in (
            ("type", "GOOGLE_ANALYTICS_4_PURCHASE"),
            ("status", "REMOVED"),
            ("owner_customer", "customers/9999999999"),
            ("resource_name", "customers/9999999999/conversionActions/42"),
        ):
            snapshot = state()
            snapshot["conversion"][key] = value
            with self.subTest(key=key), self.assertRaises(WriteBlocked):
                policy().check(change, snapshot)

    def test_protobuf_snapshot_fields(self):
        customer = self.gateway.client.get_type("Customer")
        customer.id = int(CID)
        customer.currency_code = "SEK"
        customer.time_zone = "Europe/Stockholm"
        customer.conversion_tracking_setting.google_ads_conversion_customer = (
            f"customers/{CID}"
        )
        action = self.gateway.client.get_type("ConversionAction")
        for key, value in state()["conversion"].items():
            if key in (
                "default_value",
                "default_currency_code",
                "always_use_default_value",
            ):
                setattr(action.value_settings, key, value)
            else:
                setattr(
                    action,
                    "type_" if key == "type" else key,
                    int(value) if key == "id" else value,
                )
        self.gateway.rows = Mock(
            side_effect=[
                [NS(customer=customer)],
                [NS(conversion_action=action)],
                [],
                [],
            ]
        )
        change = Change(
            action="set_conversion_primary",
            customer_id=CID,
            conversion_action_id="42",
            primary_for_goal=False,
        )
        actual = snapshot_conversion(self.gateway, change)
        self.assertEqual(actual["conversion"], state()["conversion"])

    def test_workflow_approval_drift_and_replay(self):
        fake = Mock()
        current = state()
        fake.snapshot.side_effect = lambda c: copy.deepcopy(current)
        fake.verify.return_value = {
            "conversion": {"id": "99", "primary_for_goal": False}
        }
        store = MemoryStore()
        workflow = WriteWorkflow(policy(), store, lambda _: fake)
        result = workflow.preview(create(), "owner")
        fake.mutate.assert_called_once()
        self.assertTrue(fake.mutate.call_args.kwargs["validate_only"])
        with self.assertRaises(WriteBlocked):
            workflow.execute(result["plan_id"], "owner")
        store.transition(result["plan_id"], "pending_approval", "approved")
        current["custom_goals"].append({"name": "Changed goal"})
        with self.assertRaises(WriteBlocked):
            workflow.execute(result["plan_id"], "owner")
        self.assertTrue(
            all(c.kwargs["validate_only"] for c in fake.mutate.call_args_list)
        )
        result = workflow.preview(create(), "owner")
        store.transition(result["plan_id"], "pending_approval", "approved")
        self.assertEqual(
            workflow.execute(result["plan_id"], "owner")["status"], "verified"
        )
        count = fake.mutate.call_count
        self.assertEqual(
            workflow.execute(result["plan_id"], "owner")["status"], "already_verified"
        )
        self.assertEqual(fake.mutate.call_count, count)

    def test_review_escapes_content_and_shows_before_after(self):
        fake = Mock()
        fake.snapshot.return_value = state()
        workflow = WriteWorkflow(policy(), MemoryStore(), lambda _: fake)
        change = create().model_copy(
            update={
                "conversion_settings": ConversionSettings(
                    name="<script>bad</script>",
                    category="PURCHASE",
                    counting_type="MANY_PER_CLICK",
                )
            }
        )
        result = workflow.preview(change, "owner")
        output = render_conversion_review(result)
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn("Före", output)
        self.assertIn("Efter", output)

    def test_readback_mismatch_fails(self):
        from unittest.mock import patch

        change = Change(
            action="set_conversion_primary",
            customer_id=CID,
            conversion_action_id="42",
            primary_for_goal=False,
        )
        with patch(
            "ads_mcp.conversion_writes.snapshot_conversion", return_value=state()
        ):
            with self.assertRaises(WriteBlocked):
                verify_conversion(self.gateway, change, state(), None)


if __name__ == "__main__":
    unittest.main()
