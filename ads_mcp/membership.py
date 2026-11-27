"""Subject-bound, free Viks memberships. Platform grants are independent."""

import html
import re
from urllib.parse import parse_qs

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from ads_mcp.activity import is_admin
from ads_mcp.linkedin_auth import config, connection_store
from ads_mcp.write_policy import WriteBlocked, digest

ACCOUNT = "/mitt-konto"
ADMIN = "/admin/medlemmar"
ADMIN_HOME = "/admin"
MEMBERS = "/admin/medlemslista"

STATUS_LABELS = {
    "active": "Aktiv",
    "registered": "Registrerad",
    "pending": "Väntar på granskning",
    "declined": "Avslagen",
}
DOMAIN = "socialzense.se"


def verified_user(claims):
    subject, email = claims.get("sub"), claims.get("email")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(email, str)
        or not email
        or claims.get("email_verified") not in (True, "true")
    ):
        raise WriteBlocked("Logga in med ett verifierat Google-konto.")
    return {
        "owner": digest({"provider": "google", "subject": subject}),
        "email": email.strip().casefold(),
        "email_verified": True,
        "hd": claims.get("hd")
        or (claims.get("google_user_data") or {}).get("hd"),
    }


def automatic(user):
    return is_admin(user) or (
        user.get("email_verified") is True
        and user.get("hd") == DOMAIN
        and user["email"].endswith("@" + DOMAIN)
    )


def collection():
    return connection_store().client.collection("viks_members")


def member_state(user, *, signup=False):
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import firestore

    ref = collection().document(user["owner"])
    row = ref.get().to_dict()
    if row is None:
        if not automatic(user) and not signup:
            return "missing"
        initial = "active" if automatic(user) else "registered"
        from ads_mcp.connections import crypto

        try:
            ref.create(
                {
                    "status": initial,
                    "email": crypto().encrypt(user["email"].encode()).decode(),
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "plan": "free",
                }
            )
            return initial
        except AlreadyExists:
            row = ref.get().to_dict()
    status = (row or {}).get("status", "missing")
    if status in {"registered", "pending"} and automatic(user):
        try:
            change_state(user["owner"], status, "active", user["owner"])
            return "active"
        except WriteBlocked:
            return (ref.get().to_dict() or {}).get("status", "missing")
    return status


def active_owner(owner):
    record = collection().document(owner).get().to_dict()
    return (record or {}).get("status") == "active"


class MemberGate(Middleware):
    async def authorize(self, context, call_next):
        try:
            token = get_access_token()
            user = verified_user(token.claims if token else {})
            status = await run_in_threadpool(member_state, user)
        except WriteBlocked as exc:
            raise ToolError(str(exc)) from None
        except Exception:
            raise ToolError(
                "Medlemskapet kunde inte kontrolleras. Försök igen."
            ) from None
        if status != "active":
            raise ToolError(
                "Aktivt Viks-medlemskap krävs. Öppna https://app.viksannonsverktyg.se/mitt-konto för att skapa konto eller se din ansökan."
            )
        return await call_next(context)

    on_call_tool = authorize
    on_read_resource = authorize
    on_get_prompt = authorize


def change_state(owner, expected, target, actor):
    from google.cloud import firestore

    store = connection_store()
    ref = store.client.collection("viks_members").document(owner)

    @firestore.transactional
    def apply(tx):
        row = ref.get(transaction=tx).to_dict() or {}
        if row.get("status") != expected:
            raise WriteBlocked(
                "Medlemskapet har redan ändrats. Ladda om sidan."
            )
        tx.update(
            ref,
            {
                "status": target,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "changed_by": actor,
            },
        )

    apply(store.client.transaction())


