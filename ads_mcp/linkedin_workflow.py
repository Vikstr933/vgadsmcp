"""Narrow LinkedIn changes with independent approval and non-replayable execution."""

import copy
import json
import os
import re
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ads_mcp.linkedin_client import LinkedInAPI, LinkedInRejected, linkedin_id
from ads_mcp.linkedin_creatives import (
    ImageAd,
    campaign_org,
    image_status,
    verify_creative,
)
from ads_mcp.write_policy import WriteBlocked, WritePolicy, digest
from ads_mcp.write_store import FirestorePlanStore, check_plan


def amount(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,12}(\.[0-9]{1,2})?", value
    ):
        raise WriteBlocked(
            "Money must be a positive decimal string with at most two decimal places."
        )
    if Decimal(value) <= 0:
        raise WriteBlocked("Money must be greater than zero.")
    return format(Decimal(value), ".2f")


class CampaignDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=150)
    campaign_group_id: str
    organization_id: str
    objective: Literal[
        "WEBSITE_VISIT",
        "WEBSITE_CONVERSION",
        "LEAD_GENERATION",
        "BRAND_AWARENESS",
        "ENGAGEMENT",
    ]
    locale_language: str = Field(pattern=r"^[a-z]{2}$")
    locale_country: str = Field(pattern=r"^[A-Z]{2}$")
    non_political_confirmed: Literal[True]
    daily_budget: str
    bid_amount: str
    cost_type: Literal["CPC", "CPM"]
    start_ms: int = Field(gt=0)
    end_ms: int = Field(gt=0)
    targeting_criteria: dict

    @field_validator("campaign_group_id", "organization_id")
    @classmethod
    def ids(cls, value):
        return linkedin_id(value)

    @field_validator("daily_budget", "bid_amount")
    @classmethod
    def money(cls, value):
        return amount(value)

    @field_validator("name")
    @classmethod
    def printable(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Use a printable campaign name.")
        return value

    @model_validator(mode="after")
    def valid_schedule_and_targeting(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Campaign end must follow start.")
        if not self.targeting_criteria.get("include") or set(
            self.targeting_criteria
        ) - {"include", "exclude"}:
            raise ValueError("Explicit LinkedIn include targeting is required.")
        if len(json.dumps(self.targeting_criteria)) > 20000:
            raise ValueError("Targeting is too large.")
        return self

    def payload(self, account_id, currency, marker):
        return {
            "account": f"urn:li:sponsoredAccount:{account_id}",
            "campaignGroup": f"urn:li:sponsoredCampaignGroup:{self.campaign_group_id}",
            "associatedEntity": f"urn:li:organization:{self.organization_id}",
            "name": self.name + " [viks-" + marker + "]",
            "type": "SPONSORED_UPDATES",
            "format": "STANDARD_UPDATE",
            "objectiveType": self.objective,
            "locale": {
                "language": self.locale_language,
                "country": self.locale_country,
            },
            "politicalIntent": "NOT_POLITICAL",
            "status": "PAUSED",
            "dailyBudget": {
                "amount": self.daily_budget,
                "currencyCode": currency,
            },
            "unitCost": {"amount": self.bid_amount, "currencyCode": currency},
            "costType": self.cost_type,
            "runSchedule": {"start": self.start_ms, "end": self.end_ms},
            "targetingCriteria": self.targeting_criteria,
            "audienceExpansionEnabled": False,
            "offsiteDeliveryEnabled": False,
        }


class LinkedInChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["pause", "set_budget", "create_campaign", "create_image_ad"]
    creative: ImageAd | None = None
    account_id: str
    campaign_id: str | None = None
    daily_budget: str | None = None
    campaign: CampaignDraft | None = None

    @field_validator("account_id", "campaign_id")
    @classmethod
    def ids(cls, value):
        return linkedin_id(value) if value is not None else None

    @field_validator("daily_budget")
    @classmethod
    def money(cls, value):
        return amount(value) if value is not None else None

    @model_validator(mode="after")
    def shape(self):
        if (self.action == "create_image_ad") != (self.creative is not None):
            raise ValueError("Only image ads require a creative draft.")
        create = self.action == "create_campaign"
        if create != (self.campaign is not None) or create == (
            self.campaign_id is not None
        ):
            raise ValueError(
                "Creation requires a campaign draft; changes require a campaign ID."
            )
        if (self.action == "set_budget") != (self.daily_budget is not None):
            raise ValueError("Only a budget change accepts daily_budget.")
        return self


def approval_policy():
    # Only infrastructure and the server-side signing secret are shared with Google.
    return replace(
        WritePolicy.load(),
        mode=os.getenv("LINKEDIN_MCP_WRITE_MODE", "dry_run"),
        accounts={},
        managers={},
    )


def account_policy(account_id):
    try:
        policies = json.loads(os.getenv("LINKEDIN_MCP_WRITE_ACCOUNTS", "{}"))
        policy = policies[account_id]
        if not re.fullmatch(r"[A-Z]{3}", policy["currency"]):
            raise ValueError()
        ceiling = amount(policy["max_daily_budget"])
        return {"currency": policy["currency"], "max_daily_budget": ceiling}
    except (ValueError, TypeError, KeyError):
        raise WriteBlocked(
            "LinkedIn writes require an explicit account ID, currency and daily budget ceiling in LINKEDIN_MCP_WRITE_ACCOUNTS."
        ) from None


class LinkedInPlanStore(FirestorePlanStore):
    def __init__(self, policy):
        super().__init__(policy)
        self.plans = self.client.collection("viks_linkedin_write_plans")
        self.locks = self.client.collection("viks_linkedin_write_locks")


def campaign_snapshot(data):
    # Exclude reporting counters and modification timestamps from drift detection.
    return {
        key: copy.deepcopy(data[key])
        for key in (
            "id",
            "account",
            "name",
            "status",
            "dailyBudget",
            "totalBudget",
            "campaignGroup",
            "runSchedule",
            "targetingCriteria",
            "objectiveType",
            "unitCost",
            "costType",
            "type",
            "format",
            "associatedEntity",
            "audienceExpansionEnabled",
            "offsiteDeliveryEnabled",
            "locale",
            "politicalIntent",
        )
        if key in data
    }


class LinkedInWorkflow:
    def __init__(self, api, connection_id, *, store=None, policy=None):
        self.api = api
        self.connection_id = connection_id
        self.policy = policy or approval_policy()
        self.store = store

    def plan_store(self):
        if self.store is None:
            self.store = LinkedInPlanStore(self.policy)
        return self.store

    def snapshot(self, change):
        account = self.api.account(change.account_id)
        result = {
            "account": {
                "id": str(account["id"]),
                "name": account.get("name"),
                "currency": account.get("currency"),
                "status": account.get("status"),
            }
        }
        if change.campaign_id:
            result["campaign"] = campaign_snapshot(
                self.api.campaign(change.account_id, change.campaign_id)
            )
        else:
            group = self.api.group(
                change.account_id, change.campaign.campaign_group_id
            )
            result["group"] = {
                key: group.get(key)
                for key in (
                    "id",
                    "account",
                    "status",
                    "totalBudget",
                    "budgetOptimization",
                )
            }
        if change.creative:
            org = campaign_org(result["campaign"])
            if (
                image_status(self.api, change.creative.image, org)
                != "AVAILABLE"
            ):
                raise WriteBlocked(
                    "The selected image is not AVAILABLE yet. Check its status before previewing."
                )
        return result

    def check(self, change, snapshot):
        policy = account_policy(change.account_id)
        if snapshot["account"]["currency"] != policy["currency"]:
            raise WriteBlocked(
                "LinkedIn account currency differs from its write policy."
            )
        if snapshot["account"]["status"] != "ACTIVE":
            raise WriteBlocked("The LinkedIn account must be ACTIVE.")
        budget = (
            change.campaign.daily_budget
            if change.campaign
            else change.daily_budget
        )
        if budget and Decimal(budget) > Decimal(policy["max_daily_budget"]):
            raise WriteBlocked(
                "LinkedIn daily budget exceeds the configured ceiling."
            )
        if change.campaign:
            if change.campaign.start_ms <= int(time.time() * 1000):
                raise WriteBlocked("Choose a future campaign start time.")
            if snapshot["group"].get("budgetOptimization"):
                raise WriteBlocked(
                    "Group budget optimization is not supported in this first version."
                )
        else:
            campaign = snapshot["campaign"]
            if campaign["status"] not in ("ACTIVE", "PAUSED", "DRAFT"):
                raise WriteBlocked(
                    "Only ACTIVE, PAUSED or DRAFT campaigns can be changed."
                )
            if change.action == "set_budget" and (
                not campaign.get("dailyBudget") or campaign.get("totalBudget")
            ):
                raise WriteBlocked(
                    "Only existing daily-budget campaigns without totalBudget are supported."
                )
        return policy

    def preview(self, change, owner):
        snapshot = self.snapshot(change)
        account = self.check(change, snapshot)
        plan_id = uuid.uuid4().hex
        values = (
            {"status": "PAUSED"}
            if change.action == "pause"
            else {
                "dailyBudget": {
                    "amount": change.daily_budget,
                    "currencyCode": account["currency"],
                }
            }
        )
        if change.campaign:
            values = change.campaign.payload(
                change.account_id, account["currency"], plan_id[:12]
            )
        if change.creative:
            values = change.creative.payload(snapshot["campaign"], plan_id[:12])
        plan = {
            "id": plan_id,
            "owner": owner,
            "customer_id": change.account_id,
            "change": {
                **change.model_dump(exclude_none=True),
                "customer_id": change.account_id,
            },
            "snapshot": snapshot,
            "review": {"proposed": values, "connection_id": self.connection_id},
            "status": "pending_approval",
            "expires_at": time.time() + 3600,
        }
        plan["review_digest"] = digest(
            {
                key: plan[key]
                for key in ("customer_id", "change", "snapshot", "review")
            }
        )
        result = {
            "status": "local_preview_only",
            "before": snapshot,
            "after": values,
            "validation": "Local policy validation only. No LinkedIn validation or write has been sent.",
            "creation_note": (
                "Image ad only, PAUSED."
                if change.creative
                else "Campaign settings only."
            ),
        }
        if self.policy.mode == "dry_run":
            return result
        self.policy.require_writes()
        self.plan_store().create(plan)
        return {
            **result,
            "status": "pending_approval",
            "plan_id": plan_id,
            "approval_url": self.policy.base_url
            + "/linkedin/approvals/"
            + plan_id,
        }

    def load(self, plan_id, owner, *, expired_ok=False):
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise WriteBlocked("Use the full LinkedIn plan ID.")
        plan = self.plan_store().get(plan_id)
        if not plan or plan["owner"] != owner:
            raise WriteBlocked("LinkedIn plan not found for this user.")
        if not expired_ok:
            check_plan(plan, owner)
        expected = digest(
            {
                key: plan[key]
                for key in ("customer_id", "change", "snapshot", "review")
            }
        )
        if (
            expected != plan["review_digest"]
            or plan["review"]["connection_id"] != self.connection_id
        ):
            raise WriteBlocked(
                "LinkedIn plan or connection changed. Preview again."
            )
        change_data = {
            key: value
            for key, value in plan["change"].items()
            if key != "customer_id"
        }
        return plan, LinkedInChange.model_validate(change_data)

    def verify(self, plan, change, receipt=None):
        if change.creative:
            return verify_creative(
                self.api, change.account_id, plan["review"]["proposed"], receipt
            )
        campaign_id = change.campaign_id
        if change.campaign:
            campaign_id = (receipt or {}).get("campaign_id")
            if not campaign_id:
                # A lost create response must be reconciled by its approved unique name.
                matches = []
                page_token = None
                seen_tokens = set()
                for _ in range(50):
                    page = self.api.campaigns(
                        change.account_id, page_token=page_token, count=100
                    )
                    rows = page.get("elements", [])
                    matches += [
                        row
                        for row in rows
                        if row.get("name") == plan["review"]["proposed"]["name"]
                    ]
                    page_token = page.get("metadata", {}).get("nextPageToken")
                    if not page_token:
                        break
                    if page_token in seen_tokens:
                        raise WriteBlocked(
                            "LinkedIn returned a repeated page token. Manual reconciliation required."
                        )
                    seen_tokens.add(page_token)
                else:
                    raise WriteBlocked(
                        "Campaign search was incomplete. Manual reconciliation required."
                    )
                if len(matches) != 1:
                    raise WriteBlocked(
                        "Could not identify exactly one created campaign. No writes were retried."
                    )
                campaign_id = str(matches[0]["id"])
        data = self.api.campaign(change.account_id, campaign_id)
        for key, value in plan["review"]["proposed"].items():
            actual = data.get(key)
            if key in ("dailyBudget", "unitCost"):
                if (
                    not actual
                    or actual.get("currencyCode") != value["currencyCode"]
                    or Decimal(actual["amount"]) != Decimal(value["amount"])
                ):
                    raise WriteBlocked(
                        "LinkedIn budget or bid read-back differs from the approved plan."
                    )
            elif actual != value:
                raise WriteBlocked(
                    "LinkedIn campaign read-back differs from the approved plan."
                )
        return {
            "campaign_id": campaign_id,
            "status": data["status"],
            "verified_at": time.time(),
        }

    def execute(self, plan_id, owner):
        self.policy.require_writes()
        plan, change = self.load(plan_id, owner)
        if plan["status"] == "verified":
            return {
                "status": "verified",
                "result": plan["result"],
                "replayed": False,
            }
        if plan["status"] != "approved":
            raise WriteBlocked(
                "Independent browser approval is required, or this plan is already executing/unresolved."
            )
        current = self.snapshot(change)
        self.check(change, current)
        if current != plan["snapshot"]:
            raise WriteBlocked(
                "LinkedIn state changed after preview. Create and approve a fresh plan."
            )
        store = self.plan_store()
        store.transition(plan_id, "approved", "executing", owner=owner)
        mutation_completed = False
        try:
            if change.campaign:
                _, resource_id = self.api.create_campaign(
                    change.account_id, plan["review"]["proposed"]
                )
                mutation_completed = True
                receipt = (
                    {"campaign_id": linkedin_id(str(resource_id))}
                    if resource_id
                    else {}
                )
                store.record_progress(plan_id, owner, receipt)
            elif change.creative:
                _, urn = self.api.request(
                    "POST",
                    f"adAccounts/{change.account_id}/creatives",
                    params={"action": "createInline"},
                    body={"creative": plan["review"]["proposed"]},
                )
                mutation_completed = True
                receipt = {"creative_id": urn}
                store.record_progress(plan_id, owner, receipt)
            else:
                self.api.patch_campaign(
                    change.account_id,
                    change.campaign_id,
                    plan["review"]["proposed"],
                )
                mutation_completed = True
                receipt = {"campaign_id": change.campaign_id}
            result = self.verify(plan, change, receipt)
        except LinkedInRejected:
            if mutation_completed:
                store.transition(plan_id, "executing", "unknown", owner=owner)
                raise WriteBlocked(
                    "LinkedIn accepted the write but read-back failed. Reconcile this plan before retrying."
                ) from None
            store.transition(
                plan_id,
                "executing",
                "rejected",
                owner=owner,
                result={"write_rejected": True},
            )
            raise WriteBlocked(
                "LinkedIn rejected the write. No retry was sent and the account lock was released. Check access and request settings before previewing a corrected plan."
            ) from None
        except Exception:
            store.transition(plan_id, "executing", "unknown", owner=owner)
            raise WriteBlocked(
                "LinkedIn write outcome is uncertain. Use linkedin_reconcile_plan with the full plan ID; do not create a replacement or retry the write."
            ) from None
        store.transition(
            plan_id, "executing", "verified", owner=owner, result=result
        )
        return {"status": "verified", "result": result}

    def reconcile(self, plan_id, owner):
        self.policy.require_writes()
        plan, change = self.load(plan_id, owner, expired_ok=True)
        if plan["status"] == "verified":
            return {"status": "verified", "result": plan["result"]}
        if (
            plan["status"] == "executing"
            and time.time() - plan.get("updated_at", time.time()) > 900
        ):
            # A crashed worker must not leave an unresolvable lock forever. A normal
            # execution has bounded HTTP calls and finishes well inside 15 minutes.
            self.plan_store().transition(
                plan_id, "executing", "unknown", owner=owner
            )
            plan = self.plan_store().get(plan_id)
        if plan["status"] != "unknown":
            raise WriteBlocked(
                "Only an uncertain plan can be reconciled; no write is retried."
            )
        # Reconciliation checks intended remote state and never replays the mutation.
        (
            self.check(change, self.snapshot(change))
            if not change.campaign and not change.creative
            else account_policy(change.account_id)
        )
        result = self.verify(plan, change, plan.get("progress"))
        self.plan_store().transition(
            plan_id, "unknown", "verified", owner=owner, result=result
        )
        return {"status": "verified", "result": result, "writes_sent": 0}
