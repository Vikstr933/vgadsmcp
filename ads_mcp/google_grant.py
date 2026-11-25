"""Separate, user-bound Google Ads consent for identity-only MCP connections."""

import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from ads_mcp.write_policy import WriteBlocked

ADS = "https://www.googleapis.com/auth/adwords"


def grant_ref(owner):
    from ads_mcp.linkedin_auth import connection_store

    return (
        connection_store()
        .client.collection("viks_google_ads_grants")
        .document(owner)
    )


def save(owner, response):
    from ads_mcp.connections import crypto

    refresh = response.get("refresh_token")
    if ADS not in response.get("scope", "").split() or not refresh:
        raise WriteBlocked("Google Ads behöver ett nytt godkännande.")
    data = {
        "access": response["access_token"],
        "refresh": refresh,
        "expiry": (
            datetime.now(timezone.utc)
            + timedelta(seconds=response["expires_in"])
        ).isoformat(),
    }
    grant_ref(owner).set(
        {"encrypted": crypto().encrypt(json.dumps(data).encode()).decode()}
    )


def credentials(owner):
    from ads_mcp.connections import crypto

    record = grant_ref(owner).get().to_dict()
    if not record:
        raise WriteBlocked(
            "Anslut Google Ads på https://app.viksannonsverktyg.se/anslutningar."
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
