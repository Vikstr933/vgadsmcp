"""Exercise the browser upload and paused creative paths without an ad account."""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ads_mcp.linkedin_creatives import ImageAd, validate_image, upload
from ads_mcp.linkedin_upload import upload_page
from ads_mcp.linkedin_workflow import LinkedInChange, LinkedInWorkflow
from ads_mcp.write_policy import WriteBlocked, digest
from tests.linkedin_test import ACCOUNT, API, CAMPAIGN, POLICIES, Store, policy

IMAGE = "urn:li:image:abc"
ORG = "urn:li:organization:456"
AD = ImageAd(
    image=IMAGE,
    name="Example",
    commentary="Intro",
    headline="Visit",
    landing_page="https://example.com/",
    call_to_action="LEARN_MORE",
)


def png():
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="PNG")
    return data.getvalue()


def test_image_validation_and_upload_destination():
    validate_image(png())
    for data in (b"", b"invalid", png()[:20]):
        with pytest.raises(WriteBlocked):
            validate_image(data)
    api = MagicMock()
    api.request.return_value = (
        {
            "value": {
                "image": IMAGE,
                "uploadUrl": "https://example.com/dms-uploads/file",
            }
        },
        None,
    )
    with patch("ads_mcp.linkedin_creatives.httpx.put") as put:
        with pytest.raises(WriteBlocked):
            upload(api, ACCOUNT, ORG, png())
        put.assert_not_called()
        api.request.return_value[0]["value"][
            "uploadUrl"
        ] = "https://www.linkedin.com/dms-uploads/file"
        put.return_value.status_code = 201
        assert upload(api, ACCOUNT, ORG, png()) == IMAGE
        assert put.call_args.kwargs["follow_redirects"] is False


def test_upload_form_requires_owner_origin_and_csrf():
    client = TestClient(
        Starlette(
            routes=[
                Route("/images/{ticket}", upload_page, methods=["GET", "POST"])
            ]
        )
    )
    secret = policy().approval_secret
    with (
        patch("ads_mcp.membership.active_owner",return_value=True),
        patch(
            "ads_mcp.connections.session",
            side_effect=lambda r: (
                {"owner": r.cookies["test-google"]}
                if r.cookies.get("test-google")
                else None
            ),
        ),
        patch("ads_mcp.linkedin_upload.approval_policy", return_value=policy()),
        patch("ads_mcp.linkedin_upload.connection_store") as db,
        patch("ads_mcp.linkedin_upload.send_image", return_value=IMAGE) as send,
    ):
        db.return_value.peek.return_value = {
            "owner": "owner-a",
            "account": ACCOUNT,
            "campaign": CAMPAIGN,
            "organization": ORG,
        }
        assert client.get("/images/ticket").status_code == 401
        client.cookies.set("test-google", "owner-b")
        assert client.get("/images/ticket").status_code == 400
        client.cookies.set("test-google", "owner-a")
        assert client.get("/images/ticket").status_code == 200
        proof = digest(
            {"secret": secret, "purpose": "upload", "ticket": "ticket"}
        )
        for origin, csrf in (
            ("https://example.org", proof),
            (policy().base_url, "wrong"),
        ):
            assert (
                client.post(
                    "/images/ticket",
                    headers={"origin": origin},
                    files={"image": ("a.png", png())},
                    data={"csrf": csrf},
                ).status_code
                == 400
            )
        send.assert_not_called()
        result = client.post(
            "/images/ticket",
            headers={"origin": policy().base_url},
            files={"image": ("a.png", png())},
            data={"csrf": proof},
        )
        assert result.status_code == 200
        send.assert_called_once_with("ticket", png())


@pytest.mark.parametrize("lost_receipt", [True, False])
def test_create_once_and_verify(lost_receipt):
    api = API()
    api.current.update(
        status="PAUSED",
        format="STANDARD_UPDATE",
        associatedEntity=ORG,
        objectiveType="WEBSITE_VISIT",
    )
    saved = []
    urn = "urn:li:sponsoredCreative:123"

    def request(method, path, **kw):
        if path.startswith("images/"):
            return {"owner": ORG, "status": "AVAILABLE", "id": IMAGE}, None
        if method == "POST":
            creative = kw["body"]["creative"]
            assert (
                creative["inlineContent"]["post"]["content"]["media"]["id"]
                == IMAGE
            )
            saved.append(dict(creative, id=urn, account=api.current["account"]))
            if lost_receipt:
                raise TimeoutError()
            return {}, urn
        if kw.get("finder"):
            return {"elements": saved}, None
        return saved[0], None

    api.request = request
    store = Store()
    flow = LinkedInWorkflow(api, "connection", store=store, policy=policy())
    change = LinkedInChange(
        account_id=ACCOUNT,
        campaign_id=CAMPAIGN,
        creative=AD,
        action="create_image_ad",
    )
    with patch.dict("os.environ", POLICIES):
        plan = flow.preview(change, "owner")["plan_id"]
        with pytest.raises(WriteBlocked):
            flow.execute(plan, "owner")
        store.transition(plan, "pending_approval", "approved")
        if lost_receipt:
            with pytest.raises(WriteBlocked):
                flow.execute(plan, "owner")
            assert flow.reconcile(plan, "owner")["writes_sent"] == 0
        assert flow.execute(plan, "owner")["result"]["status"] == "PAUSED"
        assert len(saved) == 1
        api.current["status"] = "ACTIVE"
        with pytest.raises(WriteBlocked):
            flow.preview(change, "owner")
