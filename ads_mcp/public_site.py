"""Public, static app information. Does not read credentials or account data."""

from base64 import b64decode

from starlette.responses import Response

from ads_mcp.product_icons import PRODUCTS, product_icon_assets

PORTAL_CSS = """
:root { color-scheme: light; --ink:#192e28; --muted:#4b6257; --paper:#f5f7f2; --line:#d7e2d9; --accent:#17523d; font: 16px/1.55 system-ui,sans-serif; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:var(--paper); }
.shell { width:min(70rem, calc(100% - 2.5rem)); margin-inline:auto; }
header { background:#fff; border-bottom:1px solid var(--line); }
.top, nav, footer, .brand, .identity, .copy, .hero-channels div { display:flex; align-items:center; gap:0.85rem; flex-wrap:wrap; }
.top { justify-content:space-between; min-height:4.7rem; padding-block:.7rem; }
.brand { font-weight:750; text-decoration:none; color:var(--ink); }
.mark { display:inline-grid; place-items:center; width:2.2rem; height:2.2rem; border-radius:.6rem; font-weight:800; color:white; background:var(--accent); }
a { color:var(--accent); text-underline-offset:.18em; }
a:focus-visible, button:focus-visible, input:focus-visible, summary:focus-visible { outline:3px solid #287b63; outline-offset:3px; }
.skip { position:absolute; top:-5rem; }
.skip:focus { top:0; background:#fff; padding:.75rem; z-index:1; }
main { min-height:62vh; padding-block:2.5rem; }
h1, h2 { line-height:1.13; margin:0 0 .75rem; letter-spacing:-.025em; }
h1 { font-size:clamp(2rem,4vw,3rem); }
h2 { font-size:1.3rem; }
p { margin:.6rem 0 1rem; }
section { border:1px solid var(--line); border-radius:1rem; padding:1.4rem; margin-block:1rem; background:#fff; }
.hero { background:#132f27; color:#f8fdf8; padding:clamp(1.5rem,5vw,4.2rem); display:grid; grid-template-columns: 3fr 2fr; align-items:center; gap:2.5rem; }
.hero h1 { font-size:clamp(2.35rem,5.75vw,4.25rem); max-width:13ch; }
.hero p { max-width:40ch; color:#d5e8dc; }
.hero .btn { background:#cdf8c6; color:#123728; margin-top:.4rem; }
.hero .btn + p { font-size:.875rem; margin-bottom:0; }
.hero-channels { display:grid; gap:.625rem; }
.hero-channels div { border:1px solid #527165; border-radius:.75rem; background:#224036; padding:.7rem; }
.product-icon { display:inline-grid; place-items:center; flex:0 0 auto; width:2.65rem; height:2.65rem; border:1px solid #dce8df; border-radius:.7rem; background:#fff; }
.product-icon img { display:block; object-fit:contain; }
.channel-trademarks { display:block; color:var(--muted); font-size:.75rem; margin-top:.75rem; }
.grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1rem; }
.admin-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
.grid section { margin:0; }
.channel-card { display:flex; flex-direction:column; gap:.9rem; min-width:0; padding:1.25rem; }
.channel-card__heading { display:grid; gap:.75rem; justify-items:start; }
.channel-card__heading h2 { margin:0; font-size:1.2rem; }
.channel-card__body { min-width:0; color:var(--muted); font-size:.92rem; }
.channel-card__body p { margin:0; }
.channel-card__body > :not(:first-child) { margin-top:.65rem; }
.channel-state { display:inline-block; border-radius:999px; padding:.2rem .65rem; background:#eef1ee; color:#3e5149; font-size:.82rem; font-weight:650; }
.channel-state--good { background:#e6f4e9; color:#24543d; }
.channel-state--warning { background:#f9eee0; color:#754524; }
.channel-accounts summary { margin-top:0; }
.channel-accounts ul { padding-inline-start:1.15rem; overflow-wrap:anywhere; }
.channel-card__primary { margin-top:auto; }
.channel-card__primary :is(.btn,button) { display:block; width:100%; min-height:2.8rem; text-align:center; }
.channel-card__secondary { min-height:1.4rem; font-size:.9rem; text-align:center; }
.channels-grid { align-items:start; }
.channel-card__primary .disconnect { color:var(--accent); background:white; border:1px solid #8ba99b; }
.channel-card__primary .disconnect:hover { background:#eff6f1; }
@media (min-width:841px) {
  .channel-card__body { min-height:3.8rem; }
}
@media (max-width: 840px) {
  .channels-grid { grid-template-columns:1fr; }
  .channel-card__secondary:empty { display:none; }
}
.getting-started { background:transparent; border:0; padding:1.5rem 0 0; }
.getting-started ol { padding-left:1.6rem; column-gap:3rem; }
.getting-started li::marker { font-weight:700; color:var(--accent); }
.btn, button { display:inline-block; background:var(--accent); color:#fff; border:0; border-radius:.55rem; padding:.65rem .95rem; font:inherit; font-weight:650; cursor:pointer; text-decoration:none; }
.btn:hover, button:hover { filter:brightness(1.13); }
.subtle { background:transparent; color:var(--accent); padding:.25rem .4rem; }
[hidden] { display:none !important; }
.badge { border-radius:2rem; background:#e6efe4; color:#234539; padding:.2rem .65rem; font-size:.875rem; }
summary { cursor:pointer; margin-top:.4rem; color:var(--accent); }
input[readonly] { flex:1 1 17rem; min-width:0; max-width:100%; padding:.65rem; border:1px solid #9dae9f; border-radius:.45rem; color:var(--ink); font:inherit; }
#copy-status:not(:empty) { margin-top:.5rem; }
.table-wrap { overflow:auto; }
.draft-payload { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--paper); padding:1rem; }
table { width:100%; border-collapse:collapse; text-align:left; }
th,td { padding:.65rem; border-bottom:1px solid var(--line); }
footer { border-top:1px solid var(--line); padding-block:1.25rem 2rem; font-size:.9rem; }
@media (max-width:680px) { .hero,.grid,.admin-grid,.channels-grid { grid-template-columns:1fr; } .hero { gap:1.25rem; } main { padding-block:1.4rem; } }
"""

