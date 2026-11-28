"""Browser channel dashboard. The Google subject is shared with the MCP grant."""

import base64
import hashlib
import html
import json
import os
import re
import secrets
from typing import NamedTuple
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ads_mcp import google_grant, google_media
from ads_mcp.membership import (
    ACCOUNT,
    ADMIN,
    ADMIN_HOME,
    MEMBERS,
    admin_page,
    membership_page,
    member_state,
    verified_user,
)
from ads_mcp.activity import PATH as ACTIVITY_PATH, activity_page, is_admin
from ads_mcp.linkedin_auth import DASHBOARD_TTL, config, connection_store
from ads_mcp.linkedin_client import LinkedInAPI
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor
from ads_mcp.product_icons import PRODUCTS, product_icon
from ads_mcp.write_approval import HEADERS
from ads_mcp.write_policy import WriteBlocked, digest

HOME = "/anslutningar"
FLOW = "__Host-viks-dashboard-google"
SESSION = "__Host-viks-dashboard-session"
PREFIX = "viks.dashboard."
ADS = google_grant.ADS
CHANNEL_SCOPES = {
    "google": ADS,
    "gtm": google_grant.GTM_READ,
    "drive": google_grant.DRIVE_READ,
    "ga4": google_grant.GA4_READ,
    "gtm_edit": google_grant.GTM_EDIT,
}
HEAD = {
    **HEADERS,
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": HEADERS["Content-Security-Policy"].replace(
        "style-src 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self'",
    ),
}


def page(
    content, status=200, *, title="Anslutningar", user=None, landing=False
):
    navigation = (
        '<a href="/mitt-konto">Min översikt</a>'
        if user
        else ('<a href="/anslutningar/google?till=/mitt-konto">Logga in</a>')
    )
    if is_admin(user):
        navigation += '<a href="/admin">Admin</a>'
    if user:
        navigation += '<form method="post" action="/logga-ut"><button class="subtle">Logga ut</button></form>'
    heading = "" if landing else f"<h1>{html.escape(title)}</h1>"
    return HTMLResponse(
        '<!doctype html><html lang="sv"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} · Viks Annonsverktyg</title>"
        '<link rel="icon" href="/favicon.ico"><link rel="stylesheet" href="/portal.css"><script src="/portal.js" defer></script>'
        '<a class="skip" href="#main">Hoppa till innehåll</a>'
        '<header><div class="shell top"><a class="brand" href="/">'
        '<span class="mark" aria-hidden="true">V</span>Viks Annonsverktyg</a>'
        f'<nav aria-label="Huvudmeny">{navigation}</nav></div></header>'
        f'<main class="shell" id="main">{heading}{content}</main>'
        '<footer class="shell"><a href="/privacy.html">Integritet</a>'
        '<a href="/terms.html">Villkor</a><a href="mailto:viktorstrindin93@gmail.com">Kontakt</a></footer>',
        status,
        headers=HEAD,
    )


def cookie(response, key, value, *, max_age=600):
    response.set_cookie(
        key,
        value,
        max_age=max_age,
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
        channel = request.query_params.get("kanal")
        linkedin_first = channel == "linkedin"
        scope = CHANNEL_SCOPES.get(channel)
        target = request.query_params.get("till", "")
        if target not in (
            ACTIVITY_PATH,
            ACCOUNT,
            ADMIN,
            ADMIN_HOME,
            MEMBERS,
        ) and not re.fullmatch(
            r"/(?:(?:(?:linkedin|gtm)/)?approvals/[a-f0-9]{32}|linkedin/(?:connect|images/upload)/[A-Za-z0-9_-]{43})",
            target,
        ):
            target = HOME
        bound = None
        if scope:
            current = await run_in_threadpool(session, request)
            if (
                not current
                or await run_in_threadpool(member_state, current) != "active"
            ):
                return RedirectResponse(ACCOUNT, 303, headers=HEAD)
            bound = current["owner"]
            if channel in ("gtm", "gtm_edit", "drive", "ga4"):
                target = ACCOUNT
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
                "scope": "openid email " + scope if scope else "openid email",
                "prompt": (
                    "select_account consent" if bound else "select_account"
                ),
                **({"access_type": "offline"} if bound else {}),
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
            .encrypt(
                json.dumps(
                    [state, verifier, linkedin_first, target, bound, channel]
                ).encode()
            )
            .decode(),
        )
        return response
    except Exception:
        return page("Google-inloggningen kunde inte startas.", 503)


