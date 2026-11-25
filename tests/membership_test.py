"""Exercise self-registration, manual approval and the direct MCP boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from ads_mcp import membership as m, google_grant
from google.api_core.exceptions import AlreadyExists


def user(email="one@example.com", hd=None):
    return m.verified_user(
        {"sub": "synthetic", "email": email, "email_verified": True, "hd": hd}
    )


def test_automatic_requires_workspace_and_verified_address():
    assert m.automatic(user("one@socialzense.se", "socialzense.se"))
    assert not m.automatic(user("one@socialzense.se"))
    assert not m.automatic(
        user("one@socialzense.se.example.com", "socialzense.se")
    )
    with pytest.raises(ValueError):
        m.verified_user(
            {
                "sub": "x",
                "email": "one@socialzense.se",
                "email_verified": "false",
                "hd": "socialzense.se",
            }
        )


def test_register_once_does_not_override_an_existing_decision():
    person = user()
    with (
        patch.object(m, "collection") as col,
        patch("ads_mcp.connections.crypto") as crypto,
    ):
        ref = col.return_value.document.return_value
        ref.get.return_value.to_dict.return_value = None
        crypto.return_value.encrypt.return_value = b"ciphertext"
        assert m.member_state(person) == "missing"
        ref.create.assert_not_called()
        assert m.member_state(person, signup=True) == "registered"
        assert ref.create.call_args.args[0]["email"] == "ciphertext"
        ref.get.return_value.to_dict.side_effect = [
            None,
            {"status": "declined"},
        ]
        ref.create.side_effect = AlreadyExists("race")
        assert m.member_state(person, signup=True) == "declined"


def test_direct_mcp_gate_protects_mounted_tools():
    app = FastMCP("members")
    app.add_middleware(m.MemberGate())
    ads = FastMCP("ads")
    calls = []

    @ads.tool
    def report():
        calls.append(1)
        return "ok"

    app.mount(ads, namespace="google")

    async def run():
        async with Client(app) as client:
            for status in ("missing", "registered", "pending", "declined"):
                lookup.return_value = status
                with pytest.raises(ToolError):
                    await client.call_tool("google_report", {})
            assert not calls
            lookup.return_value = "active"
            await client.call_tool("google_report", {})
            assert calls == [1]

    with (
        patch.object(
            m,
            "get_access_token",
            return_value=SimpleNamespace(
                claims={
                    "sub": "x",
                    "email": "a@example.com",
                    "email_verified": True,
                }
            ),
        ),
        patch.object(m, "member_state") as lookup,
    ):
        asyncio.run(run())


def test_admin_and_application_forms():
    client = TestClient(
        Starlette(
            routes=[
                Route(path, m.membership_page, methods=["GET", "POST"])
                for path in (m.ACCOUNT, m.ADMIN)
            ]
        ),
        base_url="https://testserver",
    )
    person = user()
    uid = "a" * 64
    with (
        patch("ads_mcp.connections.session", return_value=person),
        patch.object(m, "member_state", return_value="registered"),
        patch.object(m, "change_state") as change,
        patch.object(
            m, "config", return_value={"base_url": "https://testserver"}
        ),
        patch.object(m, "is_admin", return_value=False) as admin,
    ):
        assert client.get(m.ADMIN).status_code == 403
        form = {"decision": "active", "id": uid}
        origin = {"origin": "https://testserver"}
        assert (
            client.post(m.ADMIN, data=form, headers=origin).status_code == 403
        )
        assert (
            client.post(
                m.ACCOUNT,
                data={"request": "yes"},
                headers={"origin": "https://elsewhere.test"},
            ).status_code
            == 403
        )
        change.assert_not_called()
        assert (
            client.post(
                m.ACCOUNT,
                data={"request": "yes"},
                headers=origin,
                follow_redirects=False,
            ).status_code
            == 303
        )
        change.assert_called_once_with(
            person["owner"], "registered", "pending", person["owner"]
        )
        admin.return_value = True
        assert (
            client.post(
                m.ADMIN, data=form, headers=origin, follow_redirects=False
            ).status_code
            == 303
        )
        change.assert_called_with(uid, "pending", "active", person["owner"])


def test_ads_grant_requires_separate_consent_and_is_encrypted(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID", "client")
    with patch.object(google_grant, "grant_ref") as ref:
        with pytest.raises(ValueError):
            google_grant.save("id", {"scope": "openid", "refresh_token": "r"})
        google_grant.save(
            "id",
            {
                "access_token": "example-access",
                "refresh_token": "example-refresh",
                "scope": google_grant.ADS,
                "expires_in": 3600,
            },
        )
        record = ref.return_value.set.call_args.args[0]
        assert "example-" not in str(record)
        ref.return_value.get.return_value.to_dict.return_value = record
        assert google_grant.credentials("id").refresh_token == "example-refresh"
