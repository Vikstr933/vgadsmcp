"""Bind LinkedIn OAuth grants to the existing verified MCP user, durably."""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet
from starlette.concurrency import run_in_threadpool
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from ads_mcp.write_approval import HEADERS, browser_identity
from ads_mcp.write_policy import WriteBlocked, digest

COOKIE = "__Host-viks-linkedin-oauth"


def config(*, storage_only=False):
    base = os.getenv("GOOGLE_ADS_MCP_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    client_id = os.getenv("LINKEDIN_MCP_CLIENT_ID", "")
    client_secret = os.getenv("LINKEDIN_MCP_CLIENT_SECRET", "")
    project = os.getenv("GOOGLE_ADS_MCP_WRITE_FIRESTORE_PROJECT", "")
    encryption = (
        os.getenv("GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY")
        or os.getenv("GOOGLE_ADS_MCP_JWT_SIGNING_KEY")
        or os.getenv("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", "")
    )
    if (
        not project
        or not encryption
        or (not storage_only and (not client_id or not client_secret))
    ):
        raise WriteBlocked(
            "LinkedIn setup is incomplete. Configure client ID, client secret, encrypted storage and Firestore."
        )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise WriteBlocked(
            "LinkedIn requires the configured public HTTPS base URL."
        )
    return {
        "base_url": base,
        "client_id": client_id,
        "client_secret": client_secret,
        "project": project,
        "database": os.getenv(
            "GOOGLE_ADS_MCP_WRITE_FIRESTORE_DATABASE", "(default)"
        ),
        "encryption": encryption,
        "callback": base + "/linkedin/callback",
    }


class LinkedInStore:
    def __init__(self):
        from google.cloud import firestore

        settings = config(storage_only=True)
        self.client = firestore.Client(
            project=settings["project"], database=settings["database"]
        )
        key = base64.urlsafe_b64encode(
            hashlib.sha256(
                ("viks-linkedin-storage-v1:" + settings["encryption"]).encode()
            ).digest()
        )
        self.fernet = Fernet(key)
        self.grants = self.client.collection("viks_linkedin_grants")
        self.nonces = self.client.collection("viks_linkedin_oauth_nonces")

    def issue(self, kind, data):
        nonce = secrets.token_urlsafe(32)
        self.nonces.document(digest({"nonce": nonce})).create(
            {
                "kind": kind,
                "data": data,
                "expires_at": time.time() + 600,
                "delete_after": datetime.now(timezone.utc)
                + timedelta(minutes=15),
            }
        )
        return nonce

    def peek(self, nonce, kind):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce or ""):
            raise WriteBlocked("Invalid or expired LinkedIn connection link.")
        record = self.nonces.document(digest({"nonce": nonce})).get().to_dict()
        self.check_nonce(record, kind)
        return record["data"]

    @staticmethod
    def check_nonce(record, kind):
        if (
            not record
            or record["kind"] != kind
            or record["expires_at"] <= time.time()
        ):
            raise WriteBlocked("Invalid or expired LinkedIn connection link.")

    def consume(self, nonce, kind, browser=None):
        from google.cloud import firestore

        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce or ""):
            raise WriteBlocked("Invalid LinkedIn state.")
        ref = self.nonces.document(digest({"nonce": nonce}))

        @firestore.transactional
        def consume_once(transaction):
            record = ref.get(transaction=transaction).to_dict()
            self.check_nonce(record, kind)
            if browser is not None and not secrets.compare_digest(
                record["data"].get("browser", ""), digest({"browser": browser})
            ):
                raise WriteBlocked("LinkedIn browser session does not match.")
            transaction.delete(ref)
            return record["data"]

        return consume_once(self.client.transaction())

    def save_grant(self, owner, data):
        token = data.get("access_token")
        expires = data.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires, int)
            or isinstance(expires, bool)
            or expires <= 0
        ):
            raise WriteBlocked("LinkedIn returned an invalid token response.")
        grant = {
            "access_token": token,
            "connection_id": secrets.token_hex(16),
            "expires_at": time.time() + expires,
            "scope": data.get("scope", ""),
        }
        # Neither token nor upstream payload appears in logs or MCP results.
        self.grants.document(owner).set(
            {
                "encrypted": self.fernet.encrypt(
                    json.dumps(grant).encode()
                ).decode(),
                "expires_at": grant["expires_at"],
                "delete_after": datetime.now(timezone.utc)
                + timedelta(seconds=expires, days=1),
            }
        )

    def grant(self, owner):
        record = self.grants.document(owner).get().to_dict()
        if not record:
            raise WriteBlocked("Connect LinkedIn first using linkedin_connect.")
        try:
            grant = json.loads(
                self.fernet.decrypt(record["encrypted"].encode())
            )
        except Exception:
            raise WriteBlocked(
                "LinkedIn credentials could not be read. Reconnect LinkedIn."
            ) from None
        if grant["expires_at"] <= time.time() + 60:
            raise WriteBlocked(
                "LinkedIn login has expired. Reconnect using linkedin_connect."
            )
        return grant

    def disconnect(self, owner):
        self.grants.document(owner).delete()


