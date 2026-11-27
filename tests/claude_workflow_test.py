"""Exercise read-only connection checks and the GTM proposal app boundary."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ads_mcp import google_grant, gtm_drafts
from ads_mcp.gtm_review_card import REVIEW_URI
from ads_mcp.server import mcp
from ads_mcp.tools import gtm, status
from ads_mcp.write_policy import digest


def test_gtm_connection_check_uses_both_member_grants_and_only_get():
    with (
        patch.object(status, "principal", return_value="member"),
        patch.object(
            google_grant, "credentials", side_effect=[object(), object()]
        ) as grant,
        patch.object(status, "AuthorizedSession"),
        patch.object(
            gtm_drafts,
            "api_request",
            return_value={
                "name": "Web",
                "publicId": "GTM-TEST",
                "usageContext": ["web"],
            },
        ) as api,
        patch.object(
            gtm_drafts,
            "policy",
            return_value=SimpleNamespace(mode="approval_required"),
        ),
    ):

        async def run():
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "status_check_connections",
                    {
                        "product": "gtm",
                        "gtm_account_id": "1",
                        "gtm_container_id": "2",
                    },
                )
                assert result.structured_content["write_verified"] is False
                check = result.structured_content["checks"]["gtm"]
                assert check["read"]["target"] == "GTM-TEST"
                assert check["edit_grant_read_check"]["result"] == "reachable"
                assert check["write_setup"]["mode"] == "approval_required"

        asyncio.run(run())
        assert [call.args for call in grant.call_args_list] == [
            ("member", google_grant.GTM_READ),
            ("member", google_grant.GTM_EDIT),
        ]
        assert [call.args[1:] for call in api.call_args_list] == [
            ("GET", "accounts/1/containers/2"),
            ("GET", "accounts/1/containers/2"),
        ]


def test_gtm_review_card_resolves_and_never_approves_a_plan():
    plan_id = "1" * 32
    change = {
        "container": {"publicId": "GTM-TEST"},
        "workspace_name": "Review",
        "changes": {"paused": {"before": False, "after": True}},
    }
    with (
        patch.object(gtm, "principal", return_value="member") as user,
        patch.object(
            gtm_drafts,
            "policy",
            return_value=SimpleNamespace(base_url="https://test.example"),
        ),
        patch.object(gtm_drafts, "GTMStore") as store,
    ):
        plan = {
            "id": plan_id,
            "status": "pending_approval",
            "owner": "member",
            "expires_at": time.time() + 60,
            "change": change,
            "review_digest": digest(change),
        }
        store.return_value.get.return_value = plan

        async def run():
            async with Client(mcp) as client:
                review = next(
                    tool
                    for tool in await client.list_tools()
                    if tool.name == "gtm_review_tracking_draft"
                )
                assert review.meta["ui"]["resourceUri"] == REVIEW_URI
                html = (await client.read_resource(REVIEW_URI))[0]
                assert html.mime_type == "text/html;profile=mcp-app"
                assert (
                    "ui/open-link" in html.text
                    and "tools/call" not in html.text
                )
                demo = await client.call_tool(review.name, {})
                assert demo.structured_content["status"] == "demo"
                store.return_value.get.assert_not_called()
                args = {"plan_id": plan_id}
                before = await client.call_tool(review.name, args)
                assert before.structured_content["changes"] == change["changes"]
                assert (
                    before.structured_content["approval_url"]
                    == f"https://test.example/gtm/approvals/{plan_id}"
                )
                plan["expires_at"] = 0
                expired = await client.call_tool(review.name, args)
                assert expired.structured_content["status"] == "expired"
                assert expired.structured_content["approval_url"] is None
                user.return_value = "other"
                with pytest.raises(ToolError):
                    await client.call_tool(review.name, args)

        asyncio.run(run())
        store.return_value.transition.assert_not_called()
