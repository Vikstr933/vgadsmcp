"""LinkedIn tools on the existing authenticated Viks Annonsverktyg connection."""

import os
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from ads_mcp.linkedin_auth import config, connection_store
from ads_mcp.linkedin_client import LinkedInAPI, linkedin_id
from ads_mcp.linkedin_creatives import ImageAd
from ads_mcp.linkedin_upload import upload_target
from ads_mcp.linkedin_workflow import (
    CampaignDraft,
    LinkedInChange,
    LinkedInWorkflow,
)
from ads_mcp.write_policy import WriteBlocked, principal

linkedin_mcp = FastMCP("linkedin")
READ = ToolAnnotations(readOnlyHint=True)
PLAN = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True
)
Count = Annotated[int, Field(ge=1, le=100)]
PageToken = Annotated[str | None, Field(max_length=4096)]


async def guarded(call):
    try:
        return await run_in_threadpool(call)
    except WriteBlocked as error:
        raise ToolError(str(error)) from None
    except Exception:
        raise ToolError(
            "LinkedIn request could not be completed. Check connection and server configuration. No write is automatically retried."
        ) from None


def workflow():
    owner = principal()
    grant = connection_store().grant(owner)
    return owner, LinkedInWorkflow(
        LinkedInAPI(grant["access_token"]), grant["connection_id"]
    )


@linkedin_mcp.tool(annotations=READ)
async def connection_status() -> dict:
    """Check LinkedIn setup and the current user's connection without returning credentials."""
    ready = all(
        os.getenv(key)
        for key in ("LINKEDIN_MCP_CLIENT_ID", "LINKEDIN_MCP_CLIENT_SECRET")
    )

    def status():
        connected, expires = False, None
        if ready:
            try:
                grant = connection_store().grant(principal())
                connected, expires = True, grant["expires_at"]
            except WriteBlocked:
                pass
        return {
            "app_configured": ready,
            "connected": connected,
            "expires_at": expires,
            "write_mode": os.getenv("LINKEDIN_MCP_WRITE_MODE", "dry_run"),
            "write_accounts_configured": os.getenv(
                "LINKEDIN_MCP_WRITE_ACCOUNTS", "{}"
            )
            != "{}",
            "creation_status": "PAUSED",
            "campaign_creation_scope": "Campaign settings only; no creatives or activation.",
            "creative_creation_scope": "Single image ad in a paused website campaign.",
            "version": "2",
        }

    return await guarded(status)


@linkedin_mcp.tool(annotations=PLAN)
async def connect() -> dict:
    """Create a one-use owner-protected LinkedIn login link, bound to this MCP user. The owner must complete it in their browser. Never log in or approve on their behalf."""

    def begin():
        owner = principal()
        settings = config()
        ticket = connection_store().issue("connect", {"owner": owner})
        return {
            "connection_url": settings["base_url"]
            + "/linkedin/connect/"
            + ticket,
            "expires_in_seconds": 600,
            "instruction": "Open this in your own browser, sign in as owner, then authorize LinkedIn. No ads are changed.",
        }

    return await guarded(begin)


@linkedin_mcp.tool(annotations=PLAN)
async def disconnect() -> dict:
    """Delete only this MCP user's stored LinkedIn grant. Does not change LinkedIn ads or revoke other users."""

    def remove():
        connection_store().disconnect(principal())
        return {"connected": False, "local_credentials_deleted": True}

    return await guarded(remove)


@linkedin_mcp.tool(annotations=READ)
async def list_accounts(
    page_token: PageToken = None, count: Count = 100
) -> dict:
    """List LinkedIn ad accounts accessible to the connected LinkedIn member, with paging."""
    return await guarded(lambda: workflow()[1].api.accounts(page_token, count))


@linkedin_mcp.tool(annotations=READ)
async def list_campaigns(
    account_id: str, page_token: PageToken = None, count: Count = 100
) -> dict:
    """List campaigns in a numeric LinkedIn ad account ID. Google customer IDs are unrelated."""
    return await guarded(
        lambda: workflow()[1].api.campaigns(
            linkedin_id(account_id), page_token, count
        )
    )


@linkedin_mcp.tool(annotations=READ)
async def list_campaign_groups(
    account_id: str, page_token: PageToken = None, count: Count = 100
) -> dict:
    """List campaign groups in the selected LinkedIn account."""
    return await guarded(
        lambda: workflow()[1].api.groups(
            linkedin_id(account_id), page_token, count
        )
    )


