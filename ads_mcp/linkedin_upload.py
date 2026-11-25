"""One-use, owner-authorized browser upload directly to the selected LinkedIn Page."""

import html
import time

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import HTMLResponse, PlainTextResponse

from ads_mcp.linkedin_auth import connection_store
from ads_mcp.linkedin_client import LinkedInAPI
from ads_mcp.linkedin_creatives import (
    MAX_BYTES,
    campaign_org,
    image_status,
    upload,
    validate_image,
)
from ads_mcp.linkedin_workflow import account_policy, approval_policy
from ads_mcp.write_approval import HEADERS, browser_identity
from ads_mcp.write_policy import WriteBlocked, digest


def upload_target(api, account_id, campaign_id):
    policy = account_policy(account_id)
    account = api.account(account_id)
    if (
        account.get("currency") != policy["currency"]
        or account.get("status") != "ACTIVE"
    ):
        raise WriteBlocked(
            "Kontots status eller valuta stämmer inte med skrivinställningarna."
        )
    return campaign_org(api.campaign(account_id, campaign_id))


def send_image(ticket, data):
    validate_image(data)
    store = connection_store()
    target = store.consume(ticket, "image-upload")
    grant = store.grant(target["owner"])
    if grant["connection_id"] != target["connection"]:
        raise WriteBlocked(
            "Anslutningen har ändrats. Begär en ny uppladdningslänk."
        )
    api = LinkedInAPI(grant["access_token"])
    account = target["account"]
    if (
        upload_target(api, account, target["campaign"])
        != target["organization"]
    ):
        raise WriteBlocked("Kampanjens sida har ändrats. Begär en ny länk.")
    urn = upload(api, account, target["organization"], data)
    status = "Väntar på status"
    for attempt in range(3):
        try:
            status = image_status(api, urn, target["organization"])
        except WriteBlocked:
            break
        if status != "PROCESSING" and status != "WAITING_UPLOAD":
            break
        if attempt < 2:
            time.sleep(1)
    return f"Bild-ID: {urn}\nStatus: {status}\nKopiera bild-ID:t till chatten för att förhandsgranska annonsen."


async def upload_page(request):
    policy = approval_policy()
    try:
        policy.require_writes()
    except WriteBlocked:
        return PlainTextResponse(
            "LinkedIn-uppladdning är avstängd.", 503, headers=HEADERS
        )
    owner, login = await browser_identity(request)
    if login is not None:
        return login
    try:
        ticket = request.path_params["ticket"]
        target = await run_in_threadpool(
            lambda: connection_store().peek(ticket, "image-upload")
        )
        if target.get("owner") != owner:
            raise WriteBlocked("Uppladdningen tillhör en annan användare.")
        csrf = digest(
            {
                "secret": policy.approval_secret,
                "purpose": "upload",
                "ticket": ticket,
            }
        )
        if request.method == "GET":
            return HTMLResponse(
                '<!doctype html><html lang="sv"><meta charset="utf-8"><title>Ladda upp LinkedIn-bild</title>'
                "<h1>Ladda upp en bild till LinkedIn</h1>"
                f'<p>Konto {html.escape(target["account"])} · Kampanj {html.escape(target["campaign"])} · Sida {html.escape(target["organization"])}</p>'
                '<form method="post" enctype="multipart/form-data">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<label>JPG eller PNG, högst 5 MiB och 16 megapixel <input type="file" name="image" accept="image/jpeg,image/png" required></label>'
                "<button>Ladda upp till LinkedIn</button></form></html>",
                headers=HEADERS,
            )
        if request.headers.get("origin") != policy.base_url:
            raise WriteBlocked("Felaktigt ursprung.")

        async def bounded():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES + 8192:
                    raise MultiPartException("File too large")
                yield chunk

        form = await MultiPartParser(
            request.headers,
            bounded(),
            max_files=1,
            max_fields=1,
            max_part_size=2048,
        ).parse()
        try:
            file = form.get("image")
            if form.get("csrf") != csrf or not isinstance(file, UploadFile):
                raise WriteBlocked("Ogiltigt uppladdningsformulär.")
            data = await file.read(MAX_BYTES + 1)
        finally:
            await form.close()
        message = await run_in_threadpool(send_image, ticket, data)
        return PlainTextResponse(message, headers=HEADERS)
    except WriteBlocked as exc:
        return PlainTextResponse(str(exc), 400, headers=HEADERS)
    except MultiPartException:
        return PlainTextResponse(
            "Ogiltig fil eller för stor uppladdning.", 400, headers=HEADERS
        )
    except Exception:
        return PlainTextResponse(
            "Uppladdningen kunde inte bekräftas. Kontrollera mediebiblioteket i Campaign Manager.",
            502,
            headers=HEADERS,
        )


def register_upload_route(mcp):
    mcp.custom_route(
        "/linkedin/images/upload/{ticket}", methods=["GET", "POST"]
    )(upload_page)
