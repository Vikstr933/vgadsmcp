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
    from ads_mcp.connections import HEAD, page, session

    admin = request.url.path == ADMIN
    title = "Medlemsansökningar" if admin else "Mitt konto"
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
                + '<a href="/mitt-konto">Mitt konto</a>',
                title=title,
            )
        content = {
            "active": '<p>Ditt kostnadsfria medlemskap är aktivt.</p><a class="btn" href="/anslutningar">Hantera mina annonskanaler</a>',
            "registered": '<p>Ditt konto är skapat. Ansök om gratis åtkomst till annonsverktygen.</p><form method="post"><button name="request" value="yes">Ansök om åtkomst</button></form>',
            "pending": "<p>Din ansökan väntar på granskning. Du ser beslutet här.</p>",
            "declined": "<p>Din ansökan om åtkomst har avslagits.</p>",
        }[state]
        if is_admin(user):
            content += '<p><a href="/admin/medlemmar">Granska medlemsansökningar</a> · <a href="/admin/aktivitet">Användaraktivitet</a></p>'
        return page(
            "<section><h2>"
            + html.escape(user["email"])
            + "</h2>"
            + content
            + "</section>",
            title=title,
        )
    except WriteBlocked as exc:
        return page(html.escape(str(exc)), 403, title=title)
    except Exception:
        return page("Medlemskapet kunde inte hämtas just nu.", 503, title=title)
