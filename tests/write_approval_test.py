"""Test that only the separate browser approval surface can authorize a plan."""

import base64
import json
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ads_mcp.write_approval import approval_page, csrf_token
from ads_mcp.write_workflow import WriteWorkflow
from ads_mcp.write_policy import Change
from tests.write_workflow_test import CID, FakeGateway, MemoryStore, policy


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.gateway = FakeGateway()
        self.policy = policy()
        workflow = WriteWorkflow(
            self.policy, self.store, lambda _: self.gateway
        )
        self.plan_id = workflow.preview(
            Change(action="pause", customer_id=CID, campaign_id="42"), "owner-a"
        )["plan_id"]
        self.path = "/approvals/" + self.plan_id
        self.client = TestClient(
            Starlette(
                routes=[
                    Route(
                        "/approvals/{plan_id}",
                        approval_page,
                        methods=["GET", "POST"],
                    )
                ]
            ),
            base_url=self.policy.base_url,
        )
        self.patches = [
            patch(
                "ads_mcp.write_approval.WritePolicy.load",
                return_value=self.policy,
            ),
            patch(
                "ads_mcp.write_approval.FirestorePlanStore",
                return_value=self.store,
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.headers = {
            "Authorization": "Basic "
            + base64.b64encode(
                ("owner:" + self.policy.approval_secret).encode()
            ).decode()
        }

    def form(self):
        return {
            "confirm": "GODKÄNN",
            "csrf": csrf_token(
                self.store.get(self.plan_id), self.policy.approval_secret
            ),
        }

    def test_oauth_or_no_password_cannot_view_or_approve(self):
        for headers in (
            {},
            {"Authorization": "Bearer synthetic-oauth-token"},
            {"Authorization": "Basic invalid"},
        ):
            self.assertEqual(
                self.client.get(self.path, headers=headers).status_code, 401
            )
            self.assertEqual(
                self.client.post(
                    self.path, data=self.form(), headers=headers
                ).status_code,
                401,
            )
        self.assertEqual(
            self.store.get(self.plan_id)["status"], "pending_approval"
        )

    def test_get_displays_but_never_approves(self):
        response = self.client.get(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertNotIn(self.policy.approval_secret, response.text)
        self.assertEqual(
            self.store.get(self.plan_id)["status"], "pending_approval"
        )

    def test_correct_post_approves_without_google_write(self):
        response = self.client.post(
            self.path,
            data=self.form(),
            headers={**self.headers, "Origin": self.policy.base_url},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get(self.plan_id)["status"], "approved")
        self.assertEqual(self.gateway.calls, [True])

    def test_cross_origin_missing_origin_csrf_and_confirmation_denied(self):
        attempts = [
            ({}, self.form()),
            ({"Origin": "https://evil.example"}, self.form()),
            (
                {"Origin": self.policy.base_url},
                {"confirm": "GODKÄNN", "csrf": "wrong"},
            ),
            (
                {"Origin": self.policy.base_url},
                {**self.form(), "confirm": "yes"},
            ),
        ]
        for headers, data in attempts:
            response = self.client.post(
                self.path, data=data, headers={**self.headers, **headers}
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.store.get(self.plan_id)["status"], "pending_approval"
        )

    def test_replayed_approval_rejected(self):
        for expected in (200, 409):
            response = self.client.post(
                self.path,
                data=self.form(),
                headers={**self.headers, "Origin": self.policy.base_url},
            )
            self.assertEqual(response.status_code, expected)

    def test_plan_content_is_html_escaped(self):
        from ads_mcp.write_policy import digest

        plan = self.store.plans[self.plan_id]
        plan["snapshot"]["campaign"]["name"] = "<script>alert(1)</script>"
        plan["review_digest"] = digest(
            {
                k: plan[k]
                for k in ("customer_id", "change", "snapshot", "review")
            }
        )
        response = self.client.get(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_dry_run_disables_entire_approval_surface(self):
        with patch(
            "ads_mcp.write_approval.WritePolicy.load",
            return_value=policy("dry_run"),
        ):
            self.assertEqual(
                self.client.get(self.path, headers=self.headers).status_code,
                503,
            )
