"""Owner-only browser review for LinkedIn. Approval itself performs no API write."""

import html
import json
import re
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, PlainTextResponse

from ads_mcp.linkedin_workflow import LinkedInPlanStore, approval_policy
from ads_mcp.write_approval import HEADERS, authorized, csrf_token
from ads_mcp.write_policy import WriteBlocked, digest
from ads_mcp.write_store import check_plan


async def linkedin_approval_page(request):
    try:
        policy = approval_policy()
        policy.require_writes()
    except Exception:
        return PlainTextResponse(
            "LinkedIn-godkännanden är avstängda.", 503, headers=HEADERS
        )
    if not authorized(request, policy.approval_secret):
        return PlainTextResponse(
            "Logga in som owner med ditt godkännandelösenord.",
            401,
            headers={
                **HEADERS,
                "WWW-Authenticate": 'Basic realm="Google Ads approvals", charset="UTF-8"',
            },
        )
    try:
        plan_id = request.path_params["plan_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise WriteBlocked("Invalid plan.")
        store = LinkedInPlanStore(policy)
        plan = await run_in_threadpool(store.get, plan_id)
        check_plan(plan)
        if plan["status"] != "pending_approval":
            raise WriteBlocked("Already handled.")
        expected = digest(
            {
                key: plan[key]
                for key in ("customer_id", "change", "snapshot", "review")
            }
        )
        if expected != plan["review_digest"]:
            raise WriteBlocked("Changed plan.")
        csrf = csrf_token(plan, policy.approval_secret)
        if request.method == "POST":
            if request.headers.get("origin") != policy.base_url:
                raise WriteBlocked("Invalid origin.")
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
            if fields.get("csrf") != [csrf] or fields.get("confirm") != [
                "GODKÄNN"
            ]:
                raise WriteBlocked("Invalid proof.")
            if plan["change"]["action"] == "create_campaign" and fields.get(
                "non_political"
            ) != ["yes"]:
                raise WriteBlocked(
                    "Advertiser political intent confirmation is required."
                )
            await run_in_threadpool(
                store.transition, plan_id, "pending_approval", "approved"
            )
            return HTMLResponse(
                '<!doctype html><html lang="sv"><meta charset="utf-8"><h1>LinkedIn-förslaget är godkänt</h1><p>Ingen annons har ändrats. Gå tillbaka till chatten och be om genomförande av exakt detta förslag.</p></html>',
                headers=HEADERS,
            )

        def block(value):
            return html.escape(json.dumps(value, ensure_ascii=False, indent=2))

        account = html.escape(str(plan["customer_id"]))
        action = {
            "pause": "Pausa kampanj",
            "set_budget": "Ändra dagsbudget",
            "create_campaign": "Skapa pausad kampanj utan annonser",
        }[plan["change"]["action"]]
        political_confirmation = (
            "<p>LinkedIns verktyg får inte användas för att diskriminera utifrån personliga egenskaper som kön, ålder, ras eller etnicitet.</p>"
            '<label><input type="checkbox" name="non_political" value="yes" required>'
            "Jag bekräftar att detta inte är politisk reklam och att inga annonser i kampanjen är politiska enligt reglerna i de länder som målgruppen omfattar, inklusive EU.</label><br>"
            if plan["change"]["action"] == "create_campaign"
            else ""
        )
        return HTMLResponse(
            '<!doctype html><html lang="sv"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            "<title>Granska LinkedIn-ändring</title><style>body{font:17px system-ui;max-width:900px;margin:2rem auto;padding:1rem}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f4f8;padding:1rem}button,input{font:inherit;padding:.7rem}</style>"
            f"<h1>{action}</h1><p>LinkedIn-konto {account}</p>"
            "<p>Budgetändringar kan påverka pågående annonsering. Nya kampanjer skapas pausade. "
            "Förhandsgranskningen har kontrollerats lokalt, men LinkedIn har inte validerat någon skrivning ännu. "
            "Kontrollera konto, valuta, budget, bud, datum och inriktning.</p>"
            f'<h2>Före</h2><pre>{block(plan["snapshot"])}</pre>'
            f'<h2>Efter</h2><pre>{block(plan["review"]["proposed"])}</pre>'
            "<p>Skapandet omfattar kampanjinställningarna. Annonsmaterial och aktivering ingår inte. "
            "Vid avbrott kontrolleras utfallet före nya försök. Återställning av en budgetändring kräver ett nytt godkännande. "
            "En skapad kampanj lämnas pausad om du avstår från fortsättning.</p>"
            f'<form method="post"><input type="hidden" name="csrf" value="{csrf}">'
            f"{political_confirmation}"
            '<label>Skriv GODKÄNN <input name="confirm" required autocomplete="off" pattern="GODKÄNN"></label>'
            "<button>Godkänn förslaget</button></form></html>",
            headers=HEADERS,
        )
    except Exception:
        return PlainTextResponse(
            "Förslaget kan inte godkännas. Det saknas, har ändrats, behandlats eller gått ut. Ingen ändring har skickats till LinkedIn.",
            409,
            headers=HEADERS,
        )


def register_linkedin_approval_routes(mcp):
    mcp.custom_route("/linkedin/approvals/{plan_id}", methods=["GET", "POST"])(
        linkedin_approval_page
    )
