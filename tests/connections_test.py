from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ads_mcp import connections as hub
from ads_mcp.write_policy import digest


async def original_callback(request):
    return PlainTextResponse("original callback")


def test_existing_callbacks_and_browser_binding():
    app = Starlette(
        routes=[
            Route("/auth/callback", original_callback),
            Route(hub.HOME, hub.dashboard, methods=["GET", "POST"]),
            Route(hub.HOME + "/google", hub.google_start),
        ],
        middleware=[Middleware(hub.DashboardCallback)],
    )
    client = TestClient(app, base_url="https://testserver")
    other = TestClient(app, base_url="https://testserver")
    environment = {
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "test-secret",
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "test-id",
    }
    db = MagicMock()
    docs = {}

    def issue(kind, record):
        sid = ("d" if kind == "dashboard" else "t") * 43
        docs[(sid, kind)] = record
        return sid

    db.issue.side_effect = issue
    db.peek.side_effect = lambda sid, kind: docs[(sid, kind)]
    with (
        patch.dict("os.environ", environment),
        patch.object(
            hub, "config", return_value={"base_url": "https://testserver"}
        ),
        patch.object(hub, "connection_store", return_value=db),
        patch.object(hub, "member_state", return_value="active") as member,
        patch.object(hub.google_grant, "grant_ref"),
        patch.object(hub.google_grant, "save") as save,
        patch.object(hub, "google_accounts", return_value=["123"]),
        patch.object(
            hub,
            "linkedin_details",
            return_value=hub.ChannelDetails(
                "Anslutet",
                "good",
                True,
                hub.accounts_disclosure(["LinkedIn-konto 456"]),
            ),
        ),
        patch.object(hub.httpx, "AsyncClient") as transport,
    ):
        http = AsyncMock()
        transport.return_value.__aenter__.return_value = http
        http.post.return_value = MagicMock(
            **{"json.return_value": {"access_token": "secret-token"}}
        )
        http.get.return_value = MagicMock(
            **{
                "json.return_value": {
                    "sub": "member-1",
                    "email_verified": True,
                    "email": "v@example.com",
                }
            }
        )
        assert "Mitt konto" in client.get(hub.HOME).text
        assert (
            client.get("/auth/callback?state=ordinary").text
            == "original callback"
        )
        begin = client.get(hub.HOME + "/google", follow_redirects=False)
        params = parse_qs(urlsplit(begin.headers["location"]).query)
        assert params["redirect_uri"] == ["https://testserver/auth/callback"]
        callback = "/auth/callback?code=test&state=" + params["state"][0]
        assert other.get(callback).status_code == 400
        http.post.assert_not_called()
        assert client.get(callback).status_code == 200
        assert "secret-token" not in str(docs)
        assert params["scope"] == ["openid email"]
        begin_ads = client.get(
            hub.HOME + "/google?kanal=google", follow_redirects=False
        )
        ads_params = parse_qs(urlsplit(begin_ads.headers["location"]).query)
        assert hub.ADS in ads_params["scope"][0]
        assert (
            client.get(
                "/auth/callback?code=ads&state=" + ads_params["state"][0]
            ).status_code
            == 200
        )
        save.assert_called_once()
        gtm_start = client.get(
            hub.HOME + "/google?kanal=gtm", follow_redirects=False
        )
        gtm_params = parse_qs(urlsplit(gtm_start.headers["location"]).query)
        assert gtm_params["scope"] == [
            "openid email " + hub.google_grant.GTM_READ
        ]
        callback_params = {"code": "gtm", "state": gtm_params["state"][0]}
        original_identity = http.get.return_value.json.return_value
        http.get.return_value.json.return_value = {
            **original_identity,
            "sub": "other-member",
        }
        mismatch = client.get("/auth/callback", params=callback_params)
        assert mismatch.status_code == 403
        save.assert_called_once()
        assert "kanal=gtm" in client.get(hub.HOME).text
        http.get.return_value.json.return_value = original_identity
        gtm_start = client.get(
            hub.HOME + "/google?kanal=gtm", follow_redirects=False
        )
        gtm_params = parse_qs(urlsplit(gtm_start.headers["location"]).query)
        gtm_result = client.get(
            "/auth/callback",
            params={"code": "gtm", "state": gtm_params["state"][0]},
            follow_redirects=False,
        )
        assert gtm_result.headers["location"] == hub.ACCOUNT
        assert save.call_args.args[-1] == hub.google_grant.GTM_READ
        edit_start = client.get(
            hub.HOME + "/google?kanal=gtm_edit", follow_redirects=False
        )
        edit_params = parse_qs(urlsplit(edit_start.headers["location"]).query)
        assert edit_params["scope"] == [
            "openid email " + hub.google_grant.GTM_EDIT
        ]
        assert (
            client.get(
                "/auth/callback",
                params={"code": "gtm-edit", "state": edit_params["state"][0]},
                follow_redirects=False,
            ).headers["location"]
            == hub.ACCOUNT
        )
        assert save.call_args.args[-1] == hub.google_grant.GTM_EDIT
        dashboard = client.get(hub.HOME)
        assert all(x in dashboard.text for x in ("123", "456", "v@example.com"))
        assert dashboard.text.count("<summary>Visa konton</summary>") == 2
        assert dashboard.text.count('class="channel-card__primary"') == 3
        assert all(
            f"/icons/{filename}" in dashboard.text
            for filename in (
                "google-ads.webp",
                "linkedin-in.png",
                "google-tag-manager.svg",
            )
        )
        assert (
            client.post(
                hub.HOME, headers={"origin": "https://example.com"}
            ).status_code
            == 403
        )
        response = client.post(
            hub.HOME,
            headers={"origin": "https://testserver"},
            follow_redirects=False,
        )
        assert response.headers["location"].startswith("/linkedin/connect/")
        assert db.issue.call_args.args == (
            "connect",
            {"owner": digest({"provider": "google", "subject": "member-1"})},
        )
        assert client.get(callback).status_code == 400
        begin = client.get(
            hub.HOME + "/google?kanal=linkedin", follow_redirects=False
        )
        params = parse_qs(urlsplit(begin.headers["location"]).query)
        direct = client.get(
            "/auth/callback?code=another&state=" + params["state"][0],
            follow_redirects=False,
        )
        assert direct.headers["location"].startswith("/linkedin/connect/")

        proposal_path = "/approvals/" + "a" * 32
        for requested, expected in (
            (proposal_path, proposal_path),
            (hub.ACTIVITY_PATH, hub.ACTIVITY_PATH),
            ("//example.com", hub.HOME),
        ):
            begin = client.get(
                hub.HOME + "/google",
                params={"till": requested},
                follow_redirects=False,
            )
            state = parse_qs(urlsplit(begin.headers["location"]).query)[
                "state"
            ][0]
            result = client.get(
                "/auth/callback",
                params={"code": "test", "state": state},
                follow_redirects=False,
            )
            assert result.headers["location"] == expected
        member.return_value = "pending"
        begin = client.get(
            hub.HOME + "/google?kanal=google", follow_redirects=False
        )
        assert begin.headers["location"] == hub.ACCOUNT
