"""MCC scope, per-client isolation and revocation tests with synthetic accounts."""

import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ads_mcp.write_gateway import GoogleAdsWriteGateway
from ads_mcp.write_policy import (
    AccountPolicy,
    Change,
    WriteBlocked,
    WritePolicy,
)
from ads_mcp.write_workflow import WriteWorkflow
from tests.write_workflow_test import CID, FakeGateway, MemoryStore, policy

MCC = "2222222222"
SECOND = "3333333333"


class ManagerGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.linked = True
        self.scope_checks = 0
        self.unlink_during_validation = False

    def manager_scope(self, manager_id, customer_id):
        self.scope_checks += 1
        if not self.linked:
            raise WriteBlocked("Unlinked")
        return {"manager_id": manager_id, "customer_id": customer_id}

    def mutate(self, *args, **kwargs):
        result = super().mutate(*args, **kwargs)
        if self.unlink_during_validation:
            self.linked = False
        return result


class ManagerPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = replace(
            policy(), accounts={}, managers={MCC: policy().accounts[CID]}
        )
        self.gateway = ManagerGateway()
        self.workflow = WriteWorkflow(
            self.policy, MemoryStore(), lambda _: self.gateway
        )
        self.change = Change(
            action="pause",
            customer_id=CID,
            login_customer_id=MCC,
            campaign_id="42",
        )

    def test_manager_configuration_loads_and_defaults_to_dry_run(self):
        raw = {MCC: {"currency": "SEK", "max_daily_budget_micros": 1000000000}}
        with patch.dict(
            "os.environ",
            {"GOOGLE_ADS_MCP_WRITE_MANAGERS": json.dumps(raw)},
            clear=True,
        ):
            actual = WritePolicy.load()
        self.assertEqual(actual.mode, "dry_run")
        self.assertEqual(actual.managers[MCC].currency, "SEK")
        self.assertEqual(actual.accounts, {})
        self.assertFalse(actual.managers[MCC].allow_activation)

    def test_unconfigured_manager_and_missing_manager_are_blocked_before_google(
        self,
    ):
        for manager in (None, SECOND):
            change = self.change.model_copy(
                update={"login_customer_id": manager}
            )
            with self.assertRaises(WriteBlocked):
                self.workflow.preview(change, "user")
        self.assertEqual(self.gateway.scope_checks, 0)
        self.assertEqual(self.gateway.calls, [])

    def test_multiple_clients_share_ceiling_without_sharing_plans(self):
        plans = []
        self.workflow.policy = replace(self.policy, mode="dry_run")
        for cid in (CID, SECOND):
            self.gateway.current["account"]["id"] = cid
            change = Change(
                action="create_paused_search",
                customer_id=cid,
                login_customer_id=MCC,
                name="Synthetic",
                amount_micros=100000000,
                eu_political_advertising="does_not_contain",
            )
            result = self.workflow.preview(change, "user")
            self.assertEqual(result["status"], "validated_only")
            self.assertEqual(
                result["snapshot"]["manager_scope"]["customer_id"], cid
            )
            plans.append(result)
        self.assertNotEqual(plans[0]["customer_id"], plans[1]["customer_id"])
        self.assertEqual(self.gateway.calls, [True, True])

    def test_member_with_wrong_currency_is_blocked_before_validation(self):
        self.gateway.current["account"]["currency"] = "EUR"
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(self.change, "user")
        self.assertEqual(self.gateway.calls, [])

    def test_mcc_budget_ceiling_applies(self):
        change = self.change.model_copy(
            update={"action": "set_budget", "amount_micros": 500000001}
        )
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(change, "user")
        self.assertEqual(self.gateway.calls, [])

    def test_explicit_client_policy_cannot_be_bypassed_via_mcc(self):
        self.workflow.policy = replace(
            self.policy,
            accounts={
                CID: AccountPolicy(
                    currency="SEK", max_daily_budget_micros=100000000
                )
            },
        )
        change = self.change.model_copy(
            update={"action": "set_budget", "amount_micros": 200000000}
        )
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(change, "user")
        self.assertEqual(self.gateway.calls, [])

    def test_missing_or_other_membership_proof_is_rejected(self):
        for scope in (None, {"manager_id": MCC, "customer_id": SECOND}):
            before = copy.deepcopy(self.gateway.current)
            if scope is not None:
                before["manager_scope"] = scope
            with self.assertRaises(WriteBlocked):
                self.policy.check(self.change, before)

    def test_unlinked_member_cannot_even_preview(self):
        self.gateway.linked = False
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(self.change, "user")
        self.assertEqual(self.gateway.calls, [])

    def test_membership_revoked_after_approval_prevents_write(self):
        result = self.workflow.preview(self.change, "user")
        self.workflow.store.transition(
            result["plan_id"], "pending_approval", "approved"
        )
        self.gateway.linked = False
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(result["plan_id"], "user")
        self.assertEqual(self.gateway.calls, [True])
        self.assertIsNone(self.workflow.store.lock)

    def test_membership_revoked_during_revalidation_prevents_write(self):
        result = self.workflow.preview(self.change, "user")
        self.workflow.store.transition(
            result["plan_id"], "pending_approval", "approved"
        )
        self.gateway.unlink_during_validation = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(result["plan_id"], "user")
        self.assertEqual(self.gateway.calls, [True, True])

    def test_mcc_approval_is_still_required(self):
        result = self.workflow.preview(self.change, "user")
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(result["plan_id"], "user")
        self.workflow.store.transition(
            result["plan_id"], "pending_approval", "approved"
        )
        self.assertEqual(
            self.workflow.execute(result["plan_id"], "user")["status"],
            "verified",
        )
        self.assertEqual(self.gateway.calls, [True, True, False])
        self.assertEqual(self.gateway.scope_checks, 3)


class ManagerGatewayTest(unittest.TestCase):
    def setUp(self):
        self.gateway = object.__new__(GoogleAdsWriteGateway)
        self.gateway.rows = Mock()

    def row(self, **kwargs):
        data = {
            "id": int(CID),
            "manager": False,
            "level": 2,
            "status": SimpleNamespace(name="ENABLED"),
        }
        data.update(kwargs)
        return SimpleNamespace(customer_client=SimpleNamespace(**data))

    def test_transitive_child_is_verified_under_selected_manager(self):
        self.gateway.rows.return_value = [self.row()]
        self.assertEqual(
            self.gateway.manager_scope(MCC, CID),
            {"manager_id": MCC, "customer_id": CID},
        )
        manager, query = self.gateway.rows.call_args.args
        self.assertEqual(manager, MCC)
        self.assertIn(f"customer_client.id = {CID}", query)
        self.assertNotIn("level <= 1", query)

    def test_missing_ambiguous_manager_self_cancelled_wrong_customer_are_blocked(
        self,
    ):
        for rows in (
            [],
            [self.row(), self.row()],
            [self.row(manager=True)],
            [self.row(level=0)],
            [self.row(id=int(SECOND))],
            [self.row(status=SimpleNamespace(name="CANCELED"))],
        ):
            with self.subTest(rows=rows):
                self.gateway.rows.return_value = rows
                with self.assertRaises(WriteBlocked):
                    self.gateway.manager_scope(MCC, CID)
