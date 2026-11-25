"""Browser channel dashboard. The Google subject is shared with the MCP grant."""

import base64
import hashlib
import html
import json
import os
import re
import secrets
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ads_mcp.linkedin_auth import config, connection_store
from ads_mcp.linkedin_client import LinkedInAPI
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor
from ads_mcp.write_approval import HEADERS
from ads_mcp.write_policy import WriteBlocked, digest

HOME = "/anslutningar"
FLOW = "__Host-viks-dashboard-google"
SESSION = "__Host-viks-dashboard-session"
PREFIX = "viks.dashboard."
ADS = "https://www.googleapis.com/auth/adwords"
HEAD = {**HEADERS, "Referrer-Policy": "same-origin"}


def page(content, status=200):
    return HTMLResponse(
        '<!doctype html><html lang="sv"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>Anslutningar · Viks Annonsverktyg</title><style>"
        "body{margin:0;background:#f4f6f3;color:#173c32;font:1rem/1.55 system-ui}main,header{max-width:48rem;margin:auto;padding:1.2rem}"
        "header{border-bottom:1px solid #bccbc2}h1{margin:.3em 0}h2{margin-top:0}section{background:white;padding:1.3rem;border:1px solid #d6ded7;border-radius:12px;margin:1rem 0}"
        "a{color:#075640}button,.btn{background:#155640;color:white;padding:.6em 1em;border:0;border-radius:6px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}"
        'small{color:#445b51}ul{padding-left:1.3em}</style><header><a href="/">Viks Annonsverktyg</a></header>'
        "<main><h1>Anslutningar</h1>" + content + "</main></html>",
        status,
        headers=HEAD,
    )


def cookie(response, key, value):
    response.set_cookie(
        key,
        value,
        max_age=600,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def crypto():
    key = os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET"]
    if not key:
        raise ValueError("Missing OAuth configuration")
    return Fernet(
        base64.urlsafe_b64encode(
            hashlib.sha256(("viks-dashboard-v1:" + key).encode()).digest()
        )
    )


async def google_start(request):
    try:
        settings = config(storage_only=True)
        state = PREFIX + secrets.token_urlsafe(32)
        linkedin_first = request.query_params.get("kanal") == "linkedin"
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        query = urlencode(
            {
                "client_id": os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_ID"],
                "redirect_uri": settings["base_url"] + "/auth/callback",
                "response_type": "code",
                "scope": "openid email " + ADS,
                "prompt": "select_account",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth?" + query,
            303,
            headers=HEAD,
        )
        cookie(
            response,
            FLOW,
            crypto()
            .encrypt(json.dumps([state, verifier, linkedin_first]).encode())
            .decode(),
        )
        return response
    except Exception:
        return page("Google-inloggningen kunde inte startas.", 503)


def google_accounts(token):
    client = GoogleAdsClient(
        credentials=Credentials(token=token),
        developer_token=os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        use_proto_plus=True,
    )
    service = client.get_service(
        "CustomerService", interceptors=[MCPHeaderInterceptor()]
    )
    return [
        item.removeprefix("customers/")
        for item in service.list_accessible_customers(timeout=12).resource_names
    ]


async def google_callback(request):
    try:
        state, verifier, linkedin_first = json.loads(
            crypto().decrypt(request.cookies.get(FLOW, "").encode(), ttl=600)
        )
        if not secrets.compare_digest(
            state, request.query_params.get("state", "")
        ) or not request.query_params.get("code"):
            raise ValueError("Unverified login")
        settings = config(storage_only=True)
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False
        ) as client:
            exchange = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_ID"],
                    "client_secret": os.environ[
                        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET"
                    ],
                    "redirect_uri": settings["base_url"] + "/auth/callback",
                    "grant_type": "authorization_code",
                    "code": request.query_params["code"],
                    "code_verifier": verifier,
                },
            )
            exchange.raise_for_status()
            token = exchange.json()["access_token"]
            info = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": "Bearer " + token},
            )
            info.raise_for_status()
            user = info.json()
        subject = user["sub"]
        if not isinstance(subject, str) or not subject:
            raise ValueError("Missing subject")
        owner = digest({"provider": "google", "subject": subject})
        try:
            ids = await run_in_threadpool(google_accounts, token)
        except Exception:
            ids = None
        # Snapshot only. Browser sessions contain no Google access or refresh tokens.
        snapshot = (
            crypto()
            .encrypt(
                json.dumps(
                    {
                        "owner": owner,
                        "email": user.get("email", ""),
                        "accounts": ids,
                    }
                ).encode()
            )
            .decode()
        )
        sid = await run_in_threadpool(
            lambda: connection_store().issue(
                "dashboard", {"snapshot": snapshot}
            )
        )
        destination = (
            await run_in_threadpool(linkedin_link, owner)
            if linkedin_first
            else HOME
        )
        response = RedirectResponse(destination, 303, headers=HEAD)
        cookie(response, SESSION, sid)
    except Exception:
        response = page(
            '<p>Google-inloggningen avbröts eller hann gå ut.</p><a href="/anslutningar">Försök igen</a>',
            400,
        )
    response.delete_cookie(
        FLOW, path="/", secure=True, httponly=True, samesite="lax"
    )
    return response