def google_accounts(credentials):
    client = GoogleAdsClient(
        credentials=credentials,
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
        flow = json.loads(
            crypto().decrypt(request.cookies.get(FLOW, "").encode(), ttl=600)
        )
        state, verifier, linkedin_first = flow[:3]
        target = flow[3] if len(flow) >= 4 else HOME
        bound = flow[4] if len(flow) >= 5 else None
        channel = flow[5] if len(flow) >= 6 else "google"
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
        identity = verified_user(user)
        owner = identity["owner"]
        if bound and bound != owner:
            raise WriteBlocked(
                "Välj samma Google-konto som du använder för ditt medlemskap."
            )
        state = await run_in_threadpool(member_state, identity, signup=True)
        ids = None
        if bound and state == "active":
            if channel in ("gtm", "gtm_edit", "drive", "ga4"):
                await run_in_threadpool(
                    google_grant.save,
                    owner,
                    exchange.json(),
                    CHANNEL_SCOPES[channel],
                )
            else:
                await run_in_threadpool(
                    google_grant.save, owner, exchange.json()
                )
        if bound and state == "active" and channel == "google":
            try:
                ids = await run_in_threadpool(
                    google_accounts, Credentials(token=token)
                )
            except Exception:
                pass
        if (
            channel in ("gtm", "gtm_edit", "drive", "ga4")
            and bound
            and state == "active"
        ):
            previous = await run_in_threadpool(session, request)
            if previous and previous.get("owner") == owner:
                ids = previous.get("accounts")
        snapshot = (
            crypto()
            .encrypt(json.dumps({**identity, "accounts": ids}).encode())
            .decode()
        )
        sid = await run_in_threadpool(
            lambda: connection_store().issue(
                "dashboard", {"snapshot": snapshot}
            )
        )
        destination = (
            await run_in_threadpool(linkedin_link, owner)
            if linkedin_first and state == "active"
            else (target if state == "active" else ACCOUNT)
        )
        response = RedirectResponse(destination, 303, headers=HEAD)
        cookie(response, SESSION, sid, max_age=DASHBOARD_TTL)
    except WriteBlocked as exc:
        response = page(html.escape(str(exc)), 403)
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
            crypto().decrypt(record["snapshot"].encode(), ttl=DASHBOARD_TTL)
        )
    except (WriteBlocked, InvalidToken):
        return None


class ChannelDetails(NamedTuple):
    label: str
    tone: str
    linked: bool
    content: str = ""


def accounts_disclosure(rows, *, following="", expanded=False):
    listing = "<ul>" + "".join(f"<li>{row}</li>" for row in rows) + "</ul>"
    return (
        f'<details class="channel-accounts"{" open" if expanded else ""}>'
        f"<summary>Visa konton</summary>{listing}{following}</details>"
    )


def linkedin_details(owner, cursor):
    if len(cursor) > 4096:
        raise WriteBlocked("Invalid cursor")
    try:
        config()
        grant = connection_store().grant(owner)
    except WriteBlocked as exc:
        if "Connect LinkedIn first" in str(exc):
            return ChannelDetails("Ej anslutet", "neutral", False)
        if "expired" in str(exc):
            return ChannelDetails("Åtkomsten har gått ut", "warning", True)
        return ChannelDetails("Status kunde inte hämtas", "warning", False)
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
        token = result.get("metadata", {}).get("nextPageToken")
        following = (
            '<a href="?'
            + html.escape(urlencode({"sida": token}), quote=True)
            + '#linkedin-konton">Nästa sida</a>'
            if token
            else ""
        )
        listing = (
            accounts_disclosure(
                rows, following=following, expanded=bool(cursor)
            )
            if rows or following
            else "<p>Inga annonskonton hittades.</p>"
        )
        return ChannelDetails("Anslutet", "good", True, listing)
    except Exception:
        return ChannelDetails(
            "Åtkomst sparad",
            "warning",
            True,
            "<p>Kontolistan kunde inte hämtas.</p>",
        )


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
                '<a class="btn" href="/anslutningar/google?till=/mitt-konto">Öppna eller skapa Mitt konto</a></section>'
                "<section><h2>LinkedIn Ads</h2><p>Välj ditt Google-konto och fortsätt sedan till LinkedIn.</p>"
                '<a class="btn" href="/anslutningar/google?kanal=linkedin">Anslut LinkedIn</a></section>'
                '<small>Administratörerna kan se aktiva dagar och antal verktygsanrop per användare. <a href="/privacy.html">Integritetspolicy</a></small>'
            )
        if await run_in_threadpool(member_state, user) != "active":
            return RedirectResponse(ACCOUNT, 303, headers=HEAD)
        if request.method == "POST":
            if (
                request.headers.get("origin")
                != config(storage_only=True)["base_url"]
            ):
                return page("Ogiltigt ursprung.", 403)
            destination = await run_in_threadpool(linkedin_link, user["owner"])
            return RedirectResponse(destination, 303, headers=HEAD)
        return page(
            "<p>Inloggad som "
            + html.escape(user["email"])
            + "</p>"
            + await channel_cards(user, request),
            title="Anslutningar",
            user=user,
        )
    except Exception:
        return page(
            '<p>Anslutningen kunde inte öppnas just nu.</p><a href="/anslutningar">Tillbaka till anslutningar</a>',
            503,
        )


