"""Fail-closed policy and typed inputs for the self-hosted write workflow."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WriteBlocked(ValueError):
    """A policy boundary stopped a write before reaching Google."""


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["pause", "set_budget", "create_paused_search", "activate"]
    customer_id: str
    login_customer_id: str | None = None
    campaign_id: str | None = None
    amount_micros: int | None = Field(default=None, gt=0, le=10**15)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    eu_political_advertising: Literal["contains", "does_not_contain"] | None = (
        None
    )

    @field_validator("customer_id", "login_customer_id")
    @classmethod
    def account_id(cls, value):
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}", value):
            raise ValueError("Use a 10-digit Google Ads customer ID.")
        return value.replace("-", "")

    @field_validator("campaign_id")
    @classmethod
    def object_id(cls, value):
        if value is not None and not re.fullmatch(r"[0-9]{1,20}", value):
            raise ValueError("Campaign ID must contain digits only.")
        return value

    @field_validator("name")
    @classmethod
    def printable_name(cls, value):
        if value is not None and (
            not value.strip() or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Campaign name must be printable and non-empty.")
        return value

    def check_shape(self):
        create = self.action == "create_paused_search"
        if create != (self.campaign_id is None):
            raise WriteBlocked(
                "Existing changes require a campaign ID; creation must not have one."
            )
        if (self.action in ("set_budget", "create_paused_search")) != (
            self.amount_micros is not None
        ):
            raise WriteBlocked(
                "A budget amount is required only for budget changes and creation."
            )
        if create != (self.name is not None) or create != (
            self.eu_political_advertising is not None
        ):
            raise WriteBlocked(
                "Creation requires a name and an explicit EU political-ad declaration."
            )


class AccountPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    max_daily_budget_micros: int = Field(gt=0, le=10**15)
    allow_activation: bool = False


@dataclass(frozen=True)
class WritePolicy:
    mode: str
    accounts: dict[str, AccountPolicy]
    base_url: str
    approval_secret: str
    project: str
    database: str
    managers: dict[str, AccountPolicy] = field(default_factory=dict)

    @classmethod
    def load(cls):
        mode = os.getenv("GOOGLE_ADS_MCP_WRITE_MODE", "dry_run")
        if mode not in ("dry_run", "approval_required"):
            raise WriteBlocked(
                "WRITE_MODE must be dry_run or approval_required."
            )

        def policies(variable):
            raw = json.loads(os.getenv(variable, "{}"))
            if not isinstance(raw, dict) or any(
                not re.fullmatch(r"[0-9]{10}", key) for key in raw
            ):
                raise WriteBlocked(
                    f"{variable} must map 10-digit IDs to policies."
                )
            return {
                key: AccountPolicy.model_validate(val)
                for key, val in raw.items()
            }

        return cls(
            mode=mode,
            accounts=policies("GOOGLE_ADS_MCP_WRITE_ACCOUNTS"),
            base_url=os.getenv("GOOGLE_ADS_MCP_BASE_URL", "").rstrip("/"),
            approval_secret=os.getenv("GOOGLE_ADS_MCP_APPROVAL_SECRET", ""),
            project=os.getenv("GOOGLE_ADS_MCP_WRITE_FIRESTORE_PROJECT", ""),
            database=os.getenv(
                "GOOGLE_ADS_MCP_WRITE_FIRESTORE_DATABASE", "(default)"
            ),
            managers=policies("GOOGLE_ADS_MCP_WRITE_MANAGERS"),
        )

    def account_policy(self, change):
        # An explicit client policy overrides the MCC ceiling, never the reverse.
        if change.customer_id in self.accounts:
            return self.accounts[change.customer_id]
        policy = self.managers.get(change.login_customer_id)
        if policy is None:
            raise WriteBlocked(
                "Select an enabled account or provide an enabled MCC as login_customer_id."
            )
        return policy

    def requires_manager_scope(self, change):
        self.account_policy(change)
        return change.customer_id not in self.accounts

    def require_writes(self):
        if self.mode != "approval_required":
            raise WriteBlocked("Dry-run mode: no changes can be applied.")
        if len(self.approval_secret) < 32:
            raise WriteBlocked(
                "A separate approval secret of at least 32 characters is required."
            )
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.path
            or url.query
            or url.fragment
            or url.username
        ):
            raise WriteBlocked(
                "Approval requires an HTTPS BASE_URL with no path or credentials."
            )
        if not self.project:
            raise WriteBlocked(
                "Persistent Firestore storage is required for writes."
            )

    def check(self, change: Change, snapshot: dict):
        change.check_shape()
        account = self.account_policy(change)
        if self.requires_manager_scope(change):
            expected_scope = {
                "manager_id": change.login_customer_id,
                "customer_id": change.customer_id,
            }
            if snapshot.get("manager_scope") != expected_scope:
                raise WriteBlocked("Live MCC membership has not been verified.")
        if snapshot["account"]["id"] != change.customer_id:
            raise WriteBlocked(
                "Snapshot belongs to another Google Ads account."
            )
        if snapshot["account"]["currency"] != account.currency:
            raise WriteBlocked(
                "Account currency differs from the configured policy."
            )
        if change.action == "activate" and not account.allow_activation:
            raise WriteBlocked("Activation is disabled for this account.")
        amount = change.amount_micros
        if change.action == "activate":
            amount = snapshot["campaign"]["amount_micros"]
        if amount is not None and amount > account.max_daily_budget_micros:
            raise WriteBlocked(
                "The proposed daily budget exceeds the account limit."
            )
        if change.action != "create_paused_search":
            campaign = snapshot["campaign"]
            if (
                campaign["id"] != change.campaign_id
                or campaign["resource_name"]
                != f"customers/{change.customer_id}/campaigns/{change.campaign_id}"
            ):
                raise WriteBlocked("Snapshot belongs to another campaign.")
            if not re.fullmatch(
                rf"customers/{change.customer_id}/campaignBudgets/[0-9]+",
                campaign["budget_resource"],
            ):
                raise WriteBlocked("Budget belongs to another account.")
            if campaign["status"] not in ("PAUSED", "ENABLED"):
                raise WriteBlocked(
                    "Only existing PAUSED or ENABLED campaigns are supported."
                )
            if change.action == "activate" and campaign["status"] != "PAUSED":
                raise WriteBlocked(
                    "Activation requires a separately reviewed paused campaign."
                )
            if (
                change.action in ("set_budget", "activate")
                and campaign["period"] != "DAILY"
            ):
                raise WriteBlocked("Only daily budgets are supported.")
            if change.action in ("set_budget", "activate") and (
                campaign["shared"] or len(snapshot["affected_campaigns"]) != 1
            ):
                raise WriteBlocked(
                    "Shared budgets require manual review and are not writable here."
                )


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def principal() -> str:
    """Bind plans to verified Google OAuth identity; never fall back to ADC."""
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    subject = (getattr(token, "claims", None) or {}).get("sub")
    if (
        not token
        or not token.token
        or not isinstance(subject, str)
        or not subject
    ):
        raise WriteBlocked(
            "Write planning requires a verified Google OAuth user with a subject claim."
        )
    return digest({"provider": "google", "subject": subject})
