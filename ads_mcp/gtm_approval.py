"""Review the exact GTM workspace draft as its signed-in owner."""

import hmac
import html
import json

from starlette.concurrency import run_in_threadpool

from ads_mcp import gtm_drafts
from ads_mcp.connections import page
from ads_mcp.membership import posted
from ads_mcp.write_approval import browser_identity, confirmed, csrf_token
from ads_mcp.write_policy import WriteBlocked
from ads_mcp.write_store import check_plan


async def approval_page(request):
    owner, login = await browser_identity(request)
    if login is not None:
        return login
    title = "Granska GTM-utkast"
    try:
        settings = gtm_drafts.policy()
        store = gtm_drafts.GTMStore(settings)
        plan_id = request.path_params["plan_id"]
        plan = await run_in_threadpool(
            gtm_drafts.checked_plan, store, plan_id, owner
        )
        check_plan(plan, owner)
        if plan["status"] != "pending_approval":
            raise WriteBlocked("Förslaget har redan behandlats.")
        csrf = csrf_token(plan, settings.approval_secret)
        if request.method == "POST":
            fields = await posted(request)
            proof = fields.get("csrf", [])
            if (
                len(proof) != 1
                or not hmac.compare_digest(proof[0].encode(), csrf.encode())
                or not confirmed(fields)
            ):
                raise WriteBlocked("Skriv Godkänn för att bekräfta utkastet.")
            await run_in_threadpool(
                store.transition,
                plan_id,
                "pending_approval",
                "approved",
                owner=owner,
            )
            return page(
                "<p>Förslaget är godkänt. Gå tillbaka till Claude och be om att skapa den granskade arbetsytan.</p>",
                title="GTM-utkast godkänt",
            )
        change = plan["change"]
        target = html.escape(
            f'{change["container"]["name"]} · {change["container"]["publicId"]} · {change["container_path"]}'
        )
        edit = change.get("edit")
        tag_details = (
            "<p><strong>Befintlig tagg</strong> "
            + html.escape(
                f'{edit["tag_name"]} · ID {edit["tag_id"]} · version {edit["version_id"]}'
            )
            + "</p>"
            if edit
            else ""
        )
        summary = (
            "Granskade fält före och efter"
            if edit
            else "Exakta taggar, triggers och variabler"
        )
        contents = html.escape(
            json.dumps(change["changes"], ensure_ascii=False, indent=2)
        )
        return page(
            f'<section><p><strong>Container</strong> {target}</p><p><strong>Ny arbetsyta</strong> {html.escape(change["workspace_name"])}</p>{tag_details}'
            "<p>Förslaget skapar en ny arbetsyta för test i Tag Assistant. Publicering sker separat i Google Tag Manager.</p>"
            f"<details open><summary>{summary}</summary>"
            f'<pre class="draft-payload">{contents}</pre></details>'
            f'<form method="post"><input type="hidden" name="csrf" value="{csrf}">'
            '<label>Skriv Godkänn <input name="confirm" required autocomplete="off"></label> '
            "<button>Godkänn utkastet</button></form></section>",
            title=title,
        )
    except WriteBlocked as exc:
        return page(html.escape(str(exc)), 409, title=title)
    except Exception:
        return page(
            "Förslaget kunde inte kontrolleras just nu.", 503, title=title
        )


def register_gtm_approval_routes(mcp):
    mcp.custom_route("/gtm/approvals/{plan_id}", methods=["GET", "POST"])(
        approval_page
    )
