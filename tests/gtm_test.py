"""Test the separate, read-only GTM grant and mounted tool inputs."""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ads_mcp import google_grant
from ads_mcp.tools import gtm


def test_gtm_reads_use_only_the_signed_in_members_grant():
    async def verify():
        with (
            patch.object(gtm, "principal", return_value="member"),
            patch.object(google_grant, "credentials") as credential,
            patch.object(gtm, "AuthorizedSession") as transport,
        ):
            session = transport.return_value.__enter__.return_value
            session.get.return_value.status_code = 200
            session.get.return_value.json.return_value = {
                "tag": [{"tagId": "4"}]
            }
            async with Client(gtm.gtm_mcp) as client:
                args = {
                    "account_id": "1",
                    "container_id": "2",
                    "workspace_id": "3",
                    "kind": "tags",
                    "page_token": "next",
                }
                await client.call_tool("list_entities", args)
                credential.assert_called_once_with(
                    "member", google_grant.GTM_READ
                )
                url = gtm.BASE + "accounts/1/containers/2/workspaces/3/tags"
                assert session.get.call_args.args == (url,)
                assert session.get.call_args.kwargs["allow_redirects"] is False
                assert (
                    session.get.call_args.kwargs["params"]["pageToken"]
                    == "next"
                )
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "list_entities", {**args, "account_id": "1/2"}
                    )
                session.get.assert_called_once()

    asyncio.run(verify())


def test_oauth_grants_use_different_firestore_collections():
    with patch("ads_mcp.linkedin_auth.connection_store") as store:
        collection = store.return_value.client.collection
        google_grant.grant_ref("member")
        collection.assert_called_once_with("viks_google_ads_grants")
        google_grant.grant_ref("member", google_grant.GTM_READ)
        collection.assert_called_with("viks_gtm_grants")