PORTAL_JS = """
const copy = document.getElementById("copy-address");
const address = document.getElementById("mcp-address");
const status = document.getElementById("copy-status");
if (copy && address && status) {
  copy.hidden = false;
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(address.value);
      status.textContent = "Adressen är kopierad.";
    } catch {
      address.select();
      status.textContent = "Adressen är markerad. Kopiera den från textfältet.";
    }
  });
}
"""

ASSETS = {
    **product_icon_assets(),
    "/favicon.ico": (
        b64decode(
            "AAABAAEAICAAAAEAIAAkAQAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAgAAAAIAgGAAAAc3p69AAAAOtJREFUeJxjYEAD3Jpy9eJBtv9pgUFmo9sHB6wi/Pa0shgdg+waMMuxOoLelsMwzeOcqDQxUJbDQ2HUAfgku1fM+48MiDUUGYDMoCgESDGMHEcTdMDRy+dIMhAZgPRS7IDA2lwUQ0F8aqglKREihwI+XxGrjmQHoMcrNp+h+56Y9EK0A4iJW1LTCskOIJS6yfE9SQ7AZwm55QXJDsBlEbm+J9kB2BIaMQmUag5AT2wgNjlZjyIHoIcCJb4nywHooUCJ78l2AHq8k5P4KHIACKMDcs0Z3A2SkeGAAW+WD3jHZFB0zQa8czqQ3XMASHhJdv03xdkAAAAASUVORK5CYII="
        ),
        "image/x-icon",
    ),
    "/portal.css": (PORTAL_CSS, "text/css"),
    "/portal.js": (PORTAL_JS, "text/javascript"),
    "/privacy.html": (
        '<!doctype html>\n<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Integritetspolicy · Viks Annonsverktyg</title><meta name="description" content="Information om Viks Annonsverktyg och anslutningarna till Google Ads, LinkedIn Ads och Google Tag Manager."><link rel="stylesheet" href="/style.css"><meta name="theme-color" content="#12382f"></head>\n<body><a class="skip" href="#main">Hoppa till innehåll</a><header><a class="brand" href="/"><span class="mark" aria-hidden="true">V</span>Viks Annonsverktyg</a><nav aria-label="Huvudmeny"><a href="/">Om appen</a><a href="/privacy.html">Integritet</a><a href="/terms.html">Villkor</a></nav></header><main id="main">\n<article class="document"><p class="eyebrow">DINA UPPGIFTER</p><h1>Integritetspolicy</h1><p class="lead">Den här policyn beskriver hur Viks Annonsverktyg behandlar uppgifter när du använder tjänsten med Google Ads, LinkedIn Ads eller Google Tag Manager.</p><p class="date">Senast uppdaterad: 27 november 2026</p>\n<h2>1. Appen och kontakt</h2><p>Viks Annonsverktyg är en fristående, självhostad MCP-anslutning. För frågor om drift, behandling av uppgifter eller radering, kontakta appens operatör på <a href="mailto:viktorstrindin93@gmail.com">viktorstrindin93@gmail.com</a>. När ett kundkonto används ansvarar även den organisation som gett användaren uppdraget för sin behandling av kundens uppgifter.</p>\n<h2>2. Vilka uppgifter behandlas?</h2><ul><li><strong>Google-identitet:</strong> grundläggande profiluppgifter, e-postadress och identifierare som används för att autentisera dig.</li><li><strong>Anslutningsuppgifter:</strong> OAuth-token och sessionsuppgifter som behövs för att upprätthålla anslutningen. Appen tar inte emot ditt Google-lösenord.</li><li><strong>Google Ads-data:</strong> uppgifter som efterfrågas genom verktygen, till exempel kontoidentifierare, kontonamn, kampanjer, budgetar och resultatmått såsom kostnad, klick och konverteringar.</li><li><strong>Ändringsunderlag:</strong> planerade ändringar, godkännanden och körningsstatus när skrivfunktioner används.</li><li><strong>Driftuppgifter:</strong> tekniska loggar, felinformation och uppgifter om anrop som behövs för säkerhet och felsökning.</li></ul>\n<h2>3. Varför används uppgifterna?</h2><p>Uppgifterna används för att identifiera behöriga användare, hämta begärda rapporter, besvara användarens frågor och förbereda eller genomföra tillåtna ändringar. Anslutningsuppgifter används även för att hålla inloggningen fungerande. Driftuppgifter används för att underhålla tjänsten och utreda fel.</p><p>Du ska endast ansluta konton och begära data som du har rätt att använda. Denna anslutning är inte avsedd för att samla in känsliga personuppgifter eller exportera kundlistor.</p>\n<h2>4. Var behandlas och lagras uppgifterna?</h2><p>MCP-servern körs i operatörens Google Cloud-projekt. Firestore används för beständig anslutningsinformation och ändringsunderlag när detta är konfigurerat. Hemligheter hanteras via Google Cloud Secret Manager. Åtkomst styrs med behörigheter i Google Cloud. OAuth-lagringen har stöd för kryptering av innehållet.</p><p>Information som ett MCP-verktyg returnerar skickas till den anslutna klienten. Om du använder Claude behandlas den informationen även av Anthropic enligt villkoren och datainställningarna för ditt Claude-konto. Det innebär att självhosting inte håller alla verktygsresultat utanför AI-leverantören.</p>\n<h2>5. Delning och användning av Google-data</h2><p>Uppgifter behandlas av Google för Google Ads och infrastrukturen samt av den AI-klient och leverantör som användaren väljer för den begärda funktionen. Viks Annonsverktyg säljer inte Google-användardata och använder dem inte för annonsinriktning eller för att träna generella AI-modeller. AI-klientens egen behandling styrs av dess avtal och inställningar.</p><p>Viks Annonsverktyg:s användning och överföring av information från Google API:er ska följa <a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, inklusive kraven för Limited Use. Operatören får endast använda uppgifterna för den funktion du begär och för nödvändig drift, säkerhet och support.</p>\n<h2>6. Lagringstid och radering</h2><p>Anslutningsuppgifter behövs medan anslutningen används. Du kan återkalla åtkomsten i <a href="https://myaccount.google.com/connections">ditt Google-kontos anslutningar</a> och koppla från MCP-anslutningen i din AI-klient. Återkallad åtkomst stoppar framtida auktoriserad hämtning men raderar inte automatiskt uppgifter som redan har sparats i en chatt eller i serverns lagring.</p><p>Kontakta operatören för att begära radering av lagrade anslutningsuppgifter och ändringsunderlag. Ändringsunderlag har normalt en teknisk raderingsmarkering efter 30 dagar; automatisk radering förutsätter att databasens TTL-funktion är aktiverad. Underlag för oklara eller misslyckade ändringar kan behöva behållas för utredning. Driftloggar följer projektets inställda lagringstider. Chattar och data hos din AI-leverantör hanteras separat där.</p>\n<h2>7. Dina val och rättigheter</h2><p>Du kan avstå från att ansluta ett konto, återkalla åtkomst eller begära information, rättelse och radering genom kontaktadressen ovan. Ange vilket konto och vilken begäran det gäller, men skicka inte lösenord eller token. Identiteten kan behöva kontrolleras innan uppgifter lämnas ut eller raderas. Om dataskyddslagstiftning är tillämplig kan du även kontakta relevant tillsynsmyndighet, exempelvis Integritetsskyddsmyndigheten i Sverige.</p>\n<h2>8. Den här informationswebbplatsen</h2><p>Webbplatsen visar endast appinformation, policy och villkor. Den innehåller inga annonsrapporter, inga formulär för Google-inloggning och ingen egen analys- eller reklamkod. Den är hostad i operatörens Google Cloud-projekt. Hosting-leverantören kan behandla tekniska besöksuppgifter för leverans, drift och säkerhet enligt sina villkor.</p>\n<h2>9. Ändringar</h2><p>Policyn uppdateras när appens funktioner eller behandling förändras. Datumet ovan visar den senaste versionen. Frågor skickas till <a href="mailto:viktorstrindin93@gmail.com">viktorstrindin93@gmail.com</a>.</p></article>\n</main><footer><a class="brand" href="/">Viks Annonsverktyg</a><p>Ett fristående verktyg för annonsering och spårning.</p><nav aria-label="Sidfot"><a href="/privacy.html">Integritetspolicy</a><a href="/terms.html">Användningsvillkor</a><a href="mailto:viktorstrindin93@gmail.com">Kontakt</a></nav><small>Uppdaterad 27 november 2026</small></footer></body></html>',
        "text/html",
    ),
    "/terms.html": (
        '<!doctype html>\n<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Användningsvillkor · Viks Annonsverktyg</title><meta name="description" content="Information om Viks Annonsverktyg och anslutningarna till Google Ads, LinkedIn Ads och Google Tag Manager."><link rel="stylesheet" href="/style.css"><meta name="theme-color" content="#12382f"></head>\n<body><a class="skip" href="#main">Hoppa till innehåll</a><header><a class="brand" href="/"><span class="mark" aria-hidden="true">V</span>Viks Annonsverktyg</a><nav aria-label="Huvudmeny"><a href="/">Om appen</a><a href="/privacy.html">Integritet</a><a href="/terms.html">Villkor</a></nav></header><main id="main">\n<article class="document"><p class="eyebrow">ANVÄNDNING AV APPEN</p><h1>Användningsvillkor</h1><p class="lead">För behöriga användare av Viks Annonsverktyg.</p><p class="date">Senast uppdaterad: 27 november 2026</p>\n<h2>1. Ändamål och behörighet</h2><p>Viks Annonsverktyg förmedlar begäranden mellan en MCP-kompatibel klient och Google Ads, LinkedIn Ads eller Google Tag Manager. Använd bara konton och containrar som du har rätt att företräda. Åtkomst förutsätter behörighet i den anslutna tjänsten och att Viks åtkomstregler tillåter åtgärden.</p>\n<h2>2. Läsning och ändringar</h2><p>Rapporter och förslag ska granskas innan de ligger till grund för beslut. Tillgängliga skrivfunktioner beror på serverns konfiguration. Validering eller en förhandsvisning skapar i sig ingen kampanj. När en ändring godkänns och genomförs kan den påverka annonskontot. I Google Tag Manager lägger Viks godkända ändringar i en separat arbetsyta. Du testar och publicerar arbetsytans innehåll från Google Tag Manager.</p><p>Nya kampanjer som skapas av appens funktion för pausade sökkampanjer skapas pausade. Aktivering hanteras separat. Granska konto, belopp, valuta, innehåll och förväntad effekt före varje godkännande. Googles regler för genomsnittlig dagsbudget och faktisk debitering gäller även när appen har egna budgetgränser.</p>\n<h2>3. Användarens ansvar</h2><p>Du ansvarar för dina instruktioner, godkännanden, annonsinnehåll och kostnader på de konton du förvaltar. Skydda inloggning och godkännandeuppgifter. Använd inte appen för att kringgå behörigheter eller bryta mot den anslutna tjänstens policyer eller tillämplig lag.</p>\n<h2>4. Tillgänglighet och begränsningar</h2><p>Appen tillhandahålls utan löfte om oavbruten tillgänglighet eller viss annonseringseffekt. API:er, behörigheter och externa tjänster kan ändras. Vid ett oklart körningsresultat ska kontot och statusen kontrolleras innan samma ändring skickas igen. Dessa villkor begränsar inte rättigheter som inte får avtalas bort enligt tillämplig lag.</p>\n<h2>5. Externa tjänster och integritet</h2><p>Google Ads, LinkedIn Ads, Google Tag Manager, Google Cloud och vald AI-klient omfattas av sina respektive villkor. Läs <a href="/privacy.html">integritetspolicyn</a> för information om behandling av uppgifter.</p>\n<h2>6. Avsluta användningen och få hjälp</h2><p>Du kan sluta använda anslutningen, koppla från den i AI-klienten och återkalla åtkomsten i den anslutna tjänsten. Support och frågor om villkoren: <a href="mailto:viktorstrindin93@gmail.com">viktorstrindin93@gmail.com</a>.</p></article>\n</main><footer><a class="brand" href="/">Viks Annonsverktyg</a><p>Ett fristående verktyg för annonsering och spårning.</p><nav aria-label="Sidfot"><a href="/privacy.html">Integritetspolicy</a><a href="/terms.html">Användningsvillkor</a><a href="mailto:viktorstrindin93@gmail.com">Kontakt</a></nav><small>Uppdaterad 27 november 2026</small></footer></body></html>',
        "text/html",
    ),
    "/style.css": (
        '\n:root{--ink:#17352e;--muted:#50685f;--paper:#f6f7f1;--line:#d9dfd3;--accent:#d1ed81}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.65}a{color:inherit;text-underline-offset:4px}a:hover{color:#487027}a:focus-visible{outline:3px solid #6e911f;outline-offset:5px}.skip{position:absolute;top:-80px;left:16px;background:white;padding:10px;z-index:3}.skip:focus{top:12px}header,main,footer{max-width:1200px;margin:auto}header{padding:28px 40px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);gap:24px}.brand{display:flex;align-items:center;gap:10px;font-weight:750;letter-spacing:-.5px;text-decoration:none}.mark{display:grid;place-items:center;background:var(--ink);color:var(--accent);border-radius:10px;width:34px;height:34px}nav{display:flex;gap:24px;font-size:14px}nav a{text-decoration:none}main{padding:0 40px}.hero{padding:86px 0 76px;display:grid;grid-template-columns:1.2fr 1fr;gap:72px;align-items:center}.eyebrow{font-size:11px;font-weight:750;letter-spacing:2px;margin:0 0 20px;color:var(--muted)}h1,h2{line-height:1.12;letter-spacing:-1.7px;font-weight:620}h1{font-size:clamp(36px,4.5vw,57px);margin:0 0 28px}.lead{color:var(--muted);font-size:18px;line-height:1.75}.actions{display:flex;align-items:center;gap:24px;flex-wrap:wrap;margin-top:28px;font-size:14px}.button{background:var(--ink);color:white;border-radius:8px;padding:14px 18px;text-decoration:none;display:flex;gap:18px}.button:hover{background:#284e40;color:white}.flow{background:var(--ink);color:#f3f8eb;border-radius:22px;padding:30px}.flow .eyebrow{color:var(--accent)}.flow>div{display:flex;gap:18px;padding:20px 0;border-bottom:1px solid #496155}.flow b{color:var(--accent);font-size:12px;padding-top:4px}.flow span{font-size:21px;line-height:1.4}.flow small{display:block;font-size:13px;color:#c1d0c6;margin-top:7px}.flow-note{font-size:12px;color:#c1d0c6;margin:22px 0 0}.overview{padding:44px 0 64px;border-top:1px solid var(--line)}.columns{display:grid;grid-template-columns:repeat(3,1fr);gap:40px}.number{font-size:11px;color:var(--muted);letter-spacing:1px}h2{font-size:27px;letter-spacing:-.7px}.columns p,.contact p{color:var(--muted)}.note{border-radius:16px;background:#e8eedc;padding:36px;display:grid;grid-template-columns:1fr 1fr;gap:55px}.note h2{margin:0}.note p:last-child{margin:0}.contact{padding:56px 0 72px}.contact h2{font-size:24px}footer{padding:32px 40px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}footer nav{margin:20px 0}footer small{font-size:11px}.document{max-width:780px;padding:70px 0 80px;margin:0 auto}.document h1{font-size:48px}.document h2{font-size:25px;margin:40px 0 16px}.document p,.document li{color:#40594f}.document li{margin:12px 0}.date{font-size:13px}.document .lead{font-size:19px}.document a{overflow-wrap:anywhere}@media(max-width:760px){header{padding:20px;flex-wrap:wrap}nav{gap:18px}main{padding:0 22px}.hero{grid-template-columns:1fr;padding:48px 0;gap:32px}.columns{grid-template-columns:1fr;gap:24px}.columns h2{margin:12px 0}.note{grid-template-columns:1fr;gap:20px;padding:26px}.document{padding-top:44px}.document h1{font-size:38px}footer{padding:28px 22px}.contact a{overflow-wrap:anywhere}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}\n',
        "text/css",
    ),
}


