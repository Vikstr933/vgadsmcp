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


def test_portal_navigation_and_channel_setup_are_role_aware():
    from ads_mcp import connections as hub
    from ads_mcp.public_site import homepage

    client = TestClient(
        Starlette(
            routes=[
                Route("/", homepage),
                Route(m.ACCOUNT, m.membership_page),
                Route(m.ADMIN_HOME, m.admin_page),
                Route(m.MEMBERS, m.admin_page),
                Route("/logga-ut", hub.logout, methods=["POST"]),
            ]
        ),
        base_url="https://testserver",
    )
    with (
        patch.object(hub, "session", return_value=None) as session,
        patch.object(m, "member_state", return_value="pending") as state,
        patch.object(
            m,
            "member_list",
            return_value=([("member@example.com", "Aktiv")], None),
        ) as members,
        patch.object(
            hub, "config", return_value={"base_url": "https://testserver"}
        ),
        patch.object(hub, "connection_store") as storage,
        patch.object(hub, "google_details", return_value=("Anslutet.", True)),
        patch.object(hub.google_grant, "grant_ref") as grant_ref,
        patch.object(
            hub, "linkedin_details", return_value="Ännu inte anslutet."
        ),
        patch.object(m, "is_admin", return_value=False) as admin,
        patch.object(hub, "is_admin", return_value=False) as nav_admin,
    ):
        landing = client.get("/")
        assert (
            "Skapa gratis konto" in landing.text and "Logga in" in landing.text
        )
        assert landing.headers["cache-control"] == "no-store"
        assert client.get(m.ADMIN_HOME).status_code == 401
        session.return_value = user()
        assert "Öppna min översikt" in client.get("/").text
        pending = client.get(m.ACCOUNT).text
        assert (
            "väntar på granskning" in pending and "mcp-address" not in pending
        )
        assert client.get(m.MEMBERS).status_code == 403
        members.assert_not_called()
        state.return_value = "active"
        grant_ref.return_value.get.return_value.exists = False
        active = client.get(m.ACCOUNT).text
        assert all(
            part in active
            for part in (
                "Google Ads",
                "LinkedIn Ads",
                "Kopiera adress",
                "https://testserver/mcp",
            )
        )
        assert "Anslut GTM" in active
        assert 'href="/admin"' not in active
        admin.return_value = nav_admin.return_value = True
        listing = client.get(m.MEMBERS)
        assert "member@example.com" in listing.text
        admin_home = client.get(m.ADMIN_HOME).text
        assert all(
            path in admin_home
            for path in (m.ADMIN, m.MEMBERS, "/admin/aktivitet")
        )

        client.cookies.set(hub.SESSION, "a" * 43)
        assert (
            client.post(
                "/logga-ut", headers={"origin": "https://elsewhere.test"}
            ).status_code
            == 403
        )
        storage.return_value.consume.assert_not_called()
        logout = client.post(
            "/logga-ut",
            headers={"origin": "https://testserver"},
            follow_redirects=False,
        )
        assert (
            logout.headers["location"] == "/"
            and "Max-Age=0" in logout.headers["set-cookie"]
        )
        storage.return_value.consume.assert_called_once_with(
            "a" * 43, "dashboard"
        )
