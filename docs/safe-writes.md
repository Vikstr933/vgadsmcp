# Skrivfunktioner med dry run och separat godkännande

Den här utbyggnaden anropar Google Ads direkt från din egen server. Synter eller
någon annan annonsmellanhand används inte. Claude får informationen som verktygen
returnerar. Godkännandesidan och lagringen ligger i ditt Google Cloud-projekt.

## Funktioner

| Verktyg | Resultat av förhandsgranskning |
| --- | --- |
| `writes_policy_status` | Visar om konfigurationen är klar, utan hemligheter. |
| `writes_preview_pause` | Validerar pausning av en kampanj. |
| `writes_preview_budget` | Validerar ändring av en egen, daglig kampanjbudget. Delade budgetar blockeras. |
| `writes_preview_create_paused_search` | Validerar en **tom, pausad** sökkampanj och en separat budget. Inga annonser, sökord, annonsgrupper eller geografiska inställningar skapas. |
| `writes_preview_activation` | Validerar separat aktivering av en pausad kampanj, om kontopolicyn tillåter aktivering. |
| `writes_execute_approved_plan` | Genomför endast ett separat godkänt, oförändrat förslag en gång. |
| `writes_plan_status` | Visar den inloggade användarens förslag och resultat. |

Det finns inga verktyg för att godkänna, radera resurser eller skicka godtyckliga
API-operationer. Status för nya kampanjer är hårdkodad till `PAUSED`.

## Två lägen

`GOOGLE_ADS_MCP_WRITE_MODE=dry_run` är standard, även om variabeln saknas.
Alla förhandsgranskningar använder Googles `validate_only=True`. Inga Google Ads-
objekt skapas eller ändras, inga godkännanden sparas och genomförande blockeras.

`GOOGLE_ADS_MCP_WRITE_MODE=approval_required` låter förhandsgranskningen spara ett
30 minuter giltigt förslag **efter** godkänd Google-validering. Det returnerar en
länk till `/approvals/<plan-id>`. Du öppnar länken själv, granskar kontot och
före/efter, och skriver GODKÄNN. Därefter kan Claude genomföra förslaget. Servern
validerar igen, kontrollerar att tillståndet inte ändrats, tar ett beständigt lås,
skickar en enda ändring och läser tillbaka resultatet.

## Konfigurera Cloud Run

Den uppdaterade Dockerfilen installerar Firestore-stödet. Behåll nuvarande
OAuth-inställningar, `GOOGLE_ADS_MCP_BASE_URL`, port och containerkommando.

1. Ställ först in `GOOGLE_ADS_MCP_WRITE_MODE` till `dry_run`.
2. Lägg till `GOOGLE_ADS_MCP_WRITE_ACCOUNTS` som en JSON-sträng. Exemplet nedan
   använder ett fiktivt kundnummer. Ersätt med ditt kundnummer utan bindestreck,
   kontots valuta och din godkända budgetgräns. En krona är 1 000 000 micros:

   ```json
   {"1234567890":{"currency":"SEK","max_daily_budget_micros":1000000000,"allow_activation":false}}
   ```

   Budgetgränsen gäller per kampanjs genomsnittliga dagsbudget. Den är inte en
   kontogräns och inte ett hårt tak för faktisk dagskostnad. Ingen valutaomräkning
   görs. Fel valuta eller okänt konto blockeras.
3. Publicera och återanslut Claude så att verktygslistan uppdateras. Kontrollera
   `writes_policy_status`, följt av en förhandsgranskning. Den ska svara
   `validated_only`; verifiera i Google Ads att inget ändrats.
4. Skapa en Firestore-databas i **Native mode** i ditt eget projekt, lämpligen i
   samma EU-region som Cloud Run. Serverns runtime-servicekonto behöver
   `roles/datastore.user` för vald databas. Använd minsta tillåtna IAM-scope.
5. Sätt `GOOGLE_ADS_MCP_WRITE_FIRESTORE_PROJECT` till ditt projekt-ID och
   `GOOGLE_ADS_MCP_WRITE_FIRESTORE_DATABASE` till databas-ID:t, exempelvis
   `(default)`. Ingen minnes- eller filbaserad reserv används för skrivning.
6. Skapa ett separat slumpmässigt godkännandelösenord på minst 32 tecken i din
   lösenordshanterare. Spara samma värde i Google Secret Manager och referera till
   det från Cloud Run som `GOOGLE_ADS_MCP_APPROVAL_SECRET`. Runtime-servicekontot
   behöver `roles/secretmanager.secretAccessor` på just den hemligheten.
   **Lägg aldrig lösenordet i chatten, GitHub eller Claudes connector-headers.**
   Godkännandesidan använder webbläsarens inloggningsruta: användarnamn `owner`,
   lösenordet du sparade. MCP:s Google OAuth-token ger inte tillträde till sidan.
7. Aktivera Firestore TTL på fältet `delete_after` för collection group
   `ads_mcp_write_plans`. Normala förslag/resultat markeras för radering efter
   30 dagar. TTL-radering är asynkron. Poster som kan ha skickat en ändring med
   okänt resultat behålls tills ägaren stämt av dem. Lås har ingen automatisk TTL.
