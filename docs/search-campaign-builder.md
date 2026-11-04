# Kompletta sökkampanjer genom egen MCP

Använd `writes_preview_create_search_campaign` för hela kampanjer.
`writes_preview_create_paused_search` finns kvar för bakåtkompatibilitet och skapar
fortfarande bara en tom kampanj.

## Arbetsgång i Claude

1. Läs kundkontot, befintliga kampanjer, konverteringar och relevanta söktermer.
   Använd kundkontot som `customer_id` och ert tillåtna MCC som `login_customer_id`
   när åtkomsten ska gå genom MCC. Direktåtkomst följer befintlig kundpolicy.
2. Bekräfta budget, årtal och datum, budgivning, landningssidor, geografiska områden,
   språk, konverteringsåtgärder samt EU-politisk deklaration. Uppgifter som saknas
   ska inte fyllas med gissningar. Hämta riktiga geografiska och språkliga ID:n med
   läsverktygen. En godkänd budgetgräns är inte i sig ett budgetbeslut för kampanjen.
3. Skicka ett strukturerat `campaign`-objekt till det nya verktyget. Google validerar
   skapandet med `validate_only`; inget skapas i annonskontot vid förhandsgranskning.
4. Granska hela paketet på den separata godkännandesidan: samtliga rubriker,
   beskrivningar, sökord, mål, platser, länkar och budget visas. Godkänn själv.
5. `writes_execute_approved_plan` skapar paketet i pausat läge, ställer in målen
   och läser tillbaka resurserna. `verified` betyder att återlästa inställningar
   matchar. Det betyder inte att Googles annonsgranskning eller mätning är klar.
6. Kontrollera landningssidor, mätning, fakturering och kontots automation före
   start. Aktivering kräver ett separat förslag, en tillåtande kontopolicy och
   ett nytt godkännande.

Återanslut/uppdatera anslutningen i Claude efter driftsättning om det nya verktyget
inte syns. `writes_policy_status.search_campaign_builder_version` ska vara `1`.
Ingen ny hemlighet, ny tjänst eller ändring av befintliga åtkomstgränser behövs.

## Vad paketet stöder

- En kampanj och en egen daglig budget per godkännande. Belopp i micros:
  `200000000` motsvarar 200 av kontots valuta. Befintliga valuta- och budgetgränser
  gäller även detta verktyg. Budget är genomsnittlig dagsbudget, inte ett totalt
  kampanjtak eller hårt tak för faktisk dagskostnad.
- Explicit manuell CPC med bud per annonsgrupp, maximera konverteringar eller
  maximera konverteringsvärde. Ingen automatisk växling av budstrategi eller
  påhittad mål-CPA/ROAS. Välj manuell CPC när kunden inte tillåter automatisk budgivning.
- Start- och slutdatum inklusive år, tolkade i annonskontots tidszon. Starttid
  00:00:00 och sluttid 23:59:59. Startdatum i det förflutna blockeras.
- Endast Google Sök; sökpartner och Display av. Geografisk inkludering och
  exkludering med `PRESENCE`, samt explicita språk.
- Upp till 20 annonsgrupper, totalt 500 positiva/negativa sökord. Endast exakt
  matchning och frasmatchning. Negativa sökord på kampanj- och annonsgruppsnivå.
- 1–3 responsiva sökannonser per grupp; varje annons 3–15 rubriker och 2–4
  beskrivningar, med lokala längdkontroller och Googles validering. Slutlig HTTPS-URL,
  valfria visningssökvägar och statiskt URL-suffix. Ingen dynamisk text, bred matchning,
  schemaval för timmar eller godtyckliga API-operationer.
- Webbplatslänkar och informationstillägg. Kampanjen ärver inte kontots tillägg
  av just dessa två typer. AI Max, textanpassning och expansion av slutlig URL är av.
  Andra kontoomfattande automatiska tillägg och automatiskt tillämpade rekommendationer
  stängs inte av här; kontrollera dem i Google Ads. Responsiva annonser kombinerar
  fortfarande godkända texter automatiskt.
- Explicit lista över befintliga, aktiverade konverteringsåtgärder som ägs av
  kundkontot. Ett nytt anpassat mål knyts till den nya kampanjen, vars standardmål
  görs icke-budgivande. **Även sekundära åtgärder i ett anpassat mål används för
  budgivning.** Kontots primärflaggor och befintliga kampanjmål ändras inte.
  Konverteringar som ägs gemensamt av ett MCC stöds inte i denna version.

## Två steg och osäkra resultat

Google skapar `CampaignConversionGoal` och `ConversionGoalCampaignConfig`
automatiskt. Dessa kan uppdateras men inte skapas direkt av klienten.
Därför skapas hela paketet inklusive ett nytt anpassat mål först i en atomisk
`GoogleAdsService.Mutate` med `partial_failure=False`. Kampanjen är alltid `PAUSED`.
Annonsgrupper och annonser är `ENABLED` under denna pausade förälder, vilket gör
att de kan börja leverera efter en separat aktivering av kampanjen.

Resurs-ID:n sparas i förslagets `progress` innan steg två. Därefter kontrolleras
att kampanjen fortfarande är pausad och att måltyperna stämmer. Måländringarna
valideras och genomförs i ett andra atomiskt anrop. Alla uttryckligen satta fält,
resurser och konverteringsval läses tillbaka innan resultatet blir `verified`.
Inga automatiska omsändningar görs.

Om ett anrop förlorar svaret eller steg två/återläsningen misslyckas blir status
`unknown`, kontolåset behålls och befintliga resurs-ID:n finns under `progress`
om det första svaret hann sparas. Skapa inte ett nytt försök blint. Läs
`writes_plan_status`, kontrollera kampanjen och målen i Google Ads och stäm av
låset enligt rutinen i [safe-writes.md](safe-writes.md). En identisk kampanjtitel
blockeras även före skapandet. Detta ersätter inte låset eller manuell avstämning
efter ett förlorat svar.

## Begränsningar och verifiering

Verktyget skapar inte ett nytt Google Ads-konto, ordnar inte fakturering, installerar
inte taggar och kontrollerar inte evenemangsfakta. Separata
[sökordsplaneringsverktyg](keyword-planning.md) kan användas före kampanjförslaget.
Befintliga kampanjer pausas inte automatiskt.

Automatiserade tester använder syntetiska konton, riktiga Google Ads-protobufar
och simulerade transportsvar. De kontrollerar validering, budgetgränser, paketets
fält, oförändrad kontoåtkomst, granskningssidan, tvåstegsflödet, lås och återläsning.
De skapar inga kampanjer och bevisar inte Google Ads-behörighet eller godkännande
för ett riktigt konto. Första riktiga förhandsgranskningen ska valideras med Googles
API, följt av ägargranskning inför faktisk skapning.

Officiella underlag, kontrollerade 2026-11-04:

- [Skapa Search-kampanjer](https://developers.google.com/google-ads/api/docs/campaigns/search-campaigns/getting-started)
- [Kampanjens konverteringsmål](https://developers.google.com/google-ads/api/docs/conversions/goals/campaign-goals)
- [Automatiskt skapade målobjekt](https://developers.google.com/google-ads/api/docs/conversions/goals/overview)
- [Inställningar för automatiska tillgångar](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings)
- [AI Max för Search](https://developers.google.com/google-ads/api/docs/campaigns/ai-max-for-search-campaigns/getting-started)
