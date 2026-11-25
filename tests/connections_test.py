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
        patch.object(hub, "google_accounts", return_value=["123"]),
        patch.object(
            hub, "linkedin_details", return_value="LinkedIn-konto 456"
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
                    "email": "v@example.com",
                }
            }
        )
        assert "Anslut Google Ads" in client.get(hub.HOME).text
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
        dashboard = client.get(hub.HOME)
        assert all(x in dashboard.text for x in ("123", "456", "v@example.com"))
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
