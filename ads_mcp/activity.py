"""Daily per-user call totals with a product-only breakdown."""

import base64
import hashlib
import html
import logging
import re
from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from starlette.concurrency import run_in_threadpool

from ads_mcp.linkedin_auth import config
from ads_mcp.write_policy import digest

PATH = "/admin/aktivitet"
ADMIN_EMAIL_DIGESTS = frozenset(
    {
        "594784a5c8195d724d94b182accf150d0304aac75a8156c721c3fc2eef44b28b",
        "56aef067c5fce9193ce01f4aa8e6601f0861b05e10472cdab7e859f260897aef",
    }
)
STOCKHOLM = ZoneInfo("Europe/Stockholm")
DAYS = 30
PRODUCT_NAMES = {
    "drive": "Google Drive",
    "ga4": "Google Analytics 4",
    "google_ads": "Google Ads",
    "gtm": "Google Tag Manager",
    "linkedin": "LinkedIn Ads",
    "connection_check": "Anslutningstest (Testa vADS)",
    "other": "Äldre ospecificerade anrop",
    "unclassified": "Oklassificerade anrop",
}
PRODUCT_BY_CATEGORY = {
    "drive": "drive",
    "ga4": "ga4",
    "customers": "google_ads",
    "search": "google_ads",
    "metadata": "google_ads",
    "keyword_planning": "google_ads",
    "writes": "google_ads",
    "gtm": "gtm",
    "linkedin": "linkedin",
    "status": "connection_check",
}


def is_admin(user):
    return bool(
        user
        and user.get("email_verified") is True
        and hashlib.sha256(
            user.get("email", "").strip().casefold().encode()
        ).hexdigest()
        in ADMIN_EMAIL_DIGESTS
    )


@lru_cache(maxsize=1)
def storage():
    from google.cloud import firestore

    settings = config(storage_only=True)
    client = firestore.Client(
        project=settings["project"], database=settings["database"]
    )
    key = hashlib.sha256(
        ("viks-activity-v1:" + settings["encryption"]).encode()
    ).digest()
    return (
        client,
        client.collection("viks_activity_days"),
        Fernet(base64.urlsafe_b64encode(key)),
    )


def count_call(subject, email, day, product):
    from google.cloud import firestore

    _, collection, cipher = storage()
    product = (
        product
        if product in PRODUCT_NAMES and product != "other"
        else "unclassified"
    )
    user = digest({"provider": "google", "subject": subject})
    record = {
        "day": day.isoformat(),
        "user": user,
        "calls": firestore.Increment(1),
        "products": {product: firestore.Increment(1)},
        "delete_after": datetime.combine(
            day + timedelta(days=DAYS), time.min, tzinfo=STOCKHOLM
        ),
    }
    if email:
        record["label"] = cipher.encrypt(email.encode()).decode()
    collection.document(f"{day.isoformat()}_{user}").set(
        record, merge=True, retry=None, timeout=2
    )


class ActivityCounts(Middleware):
    def __init__(self, mounted_tools):
        self.tool_products = {
            name: PRODUCT_BY_CATEGORY.get(category, "unclassified")
            for name, category in mounted_tools.items()
        }

    async def on_call_tool(self, context, call_next):
        try:
            token = get_access_token()
            claims = token.claims if token else {}
            subject = claims.get("sub")
            if isinstance(subject, str) and subject:
                email = (
                    claims.get("email")
                    if claims.get("email_verified") in (True, "true")
                    else None
                )
                await run_in_threadpool(
                    count_call,
                    subject,
                    email,
                    datetime.now(STOCKHOLM).date(),
                    self.tool_products.get(
                        context.message.name, "unclassified"
                    ),
                )
        except Exception:
            # A statistics outage must not interrupt tool execution or leak call details.
            logging.getLogger(__name__).warning("Activity counter unavailable")
        return await call_next(context)


def activity_summary(today, member_id=None):
    from google.cloud.firestore_v1.base_query import FieldFilter

    client, collection, cipher = storage()
    cutoff = today - timedelta(days=DAYS - 1)
    if member_id:
        # Daily document IDs are deterministic. Read only the chosen member's
        # documents and avoid a cross-user query or a new Firestore index.
        refs = [
            collection.document(
                f"{cutoff + timedelta(days=offset)}_{member_id}"
            )
            for offset in range(DAYS)
        ]
        docs = client.get_all(refs)
    else:
        docs = (
            collection.where(
                filter=FieldFilter("day", ">=", cutoff.isoformat())
            )
            .where(filter=FieldFilter("day", "<=", today.isoformat()))
            .select(["day", "user", "calls", "label"])
            .stream()
        )

    users = {}
    for doc in docs:
        row = doc.to_dict() or {}
        uid = row.get("user")
        # Previous revisions also wrote product totals shared by all members.
        if not uid or (member_id and uid != member_id):
            continue
        day = row["day"]
        item = users.setdefault(
            uid,
            {
                "id": uid,
                "name": "Användare " + uid[:12],
                "calls": 0,
                "days": 0,
                "last": day,
                "label_day": "",
                "products": dict.fromkeys(PRODUCT_NAMES, 0),
            },
        )
        item["calls"] += row["calls"]
        item["days"] += 1
        item["last"] = max(day, item["last"])
        for product, count in row.get("products", {}).items():
            item["products"][
                product if product in PRODUCT_NAMES else "unclassified"
            ] += count
        if row.get("label") and day >= item["label_day"]:
            try:
                item["name"] = cipher.decrypt(row["label"].encode()).decode()
            except InvalidToken:
                continue
            item["label_day"] = day
    return sorted(
        users.values(), key=lambda r: (-r["days"], -r["calls"], r["name"])
    )


