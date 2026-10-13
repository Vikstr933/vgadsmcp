"""Preview -> independently approve -> atomically claim -> validate -> apply."""

import time
import uuid

from ads_mcp.write_policy import Change, WriteBlocked, WritePolicy, digest
from ads_mcp.write_store import FirestorePlanStore, check_plan
from ads_mcp.write_gateway import GoogleAdsWriteGateway


def describe(change, before):
    currency = before["account"]["currency"]
    if change.action == "create_paused_search":
        return {
            "before": "No campaign or budget created by this plan.",
            "after": {
                "name": change.name,
                "status": "PAUSED",
                "daily_budget_micros": change.amount_micros,
                "currency": currency,
                "bidding": "MANUAL_CPC",
                "network": "Google Search only",
                "eu_political_advertising": change.eu_political_advertising,
            },
            "effect": "Create an empty Search campaign and a dedicated daily budget. No ads, keywords, targeting or ad groups are created. Nothing is activated.",
            "rollback": "Leave the campaign paused. Deletion is not exposed.",
        }
    campaign = before["campaign"]
    if change.action == "set_budget":
        return {
            "before": {
                "daily_budget_micros": campaign["amount_micros"],
                "currency": currency,
            },
            "after": {
                "daily_budget_micros": change.amount_micros,
                "currency": currency,
            },
            "effect": "Changes the average daily budget. Delivery, spend and learning may change immediately for an active campaign. This is not a hard daily spend cap.",
            "rollback": "Preview and approve a new budget change to the previous amount.",
        }
    return {
        "before": campaign["status"],
        "after": "ENABLED" if change.action == "activate" else "PAUSED",
        "effect": (
            "Activation can start ad delivery and spending immediately. Review all ads, targeting, bids and policy eligibility in Google Ads before approving."
            if change.action == "activate"
            else "Stops campaign delivery; pacing and learning may be affected."
        ),
        "rollback": "Preview and approve a separate status change. Activation always needs its own approval.",
    }


class WriteWorkflow:
    def __init__(self, policy=None, store=None, gateway_factory=None):
        self.policy = policy or WritePolicy.load()
        self._store = store
        self.gateway_factory = gateway_factory or GoogleAdsWriteGateway

    @property
    def store(self):
        if self._store is None:
            self._store = FirestorePlanStore(self.policy)
        return self._store

    def preview(self, change: Change, owner: str):
        change.check_shape()
        self.policy.account_policy(change)
        gateway = self.gateway_factory(change.login_customer_id)
        before = self.snapshot(gateway, change)
        self.policy.check(change, before)
        gateway.mutate(change, before, validate_only=True)
        review = {
            "customer_id": change.customer_id,
            "change": change.model_dump(),
            "snapshot": before,
            "review": describe(change, before),
        }
        if self.policy.mode == "dry_run":
            return {
                "status": "validated_only",
                "google_ads_changed": False,
                **review,
            }
        self.policy.require_writes()
        plan = {
            **review,
            "id": uuid.uuid4().hex,
            "owner": owner,
            "created_at": time.time(),
            "expires_at": time.time() + 1800,
            "status": "pending_approval",
            "review_digest": digest(review),
        }
        self.store.create(plan)
        return {
            "status": "pending_approval",
            "google_ads_changed": False,
            "plan_id": plan["id"],
            "expires_at": plan["expires_at"],
            "approval_url": f"{self.policy.base_url}/approvals/{plan['id']}",
            **review,
        }

    def snapshot(self, gateway, change):
        scope = None
        if self.policy.requires_manager_scope(change):
            scope = gateway.manager_scope(
                change.login_customer_id, change.customer_id
            )
        before = gateway.snapshot(change)
        if scope is not None:
            before["manager_scope"] = scope
        return before

    def get_plan(self, plan_id, owner):
        plan = self.store.get(plan_id)
        if not plan or plan["owner"] != owner:
            raise WriteBlocked("Plan not found for this user.")
        return {
            key: value
            for key, value in plan.items()
            if key not in ("owner", "delete_after")
        }

    def execute(self, plan_id, owner):
        self.policy.require_writes()
        plan = self.store.get(plan_id)
        if not plan or plan["owner"] != owner:
            raise WriteBlocked("Plan not found for this user.")
        if plan["status"] == "verified":
            return {
                "status": "already_verified",
                "plan_id": plan_id,
                "result": plan["result"],
            }
        check_plan(plan, owner)
        if plan["status"] != "approved":
            raise WriteBlocked(
                "Open the approval URL and approve the exact proposal first."
            )
        review = {
            key: plan[key]
            for key in ("customer_id", "change", "snapshot", "review")
        }
        if digest(review) != plan["review_digest"]:
            raise WriteBlocked("Plan content changed; create a new preview.")
        change = Change.model_validate(plan["change"])
        self.policy.check(change, plan["snapshot"])
        self.store.transition(plan_id, "approved", "executing", owner=owner)
        sent_write = False
        try:
            gateway = self.gateway_factory(change.login_customer_id)
            current = self.snapshot(gateway, change)
            if current != plan["snapshot"]:
                raise WriteBlocked(
                    "Campaign or budget changed since preview. Create and approve a new plan."
                )
            self.policy.check(change, current)
            gateway.mutate(change, current, validate_only=True)
            # Review could expire or external changes could occur during validation.
            check_plan(plan, owner)
            if self.snapshot(gateway, change) != current:
                raise WriteBlocked(
                    "Account state changed during validation. Create a new plan."
                )
            sent_write = True
            response = gateway.mutate(change, current, validate_only=False)
            actual = gateway.verify(change, current, response)
            self.store.transition(
                plan_id, "executing", "verified", owner=owner, result=actual
            )
            return {"status": "verified", "plan_id": plan_id, "result": actual}
        except Exception:
            # Once a live call starts, a timeout may mean Google already applied it.
            # Keep the account lock and never retry that write automatically.
            self.store.transition(
                plan_id,
                "executing",
                "unknown" if sent_write else "failed_before_write",
                owner=owner,
            )
            raise WriteBlocked(
                "Write outcome uncertain. No retry is permitted; inspect Google Ads and reconcile the account lock."
                if sent_write
                else "No write sent. Validation or state check failed; create a new preview."
            ) from None
