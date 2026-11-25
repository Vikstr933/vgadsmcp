"""Direct LinkedIn REST adapter. No vendor relay, raw API tool, or write retries."""

import os
import re
from datetime import date

import httpx

from ads_mcp.write_policy import WriteBlocked


class LinkedInRejected(WriteBlocked):
    """The single-object request was explicitly rejected, not an uncertain write."""


def linkedin_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[1-9][0-9]{0,19}", value
    ):
        raise WriteBlocked("Use the numeric LinkedIn object ID, without a URN.")
    return value


class LinkedInAPI:
    def __init__(self, token: str):
        self.token = token
        self.version = os.getenv("LINKEDIN_MCP_API_VERSION", "202611")
        if not re.fullmatch(r"20[0-9]{2}(0[1-9]|1[0-2])", self.version):
            raise WriteBlocked("Invalid LinkedIn API version.")

    def request(
        self,
        method,
        path,
        *,
        params=None,
        body=None,
        partial=False,
        finder=False,
    ):
        # Paths are assembled by typed methods below, never supplied by the model.
        headers = {
            "Authorization": f"Bearer {self.token}",
            "LinkedIn-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if finder:
            headers["X-RestLi-Method"] = "FINDER"
        if partial:
            headers["X-RestLi-Method"] = "PARTIAL_UPDATE"
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(
                    method,
                    "https://api.linkedin.com/rest/" + path,
                    params=params,
                    json=body,
                    headers=headers,
                )
        except httpx.HTTPError:
            raise WriteBlocked(
                "LinkedIn request failed. A write may have succeeded; reconcile before retrying."
            ) from None
        if response.status_code >= 300:
            # Never expose response bodies, tokens, request URLs or account details.
            error = (
                LinkedInRejected
                if response.status_code in (400, 401, 403, 404, 409, 422, 429)
                else WriteBlocked
            )
            raise error(
                f"LinkedIn returned HTTP {response.status_code}. Check permissions, account access and API version."
            )
        data = response.json() if response.content else {}
        return data, response.headers.get("x-restli-id")

    @staticmethod
    def page_params(page_token=None, count=100, search=None):
        params = {"q": "search", "pageSize": count}
        if page_token:
            if not isinstance(page_token, str) or len(page_token) > 4096:
                raise WriteBlocked("Invalid LinkedIn page token.")
            params["pageToken"] = page_token
        if search:
            params["search"] = search
        return params

    def accounts(self, page_token=None, count=100):
        return self.request(
            "GET",
            "adAccounts",
            params=self.page_params(page_token, count),
        )[0]

    def account(self, account_id):
        data = self.request("GET", f"adAccounts/{linkedin_id(account_id)}")[0]
        if str(data.get("id")) != account_id:
            raise WriteBlocked(
                "LinkedIn account identity could not be verified."
            )
        return data

    def campaigns(self, account_id, page_token=None, count=100):
        return self.request(
            "GET",
            f"adAccounts/{linkedin_id(account_id)}/adCampaigns",
            params=self.page_params(
                page_token,
                count,
                "(status:(values:List(ACTIVE,PAUSED,DRAFT,ARCHIVED,COMPLETED,CANCELED,PENDING_DELETION,REMOVED)))",
            ),
        )[0]

    def groups(self, account_id, page_token=None, count=100):
        return self.request(
            "GET",
            f"adAccounts/{linkedin_id(account_id)}/adCampaignGroups",
            params=self.page_params(
                page_token,
                count,
                "(status:(values:List(ACTIVE,PAUSED,DRAFT,ARCHIVED)))",
            ),
        )[0]

    def group(self, account_id, group_id):
        data = self.request(
            "GET",
            f"adAccounts/{linkedin_id(account_id)}/adCampaignGroups/{linkedin_id(group_id)}",
        )[0]
        if (
            data.get("account") != f"urn:li:sponsoredAccount:{account_id}"
            or str(data.get("id")) != group_id
        ):
            raise WriteBlocked(
                "Campaign group does not belong to the selected account."
            )
        return data

    def campaign(self, account_id, campaign_id):
        data = self.request(
            "GET",
            f"adAccounts/{linkedin_id(account_id)}/adCampaigns/{linkedin_id(campaign_id)}",
        )[0]
        if (
            data.get("account") != f"urn:li:sponsoredAccount:{account_id}"
            or str(data.get("id")) != campaign_id
        ):
            raise WriteBlocked(
                "Campaign does not belong to the selected LinkedIn account."
            )
        return data

    def report(self, account_id, start_date, end_date):
        start, end = date.fromisoformat(start_date), date.fromisoformat(
            end_date
        )
        if end < start or (end - start).days > 366:
            raise WriteBlocked(
                "Select an ordered date range of at most 367 days."
            )

        def rest_date(value):
            return f"(year:{value.year},month:{value.month},day:{value.day})"

        data = self.request(
            "GET",
            "adAnalytics",
            params={
                "q": "analytics",
                "pivot": "CAMPAIGN",
                "timeGranularity": "DAILY",
                "accounts": f"List(urn:li:sponsoredAccount:{linkedin_id(account_id)})",
                "dateRange": f"(start:{rest_date(start)},end:{rest_date(end)})",
                "fields": "dateRange,pivotValues,impressions,clicks,costInLocalCurrency,externalWebsiteConversions",
            },
        )[0]
        return {
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
            "time_granularity": "DAILY",
            "data": data,
            "note": "LinkedIn attribution applies. Empty results can mean no activity or insufficient access; do not add these conversions to other platforms.",
        }

    def patch_campaign(self, account_id, campaign_id, values):
        return self.request(
            "POST",
            f"adAccounts/{linkedin_id(account_id)}/adCampaigns/{linkedin_id(campaign_id)}",
            body={"patch": {"$set": values}},
            partial=True,
        )

    def create_campaign(self, account_id, payload):
        return self.request(
            "POST",
            f"adAccounts/{linkedin_id(account_id)}/adCampaigns",
            body=payload,
        )
