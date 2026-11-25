"""Daily per-user MCP call counts. Never receive tool arguments or results."""

import base64
import hashlib
import html
import logging
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
    return client.collection("viks_activity_days"), Fernet(
        base64.urlsafe_b64encode(key)
    )


def count_call(subject, email, day):
    from google.cloud import firestore

    collection, cipher = storage()
    user = digest({"provider": "google", "subject": subject})
    record = {
        "day": day.isoformat(),
        "user": user,
        "calls": firestore.Increment(1),
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
                    count_call, subject, email, datetime.now(STOCKHOLM).date()
                )
        except Exception:
            # A statistics outage must not interrupt tool execution or leak call details.
            logging.getLogger(__name__).warning("Activity counter unavailable")
        return await call_next(context)


def ranking(today):
    from google.cloud.firestore_v1.base_query import FieldFilter

    collection, cipher = storage()
    cutoff = today - timedelta(days=DAYS - 1)
    docs = (
        collection.where(filter=FieldFilter("day", ">=", cutoff.isoformat()))
        .where(filter=FieldFilter("day", "<=", today.isoformat()))
        .stream()
    )
    users = {}
    for doc in docs:
        row = doc.to_dict()
        uid, day = row["user"], row["day"]
        item = users.setdefault(
            uid,
            {
                "name": "Användare " + uid[:12],
                "calls": 0,
                "days": 0,
                "last": day,
                "label_day": "",
            },
        )
        item["calls"] += row["calls"]
        item["days"] += 1
        item["last"] = max(day, item["last"])
        if row.get("label") and day >= item["label_day"]:
            try:
                item["name"] = cipher.decrypt(row["label"].encode()).decode()
            except InvalidToken:
                continue
            item["label_day"] = day
    return sorted(
        users.values(), key=lambda r: (-r["days"], -r["calls"], r["name"])
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
        items = await run_in_threadpool(ranking, today)
        rows = "".join(
            f'<tr><td>{html.escape(r["name"])}</td><td>{r["days"]}</td><td>{r["calls"]}</td><td>{r["last"]}</td></tr>'
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
            f"<p>{today-timedelta(days=DAYS-1)} – {today} · svensk tid</p><section>{table}</section>"
            "<p>Varje användare visas från sitt första verktygsanrop. Listan sorteras efter aktiva dagar och därefter antal anrop. Även anrop som ger fel räknas.</p>",
            title="Användaraktivitet",
        )
    except Exception:
        return page(
            "Statistiken kunde inte hämtas just nu.",
            503,
            title="Användaraktivitet",
        )
