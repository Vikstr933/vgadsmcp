"""Separate, encrypted Google service grants bound to the Viks member."""

import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from ads_mcp.write_policy import WriteBlocked

ADS = "https://www.googleapis.com/auth/adwords"


GTM_READ = "https://www.googleapis.com/auth/tagmanager.readonly"
SERVICES = {
    ADS: ("Google Ads", "viks_google_ads_grants"),
    GTM_READ: ("Google Tag Manager", "viks_gtm_grants"),
}


def grant_ref(owner, scope=ADS):
    from ads_mcp.linkedin_auth import connection_store

    return (
        connection_store().client.collection(SERVICES[scope][1]).document(owner)
    )


def save(owner, response, scope=ADS):
    from ads_mcp.connections import crypto

    refresh = response.get("refresh_token")
    if scope not in response.get("scope", "").split() or not refresh:
        raise WriteBlocked(
            f"{SERVICES[scope][0]} behöver ett nytt godkännande."
        )
    data = {
        "access": response["access_token"],
        "refresh": refresh,
        "expiry": (
            datetime.now(timezone.utc)
            + timedelta(seconds=response["expires_in"])
        ).isoformat(),
    }
    grant_ref(owner, scope).set(
        {"encrypted": crypto().encrypt(json.dumps(data).encode()).decode()}
    )


def credentials(owner, scope=ADS):
    from ads_mcp.connections import crypto

    record = grant_ref(owner, scope).get().to_dict()
    if not record:
        raise WriteBlocked(
            f"Anslut {SERVICES[scope][0]} på https://app.viksannonsverktyg.se/mitt-konto."
        )
    data = json.loads(crypto().decrypt(record["encrypted"].encode()))
    return Credentials(
        token=data["access"],
        refresh_token=data["refresh"],
        expiry=datetime.fromisoformat(data["expiry"])
        .astimezone(timezone.utc)
        .replace(tzinfo=None),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET"],
    )