async def posted(request):
    if (
        request.headers.get("origin") != config(storage_only=True)["base_url"]
        or request.headers.get("content-type", "").split(";")[0]
        != "application/x-www-form-urlencoded"
    ):
        raise WriteBlocked("Ogiltigt formulär.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1024:
            raise WriteBlocked("Ogiltigt formulär.")
        body.extend(chunk)
    return parse_qs(body.decode(), max_num_fields=4)


def application_list():
    from ads_mcp.connections import crypto
    from google.cloud.firestore_v1.base_query import FieldFilter

    rows = []
    for doc in (
        collection()
        .where(filter=FieldFilter("status", "==", "pending"))
        .stream()
    ):
        email = crypto().decrypt(doc.to_dict()["email"].encode()).decode()
        rows.append((email, doc.id))
    return sorted(rows)


async def membership_page(request):
    from ads_mcp.connections import (
        HEAD,
        channel_cards,
        claude_setup,
        page,
        session,
    )

    admin = request.url.path == ADMIN
    title = "Medlemsansökningar" if admin else "Min översikt"
    try:
        user = await run_in_threadpool(session, request)
        if (
            not user
            or user.get("email_verified") is not True
            or "hd" not in user
        ):
            return page(
                "<p>Viks Annonsverktyg är kostnadsfritt. Logga in med Google för att öppna eller skapa ditt konto.</p>"
                f'<a class="btn" href="/anslutningar/google?till={ADMIN if admin else ACCOUNT}">Fortsätt med Google</a>',
                title=title,
            )
        if admin and not is_admin(user):
            return page(
                "Ditt konto saknar administratörsbehörighet.", 403, title=title
            )
        state = await run_in_threadpool(member_state, user, signup=True)
        if request.method == "POST":
            fields = await posted(request)
            if admin:
                uid = fields.get("id", [""])[0]
                decision = fields.get("decision", [])
                if not re.fullmatch(r"[a-f0-9]{64}", uid) or decision not in (
                    ["active"],
                    ["declined"],
                ):
                    raise WriteBlocked("Ogiltigt beslut.")
                await run_in_threadpool(
                    change_state, uid, "pending", decision[0], user["owner"]
                )
            elif fields.get("request") == ["yes"]:
                await run_in_threadpool(
                    change_state,
                    user["owner"],
                    "registered",
                    "pending",
                    user["owner"],
                )
            else:
                raise WriteBlocked("Ogiltig begäran.")
            return RedirectResponse(request.url.path, 303, headers=HEAD)
        if admin:
            rows = await run_in_threadpool(application_list)
            content = "".join(
                f'<section><strong>{html.escape(email)}</strong><form method="post"><input type="hidden" name="id" value="{uid}">'
                '<button name="decision" value="active">Godkänn gratisåtkomst</button> <button name="decision" value="declined">Avslå</button></form></section>'
                for email, uid in rows
            )
            return page(
                (content or "<p>Inga väntande ansökningar.</p>")
                + '<p><a href="/admin">Till adminöversikten</a></p>',
                title=title,
                user=user,
            )
        content = {
            "active": "<p>Ditt kostnadsfria medlemskap är aktivt.</p>",
            "registered": '<p>Ditt konto är skapat. Ansök om gratis åtkomst till annonsverktygen.</p><form method="post"><button name="request" value="yes">Ansök om åtkomst</button></form>',
            "pending": "<p>Din ansökan väntar på granskning. Du ser beslutet här.</p>",
            "declined": "<p>Din ansökan om åtkomst har avslagits.</p>",
        }[state]
        overview = (
            '<div class="identity"><strong>'
            + html.escape(user["email"])
            + "</strong>"
            f'<span class="badge">{STATUS_LABELS[state]}</span></div>' + content
        )
        if state == "active":
            overview += await channel_cards(user, request)
            overview += claude_setup()
        return page(overview, title=title, user=user)
    except WriteBlocked as exc:
        return page(html.escape(str(exc)), 403, title=title)
    except Exception:
        return page("Medlemskapet kunde inte hämtas just nu.", 503, title=title)


def member_list(after):
    from ads_mcp.connections import crypto

    ref = collection()
    query = ref.order_by("__name__")
    if after:
        query = query.start_after({"__name__": ref.document(after)})
    docs = list(query.limit(51).stream())
    rows = []
    for doc in docs[:50]:
        data = doc.to_dict()
        email = crypto().decrypt(data["email"].encode()).decode()
        rows.append((email, STATUS_LABELS.get(data["status"], "Okänd")))
    return rows, docs[49].id if len(docs) > 50 else None


async def admin_page(request):
    from ads_mcp.connections import page, session

    listing = request.url.path == MEMBERS
    title = "Medlemmar" if listing else "Adminöversikt"
    try:
        user = await run_in_threadpool(session, request)
        if not user:
            return page(
                "<p>Logga in för att öppna administrationen.</p>"
                f'<a class="btn" href="/anslutningar/google?till={request.url.path}">Fortsätt med Google</a>',
                401,
                title=title,
            )
        if not is_admin(user):
            return page(
                "Ditt konto saknar administratörsbehörighet.",
                403,
                title=title,
                user=user,
            )
        if not listing:
            cards = (
                ("Ansökningar", "Granska nya ansökningar om åtkomst.", ADMIN),
                (
                    "Medlemmar",
                    "Se registrerade konton och deras åtkomststatus.",
                    MEMBERS,
                ),
                (
                    "Användning",
                    "Se användarnas totala aktivitet och produktuppdelning.",
                    "/admin/aktivitet",
                ),
            )
            content = (
                '<div class="grid admin-grid">'
                + "".join(
                    f'<section><h2>{name}</h2><p>{description}</p><a class="btn" href="{link}">Visa {name.lower()}</a></section>'
                    for name, description, link in cards
                )
                + "</div>"
            )
        else:
            after = request.query_params.get("efter", "")
            if after and not re.fullmatch(r"[a-f0-9]{64}", after):
                return page("Ogiltig sidmarkör.", 400, title=title, user=user)
            members, next_page = await run_in_threadpool(member_list, after)
            rows = "".join(
                f"<tr><td>{html.escape(email)}</td><td>{html.escape(status)}</td></tr>"
                for email, status in members
            )
            content = (
                '<section><div class="table-wrap"><table><thead><tr><th>Google-konto</th><th>Status</th></tr></thead><tbody>'
                + rows
                + "</tbody></table></div></section>"
                if rows
                else "<p>Inga medlemmar att visa.</p>"
            )
            if next_page:
                content += f'<p><a class="btn" href="?efter={next_page}">Nästa sida</a></p>'
            content += '<p><a href="/admin">Till adminöversikten</a></p>'
        return page(content, title=title, user=user)
    except Exception:
        return page(
            "Administrationen kunde inte öppnas just nu.", 503, title=title
        )
