"""Adversarial tests use synthetic data only; no Google Ads writes."""

import copy
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from ads_mcp.write_policy import (
    AccountPolicy,
    Change,
    WriteBlocked,
    WritePolicy,
    principal,
)
from ads_mcp.write_workflow import WriteWorkflow
from ads_mcp.write_store import check_plan

CID = "1234567890"


def policy(mode="approval_required", activation=False):
    return WritePolicy(
        mode,
        {
            CID: AccountPolicy(
                currency="SEK",
                max_daily_budget_micros=500_000_000,
                allow_activation=activation,
            )
        },
        "https://ads.example.com",
        "synthetic-test-only-password-1234567890",
        "test-project",
        "(default)",
    )


def snapshot():
    return {
        "account": {
            "id": CID,
            "currency": "SEK",
            "time_zone": "Europe/Stockholm",
        },
        "campaign": {
            "id": "42",
            "name": "Fixture",
            "status": "ENABLED",
            "resource_name": f"customers/{CID}/campaigns/42",
            "budget_resource": f"customers/{CID}/campaignBudgets/7",
            "amount_micros": 100_000_000,
            "period": "DAILY",
            "shared": False,
            "channel": "SEARCH",
        },
        "affected_campaigns": [
            {"id": "42", "name": "Fixture", "status": "ENABLED"}
        ],
    }


class MemoryStore:
    """Test double only. Production has no in-memory write store."""

    def __init__(self):
        self.plans = {}
        self.lock = None

    def create(self, plan):
        self.plans[plan["id"]] = copy.deepcopy(plan)

    def get(self, plan_id):
        return copy.deepcopy(self.plans.get(plan_id))

    def transition(self, plan_id, expected, target, owner=None, result=None):
        plan = self.plans[plan_id]
        if expected != "executing":
            check_plan(plan, owner)
        if plan["status"] != expected:
            raise WriteBlocked("Wrong state")
        if target == "executing":
            if self.lock:
                raise WriteBlocked("Locked")
            self.lock = plan_id
        if expected == "executing" and target != "unknown":
            self.lock = None
        plan["status"] = target
        if result is not None:
            plan["result"] = result
        return copy.deepcopy(plan)


