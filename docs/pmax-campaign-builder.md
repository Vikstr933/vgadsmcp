# Bygg standard Performance Max

`writes_preview_create_pmax_campaign` förhandsvisar en komplett standardkampanj
med en tillgångsgrupp. Det passar till exempel biljettköp och besöksregistrering
för en mässa.

## Förbered kampanjen

1. Välj annonskonto och vid behov MCC för åtkomst. Ange geografi, språk,
   kampanjperiod, en riktig HTTPS-landningssida och EU-deklaration.
2. Välj en genomsnittlig dagsbudget eller en totalbudget för 3–90 kalenderdagar.
   Ange belopp i micros. 1 SEK motsvarar 1 000 000 micros.
3. Välj ett befintligt aktiverat konverteringsmål i kundkontot för exempelvis
   köp eller slutförd registrering. Verifiera mätningen före aktivering. Om
   budgivningen ska maximera konverteringsvärde, verifiera även värde och valuta.
4. Välj befintliga tillgångs-ID:n i samma Google Ads-konto. Använd
   `search_search` på `asset` med `asset.id`, `asset.name`, `asset.type`,
   `asset.image_asset.full_size.width_pixels`,
   `asset.image_asset.full_size.height_pixels` och vid behov
   `asset.youtube_video_asset.youtube_video_id`. Ladda upp nya medier i
   Google Ads tillgångsbibliotek innan du använder deras ID:n här.
5. Lägg till 3–15 korta rubriker, 1–5 långa rubriker och 2–5 beskrivningar.
   Inkludera en kort rubrik på högst 15 tecken och ett företagsnamn på högst
   25 tecken som följer Googles verifieringskrav. Välj en kvadratisk logotyp,
   minst en liggande bild (1,91:1) och minst en kvadratisk bild.
6. Välj godkänd YouTube-video eller ta ett uttryckligt beslut om att acceptera
   Googles möjliga videogenerering. Google kan skapa video när lämpliga egna
   format saknas. Verktyget stänger av URL-expansion, textanpassning,
   bildextraktion och de konfigurerade bild- och videoförbättringarna.

## Granska och genomför

Förhandsvisningen validerar förslaget via Google Ads utan att skapa kampanjen.
Godkännandesidan visar materialets ID:n, texter, budget, datum och konverteringar.
Efter ett separat webbgodkännande skapar `writes_execute_approved_plan`
kampanjen pausad och läser tillbaka resurser och kampanjmål. Befintliga mål med
exakt samma åtgärder kan återanvändas utan ändringar.

Vid status `unknown`, läs `writes_plan_status`. Om planen har sparade resurs-ID:n
kan `writes_reconcile_pmax_plan` kontrollera befintliga resurser och släppa
skrivlåset när verifieringen lyckas. Ange hela `plan_id` eller kundkontots
`customer_id`. Vid start samma dag kan Google spara ett annat klockslag på
det godkända datumet i kontots tidszon. Skicka aldrig skapandet igen efter ett
osäkert svar. Aktivering hanteras separat. För totalbudget sker den i Google Ads
eftersom MCP:ns befintliga aktiveringsverktyg kräver dagsbudget.

- [Skapa Performance Max via Google Ads API](https://developers.google.com/google-ads/api/performance-max/getting-started)
- [Googles materialkrav](https://developers.google.com/google-ads/api/performance-max/asset-requirements)
- [Googles tillgångsautomation](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings)