@lru_cache(maxsize=1)
def connection_store():
    return LinkedInStore()


async def connect_page(request):
    secret = os.getenv("GOOGLE_ADS_MCP_APPROVAL_SECRET", "")
    if len(secret) < 32:
        return PlainTextResponse(
            "Anslutningssidan saknar serverkonfiguration.", 503, headers=HEADERS
        )
    owner, login = await browser_identity(request)
    if login is not None:
        return login
    try:
        settings = config()
        store = connection_store()
        ticket = request.path_params["ticket"]
        target = await run_in_threadpool(store.peek, ticket, "connect")
        if target.get("owner") != owner:
            raise WriteBlocked("Anslutningslänken tillhör en annan användare.")
        proof = digest(
            {"secret": secret, "ticket": ticket, "purpose": "linkedin-connect"}
        )
        if request.method == "POST":
            if request.headers.get("origin") != settings["base_url"]:
                raise WriteBlocked("Origin mismatch.")
            if (
                request.headers.get("content-type", "").split(";")[0]
                != "application/x-www-form-urlencoded"
            ):
                raise WriteBlocked("Invalid form.")
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > 2048:
                    raise WriteBlocked("Form too large.")
            fields = parse_qs(body.decode())
            if fields.get("csrf") != [proof]:
                raise WriteBlocked("Invalid form proof.")
            data = await run_in_threadpool(store.consume, ticket, "connect")
            browser = secrets.token_urlsafe(32)
            state = await run_in_threadpool(
                store.issue,
                "callback",
                {
                    "owner": data["owner"],
                    "browser": digest({"browser": browser}),
                },
            )
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": settings["client_id"],
                    "redirect_uri": settings["callback"],
                    "state": state,
                    "scope": "r_ads r_ads_reporting rw_ads",
                }
            )
            response = RedirectResponse(
                "https://www.linkedin.com/oauth/v2/authorization?" + query,
                status_code=303,
                headers=HEADERS,
            )
            response.set_cookie(
                COOKIE,
                browser,
                max_age=600,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response
        return HTMLResponse(
            '<!doctype html><html lang="sv"><meta charset="utf-8"><title>Anslut LinkedIn</title>'
            "<h1>Anslut LinkedIn till Viks Annonsverktyg</h1>"
            "<p>Anslutningen knyts till det Google-konto du använde i Viks Annonsverktyg. Fortsätt om du själv startade anslutningen.</p>"
            "<p>LinkedIn ber om åtkomst till annonskonton, rapporter och annonsändringar. "
            "Ändringar i verktyget kräver separat kontopolicy och godkännande. "
            "Anslutningsuppgifter sparas krypterat i ditt Google Cloud-projekt.</p>"
            f'<form method="post"><input type="hidden" name="csrf" value="{proof}">'
            "<button>Fortsätt till LinkedIn</button></form></html>",
            headers={
                **HEADERS,
                "Content-Security-Policy": HEADERS[
                    "Content-Security-Policy"
                ].replace(
                    "form-action 'self'",
                    "form-action 'self' https://www.linkedin.com",
                ),
            },
        )
    except Exception:
        return PlainTextResponse(
            "Länken saknas, har använts eller har gått ut. Skapa en ny anslutningslänk i chatten.",
            409,
            headers=HEADERS,
        )


async def callback_page(request):
    try:
        settings = config()
        store = connection_store()
        browser = request.cookies.get(COOKIE)
        if not browser:
            raise WriteBlocked("Browser binding missing.")
        data = await run_in_threadpool(
            store.consume,
            request.query_params.get("state", ""),
            "callback",
            browser,
        )
        if request.query_params.get("error") or not request.query_params.get(
            "code"
        ):
            raise WriteBlocked("LinkedIn authorization declined.")
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            response = await client.post(
                "https://www.linkedin.com/oauth/v2/accessToken",
                data={
                    "grant_type": "authorization_code",
                    "code": request.query_params["code"],
                    "redirect_uri": settings["callback"],
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                },
            )
        if response.status_code != 200:
            raise WriteBlocked("LinkedIn token exchange failed.")
        await run_in_threadpool(
            store.save_grant, data["owner"], response.json()
        )
        result = HTMLResponse(
            '<!doctype html><html lang="sv"><meta charset="utf-8"><title>LinkedIn anslutet</title><h1>LinkedIn är anslutet</h1><p><a href="/anslutningar">Visa mina anslutningar och annonskonton</a></p><p>Du kan nu använda LinkedIn i din anslutna chatt.</p></html>',
            headers=HEADERS,
        )
    except Exception:
        result = PlainTextResponse(
            "LinkedIn kunde inte anslutas. Kontrollera appinställningarna och skapa en ny anslutningslänk.",
            400,
            headers=HEADERS,
        )
    result.delete_cookie(
        COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    return result


def register_linkedin_routes(mcp):
    mcp.custom_route("/linkedin/connect/{ticket}", methods=["GET", "POST"])(
        connect_page
    )
    mcp.custom_route("/linkedin/callback", methods=["GET"])(callback_page)
