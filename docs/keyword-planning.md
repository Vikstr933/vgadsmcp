# Sökordsplanering

Tre läsverktyg använder Google Ads Keyword Planner direkt från denna server.
Samma Google-inloggning och kundåtkomst används som för övriga läsverktyg.
Ingen kampanj, budget eller beständig KeywordPlan skapas. Planeringsverktygen
är separata från skrivpolicyn och godkännandesidan; en prognosbudget är ett
räkneexempel och ger ingen rätt att spendera pengar.

| Verktyg | Användning |
|---|---|
| `keyword_planning_generate_ideas` | Nya idéer från upp till 20 startord, en offentlig HTTPS-URL eller båda. En begränsad resultatsida med högst 500 idéer. `has_more` visar om fler finns. |
| `keyword_planning_historical_metrics` | Historisk sökvolym per månad, konkurrens och historiska budintervall för upp till 500 ord. Närliggande varianter kan slås ihop av Google. |
| `keyword_planning_forecast` | Uppskattad trafik/kostnad för ett valt framtida scenario med datum, budget och budstrategi. Inga ändringar i annonskontot. |

## Inmatning och arbetsgång

Varje verktyg tar ett strukturerat `request`-objekt. Ange:

- Kundkontots `customer_id`, och `login_customer_id` om åtkomsten går via MCC.
  Använd klientkontot, inte MCC:t, som kundkonto för planeringen.
- `geo_target_ids`: riktiga geografiska ID:n, högst tio. Slå först upp regionerna
  via läsverktygen och bekräfta namn/region. Ett ortsord i sökordet ersätter inte
  geografiskt urval.
- `language_id`: ett uttryckligt språk-ID. Jämför olika språk i separata körningar.
- `keywords` och/eller `url` för idéer; `keywords` för historik.
  `start_month` och `end_month` kan anges som `YYYY-MM` för en historisk period.
  Utan dem används Googles standardperiod. Endast avslutade månader inom fyra år
  accepteras lokalt; Googles datatillgång avgör vilka månader som faktiskt returneras.

För en besöksmässa: separera varumärkesord, generella mässord och ort. Kör separat
research för olika städer även om mässorna delar annonskonto. Använd den specifika
stadens landningssida som start-URL. Granska idéernas relevans innan de blir
kampanjsökord. Skapa inte automatiskt negativa sökord från en generell mall.

Idéer och historik använder uttryckligen `GOOGLE_SEARCH`. De returnerar kundkontots
valuta, tidszon, geografiska ID:n, språk, hämttid och den efterfrågade perioden.
Månadsraderna visar vilken historik som faktiskt fanns. Saknade värden är `null`,
inte noll. Budbelopp returneras i micros: 1 000 000 micros = 1 valutaenhet.

Historikverktyget returnerar `close_variants` och inmatade ord som inte kunde
matchas mot svaret. Summera inte samma eller överlappande efterfrågan från flera
körningar. Budintervallen är historiska uppskattningar, inte fastställda CPC-bud.

## Prognosens begränsningar

Ange `start_date`/`end_date` (`YYYY-MM-DD`), framtida period inom ett år,
`daily_budget_micros`, ord med `EXACT`/`PHRASE` samt budstrategin:

- `MANUAL_CPC` kräver även `max_cpc_bid_micros`.
- `MAXIMIZE_CONVERSIONS` använder budgeten som scenario för daglig målkostnad.

API v25:s prognosmodell har ingen väljare för nätverk, inga negativa sökord,
ingen annonstext och inget val av kampanjens specifika konverteringsmål.
Svaret märks därför `API_DEFINED_NO_NETWORK_SELECTOR`; det får inte beskrivas
som en exakt simulering av kampanjbyggaren. Prognosen returnerar bara de mått
Google lämnar, exempelvis klick, kostnad, CPC, konverteringar och CPA. Denna
version av modellen har inget impressionsfält. Mått kan saknas beroende på
budstrategi och data. Prognoser är inga garantier.

## Åtkomst och drift

Namespace `keyword_planning` är aktiverat i paketets standardkonfiguration.
Vid egen `tools_config.yaml` behöver namespace också aktiveras där.
Uppdatera anslutningen i MCP-klienten efter driftsättning för att läsa in nya verktyg.

**Basic eller Standard API access krävs. Explorer blockerar KeywordPlanIdeaService**,
vilket berör alla tre verktygen. Vid Explorer behöver projektets API-åtkomst
uppgraderas genom Googles ansökningsflöde. En OAuth-inloggning eller högre
användarbehörighet på annonskontot ersätter inte detta krav.
Googles API-åtkomstnivå och kontobehörigheter avgör om planering är tillgänglig.
Fungerande rapportläsning bevisar inte att planeringsanrop är tillåtna.
Kvoter eller behörighetsfel ska visas som fel, aldrig ersättas med påhittade volymer.
Anropen är begränsade i storlek och gör inga automatiska retries. Om idélistan
trunkeras, gör ett mer fokuserat urval i nästa anrop.

Tester använder riktiga Google-protobufar och simulerade svar; ingen kunddata
eller produktionsinloggning används. Första autentiserade körningen i klienten
behöver fortfarande verifiera faktisk kontoåtkomst och Googles tillgängliga data.

Officiella källor, kontrollerade 2026-11-04:

- [Keyword planning](https://developers.google.com/google-ads/api/docs/keyword-planning/overview)
- [API access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)
- [Keyword ideas](https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas)
- [Historical metrics](https://developers.google.com/google-ads/api/docs/keyword-planning/generate-historical-metrics)
- [Forecast metrics](https://developers.google.com/google-ads/api/docs/keyword-planning/generate-forecast-metrics)