class FakeGateway:
    def __init__(self):
        self.current = snapshot()
        self.calls = []
        self.fail_validate = False
        self.fail_write = False
        self.fail_verify = False
        self.drift_on_validate = False

    def snapshot(self, change):
        if change.action == "create_paused_search":
            return {"account": copy.deepcopy(self.current["account"])}
        return copy.deepcopy(self.current)

    def mutate(self, change, before, *, validate_only):
        self.calls.append(validate_only)
        if validate_only and self.fail_validate:
            raise RuntimeError("Validation failed")
        if validate_only and self.drift_on_validate:
            self.current["campaign"]["amount_micros"] += 1
        if not validate_only and self.fail_write:
            raise TimeoutError("Remote result unknown")
        return object()

    def verify(self, *args):
        if self.fail_verify:
            raise RuntimeError("Read-back failed")
        return {"campaign": {"status": "PAUSED"}}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryStore()
        self.workflow = WriteWorkflow(
            policy(), self.store, lambda _: self.gateway
        )
        self.change = Change(action="pause", customer_id=CID, campaign_id="42")

    def approved(self, change=None):
        plan = self.workflow.preview(change or self.change, "owner-a")
        self.store.transition(plan["plan_id"], "pending_approval", "approved")
        return plan["plan_id"]

    def test_default_mode_is_dry_run(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(WritePolicy.load().mode, "dry_run")
            with self.assertRaises(WriteBlocked):
                WritePolicy.load().require_writes()

    def test_dry_run_never_saves_approval_or_writes(self):
        self.workflow.policy = policy("dry_run")
        result = self.workflow.preview(self.change, "owner-a")
        self.assertEqual(result["status"], "validated_only")
        self.assertEqual(self.gateway.calls, [True])
        self.assertFalse(self.store.plans)
        self.assertNotIn("approval_url", result)

    def test_preview_calls_only_google_validation(self):
        result = self.workflow.preview(self.change, "owner-a")
        self.assertEqual(self.gateway.calls, [True])
        self.assertEqual(
            self.store.get(result["plan_id"])["status"], "pending_approval"
        )

    def test_unapproved_plan_is_blocked(self):
        result = self.workflow.preview(self.change, "owner-a")
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(result["plan_id"], "owner-a")
        self.assertEqual(self.gateway.calls, [True])

    def test_approval_does_not_itself_send_write(self):
        self.approved()
        self.assertEqual(self.gateway.calls, [True])

    def test_same_plan_executes_only_once(self):
        plan_id = self.approved()
        self.assertEqual(
            self.workflow.execute(plan_id, "owner-a")["status"], "verified"
        )
        self.assertEqual(
            self.workflow.execute(plan_id, "owner-a")["status"],
            "already_verified",
        )
        self.assertEqual(self.gateway.calls, [True, True, False])
        self.assertIsNone(self.store.lock)

    def test_other_user_cannot_read_or_execute(self):
        plan_id = self.approved()
        for fn in (self.workflow.get_plan, self.workflow.execute):
            with self.assertRaises(WriteBlocked):
                fn(plan_id, "owner-b")
        self.assertEqual(self.gateway.calls, [True])

    def test_expired_approval_cannot_execute(self):
        plan_id = self.approved()
        self.store.plans[plan_id]["expires_at"] = time.time() - 1
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True])

    def test_tampered_plan_cannot_execute(self):
        plan_id = self.approved()
        self.store.plans[plan_id]["change"]["action"] = "activate"
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True])

    def test_budget_or_status_drift_stops_apply(self):
        plan_id = self.approved()
        self.gateway.current["campaign"]["amount_micros"] += 1
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True])
        self.assertEqual(
            self.store.get(plan_id)["status"], "failed_before_write"
        )

    def test_drift_during_validation_stops_apply(self):
        plan_id = self.approved()
        self.gateway.drift_on_validate = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True, True])

    def test_failed_revalidation_stops_apply(self):
        plan_id = self.approved()
        self.gateway.fail_validate = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True, True])
        self.assertIsNone(self.store.lock)

    def test_write_timeout_keeps_lock_and_cannot_retry(self):
        plan_id = self.approved()
        self.gateway.fail_write = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.store.get(plan_id)["status"], "unknown")
        self.assertEqual(self.store.lock, plan_id)
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        next_plan = self.approved()
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(next_plan, "owner-a")
        self.assertEqual(self.gateway.calls.count(False), 1)

    def test_readback_failure_keeps_lock(self):
        plan_id = self.approved()
        self.gateway.fail_verify = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.store.get(plan_id)["status"], "unknown")
        self.assertEqual(self.store.lock, plan_id)

    def test_execution_rechecks_current_policy(self):
        plan_id = self.approved()
        self.workflow.policy = replace(policy(), accounts={})
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.gateway.calls, [True])

    def test_switching_back_to_dry_run_blocks_old_approval(self):
        plan_id = self.approved()
        self.workflow.policy = policy("dry_run")
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")

    def test_account_allowlist_checked_before_google(self):
        self.workflow.policy = replace(policy(), accounts={})
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(self.change, "owner-a")
        self.assertFalse(self.gateway.calls)

    def test_budget_ceiling_currency_shared_and_period(self):
        change = Change(
            action="set_budget",
            customer_id=CID,
            campaign_id="42",
            amount_micros=200_000_000,
        )
        for field, value in (
            ("amount", 500_000_001),
            ("currency", "USD"),
            ("shared", True),
            ("period", "CUSTOM_PERIOD"),
            ("affected", "extra"),
        ):
            with self.subTest(field=field):
                state = snapshot()
                candidate = change
                if field == "amount":
                    candidate = change.model_copy(
                        update={"amount_micros": value}
                    )
                elif field == "currency":
                    state["account"]["currency"] = value
                elif field == "affected":
                    state["affected_campaigns"].append({"id": "43"})
                else:
                    state["campaign"][field] = value
                with self.assertRaises(WriteBlocked):
                    policy().check(candidate, state)

    def test_activation_disabled_by_default_and_separate(self):
        change = Change(action="activate", customer_id=CID, campaign_id="42")
        self.gateway.current["campaign"]["status"] = "PAUSED"
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(change, "owner-a")
        self.workflow.policy = policy(activation=True)
        result = self.workflow.preview(change, "owner-a")
        self.assertEqual(
            self.store.get(result["plan_id"])["status"], "pending_approval"
        )
        self.assertIn("spending", result["review"]["effect"])

    def test_missing_short_secret_or_storage_blocks_writes(self):
        for p in (
            replace(policy(), approval_secret=""),
            replace(policy(), project=""),
            replace(policy(), base_url="http://example.com"),
        ):
            with self.assertRaises(WriteBlocked):
                p.require_writes()

    def test_cannot_add_approved_status_or_raw_operations(self):
        for kwargs in (
            {"approved": True},
            {"status": "ENABLED"},
            {"operations": []},
            {"validate_only": False},
        ):
            with self.assertRaises(ValidationError):
                Change(
                    action="pause", customer_id=CID, campaign_id="42", **kwargs
                )

    def test_ids_cannot_inject_gaql(self):
        for value in ("42 OR 1=1", "42'", "-1", "１"):
            with self.assertRaises(ValidationError):
                Change(action="pause", customer_id=CID, campaign_id=value)

    def test_invalid_amounts_and_action_shapes(self):
        for value in (0, -1, True, 1.5, "100", 10**16):
            with self.assertRaises(ValidationError):
                Change(
                    action="set_budget",
                    customer_id=CID,
                    campaign_id="42",
                    amount_micros=value,
                )
        for candidate in (
            Change(action="pause", customer_id=CID),
            Change(
                action="pause",
                customer_id=CID,
                campaign_id="42",
                amount_micros=3,
            ),
            Change(
                action="create_paused_search",
                customer_id=CID,
                name="x",
                amount_micros=3,
            ),
        ):
            with self.assertRaises(WriteBlocked):
                candidate.check_shape()

    def test_oauth_required_no_adc_fallback(self):
        with patch(
            "fastmcp.server.dependencies.get_access_token", return_value=None
        ):
            with self.assertRaises(WriteBlocked):
                principal()
        token = SimpleNamespace(token="synthetic", claims={"sub": "user-1"})
        with patch(
            "fastmcp.server.dependencies.get_access_token", return_value=token
        ):
            identity = principal()
            self.assertEqual(len(identity), 64)
            self.assertNotIn("user-1", identity)
            self.assertNotIn("synthetic", identity)
