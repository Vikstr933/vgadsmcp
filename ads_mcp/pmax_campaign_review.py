"""Owner review for an exact standard Performance Max proposal."""

from datetime import datetime, timezone
from decimal import Decimal
from html import escape


def render_pmax_review(plan):
    change, snapshot = plan["change"], plan["snapshot"]
    spec = change["pmax_campaign"]
    account = snapshot["account"]

    def table(rows):
        entries = "".join(
            f"<dt><strong>{escape(label)}</strong></dt><dd>{escape(str(value))}</dd>"
            for label, value in rows
        )
        return "<dl>" + entries + "</dl>"

    total = spec.get("total_budget_micros")
    amount = Decimal(total if total is not None else spec["daily_budget_micros"])
    budget = f"{amount / Decimal(1_000_000):,.2f} {account['currency']}"
    parts = [
        table(
            [
                ("Åtgärd", "Skapa standard Performance Max i pausat läge"),
                ("Konto", f"{change['customer_id']} · {account['name']}"),
                (
                    "MCC för åtkomst",
                    change.get("login_customer_id") or "Direkt åtkomst",
                ),
                ("Kampanj", spec["name"]),
                (
                    "Totalbudget" if total is not None else "Genomsnittlig dagsbudget",
                    budget,
                ),
                (
                    "Period",
                    f"{spec['start_date']} till {spec['end_date']} ({account['time_zone']})",
                ),
                ("Budgivning", spec["bidding"]),
                (
                    "Politisk reklam i EU",
                    "Ja" if change["eu_political_advertising"] == "contains" else "Nej",
                ),
                ("Tillgångsgrupp", spec["asset_group_name"]),
                ("Landningssida", spec["final_url"]),
                ("Spårningssuffix", spec.get("final_url_suffix") or "Inget"),
                ("Företagsnamn", spec["business_name"]),
                (
                    "Godkännandet går ut",
                    datetime.fromtimestamp(plan["expires_at"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    ),
                ),
            ]
        ),
        "<p>Vid start samma dag kan Google spara sitt aktuella klockslag för startdatumet.</p>",
        "<h2>Geografi och språk</h2>",
        table(
            [
                (
                    (
                        "Uteslut"
                        if geo["id"] in spec["excluded_geo_target_ids"]
                        else "Inkludera"
                    ),
                    f"{geo['name']}, {geo['country']} ({geo['id']})",
                )
                for geo in snapshot["locations"]
            ]
            + [
                ("Språk", f"{lang['name']} ({lang['id']})")
                for lang in snapshot["languages"]
            ]
        ),
        "<p>Geografin utgår från fysisk närvaro. Google kan visa annonser på flera av sina kanaler.</p>",
        "<h2>Konverteringsmål</h2>",
        table(
            [
                (
                    action["name"],
                    f"ID {action['id']} · {action['category']} · {action['origin']} · "
                    f"{'primär' if action['primary_for_goal'] else 'sekundär'}",
                )
                for action in snapshot["conversions"]
            ]
        ),
        "<p>Kampanjen använder enbart de listade åtgärderna i sitt anpassade mål. "
        "Även sekundära åtgärder i målet kan användas för budgivning.</p>",
    ]
    existing = snapshot.get("existing_custom_goal")
    if existing:
        parts.append(
            table(
                [
                    (
                        "Befintligt mål återanvänds",
                        f"{existing['name']} ({existing['resource_name']})",
                    )
                ]
            )
        )
    parts.append("<h2>Annonstexter</h2>")
    for label, key in (
        ("Kort rubrik", "headlines"),
        ("Lång rubrik", "long_headlines"),
        ("Beskrivning", "descriptions"),
    ):
        parts.append(
            table([(f"{label} {i}", text) for i, text in enumerate(spec[key], 1)])
        )

    media = []
    for label, ids in (
        ("Logotyp", [spec["logo_asset_id"]]),
        ("Liggande bild", spec["landscape_image_asset_ids"]),
        ("Kvadratisk bild", spec["square_image_asset_ids"]),
        ("YouTube-video", spec["video_asset_ids"]),
    ):
        for asset_id in ids:
            asset = snapshot["assets"][asset_id]
            info = (
                f"{asset['width']} × {asset['height']} px"
                if asset["type"] == "IMAGE"
                else f"{asset['title']} · YouTube-ID {asset['video_id']}"
            )
            media.append((label, f"{asset['name']} · tillgång {asset_id} · {info}"))
    parts.extend(["<h2>Befintligt material i Google Ads</h2>", table(media)])
    video_note = (
        "Google får skapa video om lämplig video saknas."
        if spec["allow_auto_video"]
        else "Egen video ingår. Kontrollera att videoformaten täcker önskade placeringar."
    )
    parts.extend(
        [
            f"<p>{escape(video_note)}</p>",
            "<p>URL-expansion, textanpassning, bildextraktion och valda bild- och "
            "videoförbättringar är avstängda. Google optimerar bud och visningar.</p>",
            "<p>Kampanjen skapas pausad med tillgångsgruppen aktiverad under den. "
            "Kontrollera mätning, policygranskning och eventuell automatisering på kontonivå före aktivering.</p>",
        ]
    )
    return "".join(parts)