8. Sätt `GOOGLE_ADS_MCP_WRITE_MODE=approval_required` och publicera. Kör första
   genomförandet på en separat testkampanj efter granskning av exakt förslag.
   Kontots Google Ads-användare måste också ha rätt att göra ändringen.
9. För aktivering måste kontopolicyn uttryckligen ha `allow_activation:true`.
   Aktivering får alltid ett nytt förslag och nytt godkännande. Ett godkännande
   för skapande kan aldrig återanvändas för aktivering.

Använd samma Firestore-projekt och databas i alla revisioner/instanser. Vid byte
av databas: stäng först av skrivning, stäm av oklara körningar och gamla revisioner,
och skapa nya förslag. Återanvänd inte godkännanden från en tidigare driftsättning.
Google Ads klientbibliotek och Firestore använder olika identiteter: Ads använder
den inloggade OAuth-användaren; lagringen använder Cloud Run-servicekontot.

OAuth-state bör också lagras beständigt vid flera instanser eller nya revisioner.
Se huvud-README:ns `GOOGLE_ADS_MCP_STORAGE_*`, JWT-signering och kryptering. Den
lagringen är separat från ändringsförslagen och innehåller OAuth-hemligheter.

## Godkännande och datagränser

Godkännandesidan använder separat HTTP Basic-inloggning över HTTPS, samma ursprung
för formulär, HMAC-bundet CSRF-skydd, HTML-escaping, ingen tredjepartskod och
`Cache-Control: no-store`. GET gör aldrig ett godkännande. Ett godkännande gäller
exakt ett oförändrat förslag, en OAuth-användare och ett konto. Det separata
ägarlösenordet är avsett för en enda betrodd ägare; dela det inte med agenter eller
andra användare. Det ersätter inte en fleranvändarlösning med individuell attest.

Firestore lagrar minsta nödvändiga före/efter-data, kampanjnamn och ID, tidpunkter,
status och verifierat resultat. Det är annonsdata: begränsa läsning till servern
och ägaren. OAuth-token och godkännandelösenord lagras aldrig i förslaget. Google
Cloud sköter kryptering under transport och standardkryptering i vila. Aktivera
TTL och välj loggretention i ditt eget projekt. Ingen telemetritjänst läggs till.

## Fel, återställning och begränsningar

- Förändrade status-, budget-, valuta- eller delningsuppgifter gör förslaget ogiltigt.
  Förhandsgranska på nytt. Kontrollen är inte en transaktion med Google Ads UI;
  undvik samtidiga manuella ändringar under genomförandet.
- Aktivering kräver att ägaren granskar befintliga annonser, bud och inriktning i
  Google Ads. Förhandsgranskningen är ingen komplett annons- eller policyaudit.
- Vid timeout, avbrott eller misslyckad återläsning efter ett skrivförsök blir
  resultatet `unknown` (eller kvar i `executing` vid processkrasch). Kontot förblir
  låst i Firestore och får inte skrivas igen automatiskt.
- Ägaren stämmer av plan-ID:t mot Google Ads och dess ändringshistorik. Dokumentera
  resultatet i planposten, avsluta som `verified` eller `reconciled_no_write`, sätt
  åter `delete_after` enligt retentionen, och ta först därefter bort motsvarande
  dokument i `ads_mcp_write_locks`. Detta är en manuell driftåtgärd i Google Cloud,
  inte ett MCP-verktyg. Vid osäkerhet: behåll låset.
- En verifierad plan skickas aldrig igen. En ny plan kräver ett nytt godkännande.
- Återställ en budget med ett nytt godkänt förslag. Återställ en aktivering genom
  en godkänd pausning. Skapade kampanjer lämnas pausade; borttagning stöds inte.
- Nödstopp: sätt `GOOGLE_ADS_MCP_WRITE_MODE=dry_run` och skicka all trafik till
  den revisionen. Redan skickade Google-anrop kan inte återkallas med denna flagga.
- Avinstallation: stäng av skrivning, stäm av lås, ta bort `writes`-namnrymden och
  hemlighetsreferensen. Radera journaldata först efter vald retention.

## Verifiering

Lokala tester använder bara syntetiska data och mockade Google Ads-anrop. De
bevisar inte att ett visst riktigt konto har behörighet eller att en viss
kampanjtyp kan ändras. Cloud Run, Firestore IAM, webbläsarinloggning och ett
uttryckligen godkänt test måste verifieras efter driftsättning.

```text
python -m unittest discover --buffer -s tests -p '*_test.py'
python -m unittest tests.smoke.smoke_test
```

Källor: [Google Ads mutates](https://developers.google.com/google-ads/api/docs/mutating/overview),
[validate_only](https://developers.google.com/google-ads/api/docs/best-practices/testing),
[pausad kampanjskapning](https://developers.google.com/google-ads/api/samples/add-campaigns),
[Firestore-transaktioner](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions).