@linkedin_mcp.tool(annotations=READ)
async def get_campaign(account_id: str, campaign_id: str) -> dict:
    """Read campaign settings and verify membership of the selected LinkedIn account."""
    return await guarded(
        lambda: workflow()[1].api.campaign(
            linkedin_id(account_id), linkedin_id(campaign_id)
        )
    )


@linkedin_mcp.tool(annotations=READ)
async def performance(account_id: str, start_date: str, end_date: str) -> dict:
    """Daily campaign impressions, clicks, spend and website conversions. Use YYYY-MM-DD and at most 367 days; report currency and attribution separately from other platforms."""
    return await guarded(
        lambda: workflow()[1].api.report(
            linkedin_id(account_id), start_date, end_date
        )
    )


async def preview(change):
    def run():
        owner, service = workflow()
        return service.preview(change, owner)

    return await guarded(run)


@linkedin_mcp.tool(annotations=PLAN)
async def image_upload_link(account_id: str, campaign_id: str) -> dict:
    """Get a one-use protected browser link to upload a JPG or PNG for a paused website campaign. The owner chooses and submits the file. Returns no credentials."""

    def begin():
        owner, service = workflow()
        service.policy.require_writes()
        org = upload_target(
            service.api, linkedin_id(account_id), linkedin_id(campaign_id)
        )
        ticket = connection_store().issue(
            "image-upload",
            {
                "owner": owner,
                "connection": service.connection_id,
                "account": account_id,
                "campaign": campaign_id,
                "organization": org,
            },
        )
        return {
            "url": service.policy.base_url
            + "/linkedin/images/upload/"
            + ticket,
            "expires_in_seconds": 600,
        }

    return await guarded(begin)


@linkedin_mcp.tool(annotations=PLAN)
async def preview_image_ad(
    account_id: str, campaign_id: str, creative: ImageAd
) -> dict:
    """Preview a complete paused LinkedIn single-image website ad using an AVAILABLE Page image. Requires separate browser approval before execution."""
    return await preview(
        LinkedInChange(
            action="create_image_ad",
            account_id=account_id,
            campaign_id=campaign_id,
            creative=creative,
        )
    )


@linkedin_mcp.tool(annotations=PLAN)
async def preview_pause(account_id: str, campaign_id: str) -> dict:
    """Preview a LinkedIn campaign pause. Never sends an ad mutation. Owner approval required for execution."""
    return await preview(
        LinkedInChange(
            action="pause", account_id=account_id, campaign_id=campaign_id
        )
    )


@linkedin_mcp.tool(annotations=PLAN)
async def preview_budget(
    account_id: str, campaign_id: str, daily_budget: str
) -> dict:
    """Preview a daily budget in account currency, e.g. '250.00'. Requires a separate LinkedIn account ceiling. No ad mutation is sent."""
    return await preview(
        LinkedInChange(
            action="set_budget",
            account_id=account_id,
            campaign_id=campaign_id,
            daily_budget=daily_budget,
        )
    )


@linkedin_mcp.tool(annotations=PLAN)
async def preview_create_campaign(
    account_id: str, campaign: CampaignDraft
) -> dict:
    """Preview PAUSED Sponsored Content campaign settings with explicit group, organization, targeting, future schedule, bid and daily budget. Does NOT create creatives or activate delivery. Local validation only. Owner must review the full proposal."""
    return await preview(
        LinkedInChange(
            action="create_campaign", account_id=account_id, campaign=campaign
        )
    )


@linkedin_mcp.tool(annotations=WRITE)
async def execute_plan(plan_id: str) -> dict:
    """Execute an independently browser-approved LinkedIn plan once. Never approve for the owner. On unknown result use reconcile_plan; do not create a replacement."""

    def run():
        owner, service = workflow()
        return service.execute(plan_id, owner)

    return await guarded(run)


@linkedin_mcp.tool(annotations=READ)
async def plan_status(plan_id: str) -> dict:
    """Read a full LinkedIn plan ID's status, including expired or unresolved plans. No ad mutation."""

    def run():
        owner, service = workflow()
        plan, _ = service.load(plan_id, owner, expired_ok=True)
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "expires_at": plan["expires_at"],
            "result": plan.get("result"),
        }

    return await guarded(run)


@linkedin_mcp.tool(annotations=PLAN)
async def reconcile_plan(plan_id: str) -> dict:
    """Read back an uncertain LinkedIn write, including expired plans, and release its lock only if the intended state is verified. Sends zero ad mutations."""

    def run():
        owner, service = workflow()
        return service.reconcile(plan_id, owner)

    return await guarded(run)
