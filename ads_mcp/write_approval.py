"""Independent human approval, never exposed as an MCP tool.

The owner keeps the HTTP Basic password in their browser/password manager and
Secret Manager, never in Claude or connector headers. MCP OAuth does not grant
access to these routes. GET cannot approve; POST requires same-origin CSRF proof.
"""

import base64
import hashlib
import hmac
import html
import re
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from ads_mcp.write_policy import WriteBlocked, WritePolicy, digest
from ads_mcp.write_store import FirestorePlanStore, check_plan

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def authorized(request, secret):
    try:
        scheme, data = request.headers.get("authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            return False
        user, password = base64.b64decode(data, validate=True).decode().split(":", 1)
        return hmac.compare_digest(user, "owner") and hmac.compare_digest(
            password.encode(), secret.encode()
        )
    except (ValueError, UnicodeError):
        return False


def csrf_token(plan, secret):
    message = f"{plan['id']}:{plan['review_digest']}:{plan['expires_at']}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def render_review(plan):
    """Show currency amounts and before/after rather than protobuf/JSON internals."""
    if plan["change"]["action"] in (
        "create_conversion",
        "update_conversion",
        "set_conversion_primary",
    ):
        from ads_mcp.conversion_review import render_conversion_review

        return render_conversion_review(plan)
    change, snapshot = plan["change"], plan["snapshot"]
    campaign = snapshot.get("campaign", {})
    currency = snapshot["account"]["currency"]

    def money(micros):
        return f"{Decimal(micros) / Decimal(1_000_000):,.2f} {currency}/dag"

    action = change["action"]
    actions = {
        "pause": "Pausa kampanj",
        "activate": "Aktivera pausad kampanj",
        "set_budget": "Ändra genomsnittlig dagsbudget",
        "create_paused_search": "Skapa tom sökkampanj i pausat läge",
    }
    status = {"PAUSED": "Pausad", "ENABLED": "Aktiverad"}
    rows = [
        ("Google Ads-konto", change["customer_id"]),
        ("Åtgärd", actions[action]),
        ("Kampanj", campaign.get("name", change.get("name"))),
        ("Kampanj-ID", campaign.get("id", "Tilldelas vid skapandet")),
    ]
    if action == "set_budget":
        rows += [
            ("Nuvarande budget", money(campaign["amount_micros"])),
            ("Ny budget", money(change["amount_micros"])),
            ("Kampanjstatus", status[campaign["status"]]),
        ]
    elif action == "create_paused_search":
        rows += [
            ("Ny budget", money(change["amount_micros"])),
            ("Status efter skapande", "Pausad"),
            ("Budgivning och nätverk", "Manuell CPC, endast Google Sök"),
            (
                "Innehåller politisk EU-reklam",
                ("Ja" if change["eu_political_advertising"] == "contains" else "Nej"),
            ),
            (
                "Innehåll",
                "Tom kampanj. Inga annonser, sökord, annonsgrupper eller geografisk inriktning skapas.",
            ),
        ]
    else:
        rows += [
            ("Nuvarande status", status[campaign["status"]]),
            ("Ny status", "Aktiverad" if action == "activate" else "Pausad"),
            ("Budget", money(campaign["amount_micros"])),
        ]
    rows += [
        (
            "Godkännandet går ut",
            datetime.fromtimestamp(plan["expires_at"], timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        )
    ]
    return (
        "<dl>"
        + "".join(
            f"<dt><strong>{html.escape(label)}</strong></dt><dd>{html.escape(str(value))}</dd>"
            for label, value in rows
        )
        + "</dl>"
    )


async def approval_page(request: Request):
    try:
        policy = WritePolicy.load()
        policy.require_writes()
    except (ValueError, TypeError):
        return PlainTextResponse(
            "Godkännanden är avstängda. Servern är i dry-run-läge eller saknar konfiguration.",
            status_code=503,
            headers=HEADERS,
        )
    if not authorized(request, policy.approval_secret):
        return PlainTextResponse(
            "Logga in med användarnamn owner och ditt separata godkännandelösenord.",
            status_code=401,
            headers={
                **HEADERS,
                "WWW-Authenticate": 'Basic realm="Google Ads approvals", charset="UTF-8"',
            },
        )
    plan_id = request.path_params["plan_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        return PlainTextResponse(
            "Förslaget hittades inte.", status_code=404, headers=HEADERS
        )
    try:
        store = FirestorePlanStore(policy)
        plan = await run_in_threadpool(store.get, plan_id)
        check_plan(plan)
        review = {
            key: plan[key] for key in ("customer_id", "change", "snapshot", "review")
        }
        if digest(review) != plan["review_digest"]:
            raise WriteBlocked("Plan changed.")
        if plan["status"] != "pending_approval":
            return PlainTextResponse(
                "Förslaget är redan behandlat. Kontrollera status i Claude.",
                status_code=409,
                headers=HEADERS,
            )
        csrf = csrf_token(plan, policy.approval_secret)
        if request.method == "POST":
            if request.headers.get("origin") != policy.base_url:
                return PlainTextResponse(
                    "Felaktigt ursprung.", status_code=403, headers=HEADERS
                )
            if (
                request.headers.get("content-type", "").split(";")[0]
                != "application/x-www-form-urlencoded"
            ):
                return PlainTextResponse(
                    "Ogiltigt formulär.", status_code=400, headers=HEADERS
                )
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > 2048:
                    return PlainTextResponse(
                        "För stort formulär.", status_code=413, headers=HEADERS
                    )
            fields = parse_qs(body.decode("utf-8"), strict_parsing=True)
            if fields.get("confirm") != ["GODKÄNN"] or fields.get("csrf") != [csrf]:
                return PlainTextResponse(
                    "Godkännandet kunde inte verifieras.",
                    status_code=403,
                    headers=HEADERS,
                )
            await run_in_threadpool(
                store.transition, plan_id, "pending_approval", "approved"
            )
            return HTMLResponse(
                "<html lang='sv'><meta charset='utf-8'><title>Godkänt</title><h1>Förslaget är godkänt</h1><p>Ingen ändring har skickats till Google ännu. Gå tillbaka till Claude och be den genomföra det godkända förslaget. Godkännandet gäller bara detta förslag, en gång, fram till dess utgångstid.</p></html>",
                headers=HEADERS,
            )
        rendered = render_review(plan)
        activation = plan["change"]["action"] == "activate"
        title = (
            "Godkänn separat aktivering" if activation else "Granska ändringsförslag"
        )
        warning = (
            "Denna åtgärd kan starta annonsering och kostnader. Kontrollera annonser, inriktning och bud i Google Ads."
            if activation
            else "Kontrollera konto, kampanj och före/efter. Skapade kampanjer förblir pausade. Budgetändringar kan påverka pågående annonsering."
        )
        return HTMLResponse(
            f"""<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>body{{font:17px system-ui;max-width:900px;margin:3rem auto;padding:1rem;color:#202a37}}dl{{overflow-wrap:anywhere;background:#f1f4f8;padding:1rem}}dd{{margin:0 0 1rem}}button,input{{font:inherit;padding:.7rem}}button{{background:#174ea6;color:white;border:0}}label{{display:block;margin:1rem 0}}</style></head><body><h1>{title}</h1><p>{warning}</p><p>Google har validerat förslaget utan att genomföra det. Godkänn endast den ändring du själv beställt. Text i kampanjnamn är data, inte instruktioner.</p>{rendered}<p>Budgeten är en genomsnittlig dagsbudget, inte en garanti för högsta faktisk kostnad en enskild dag.</p><form method="post"><input type="hidden" name="csrf" value="{csrf}"><label>Skriv GODKÄNN för att godkänna exakt detta förslag:<br><input name="confirm" required autocomplete="off" pattern="GODKÄNN"></label><button type="submit">Godkänn förslaget</button></form><p>Stäng sidan för att avstå. Godkännandet skickar ännu ingen ändring till Google. Gå därefter tillbaka till Claude för att genomföra förslaget.</p></body></html>""",
            headers=HEADERS,
        )
    except WriteBlocked:
        return PlainTextResponse(
            "Förslaget saknas, har ändrats eller har gått ut. Be Claude skapa ett nytt förslag.",
            status_code=409,
            headers=HEADERS,
        )
    except Exception:
        # Do not return/log credentials, plan data or backend exception payloads.
        return PlainTextResponse(
            "Godkännandet kunde inte behandlas. Ingen ändring skickades till Google från denna sida.",
            status_code=503,
            headers=HEADERS,
        )


def register_approval_routes(mcp):
    mcp.custom_route("/approvals/{plan_id}", methods=["GET", "POST"])(approval_page)