async def homepage(request):
    from starlette.concurrency import run_in_threadpool
    from ads_mcp.connections import page, session

    try:
        user = await run_in_threadpool(session, request)
    except Exception:
        user = None
    action = (
        '<a class="btn" href="/mitt-konto">Öppna min översikt</a>'
        if user
        else '<a class="btn" href="/anslutningar/google?till=/mitt-konto">Skapa gratis konto</a>'
        "<p>För dig som söker extern åtkomst granskar vi din ansökan efter registrering.</p>"
    )
    return page(
        '<section class="hero"><div><h1>Annonser och spårning i Claude</h1>'
        "<p>Analysera kampanjer i Google Ads och LinkedIn Ads. Felsök spårningen och förbered taggändringar i Google Tag Manager.</p>"
        + action
        + '</div><div class="hero-channels">'
        + "".join(
            f"<div><strong>{title}</strong></div>"
            for key, (title, _, _) in PRODUCTS.items()
            if key != "drive"
        )
        + '</div></section><section class="getting-started"><h2>Så kommer du igång</h2>'
        '<ol class="grid admin-grid"><li><strong>Logga in med Google</strong><p>Skapa eller öppna ditt kostnadsfria Viks-konto.</p></li>'
        "<li><strong>Anslut dina produkter</strong><p>Välj de produkter du vill använda från din översikt.</p></li>"
        "<li><strong>Lägg till verktyget i Claude</strong><p>Kopiera adressen från din översikt. Skriv sedan Testa vADS i Claude för att kontrollera dina anslutningar.</p></li></ol>"
        "</section>",
        title="Annonser och spårning i Claude",
        user=user,
        landing=True,
    )