def product_summary_html(product_calls, total_calls):
    recorded = sum(product_calls.values())
    if not recorded:
        return "<p>Produktstatistik visas från användarens första anrop efter uppdateringen. Tidigare anrop ingår i totalen ovan.</p>"
    rows = "".join(
        f'<tr><th scope="row">{PRODUCT_NAMES[key]}</th><td>{count}</td>'
        f'<td><meter min="0" max="{recorded}" value="{count}" aria-label="Andel för {PRODUCT_NAMES[key]}"></meter> {count / recorded:.0%}</td></tr>'
        for key, count in sorted(
            product_calls.items(), key=lambda item: -item[1]
        )
        if key in ("google_ads", "gtm", "linkedin", "drive", "ga4") or count
    )
    previous = total_calls - recorded
    note = (
        f"<p>{previous} tidigare anrop ingår i totalen ovan. Andelarna avser anrop med produktuppdelning.</p>"
        if previous > 0
        else ""
    )
    if product_calls.get("connection_check"):
        note += "<p>Anslutningstest räknas en gång, även när det kontrollerar flera produkter.</p>"
    if product_calls.get("other"):
        note += "<p>Äldre ospecificerade anrop sparades innan anslutningstestet fick en egen kategori. De går inte att identifiera i efterhand.</p>"
    return (
        '<div class="table-wrap"><table><thead><tr><th>Produkt eller funktion</th><th>Anrop</th>'
        "<th>Andel</th></tr></thead><tbody>"
        + rows
        + "</tbody></table></div>"
        + note
    )


async def activity_page(request):
    from ads_mcp.connections import page, session

    try:
        user = await run_in_threadpool(session, request)
        if not user:
            return page(
                "<p>Logga in med administratörens Google-konto för att se användarstatistiken.</p>"
                '<a class="btn" href="/anslutningar/google?till=/admin/aktivitet">Fortsätt med Google</a>',
                401,
                title="Användaraktivitet",
            )
        if not is_admin(user):
            return page(
                '<p>Ditt konto saknar åtkomst till användarstatistiken.</p><a href="/anslutningar/google?till=/admin/aktivitet">Välj Google-konto igen</a>',
                403,
                title="Användaraktivitet",
            )
        today = datetime.now(STOCKHOLM).date()
        member_id = request.query_params.get("anvandare")
        period = f"<p>{today-timedelta(days=DAYS-1)} – {today} · svensk tid</p>"
        if member_id is not None:
            if not re.fullmatch(r"[a-f0-9]{64}", member_id):
                return page(
                    "Användaren hittades inte.",
                    404,
                    title="Användaraktivitet",
                    user=user,
                )
            items = await run_in_threadpool(activity_summary, today, member_id)
            back = f'<p><a href="{PATH}">Till alla användare</a></p>'
            if not items:
                return page(
                    back
                    + "<p>Ingen aktivitet för användaren under perioden.</p>",
                    404,
                    title="Användaraktivitet",
                    user=user,
                )
            member = items[0]
            products = product_summary_html(member["products"], member["calls"])
            return page(
                back
                + period
                + f'<p>{member["calls"]} anrop totalt · {member["days"]} aktiva dagar · Senast aktiv {member["last"]}</p>'
                + f"<section><h2>Anrop per produkt</h2>{products}</section>",
                title=member["name"],
                user=user,
            )
        items = await run_in_threadpool(activity_summary, today)
        rows = "".join(
            f'<tr><td><a href="{PATH}?anvandare={r["id"]}">{html.escape(r["name"])}</a></td><td>{r["days"]}</td><td>{r["calls"]}</td><td>{r["last"]}</td></tr>'
            for r in items
        )
        table = (
            (
                '<div style="overflow-x:auto"><table><thead><tr><th>Google-konto</th><th>Aktiva dagar</th>'
                "<th>Anrop</th><th>Senast aktiv</th></tr></thead><tbody>"
                + rows
                + "</tbody></table></div>"
            )
            if rows
            else "<p>Inga verktygsanrop har registrerats för perioden.</p>"
        )
        return page(
            period + f"<section><h2>Anrop per användare</h2>{table}</section>"
            "<p>Välj ett Google-konto för att se produktanvändningen. Varje användare visas från sitt första verktygsanrop. Listan sorteras efter aktiva dagar och därefter antal anrop. Även anrop som ger fel räknas.</p>"
            '<p><a href="/admin">Till adminöversikten</a></p>',
            title="Användaraktivitet",
            user=user,
        )
    except Exception:
        return page(
            "Statistiken kunde inte hämtas just nu.",
            503,
            title="Användaraktivitet",
        )
