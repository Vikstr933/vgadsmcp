"""Single-image Direct Sponsored Content for website campaigns."""

import re
from io import BytesIO
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ads_mcp.write_policy import WriteBlocked

IMAGE_PATTERN = r"^urn:li:image:[A-Za-z0-9_-]{1,200}$"
MAX_BYTES = 5 * 1024 * 1024


def validate_image(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise WriteBlocked("Bilden får vara högst 5 MiB.")
    try:
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
            if (
                image.width * image.height > 16_000_000
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError()
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
        raise WriteBlocked(
            "Välj en giltig stillbild i JPG eller PNG på högst 16 megapixel."
        ) from None


def campaign_org(campaign):
    org = campaign.get("associatedEntity", "")
    if (
        not re.fullmatch(r"urn:li:organization:[1-9][0-9]{0,19}", org)
        or campaign.get("format") != "STANDARD_UPDATE"
        or campaign.get("objectiveType")
        not in ("WEBSITE_VISIT", "WEBSITE_CONVERSION")
        or campaign.get("status") != "PAUSED"
    ):
        raise WriteBlocked(
            "Välj en pausad LinkedIn-kampanj för webbplatsbesök eller webbplatskonverteringar med formatet STANDARD_UPDATE och en kopplad sida."
        )
    return org


class ImageAd(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True
    )
    image: str = Field(pattern=IMAGE_PATTERN)
    name: str = Field(min_length=1, max_length=150)
    commentary: str = Field(min_length=1, max_length=600)
    headline: str = Field(min_length=1, max_length=200)
    landing_page: str = Field(max_length=2000)
    call_to_action: Literal["LEARN_MORE", "REGISTER", "SIGN_UP", "DOWNLOAD"]

    @field_validator("landing_page")
    @classmethod
    def https_destination(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Use a public HTTPS landing page.")
        return value

    def payload(self, campaign, marker):
        post = {
            "author": campaign_org(campaign),
            "adContext": {
                "dscAdAccount": campaign["account"],
                "dscStatus": "ACTIVE",
            },
            "visibility": "PUBLIC",
            "lifecycleState": "PUBLISHED",
            "commentary": self.commentary,
            "contentLandingPage": self.landing_page,
            "contentCallToActionLabel": self.call_to_action,
            "content": {"media": {"title": self.headline, "id": self.image}},
        }
        return {
            "name": f"{self.name} [viks-{marker}]",
            "intendedStatus": "PAUSED",
            "campaign": f"urn:li:sponsoredCampaign:{campaign['id']}",
            "inlineContent": {"post": post},
        }


def upload(api, account, org, data):
    value = api.request(
        "POST",
        "images",
        params={"action": "initializeUpload"},
        body={
            "initializeUploadRequest": {
                "owner": org,
                "mediaLibraryMetadata": {
                    "associatedAccount": f"urn:li:sponsoredAccount:{account}",
                    "assetName": "Viks Annonsverktyg",
                },
            }
        },
    )[0]["value"]
    urn, url = value["image"], value["uploadUrl"]
    parsed = urlsplit(url)
    if (
        not re.fullmatch(IMAGE_PATTERN, urn)
        or parsed.scheme != "https"
        or parsed.netloc != "www.linkedin.com"
        or not parsed.path.startswith("/dms-uploads/")
    ):
        raise WriteBlocked(
            "LinkedIn returnerade en oväntad uppladdningsadress."
        )
    response = httpx.put(
        url,
        content=data,
        headers={
            "Authorization": "Bearer " + api.token,
            "Content-Type": "application/octet-stream",
        },
        timeout=30,
        follow_redirects=False,
    )
    if not 200 <= response.status_code < 300:
        raise WriteBlocked("LinkedIn kunde inte bekräfta bilduppladdningen.")
    return urn


def image_status(api, image, organization):
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise WriteBlocked("Invalid LinkedIn image ID.")
    data = api.request("GET", "images/" + quote(image, safe=""))[0]
    if data.get("id") != image or data.get("owner") != organization:
        raise WriteBlocked("LinkedIn image belongs to a different Page.")
    return data.get("status")


def verify_creative(api, account, proposed, receipt):
    base = f"adAccounts/{account}/creatives"
    urn = (receipt or {}).get("creative_id")
    if not urn:
        params = {
            "q": "criteria",
            "campaigns": f"List({proposed['campaign']})",
            "pageSize": 100,
        }
        matches = []
        for _ in range(50):
            page = api.request("GET", base, params=params, finder=True)[0]
            matches.extend(
                row["id"]
                for row in page.get("elements", [])
                if row.get("name") == proposed["name"]
            )
            if not (token := page.get("metadata", {}).get("nextPageToken")):
                break
            params["pageToken"] = token
        else:
            raise WriteBlocked("Creative search incomplete.")
        if len(matches) != 1:
            raise WriteBlocked(
                "Could not identify one matching creative. No write retried."
            )
        urn = matches[0]
    if not re.fullmatch(r"urn:li:sponsoredCreative:[1-9][0-9]{0,19}", urn):
        raise WriteBlocked("Invalid LinkedIn creative receipt.")
    data = api.request("GET", base + "/" + quote(urn, safe=""))[0]
    if (
        data.get("account") != f"urn:li:sponsoredAccount:{account}"
        or data.get("id") != urn
        or any(
            data.get(key) != proposed[key]
            for key in ("campaign", "intendedStatus", "name")
        )
        or data.get("isServing") is True
    ):
        raise WriteBlocked("Could not verify the paused creative.")
    return {
        "creative_id": urn,
        "status": "PAUSED",
        "campaign": data["campaign"],
    }
