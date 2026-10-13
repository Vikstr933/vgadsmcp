"""Exercise production transaction logic against a deterministic Firestore double."""

import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from google.cloud import firestore

from ads_mcp.write_policy import Change, WriteBlocked
from ads_mcp.write_store import FirestorePlanStore
from ads_mcp.write_workflow import WriteWorkflow
from tests.write_workflow_test import CID, FakeGateway, policy


class Ref:
    def __init__(self, data, key):
        self.data, self.key = data, key

    def create(self, value):
        if self.key in self.data:
            raise ValueError("Already exists")
        self.data[self.key] = copy.deepcopy(value)

    def get(self, transaction=None):
        return self

    def to_dict(self):
        return copy.deepcopy(self.data.get(self.key))


class Collection:
    def __init__(self, data, name):
        self.data, self.name = data, name

    def document(self, name):
        return Ref(self.data, f"{self.name}/{name}")


class Transaction:
    def __init__(self):
        self.actions = []

    def create(self, ref, value):
        self.actions.append(lambda: ref.create(value))

    def delete(self, ref):
        self.actions.append(lambda: ref.data.pop(ref.key))

    def update(self, ref, values):
        def apply():
            for key, value in values.items():
                if value is firestore.DELETE_FIELD:
                    ref.data[ref.key].pop(key, None)
                else:
                    ref.data[ref.key][key] = copy.deepcopy(value)

        self.actions.append(apply)


class FirestoreDouble:
    def __init__(self):
        self.data = {}
        self.mutex = threading.RLock()

    def collection(self, name):
        return Collection(self.data, name)

    def transaction(self):
        return Transaction()

    def transactional(self, func):
        def run(transaction):
            with self.mutex:
                result = func(transaction)
                for action in transaction.actions:
                    action()
                return result

        return run


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = FirestoreDouble()
        for p in (
            patch("google.cloud.firestore.Client", return_value=self.db),
            patch(
                "google.cloud.firestore.transactional",
                side_effect=self.db.transactional,
            ),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.store = FirestorePlanStore(policy())
        self.workflow = WriteWorkflow(
            policy(), self.store, lambda _: FakeGateway()
        )
        self.plan_id = self.workflow.preview(
            Change(action="pause", customer_id=CID, campaign_id="42"), "owner"
        )["plan_id"]

    def test_approve_claim_complete_and_persistent_retention(self):
        self.store.transition(self.plan_id, "pending_approval", "approved")
        self.store.transition(
            self.plan_id, "approved", "executing", owner="owner"
        )
        self.assertNotIn("delete_after", self.store.get(self.plan_id))
        self.store.transition(
            self.plan_id,
            "executing",
            "verified",
            owner="owner",
            result={"status": "PAUSED"},
        )
        self.assertIn("delete_after", self.store.get(self.plan_id))
        self.assertEqual(
            self.store.get(self.plan_id)["result"], {"status": "PAUSED"}
        )
        self.assertNotIn(f"ads_mcp_write_locks/{CID}", self.db.data)

    def test_two_replicas_cannot_claim_same_approval(self):
        self.store.transition(self.plan_id, "pending_approval", "approved")

        def claim(_):
            try:
                FirestorePlanStore(policy()).transition(
                    self.plan_id, "approved", "executing", owner="owner"
                )
                return True
            except WriteBlocked:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_unknown_outcome_keeps_persistent_lock_across_instances(self):
        self.store.transition(self.plan_id, "pending_approval", "approved")
        self.store.transition(
            self.plan_id, "approved", "executing", owner="owner"
        )
        self.store.transition(
            self.plan_id, "executing", "unknown", owner="owner"
        )
        self.assertNotIn("delete_after", self.store.get(self.plan_id))
        another = self.workflow.preview(
            Change(action="pause", customer_id=CID, campaign_id="42"), "owner"
        )["plan_id"]
        self.store.transition(another, "pending_approval", "approved")
        with self.assertRaises(WriteBlocked):
            FirestorePlanStore(policy()).transition(
                another, "approved", "executing", owner="owner"
            )

    def test_wrong_owner_and_expired_approval_are_rejected(self):
        with self.assertRaises(WriteBlocked):
            self.store.transition(
                self.plan_id, "pending_approval", "approved", owner="attacker"
            )
        self.db.data[f"ads_mcp_write_plans/{self.plan_id}"]["expires_at"] = (
            time.time() - 1
        )
        with self.assertRaises(WriteBlocked):
            self.store.transition(self.plan_id, "pending_approval", "approved")

    def test_write_result_can_be_recorded_after_plan_expiry(self):
        self.store.transition(self.plan_id, "pending_approval", "approved")
        self.store.transition(
            self.plan_id, "approved", "executing", owner="owner"
        )
        self.db.data[f"ads_mcp_write_plans/{self.plan_id}"]["expires_at"] = (
            time.time() - 1
        )
        self.store.transition(
            self.plan_id, "executing", "verified", owner="owner", result={}
        )
        self.assertEqual(self.store.get(self.plan_id)["status"], "verified")

    def test_wrong_account_lock_cannot_be_released(self):
        self.store.transition(self.plan_id, "pending_approval", "approved")
        self.store.transition(
            self.plan_id, "approved", "executing", owner="owner"
        )
        self.db.data[f"ads_mcp_write_locks/{CID}"]["plan_id"] = "different-plan"
        with self.assertRaises(WriteBlocked):
            self.store.transition(
                self.plan_id, "executing", "verified", owner="owner", result={}
            )