class DashboardCallback:
    """Dispatch only dashboard-prefixed state at the registered Google callback."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["path"] == "/auth/callback"
            and scope["method"] == "GET"
        ):
            request = Request(scope, receive)
            if request.query_params.get("state", "").startswith(PREFIX):
                response = await google_callback(request)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def session(request):
    sid = request.cookies.get(SESSION, "")
    if not re.fullmatch(r"[\w-]{43}", sid, re.ASCII):
        return None
    try:
        record = connection_store().peek(sid, "dashboard")
        return json.loads(
            crypto().decrypt(record["snapshot"].encode(), ttl=600)
        )
    except (WriteBlocked, InvalidToken):
        return None


def linkedin_details(owner, cursor):
    if len(cursor) > 4096:
        raise WriteBlocked("Invalid cursor")
    try:
        config()
        grant = connection_store().grant(owner)
    except WriteBlocked as exc:
        if "Connect LinkedIn first" in str(exc):
            return "Ännu inte anslutet."
        if "expired" in str(exc):
            return "Inloggningen har gått ut. Anslut LinkedIn igen."
        return "Anslutningens status kunde inte kontrolleras."
    try:
        result = LinkedInAPI(grant["access_token"]).accounts(
            cursor or None, 100
        )
        rows = [
            html.escape(str(item.get("name", "")))
            + " · "
            + html.escape(str(item["id"]))
            for item in result["elements"]
        ]
        listing = (
            "<ul>" + "".join("<li>" + row + "</li>" for row in rows) + "</ul>"
            if rows
            else "Inga annonskonton hittades."
        )
        token = result.get("metadata", {}).get("nextPageToken")
        if token:
            listing += (
                '<a href="?'
                + html.escape(urlencode({"sida": token}), quote=True)
                + '">Nästa sida</a>'
            )
        return "Anslutet. " + listing
    except Exception:
        return "Inloggning sparad. LinkedIn kunde inte returnera annonskontona just nu."


def linkedin_link(owner):
    config()
    ticket = connection_store().issue("connect", {"owner": owner})
    return "/linkedin/connect/" + ticket


async def dashboard(request):
    try:
        user = await run_in_threadpool(session, request)
        if not user:
            return page(
                "<section><h2>Google Ads</h2><p>Välj samma Google-konto som du använder för vAds i Claude.</p>"
                '<a class="btn" href="/anslutningar/google">Anslut Google Ads</a></section>'
                "<section><h2>LinkedIn Ads</h2><p>Välj ditt Google-konto och fortsätt sedan till LinkedIn.</p>"
                '<a class="btn" href="/anslutningar/google?kanal=linkedin">Anslut LinkedIn</a></section>'
            )
        if request.method == "POST":
            if (
                request.headers.get("origin")
                != config(storage_only=True)["base_url"]
            ):
                return page("Ogiltigt ursprung.", 403)
            destination = await run_in_threadpool(linkedin_link, user["owner"])
            return RedirectResponse(destination, 303, headers=HEAD)
        ids = user["accounts"]
        if ids is None:
            google = "Google Ads kunde inte returnera annonskontona. Kontrollera behörigheten eller försök igen."
        elif ids:
            google = (
                "Direkt åtkomliga annons- och förvaltarkonton<ul>"
                + "".join("<li>" + html.escape(i) + "</li>" for i in ids)
                + "</ul>"
            )
        else:
            google = "Inga direkt åtkomliga annonskonton hittades."
        linkedin = await run_in_threadpool(
            linkedin_details,
            user["owner"],
            request.query_params.get("sida", ""),
        )
        return page(
            "<p>Inloggad med "
            + html.escape(user["email"])
            + ".</p><section><h2>Google Ads</h2>"
            + google
            + '<p><a href="/anslutningar/google">Välj Google-konto igen</a></p></section><section><h2>LinkedIn Ads</h2>'
            + linkedin
            + '<form method="post"><button>Anslut LinkedIn</button></form></section>'
            "<small>Använd samma Google-konto när du lägger till vAds i din AI-klient. Den här sidan är inloggad i tio minuter.</small>"
        )
    except Exception:
        return page(
            '<p>Anslutningen kunde inte öppnas just nu.</p><a href="/anslutningar">Tillbaka till anslutningar</a>',
            503,
        )


def register_dashboard(mcp):
    mcp.custom_route(HOME, methods=["GET", "POST"])(dashboard)
    mcp.custom_route(HOME + "/google", methods=["GET"])(google_start)