async def google_details(user):
    ids = user.get("accounts")
    if ids is None:
        try:
            credentials = await run_in_threadpool(
                google_grant.credentials, user["owner"]
            )
        except WriteBlocked:
            return ChannelDetails("Ej anslutet", "neutral", False)
        except Exception:
            return ChannelDetails("Status kunde inte hämtas", "warning", False)
        try:
            ids = await run_in_threadpool(google_accounts, credentials)
        except Exception:
            return ChannelDetails(
                "Åtkomst sparad",
                "warning",
                True,
                "<p>Kontolistan kunde inte hämtas.</p>",
            )
    listing = (
        accounts_disclosure([html.escape(i) for i in ids])
        if ids
        else "<p>Inga direkt åtkomliga annonskonton hittades.</p>"
    )
    return ChannelDetails("Anslutet", "good", True, listing)


def channel_card(key, info, primary, secondary=""):
    title = PRODUCTS[key][0]
    return (
        f'<section class="channel-card" id="{key}-konton" aria-labelledby="{key}-product">'
        '<div class="channel-card__heading">'
        + product_icon(key)
        + f'<h2 id="{key}-product">{title}</h2></div>'
        f'<div class="channel-card__body"><p class="channel-state channel-state--{info.tone}">{info.label}</p>{info.content}</div>'
        f'<div class="channel-card__primary">{primary}</div>'
        f'<div class="channel-card__secondary">{secondary}</div></section>'
    )


async def channel_cards(user, request):
    import asyncio

    google, linkedin, gtm, ga4 = await asyncio.gather(
        google_details(user),
        run_in_threadpool(
            linkedin_details,
            user["owner"],
            request.query_params.get("sida", ""),
        ),
        run_in_threadpool(gtm_card, user["owner"]),
        run_in_threadpool(read_card, user["owner"], "ga4"),
    )
    google_action = "Förnya åtkomst" if google.linked else "Anslut Google Ads"
    linkedin_action = "Förnya åtkomst" if linkedin.linked else "Anslut LinkedIn"
    return (
        '<div class="grid channels-grid">'
        + channel_card(
            "google",
            google,
            f'<a class="btn" href="/anslutningar/google?kanal=google&amp;till=/mitt-konto" aria-label="{google_action} – Google Ads">{google_action}</a>',
        )
        + channel_card(
            "linkedin",
            linkedin,
            '<form method="post" action="/anslutningar">'
            f'<button aria-label="{linkedin_action} – LinkedIn Ads">{linkedin_action}</button></form>',
        )
        + gtm
        + ga4
        + '</div><small class="channel-trademarks">Google Ads och Google Tag Manager är varumärken som tillhör Google LLC. '
        "LinkedIn är ett varumärke som tillhör LinkedIn Corporation och dess närstående bolag.</small>"
    )


def read_card(owner, channel):
    try:
        linked = (
            google_grant.grant_ref(owner, CHANNEL_SCOPES[channel]).get().exists
        )
        status = ChannelDetails(
            "Läsåtkomst sparad" if linked else "Ej anslutet",
            "good" if linked else "neutral",
            linked,
        )
        if channel == "drive" and linked:
            listed = "".join(
                f'<li>{html.escape(row.get("name"))} <button name="remove" value="{html.escape(row.id, quote=True)}">Koppla från mappen</button></li>'
                for row in google_media.folders(owner).stream()
            )
            status = status._replace(
                content=(
                    f'<form method="post" action="/anslutningar/drive-mappar"><ul>{listed}</ul></form>'
                    '<form method="post" action="/anslutningar/drive-mappar"><label>Kundens mapplänk (kan också skickas i Claude) <input type="url" required name="link"></label><button>Koppla mapp</button></form>'
                )
            )
        elif channel == "drive":
            status = status._replace(
                content="<p>Google ber om läsåtkomst till din Drive. I Viks väljer du vilka kundmappar Claude får läsa.</p>"
            )
    except Exception:
        status = ChannelDetails("Status kunde inte hämtas", "warning", False)
    action = (
        "Förnya läsåtkomst"
        if status.linked
        else "Anslut " + PRODUCTS[channel][0]
    )
    return channel_card(
        channel,
        status,
        f'<a class="btn" href="/anslutningar/google?kanal={channel}">{action}</a>',
    )


