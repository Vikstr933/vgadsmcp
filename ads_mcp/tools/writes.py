"""Narrow typed write tools; neither raw mutations nor approval are tools."""

import re
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.write_policy import Change, WriteBlocked, WritePolicy, principal
from ads_mcp.write_workflow import WriteWorkflow
from ads_mcp.conversion_writes import ConversionSettings

writes_mcp = FastMCP("writes")
PREVIEW = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)
APPLY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def preview(change):
    try:
        owner = principal()
        return WriteWorkflow().preview(change, owner)
    except WriteBlocked as error:
        raise ToolError(str(error)) from None
    except Exception:
        raise ToolError(
            "Preview failed. No live write was sent. Check account access, policy, request values and service configuration."
        ) from None


@writes_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def policy_status() -> dict:
    """Show write mode and setup readiness. No credentials or account data returned."""
    policy = WritePolicy.load()
    return {
        "mode": policy.mode,
        "account_policies_configured": bool(policy.accounts or policy.managers),
        "manager_policies_configured": bool(policy.managers),
        "mcc_usage": "Use the selected client as customer_id and your configured MCC as login_customer_id. Membership and currency are checked live.",
        "persistent_store_configured": bool(policy.project),
        "approval_secret_configured": len(policy.approval_secret) >= 32,
        "creation_status": "PAUSED",
        "conversion_tools_version": "1",
        "conversion_creation": "Native WEBPAGE, ENABLED and secondary; no tags installed. Separate primary/secondary approval. Shared MCC-owned conversions and GA4 imports excluded.",
        "approval": "Separate password-protected browser page; never an MCP approval argument.",
    }


@writes_mcp.tool(annotations=PREVIEW)
def preview_pause(
    customer_id: str, campaign_id: str, login_customer_id: str | None = None
) -> dict:
    """Dry-run a campaign pause. No Google Ads changes. In approval mode return an owner review URL. Never approve on behalf of the owner."""
    return preview(
        Change(
            action="pause",
            customer_id=customer_id,
            campaign_id=campaign_id,
            login_customer_id=login_customer_id,
        )
    )


@writes_mcp.tool(annotations=PREVIEW)
def preview_budget(
    customer_id: str,
    campaign_id: str,
    amount_micros: int,
    login_customer_id: str | None = None,
) -> dict:
    """Dry-run a dedicated daily budget change. 1 currency unit = 1,000,000 micros. Rejects shared budgets and amounts above the owner's configured ceiling. No Google Ads changes."""
    return preview(
        Change(
            action="set_budget",
            customer_id=customer_id,
            campaign_id=campaign_id,
            amount_micros=amount_micros,
            login_customer_id=login_customer_id,
        )
    )


@writes_mcp.tool(annotations=PREVIEW)
def preview_create_paused_search(
    customer_id: str,
    name: str,
    amount_micros: int,
    eu_political_advertising: Literal["contains", "does_not_contain"],
    login_customer_id: str | None = None,
) -> dict:
    """Dry-run an empty PAUSED Search campaign with a dedicated daily budget and manual CPC. No ads, keywords or targeting are created. Obtain the EU political-ad declaration from the owner. No Google Ads changes; there is no enabled creation option."""
    return preview(
        Change(
            action="create_paused_search",
            customer_id=customer_id,
            name=name,
            amount_micros=amount_micros,
            eu_political_advertising=eu_political_advertising,
            login_customer_id=login_customer_id,
        )
    )


@writes_mcp.tool(annotations=PREVIEW)
def preview_activation(
    customer_id: str, campaign_id: str, login_customer_id: str | None = None
) -> dict:
    """Dry-run SEPARATE activation of a paused campaign. Requires allow_activation in server account policy and a NEW owner browser approval. Can start spending. Ask the owner to review ads, targeting, bids and policy eligibility in Google Ads."""
    return preview(
        Change(
            action="activate",
            customer_id=customer_id,
            campaign_id=campaign_id,
            login_customer_id=login_customer_id,
        )
    )


def validated_plan_id(plan_id):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ToolError("Invalid plan ID.")
    return plan_id


@writes_mcp.tool(annotations=PREVIEW)
def preview_create_conversion(
    customer_id: str,
    settings: ConversionSettings,
    login_customer_id: str | None = None,
) -> dict:
    """Validate creating a native WEBPAGE conversion in the selected client account. Requires name/category/counting_type. Creates SECONDARY, with ENABLED status (conversions have no pause state), only after separate human browser approval. Does not deploy tags or import GA4 events. Shared MCC-owned conversions are blocked. No Google Ads changes during preview."""
    return preview(
        Change(
            action="create_conversion",
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            conversion_settings=settings,
        )
    )


@writes_mcp.tool(annotations=PREVIEW)
def preview_update_conversion(
    customer_id: str,
    conversion_action_id: str,
    settings: ConversionSettings,
    login_customer_id: str | None = None,
) -> dict:
    """Validate changing explicit settings on an enabled native WEBPAGE conversion owned by the selected client. Only non-null settings change; no deletion, type/status/primary changes, GA4 imports or shared MCC conversions. Reporting and bidding may be affected, including for custom goals. Return an exact before/after and independent human approval URL."""
    return preview(
        Change(
            action="update_conversion",
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            conversion_action_id=conversion_action_id,
            conversion_settings=settings,
        )
    )


@writes_mcp.tool(annotations=PREVIEW)
def preview_conversion_primary(
    customer_id: str,
    conversion_action_id: str,
    primary_for_goal: bool,
    login_customer_id: str | None = None,
) -> dict:
    """Validate a SEPARATE primary/secondary change for a native WEBPAGE conversion. Can affect reporting and Smart Bidding immediately across the client account; review goals first. Secondary actions can still be used by custom goals. No other setting changes. Requires a NEW human browser approval before execute_approved_plan."""
    return preview(
        Change(
            action="set_conversion_primary",
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            conversion_action_id=conversion_action_id,
            primary_for_goal=primary_for_goal,
        )
    )


@writes_mcp.tool(annotations=APPLY)
def execute_approved_plan(plan_id: str) -> dict:
    """Apply only a plan independently approved by the human on the owner-only browser page. Dry-run mode blocks execution. Validates again, rejects stale plans, applies once and reads back. No approval or arbitrary operation parameters exist. Never retry an uncertain write."""
    try:
        return WriteWorkflow().execute(validated_plan_id(plan_id), principal())
    except WriteBlocked as error:
        raise ToolError(str(error)) from None
    except Exception:
        raise ToolError(
            "Execution could not be confirmed. Inspect plan status and Google Ads; do not retry automatically."
        ) from None


@writes_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def plan_status(plan_id: str) -> dict:
    """Read only this OAuth user's proposal and execution result. Does not approve or execute anything."""
    try:
        return WriteWorkflow().get_plan(validated_plan_id(plan_id), principal())
    except WriteBlocked as error:
        raise ToolError(str(error)) from None
    except Exception:
        raise ToolError(
            "Plan could not be read. Check persistent storage configuration."
        ) from None
