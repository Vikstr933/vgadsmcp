"""Escaped, complete human review of the proposed Search package."""

from datetime import datetime, timezone
from decimal import Decimal
from html import escape


def render_search_review(plan):
    change, snapshot = plan["change"], plan["snapshot"]
    spec = change["search_campaign"]
    currency = snapshot["account"]["currency"]

    def money(value):
        return f"{Decimal(value) / Decimal(1_000_000):,.2f} {currency}"

    total = spec.get("total_budget_micros")
    budget = (
        ("Totalbudget för kampanjperioden", money(total))
        if total is not None
        else ("Genomsnittlig dagsbudget", money(spec["daily_budget_micros"]))
    )

    def table(rows):
        return (
            "<dl>"
            + "".join(
                f"<dt><strong>{escape(str(k))}</strong></dt><dd>{escape(str(v))}</dd>"
                for k, v in rows
            )
            + "</dl>"
        )

    def keywords(values):
        return (
            "<ul>"
            + "".join(
                f"<li>{escape(k['match_type'])}: {escape(k['text'])}</li>"
                for k in values
            )
            + "</ul>"
        )

    parts = [
        table(
            [
                ("Åtgärd", "Skapa en komplett ny sökkampanj — PAUSAD"),
                ("Konto", f"{change['customer_id']} · {snapshot['account']['name']}"),
                (
                    "MCC för åtkomst",
                    change.get("login_customer_id") or "Direkt åtkomst",
                ),
                ("Kampanj", spec["name"]),
                budget,
                ("Budgivning", spec["bidding"]),
                (
                    "Period",
                    f"{spec['start_date']} 00:00:00 – {spec['end_date']} 23:59:59 ({snapshot['account']['time_zone']})",
                ),
                (
                    "Politisk EU-reklam",
                    "Ja" if change["eu_political_advertising"] == "contains" else "Nej",
                ),
                (
                    "Nätverk och geografisk princip",
                    "Endast Google Sök. Personer som befinner sig eller regelbundet befinner sig i området (PRESENCE).",
                ),
                (
                    "Språk",
                    ", ".join(
                        f"{v['name']} ({v['id']})" for v in snapshot["languages"]
                    ),
                ),
                ("Spårningssuffix", spec.get("final_url_suffix") or "Inget"),
                (
                    "Godkännandet går ut",
                    datetime.fromtimestamp(plan["expires_at"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    ),
                ),
            ]
        )
    ]
    parts.append(
        "<h2>Platser</h2>"
        + table(
            [
                (
                    (
                        "Uteslut"
                        if v["id"] in spec["excluded_geo_target_ids"]
                        else "Inkludera"
                    ),
                    f"{v['name']}, {v['country']} ({v['id']})",
                )
                for v in snapshot["locations"]
            ]
        )
    )
    parts.append(
        "<h2>Kampanjens konverteringsmål</h2><p>Endast nedanstående åtgärder väljs i ett nytt anpassat mål för denna kampanj. Kontots primära inställningar ändras inte. <strong>Även sekundära åtgärder i ett anpassat mål används för budgivning.</strong> Övriga standardmål stängs av för den nya kampanjen.</p>"
        + table(
            [
                (
                    v["name"],
                    f"ID {v['id']} · {v['category']} · {v['origin']} · {'primär' if v['primary_for_goal'] else 'sekundär'}",
                )
                for v in snapshot["conversions"]
            ]
        )
    )
    parts.append(
        "<h2>Negativa sökord för kampanjen</h2>" + keywords(spec["negative_keywords"])
    )
    for group in spec["ad_groups"]:
        parts.append("<h2>Annonsgrupp: " + escape(group["name"]) + "</h2>")
        if group.get("cpc_bid_micros") is not None:
            parts.append(table([("CPC-bud", money(group["cpc_bid_micros"]))]))
        parts.append("<h3>Sökord</h3>" + keywords(group["keywords"]))
        parts.append("<h3>Negativa sökord</h3>" + keywords(group["negative_keywords"]))
        for index, ad in enumerate(group["ads"], 1):
            parts.append(
                f"<h3>Responsiv sökannons {index}</h3>"
                + table(
                    [
                        ("Landningssida", ad["final_url"]),
                        (
                            "Visningssökväg",
                            "/".join(
                                v for v in (ad.get("path1"), ad.get("path2")) if v
                            ),
                        ),
                    ]
                    + [
                        (f"Rubrik {i}", text)
                        for i, text in enumerate(ad["headlines"], 1)
                    ]
                    + [
                        (f"Beskrivning {i}", text)
                        for i, text in enumerate(ad["descriptions"], 1)
                    ]
                )
            )
    parts.append("<h2>Webbplatslänkar</h2>")
    for link in spec["sitelinks"]:
        parts.append(
            table(
                [
                    ("Text", link["text"]),
                    ("URL", link["final_url"]),
                    ("Beskrivning 1", link.get("description1") or ""),
                    ("Beskrivning 2", link.get("description2") or ""),
                ]
            )
        )
    parts.append(
        "<h2>Informationstillägg</h2><ul>"
        + "".join("<li>" + escape(v) + "</li>" for v in spec["callouts"])
        + "</ul>"
    )
    parts.append(
        "<h2>Vad godkännandet innebär</h2><ul>"
        "<li>Kampanjen skapas pausad. Annonsgrupper och annonser är aktiverade under den pausade kampanjen och kan börja visas först efter separat kampanjaktivering.</li>"
        "<li>AI Max, textanpassning och expansion av slutlig URL är avstängda. Responsiva annonser kombinerar fortfarande de texter du godkänner.</li>"
        "<li>Kontots webbplatslänkar och informationstillägg ärvs inte. Andra automatiska tillägg och automatiskt tillämpade rekommendationer på kontonivå måste kontrolleras manuellt före start.</li>"
        "<li>Skapandet görs som ett sammanhållet paket. Målen konfigureras därefter medan kampanjen är pausad. Vid fel efter skapandet sparas tillgängliga resurs-ID:n och kontot låses för kontroll. Skapa inte en dubblett.</li>"
        "<li>Befintliga kampanjer ändras inte. Kampanjen läses tillbaka innan resultatet markeras som verifierat. Googles annonsgranskning, mätning, fakturering, datum och landningssidor måste också vara klara inför start.</li>"
        "</ul>"
    )
    return "".join(parts)
