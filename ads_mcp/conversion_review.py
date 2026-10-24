"""Escaped, independent before/after review for conversion action proposals."""

import html
from datetime import datetime, timezone

LABELS = {
    "name": "Namn",
    "category": "Kategori",
    "type": "Typ",
    "status": "Status",
    "primary_for_goal": "Primär för standardmålens budgivning",
    "counting_type": "Räknesätt",
    "click_through_lookback_window_days": "Klickfönster (dagar)",
    "view_through_lookback_window_days": "Visningsfönster (dagar)",
    "default_value": "Standardvärde",
    "default_currency_code": "Värdets valuta",
    "always_use_default_value": "Använd alltid standardvärde",
}


def render_conversion_review(plan):
    change, snapshot, review = plan["change"], plan["snapshot"], plan["review"]
    escape = lambda value: html.escape(str(value))
    names = {
        "create_conversion": "Skapa sekundär webbplatskonvertering",
        "update_conversion": "Ändra konverteringsinställningar",
        "set_conversion_primary": "Ändra primär/sekundär konvertering",
    }
    before = review["before"] if isinstance(review["before"], dict) else {}
    rows = []
    for key, value in review["after"].items():
        rows.append(
            f"<tr><th>{escape(LABELS.get(key, key))}</th><td>{escape(before.get(key, 'Ej skapad'))}</td><td>{escape(value)}</td></tr>"
        )
    campaigns = snapshot.get("potentially_affected_campaigns", [])
    campaign_text = (
        "; ".join(
            f"{item['name']} ({item['id']}, {item['status']})" for item in campaigns
        )
        or "Inga befintliga kampanjer"
    )
    expiry = datetime.fromtimestamp(plan["expires_at"], timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    return (
        f"<h2>{escape(names[change['action']])}</h2>"
        f"<p>Kundkonto: <strong>{escape(change['customer_id'])}</strong>. Konverterings-ID: {escape(change.get('conversion_action_id') or 'Tilldelas efter skapandet')}.</p>"
        "<table><thead><tr><th>Inställning</th><th>Före</th><th>Efter</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        "<p><strong>Påverkan:</strong> Ändringar av befintliga konverteringar kan påverka rapportering och automatisk budgivning direkt. Sekundära åtgärder kan fortfarande användas för budgivning via anpassade mål. Nya åtgärder skapas aktiverade men sekundära; konverteringsåtgärder har inget pausat läge.</p>"
        f"<p>Potentiellt berörda kampanjer (hela kundkontot, inte fastställd användning): {escape(campaign_text)}.</p>"
        f"<p>Anpassade mål i kontot: {len(snapshot.get('custom_goals', []))}. Kontrollera målens användning i Google Ads före godkännande.</p>"
        "<p>Inga taggar installeras, GA4-händelser importeras inte och inga faktiska konverteringar laddas upp.</p>"
        "<p>Återställning kräver en ny förhandsgranskning och ett nytt godkännande av tidigare inställningar. Historisk rapportering och budgivning kan inte garanteras återställda. Nya åtgärder kan lämnas sekundära och utan tagg; borttagning erbjuds inte.</p>"
        f"<p>Godkännandet går ut: {escape(expiry)}. Läs tillbaka inställningarna efter ändringen och verifiera mätningen separat.</p>"
    )
