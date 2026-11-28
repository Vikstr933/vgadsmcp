"""Read-only live checks for the Viks product connections."""

import asyncio
import os
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from ads_mcp import google_grant, google_media, gtm_drafts
from ads_mcp.linkedin_auth import connection_store
from ads_mcp.linkedin_client import LinkedInAPI
from ads_mcp.utils import get_googleads_service, google_ads_error_codes
from ads_mcp.write_policy import WriteBlocked, WritePolicy, principal

status_mcp = FastMCP("status")
GAdsId = Annotated[
    str, Field(pattern=r"^(?:[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4})$")
]
GtmId = Annotated[str, Field(pattern=r"^[0-9]{1,24}$")]
LinkedInId = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


def safe_check(call):
    try:
        return call()
    except RefreshError:
        return {
            "result": "reconnect_required",
            "message": "Google-åtkomsten behöver förnyas i Min översikt.",
        }
    except GoogleAdsException as exc:
        return {
            "result": "access_failed",
            "api_codes": google_ads_error_codes(exc),
        }
    except WriteBlocked as exc:
        return {"result": "unavailable", "message": str(exc)}
    except Exception:
        return {
            "result": "unavailable",
            "message": "Anslutningen kunde inte kontrolleras just nu.",
        }


def google_read(customer_id, manager_id):
    if customer_id:
        customer = customer_id.replace("-", "")
        service = get_googleads_service("GoogleAdsService", manager_id)
        response = service.search(
            customer_id=customer,
            query="SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1",
            timeout=10,
        )
        identity = next(iter(response), None)
        if identity is None or str(identity.customer.id) != customer:
            raise WriteBlocked(
                "Google Ads returnerade inte det valda annonskontot."
            )
        return {
            "result": "reachable",
            "target": customer,
            "name": identity.customer.descriptive_name,
        }
    service = get_googleads_service("CustomerService")
    accounts = service.list_accessible_customers(timeout=10)
    return {
        "result": "reachable",
        "direct_account_count": len(accounts.resource_names),
    }


def google_check(customer_id, manager_id):
    def write_setup():
        policy = WritePolicy.load()
        policy.require_writes()
        configured = (
            customer_id.replace("-", "") in policy.accounts
            or (manager_id or "").replace("-", "") in policy.managers
            if customer_id
            else bool(policy.accounts or policy.managers)
        )
        return {
            "result": "configured" if configured else "policy_missing",
            "mode": policy.mode,
        }

    return {
        "read": safe_check(lambda: google_read(customer_id, manager_id)),
        "write_setup": safe_check(write_setup),
    }


def gtm_access(owner, scope, path):
    credential = google_grant.credentials(owner, scope)
    with AuthorizedSession(credential, refresh_timeout=8) as client:
        if path:
            container = gtm_drafts.api_request(
                client, "GET", path, fields="name,publicId,usageContext"
            )
            return {
                "result": "reachable",
                "target": container.get("publicId"),
                "name": container.get("name"),
                "web_container": "web" in container.get("usageContext", []),
            }
        data = gtm_drafts.api_request(
            client, "GET", "accounts", fields="account(accountId),nextPageToken"
        )
        return {
            "result": "reachable",
            "account_count_on_first_page": len(data.get("account", [])),
        }


def gtm_check(owner, account_id, container_id):
    path = (
        gtm_drafts.container_path_for(account_id, container_id)
        if account_id and container_id
        else None
    )

    def write_setup():
        settings = gtm_drafts.policy()
        return {"result": "configured", "mode": settings.mode}

    return {
        "read": safe_check(
            lambda: gtm_access(owner, google_grant.GTM_READ, path)
        ),
        "edit_grant_read_check": safe_check(
            lambda: gtm_access(owner, google_grant.GTM_EDIT, path)
        ),
        "write_setup": safe_check(write_setup),
    }


def linkedin_check(owner, account_id):
    def read():
        grant = connection_store().grant(owner)
        api = LinkedInAPI(grant["access_token"])
        if account_id:
            account = api.account(account_id)
            return {
                "result": "reachable",
                "target": account_id,
                "name": account.get("name"),
            }
        data = api.accounts(count=1)
        return {
            "result": "reachable",
            "has_visible_accounts": bool(data.get("elements")),
        }

    mode = os.getenv("LINKEDIN_MCP_WRITE_MODE", "dry_run")
    return {"read": safe_check(read), "write_setup": {"mode": mode}}


def google_product_check(owner, scope):
    def read():
        if scope == google_grant.GA4_READ:
            url = (
                "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
            )
            params = {"pageSize": 1}
        else:
            url = "https://www.googleapis.com/drive/v3/about"
            params = {"fields": "kind"}
        with google_media.google_session(owner, scope) as client:
            google_media.checked(client.get(url, params=params, timeout=12))
        return {"result": "reachable"}

    return {"read": safe_check(read)}


@status_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def check_connections(
    product: Literal[
        "all", "google_ads", "gtm", "linkedin", "ga4", "drive"
    ] = "all",
    google_customer_id: GAdsId | None = None,
    google_manager_id: GAdsId | None = None,
    gtm_account_id: GtmId | None = None,
    gtm_container_id: GtmId | None = None,
    linkedin_account_id: LinkedInId | None = None,
) -> dict:
    """Test Viks connections and optional target accounts using read calls only. GTM also checks whether the separate edit grant can read the specified web container. A successful read check never verifies a write. Use known numeric IDs; omit them for a connection-level check."""
    try:
        owner = principal()
    except WriteBlocked as exc:
        raise ToolError(str(exc)) from None
    if product in ("gtm", "all") and bool(gtm_account_id) != bool(
        gtm_container_id
    ):
        raise ToolError("Ange både GTM-kontots och containerns numeriska ID.")
    if (
        product in ("google_ads", "all")
        and google_manager_id
        and not google_customer_id
    ):
        raise ToolError(
            "Ange även annonskontots ID för kontroll via ett Google Ads-förvaltarkonto."
        )
    checks = {
        "google_ads": (google_check, google_customer_id, google_manager_id),
        "ga4": (google_product_check, owner, google_grant.GA4_READ),
        "drive": (google_product_check, owner, google_grant.DRIVE_READ),
        "gtm": (gtm_check, owner, gtm_account_id, gtm_container_id),
        "linkedin": (linkedin_check, owner, linkedin_account_id),
    }
    selected = checks if product == "all" else {product: checks[product]}
    results = await asyncio.gather(
        *(run_in_threadpool(*check) for check in selected.values())
    )
    return {
        "checks": dict(zip(selected, results)),
        "write_verified": False,
        "message": "Kontrollen läser API:erna. En godkänd skrivning behöver verifieras i en separat körning.",
    }
