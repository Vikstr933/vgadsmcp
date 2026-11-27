"""Synthetic LinkedIn tests. No credentials, customer exports or live writes."""

import copy
import json
import re
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ads_mcp.linkedin_auth import (
    COOKIE,
    LinkedInStore,
    callback_page,
    connect_page,
)
from ads_mcp.linkedin_approval import linkedin_approval_page
from ads_mcp.linkedin_client import LinkedInAPI, LinkedInRejected, linkedin_id
from ads_mcp.linkedin_workflow import (
    CampaignDraft,
    LinkedInChange,
    LinkedInWorkflow,
)
from ads_mcp.write_approval import csrf_token
from ads_mcp.write_policy import WriteBlocked
from tests.write_workflow_test import MemoryStore, policy

ACCOUNT = "111222333"
CAMPAIGN = "444555666"
POLICIES = {
    "LINKEDIN_MCP_WRITE_ACCOUNTS": json.dumps(
        {ACCOUNT: {"currency": "SEK", "max_daily_budget": "1000.00"}}
    )
}


class Store(MemoryStore):
    def record_progress(self, plan_id, owner, receipt):
        self.plans[plan_id]["progress"] = copy.deepcopy(receipt)


class API:
    def __init__(self):
        self.current = {
            "id": int(CAMPAIGN),
            "account": f"urn:li:sponsoredAccount:{ACCOUNT}",
            "name": "Synthetic",
            "status": "ACTIVE",
            "dailyBudget": {"amount": "100.00", "currencyCode": "SEK"},
        }
        self.calls = []
        self.lost_response = False
        self.created = None

    def account(self, account_id):
        return {"id": int(account_id), "currency": "SEK", "status": "ACTIVE"}

    def campaign(self, *args):
        return copy.deepcopy(self.created or self.current)

    def group(self, account_id, group_id):
        return {
            "id": int(group_id),
            "account": f"urn:li:sponsoredAccount:{account_id}",
            "status": "ACTIVE",
        }

    def patch_campaign(self, account_id, campaign_id, values):
        self.calls.append(copy.deepcopy(values))
        self.current.update(values)
        if self.lost_response:
            raise TimeoutError("Synthetic lost response")
        return {}, None

    def create_campaign(self, account_id, values):
        self.calls.append(copy.deepcopy(values))
        self.created = {**copy.deepcopy(values), "id": int(CAMPAIGN)}
        if self.lost_response:
            raise TimeoutError("Synthetic lost create response")
        return {}, CAMPAIGN

    def campaigns(self, *args, **kwargs):
        return {"elements": [self.created] if self.created else []}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", POLICIES)
        env.start()
        self.addCleanup(env.stop)
        self.api, self.store = API(), Store()
        self.workflow = LinkedInWorkflow(
            self.api, "synthetic-connection", store=self.store, policy=policy()
        )
        self.change = LinkedInChange(
            action="pause", account_id=ACCOUNT, campaign_id=CAMPAIGN
        )

    def approved(self, change=None):
        result = self.workflow.preview(change or self.change, "owner-a")
        self.store.transition(result["plan_id"], "pending_approval", "approved")
        return result["plan_id"]

    def campaign_change(self):
        return LinkedInChange(
            action="create_campaign",
            account_id=ACCOUNT,
            campaign=CampaignDraft(
                name="Synthetic draft",
                campaign_group_id="123",
                organization_id="456",
                objective="WEBSITE_VISIT",
                locale_language="sv",
                locale_country="SE",
                non_political_confirmed=True,
                daily_budget="250.00",
                bid_amount="5.00",
                cost_type="CPC",
                start_ms=int((time.time() + 3600) * 1000),
                end_ms=int((time.time() + 86400) * 1000),
                targeting_criteria={
                    "include": {
                        "and": [
                            {
                                "or": {
                                    "urn:li:adTargetingFacet:locations": [
                                        "urn:li:geo:105117694"
                                    ]
                                }
                            }
                        ]
                    }
                },
            ),
        )

    def test_preview_sends_zero_writes(self):
        self.assertEqual(
            self.workflow.preview(self.change, "owner-a")["status"],
            "pending_approval",
        )
        self.assertEqual(self.api.calls, [])

    def test_dry_run_saves_no_plan_and_cannot_execute(self):
        self.workflow.policy = replace(policy(), mode="dry_run")
        result = self.workflow.preview(self.change, "owner-a")
        self.assertEqual(result["status"], "local_preview_only")
        self.assertEqual(self.store.plans, {})
        with self.assertRaises(WriteBlocked):
            self.workflow.execute("0" * 32, "owner-a")

    def test_no_implicit_google_account_policy(self):
        with patch.dict("os.environ", {"LINKEDIN_MCP_WRITE_ACCOUNTS": "{}"}):
            with self.assertRaises(WriteBlocked):
                self.workflow.preview(self.change, "owner-a")

    def test_budget_ceiling_and_currency(self):
        with self.assertRaises(WriteBlocked):
            self.workflow.preview(
                LinkedInChange(
                    action="set_budget",
                    account_id=ACCOUNT,
                    campaign_id=CAMPAIGN,
                    daily_budget="1000.01",
                ),
                "owner-a",
            )
        with patch.dict(
            "os.environ",
            {
                "LINKEDIN_MCP_WRITE_ACCOUNTS": json.dumps(
                    {ACCOUNT: {"currency": "EUR", "max_daily_budget": "1000"}}
                )
            },
        ):
            with self.assertRaises(WriteBlocked):
                self.workflow.preview(self.change, "owner-a")

    def test_owner_approval_and_connection_binding(self):
        plan_id = self.workflow.preview(self.change, "owner-a")["plan_id"]
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.store.transition(plan_id, "pending_approval", "approved")
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-b")
        self.workflow.connection_id = "different-connection"
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.api.calls, [])

    def test_verified_execution_does_not_replay(self):
        plan_id = self.approved()
        self.assertEqual(
            self.workflow.execute(plan_id, "owner-a")["status"], "verified"
        )
        self.assertFalse(self.workflow.execute(plan_id, "owner-a")["replayed"])
        self.assertEqual(len(self.api.calls), 1)
        self.assertIsNone(self.store.lock)

    def test_drift_and_tampering_block_before_mutation(self):
        plan_id = self.approved()
        self.api.current["name"] = "Changed elsewhere"
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.store.plans[plan_id]["review"]["proposed"]["status"] = "ACTIVE"
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.api.calls, [])

    def test_lost_write_response_reconciles_even_after_expiry(self):
        plan_id = self.approved()
        self.api.lost_response = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.store.plans[plan_id]["status"], "unknown")
        self.store.plans[plan_id]["expires_at"] = 1
        # Restore a consistent integrity digest after simulating time rather than changing a signed review.
        result = self.workflow.reconcile(plan_id, "owner-a")
        self.assertEqual(result["writes_sent"], 0)
        self.assertEqual(len(self.api.calls), 1)
        self.assertIsNone(self.store.lock)

    def test_create_is_paused_and_lost_response_can_be_found_by_marker(self):
        plan_id = self.approved(self.campaign_change())
        self.api.lost_response = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.assertEqual(self.api.created["status"], "PAUSED")
        self.assertEqual(self.api.created["format"], "STANDARD_UPDATE")
        self.assertEqual(self.api.created["objectiveType"], "WEBSITE_VISIT")
        self.assertEqual(self.api.created["politicalIntent"], "NOT_POLITICAL")
        self.assertIn("viks-" + plan_id[:12], self.api.created["name"])
        self.assertEqual(
            self.workflow.reconcile(plan_id, "owner-a")["status"], "verified"
        )
        self.assertEqual(len(self.api.calls), 1)

    def test_unverified_state_keeps_lock(self):
        plan_id = self.approved()
        self.api.lost_response = True
        with self.assertRaises(WriteBlocked):
            self.workflow.execute(plan_id, "owner-a")
        self.api.current["status"] = "ACTIVE"
        with self.assertRaises(WriteBlocked):
            self.workflow.reconcile(plan_id, "owner-a")
        self.assertEqual(self.store.lock, plan_id)

    def test_activation_and_raw_payload_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            LinkedInChange(
                action="activate", account_id=ACCOUNT, campaign_id=CAMPAIGN
            )
        with self.assertRaises(ValidationError):
            LinkedInChange(
                action="pause",
                account_id=ACCOUNT,
                campaign_id=CAMPAIGN,
                payload={"status": "ACTIVE"},
            )

    def test_explicit_rejection_releases_lock_without_retry(self):
        plan_id = self.approved()
        with patch.object(
            self.api, "patch_campaign", side_effect=LinkedInRejected("Rejected")
        ) as mutate:
            with self.assertRaises(WriteBlocked):
                self.workflow.execute(plan_id, "owner-a")
        mutate.assert_called_once()
        self.assertEqual(self.store.plans[plan_id]["status"], "rejected")
        self.assertIsNone(self.store.lock)

    def test_crashed_worker_can_be_reconciled_after_grace_period(self):
        plan_id = self.approved()
        self.store.transition(plan_id, "approved", "executing", owner="owner-a")
        self.store.plans[plan_id]["updated_at"] = time.time() - 901
        self.api.current["status"] = "PAUSED"
        self.assertEqual(
            self.workflow.reconcile(plan_id, "owner-a")["status"], "verified"
        )
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(self.store.lock)

    def test_browser_approval_requires_owner_csrf_origin_and_creation_confirmation(
        self,
    ):
        plan_id = self.workflow.preview(self.campaign_change(), "owner-a")[
            "plan_id"
        ]
        client = TestClient(
            Starlette(
                routes=[
                    Route(
                        "/linkedin/approvals/{plan_id}",
                        linkedin_approval_page,
                        methods=["GET", "POST"],
                    )
                ]
            ),
            base_url=self.workflow.policy.base_url,
        )
        path = "/linkedin/approvals/" + plan_id
        headers = {"Cookie": "test-google=owner-a"}
        with (
            patch("ads_mcp.membership.active_owner", return_value=True),
            patch(
                "ads_mcp.connections.session",
                side_effect=lambda r: (
                    {"owner": r.cookies["test-google"]}
                    if r.cookies.get("test-google")
                    else None
                ),
            ),
            patch(
                "ads_mcp.linkedin_approval.approval_policy",
                return_value=self.workflow.policy,
            ),
            patch(
                "ads_mcp.linkedin_approval.LinkedInPlanStore",
                return_value=self.store,
            ),
        ):
            self.assertEqual(client.get(path).status_code, 401)
            wrong = {
                "Cookie": "test-google=owner-b",
                "Origin": self.workflow.policy.base_url,
            }
            self.assertEqual(client.get(path, headers=wrong).status_code, 409)
            self.assertEqual(client.post(path, headers=wrong).status_code, 409)
            self.assertEqual(client.get(path, headers=headers).status_code, 200)
            self.assertEqual(
                self.store.plans[plan_id]["status"], "pending_approval"
            )
            form = {
                "csrf": csrf_token(
                    self.store.get(plan_id),
                    self.workflow.policy.approval_secret,
                ),
                "confirm": "GODKÄNN",
            }
            self.assertEqual(
                client.post(path, data=form, headers=headers).status_code, 409
            )
            same_origin = {**headers, "Origin": self.workflow.policy.base_url}
            self.assertEqual(
                client.post(path, data=form, headers=same_origin).status_code,
                409,
            )
            form["non_political"] = "yes"
            self.assertEqual(
                client.post(path, data=form, headers=same_origin).status_code,
                200,
            )
        self.assertEqual(self.store.plans[plan_id]["status"], "approved")
        self.assertEqual(self.api.calls, [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "base_url": "https://ads.example.com",
            "callback": "https://ads.example.com/linkedin/callback",
            "client_id": "synthetic-client",
            "client_secret": "synthetic-client-secret",
        }
        self.store = MagicMock()
        self.client = TestClient(
            Starlette(
                routes=[
                    Route(
                        "/linkedin/connect/{ticket}",
                        connect_page,
                        methods=["GET", "POST"],
                    ),
                    Route("/linkedin/callback", callback_page),
                ]
            ),
            base_url=self.settings["base_url"],
        )
        self.secret = "synthetic-owner-password-1234567890123456"
        self.headers = {"Cookie": "test-google=synthetic-owner"}
        self.store.peek.return_value = {"owner": "synthetic-owner"}
        for p in (
            patch("ads_mcp.membership.active_owner", return_value=True),
            patch(
                "ads_mcp.connections.session",
                side_effect=lambda r: (
                    {"owner": r.cookies["test-google"]}
                    if r.cookies.get("test-google")
                    else None
                ),
            ),
            patch("ads_mcp.linkedin_auth.config", return_value=self.settings),
            patch(
                "ads_mcp.linkedin_auth.connection_store",
                return_value=self.store,
            ),
            patch.dict(
                "os.environ", {"GOOGLE_ADS_MCP_APPROVAL_SECRET": self.secret}
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_connect_requires_owner_and_get_never_redirects_or_consumes(self):
        path = "/linkedin/connect/" + "A" * 43
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(
            self.client.get(
                path, headers={"Cookie": "test-google=other"}
            ).status_code,
            409,
        )
        result = self.client.get(path, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.store.consume.assert_not_called()
        self.assertNotIn(self.settings["client_secret"], result.text)

    def test_valid_connect_sets_browser_cookie_and_uses_linkedin_callback(self):
        path = "/linkedin/connect/" + "A" * 43
        self.store.consume.return_value = {"owner": "synthetic-owner"}
        self.store.issue.return_value = "B" * 43
        page = self.client.get(path, headers=self.headers)
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
        response = self.client.post(
            path,
            data={"csrf": csrf},
            headers={**self.headers, "Origin": self.settings["base_url"]},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        target = urlsplit(response.headers["location"])
        self.assertEqual(target.netloc, "www.linkedin.com")
        values = parse_qs(target.query)
        self.assertEqual(values["redirect_uri"], [self.settings["callback"]])
        self.assertEqual(values["state"], ["B" * 43])
        cookie = response.headers["set-cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.store.save_grant.assert_not_called()

    def test_valid_callback_saves_grant_without_showing_token(self):
        self.client.cookies.set(COOKIE, "synthetic-browser")
        self.store.consume.return_value = {"owner": "synthetic-owner"}
        token_response = MagicMock(status_code=200)
        token_response.json.return_value = {
            "access_token": "synthetic-private-token",
            "expires_in": 3600,
        }
        with patch("ads_mcp.linkedin_auth.httpx.AsyncClient") as factory:
            client = AsyncMock()
            client.post.return_value = token_response
            factory.return_value.__aenter__.return_value = client
            result = self.client.get(
                "/linkedin/callback?state=synthetic&code=synthetic"
            )
        self.assertEqual(result.status_code, 200)
        self.store.save_grant.assert_called_once_with(
            "synthetic-owner", token_response.json.return_value
        )
        self.assertNotIn("synthetic-private-token", result.text)

    def test_connect_post_requires_origin_and_csrf(self):
        path = "/linkedin/connect/" + "A" * 43
        for origin in (
            None,
            "https://other.example",
            self.settings["base_url"],
        ):
            headers = {**self.headers, **({"Origin": origin} if origin else {})}
            self.assertEqual(
                self.client.post(
                    path, data={"csrf": "wrong"}, headers=headers
                ).status_code,
                409,
            )
        self.store.consume.assert_not_called()

    def test_callback_without_browser_cookie_does_not_exchange_code(self):
        with patch("ads_mcp.linkedin_auth.httpx.AsyncClient") as client:
            self.assertEqual(
                self.client.get(
                    "/linkedin/callback?state=synthetic&code=synthetic"
                ).status_code,
                400,
            )
            client.assert_not_called()
        self.store.save_grant.assert_not_called()

    def test_bad_state_does_not_exchange_code(self):
        self.client.cookies.set(COOKIE, "synthetic-browser")
        self.store.consume.side_effect = WriteBlocked("Mismatch")
        with patch("ads_mcp.linkedin_auth.httpx.AsyncClient") as client:
            self.assertEqual(
                self.client.get(
                    "/linkedin/callback?state=bad&code=synthetic"
                ).status_code,
                400,
            )
            client.assert_not_called()


class ClientTests(unittest.TestCase):
    def test_linkedin_read_requests_use_restli_query_syntax(self):
        requests = []

        def capture(request):
            requests.append(request)
            return httpx.Response(200, json={"elements": []})

        api = LinkedInAPI("synthetic")
        clients = [
            httpx.Client(transport=httpx.MockTransport(capture))
            for _ in range(4)
        ]
        with patch("ads_mcp.linkedin_client.httpx.Client", side_effect=clients):
            api.accounts(count=25)
            api.campaigns(ACCOUNT, page_token="next/+&=", count=25)
            api.groups(ACCOUNT)
            api.report(ACCOUNT, "2026-11-20", "2026-11-27")

        self.assertEqual(requests[0].url.query, b"q=search&pageSize=25")
        self.assertEqual(
            requests[1].url.query,
            b"q=search&pageSize=25&pageToken=next%2F%2B%26%3D"
            b"&search=(status:(values:List(ACTIVE,PAUSED,DRAFT,ARCHIVED,"
            b"COMPLETED,CANCELED,PENDING_DELETION,REMOVED)))",
        )
        self.assertEqual(
            requests[2].url.query,
            b"q=search&pageSize=100&search=(status:(values:List(ACTIVE,PAUSED,DRAFT,ARCHIVED)))",
        )
        self.assertEqual(
            requests[3].url.query,
            b"q=analytics&pivot=CAMPAIGN&timeGranularity=DAILY"
            b"&accounts=List(urn%3Ali%3AsponsoredAccount%3A111222333)"
            b"&dateRange=(start:(year:2026,month:11,day:20),end:(year:2026,month:11,day:27))"
            b"&fields=dateRange,pivotValues,impressions,clicks,costInLocalCurrency,externalWebsiteConversions",
        )
        self.assertTrue(all(request.method == "GET" for request in requests))

    def test_invalid_ids_and_date_windows(self):
        for value in (
            "1/../../secrets",
            "urn:li:sponsoredAccount:123",
            "",
            "0",
        ):
            with self.assertRaises(WriteBlocked):
                linkedin_id(value)
        with self.assertRaises(WriteBlocked):
            LinkedInAPI("synthetic").report(ACCOUNT, "2026-11-24", "2026-11-23")

    def test_no_error_body_leak_or_mutation_retry(self):
        client = MagicMock()
        client.request.return_value = httpx.Response(
            403, json={"message": "synthetic-sensitive-payload"}
        )
        with patch("ads_mcp.linkedin_client.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            with self.assertRaises(WriteBlocked) as error:
                LinkedInAPI("synthetic-token").patch_campaign(
                    ACCOUNT, CAMPAIGN, {"status": "PAUSED"}
                )
        self.assertEqual(client.request.call_count, 1)
        self.assertNotIn("synthetic-sensitive", str(error.exception))
        self.assertNotIn("synthetic-token", str(error.exception))

    def test_campaign_account_is_checked(self):
        api = LinkedInAPI("synthetic")
        with patch.object(
            api,
            "request",
            return_value=(
                {"id": int(CAMPAIGN), "account": "urn:li:sponsoredAccount:999"},
                None,
            ),
        ):
            with self.assertRaises(WriteBlocked):
                api.campaign(ACCOUNT, CAMPAIGN)


class EncryptionTests(unittest.TestCase):
    def test_stored_grants_are_encrypted_and_disconnect_is_scoped(self):
        store = object.__new__(LinkedInStore)
        store.fernet = Fernet(Fernet.generate_key())
        store.grants = MagicMock()
        store.save_grant(
            "synthetic-owner",
            {"access_token": "synthetic-private-token", "expires_in": 3600},
        )
        record = store.grants.document.return_value.set.call_args.args[0]
        self.assertNotIn(
            "synthetic-private-token", json.dumps(record, default=str)
        )
        store.grants.document.return_value.get.return_value.to_dict.return_value = (
            record
        )
        self.assertEqual(
            store.grant("synthetic-owner")["access_token"],
            "synthetic-private-token",
        )
        store.disconnect("synthetic-owner")
        store.grants.document.assert_called_with("synthetic-owner")
        store.grants.document.return_value.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