def register_public_site_routes(mcp):
    mcp.custom_route("/", methods=["GET"])(homepage)
    for path, (body, media_type) in ASSETS.items():
        if path == "/privacy.html":
            body = body.replace(
                "</article>",
                "<h2>Google Tag Manager</h2><p>Om du ansluter GTM sparar Viks åtkomstuppgifterna krypterat för ditt medlemskonto. Viks läser din GTM-konfiguration direkt från Google och skickar begärda resultat till din anslutna AI-klient. Om du aktiverar utkast sparas ändringsförslaget och ditt godkännande. Godkända utkast kan skrivas i en ny GTM-arbetsyta. Du kan återkalla åtkomsten i ditt Google-konto.</p></article>",
            )
            body = body.replace(
                "</article>",
                "<h2>Google Drive och Google Analytics 4</h2><p>Varje anslutning har separat läsåtkomst som sparas krypterat för medlemmen. För Drive ber Google om läsåtkomst till dina filer. Den separata Drive-anslutningen i Viks har en egen sida för mapphantering. Mapp-ID och namn sparas. Filuppgifter och begärda bildförhandsvisningar från dessa mappar, samt begärda GA4-rapporter, skickas till medlemmens AI-klient. Du kan ta bort en mapp på <a href='/anslutningar/drive-mappar'>Drive-mappar i Viks</a> och återkalla åtkomsten i Google.</p><h2>Viks-medlemskap</h2><p>När du registrerar dig sparar vi din verifierade Google-identitet, krypterad e-postadress och din åtkomststatus. Administratörerna granskar ansökningar från externa användare. Medlemskapet är kostnadsfritt. När du ansluter Google Ads separat sparas dina åtkomstuppgifter krypterat för att kunna hämta och förnya åtkomsten. För att avsluta medlemskapet eller begära radering, använd kontaktadressen ovan.</p></article>",
            )
            body = body.replace(
                "</article>",
                "<h2>Användarstatistik</h2><p>Behöriga administratörer för Viks Annonsverktyg ser Google-adress, totalt antal verktygsanrop, aktiva dagar och senaste aktiva datum för de senaste 30 dagarna. Vi summerar anrop per användare och svensk kalenderdag från första anropet. E-postadressen lagras krypterad.</p><p>För att förstå hur tjänsten används och prioritera utvecklingen summerar vi också antalet anrop per produkt och användare. Det gör att behöriga administratörer kan se hur många av en användares anrop som gäller exempelvis Google Ads, LinkedIn Ads eller Google Tag Manager. Övergripande anslutningstest räknas i en egen kategori. Uppdelningen sparas per svensk kalenderdag och visas i 30-dagarsvyn. Statistiken sparar inga frågor, verktygsnamn, annonskonton eller svar. Statistikposter får en raderingsmarkering efter 30 dagar. Automatisk radering förutsätter att tidsstyrd radering (TTL) är aktiverad i databasen.</p></article>",
            )
            body = body.replace(
                "Webbplatsen visar endast appinformation, policy och villkor. Den innehåller inga annonsrapporter, inga formulär för Google-inloggning och ingen egen analys- eller reklamkod.",
                "På sidan Mina anslutningar kan du identifiera dig med Google och se dina åtkomliga annonskonton. En skyddad webbläsarkaka identifierar din tillfälliga session. Webbsessionen gäller i upp till åtta timmar och kan avslutas via Logga ut.",
            )
            body = body.replace(
                "</article>",
                "<h2>LinkedIn-anslutning</h2><p>Om du väljer att ansluta LinkedIn hämtar verktyget annonskonton, kampanjinställningar och resultat från LinkedIns API. LinkedIn-inloggningen är separat från Google. Åtkomsttoken sparas krypterat i operatörens Firestore-databas och knyts till din verifierade användare. Ingen extern annonseringstjänst används som mellanhand. Begärda verktygsresultat skickas till den AI-klient du använder.</p><p>LinkedIn-ändringar kräver en separat kontopolicy och ett godkännande för varje förslag. Underlag och körningsstatus lagras för kontroll och felsökning. Du kan radera din lagrade LinkedIn-anslutning med verktygets frånkopplingsfunktion och återkalla appens behörighet hos LinkedIn. Tokenposter har en teknisk raderingsmarkering en dag efter giltighetstidens slut. Engångslänkar gäller i tio minuter. Automatisk databasradering förutsätter aktiverad TTL. Övriga lagringstider och kontaktvägar beskrivs ovan.</p></article>",
            )

        if media_type == "text/html":
            body = body.replace(
                "</head>", '<link rel="icon" href="/favicon.ico"></head>'
            )

        def make_handler(content, content_type):
            async def serve(request):
                return Response(
                    content,
                    media_type=content_type,
                    headers={
                        "Cache-Control": "public, max-age=300",
                        "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                    },
                )

            return serve

        mcp.custom_route(path, methods=["GET"])(make_handler(body, media_type))