async def manage_drive_folders(request):
    user = await run_in_threadpool(session, request)
    if not user or await run_in_threadpool(member_state, user) != "active":
        return RedirectResponse(ACCOUNT, 303, headers=HEAD)
    if request.method == "GET":
        card = await run_in_threadpool(read_card, user["owner"], "drive")
        return page(card, title="Drive-mappar i Viks", user=user)
    if request.headers.get("origin") != config(storage_only=True)["base_url"]:
        return page("Ogiltigt ursprung.", 403)
    form = await request.form()
    try:
        if "remove" in form:
            root = str(form["remove"])
            if not re.fullmatch(google_media.FILE_ID, root):
                raise WriteBlocked("Ogiltigt mapp-ID.")
            await run_in_threadpool(
                lambda: google_media.folders(user["owner"])
                .document(root)
                .delete()
            )
        else:
            await run_in_threadpool(
                google_media.link_folder,
                user["owner"],
                str(form.get("link", "")).strip(),
            )
    except WriteBlocked as exc:
        return page(html.escape(str(exc)), 400, user=user)
    return RedirectResponse(HOME + "/drive-mappar", 303, headers=HEAD)


def gtm_card(owner):
    try:
        linked = (
            google_grant.grant_ref(owner, google_grant.GTM_READ).get().exists
        )
        edit_linked = (
            google_grant.grant_ref(owner, google_grant.GTM_EDIT).get().exists
        )
    except Exception:
        status = ChannelDetails("Status kunde inte hämtas", "warning", False)
        linked = edit_linked = False
    else:
        status = ChannelDetails(
            "Läsåtkomst sparad" if linked else "Ej anslutet",
            "good" if linked else "neutral",
            linked,
            "<p>Utkaståtkomst sparad.</p>" if edit_linked else "",
        )
    action = "Förnya läsåtkomst" if linked else "Anslut GTM"
    edit_action = (
        "Förnya utkaståtkomst" if edit_linked else "Aktivera GTM-utkast"
    )
    return channel_card(
        "gtm",
        status,
        f'<a class="btn" href="/anslutningar/google?kanal=gtm">{action}</a>',
        f'<a href="/anslutningar/google?kanal=gtm_edit">{edit_action}</a>',
    )


def claude_setup():
    address = html.escape(
        config(storage_only=True)["base_url"] + "/mcp", quote=True
    )
    return (
        '<section id="claude"><h2>Anslut i Claude</h2>'
        "<p>Lägg till Viks Annonsverktyg en gång i Claude. Samma adress fungerar för Google Ads, LinkedIn Ads och Google Tag Manager.</p>"
        '<label for="mcp-address">Anslutningsadress</label><div class="copy">'
        f'<input id="mcp-address" value="{address}" readonly>'
        '<button type="button" id="copy-address" hidden>Kopiera adress</button></div><div id="copy-status" role="status"></div>'
        "<ol><li>Öppna <strong>Customize → Connectors</strong> i Claude och lägg till en anpassad webbanslutning.</li>"
        "<li>Ange namnet Viks Annonsverktyg och klistra in adressen.</li>"
        "<li>Logga in med samma Google-konto som här. Aktivera anslutningen i chatten för att använda dina anslutna produkter.</li></ol>"
        '<a href="https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp">Claude: guide för att lägga till en anslutning</a></section>'
    )


async def logout(request):
    if request.headers.get("origin") != config(storage_only=True)["base_url"]:
        return page("Ogiltigt ursprung.", 403)
    sid = request.cookies.get(SESSION)
    if sid:
        try:
            await run_in_threadpool(
                connection_store().consume, sid, "dashboard"
            )
        except WriteBlocked:
            pass
    response = RedirectResponse("/", 303, headers=HEAD)
    response.delete_cookie(
        SESSION, path="/", secure=True, httponly=True, samesite="lax"
    )
    return response


def register_dashboard(mcp):
    for path in (ACCOUNT, ADMIN):
        mcp.custom_route(path, methods=["GET", "POST"])(membership_page)
    for path in (ADMIN_HOME, MEMBERS):
        mcp.custom_route(path, methods=["GET"])(admin_page)
    mcp.custom_route("/logga-ut", methods=["POST"])(logout)
    mcp.custom_route(ACTIVITY_PATH, methods=["GET"])(activity_page)
    mcp.custom_route(HOME, methods=["GET", "POST"])(dashboard)
    mcp.custom_route(HOME + "/drive-mappar", methods=["GET", "POST"])(
        manage_drive_folders
    )
    mcp.custom_route(HOME + "/google", methods=["GET"])(google_start)
