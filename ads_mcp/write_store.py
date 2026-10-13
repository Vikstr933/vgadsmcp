"""Persistent plans and atomic replay protection shared across Cloud Run replicas."""

import copy
import time
from datetime import datetime, timedelta, timezone

from ads_mcp.write_policy import WriteBlocked, WritePolicy


def check_plan(plan, owner=None, now=None):
    if not plan or (owner is not None and plan["owner"] != owner):
        raise WriteBlocked("Plan not found for this user.")
    if plan["expires_at"] <= (time.time() if now is None else now):
        raise WriteBlocked("Plan expired. Preview and approve a new plan.")


class FirestorePlanStore:
    """Never fall back to ephemeral memory for approved writes."""

    def __init__(self, policy: WritePolicy):
        from google.cloud import firestore

        if not policy.project:
            raise WriteBlocked(
                "Configure WRITE_FIRESTORE_PROJECT before saving approval plans."
            )
        self.client = firestore.Client(
            project=policy.project, database=policy.database
        )
        self.plans = self.client.collection("ads_mcp_write_plans")
        self.locks = self.client.collection("ads_mcp_write_locks")

    def create(self, plan):
        record = copy.deepcopy(plan)
        record["delete_after"] = datetime.now(timezone.utc) + timedelta(days=30)
        self.plans.document(plan["id"]).create(record)

    def get(self, plan_id):
        return self.plans.document(plan_id).get().to_dict()

    def transition(self, plan_id, expected, target, owner=None, result=None):
        from google.cloud import firestore

        ref = self.plans.document(plan_id)

        @firestore.transactional
        def change(transaction):
            plan = ref.get(transaction=transaction).to_dict()
            if not plan or (owner is not None and plan["owner"] != owner):
                raise WriteBlocked("Plan not found for this user.")
            if expected != "executing":
                check_plan(plan, owner)
            if plan["status"] != expected:
                raise WriteBlocked("Plan is no longer in the required state.")
            lock_ref = self.locks.document(plan["change"]["customer_id"])
            lock = lock_ref.get(transaction=transaction).to_dict()
            if target == "executing" and lock:
                raise WriteBlocked(
                    "This account has an executing or unresolved change. Reconcile it before retrying."
                )
            updates = {"status": target, "updated_at": time.time()}
            if result is not None:
                updates["result"] = result
            if target == "executing":
                transaction.create(lock_ref, {"plan_id": plan_id})
                # An uncertain mutation must not be silently forgotten by TTL.
                updates["delete_after"] = firestore.DELETE_FIELD
            if expected == "executing" and target != "unknown":
                if not lock or lock["plan_id"] != plan_id:
                    raise WriteBlocked(
                        "Account lock changed; operator reconciliation required."
                    )
                transaction.delete(lock_ref)
                updates["delete_after"] = datetime.now(
                    timezone.utc
                ) + timedelta(days=30)
            transaction.update(ref, updates)
            return {**plan, **updates}

        return change(self.client.transaction())
