"""Self-contained read-only MCP App for the GTM review preview."""

REVIEW_URI = "ui://viks/gtm-review.html"
REVIEW_HTML = r"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GTM-förslag</title>
<style>
:root { color-scheme: light dark; font: 15px/1.5 var(--font-sans, system-ui), sans-serif; }
body { margin: 0; color: var(--color-text-primary, CanvasText); background: var(--color-background-primary, Canvas); }
main { border: 1px solid var(--color-border-primary, GrayText); border-radius: 12px; padding: 1rem; }
h1 { margin: 0; font-size: 1.2rem; }
p { margin: .4rem 0 1rem; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .45rem .55rem; border-bottom: 1px solid var(--color-border-primary, GrayText); overflow-wrap: anywhere; }
.details { overflow-x: auto; }
pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
a { display: inline-block; margin-top: .9rem; font-weight: 650; color: var(--color-text-info, LinkText); }
a:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; }
</style>
</head>
<body>
<main>
<h1>GTM-förslag</h1>
<p id="summary" role="status">Hämtar förslaget.</p>
<div id="details" class="details"></div>
<a id="approval" target="_blank" rel="noopener noreferrer" hidden>Öppna godkännandet i Viks</a>
<p id="link-status" role="status" hidden></p>
</main>
<script>
const summary = document.getElementById("summary");
const details = document.getElementById("details");
const approval = document.getElementById("approval");
const linkStatus = document.getElementById("link-status");
const states = {
  pending_approval: "Väntar på godkännande",
  approved: "Godkänt",
  executing: "Pågår",
  applied: "Genomfört",
  unknown: "Utfallet behöver kontrolleras",
  expired: "Giltighetstiden har gått ut",
  demo: "Demo med exempeldata"
};
let canOpenLinks = false;
let lastSize = "";
let approvalUrl = "";

function send(message) {
  window.parent.postMessage({jsonrpc: "2.0", ...message}, "*");
}

function element(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value === null || value === undefined ? "—" :
    typeof value === "object" ? JSON.stringify(value) : String(value);
  return node;
}

function render(result) {
  details.replaceChildren();
  approval.hidden = true;
  approval.removeAttribute("href");
  linkStatus.hidden = true;
  approvalUrl = "";
  const data = result?.structuredContent;
  if (result?.isError || !data || (data.status !== "demo" && !/^[a-f0-9]{32}$/.test(data.plan_id))) {
    summary.textContent = "Förslaget kunde inte visas. Läs resultatet i chatten.";
    return;
  }
  summary.textContent = [
    data.container?.publicId, data.tag?.tag_name, data.workspace, states[data.status] || "Okänd status"
  ].filter(Boolean).join(" · ");
  if (data.tag && data.changes && !Array.isArray(data.changes)) {
    const table = document.createElement("table");
    const header = table.createTHead().insertRow();
    for (const title of ["Fält", "Före", "Efter"]) {
      const cell = element("th", title);
      cell.scope = "col";
      header.append(cell);
    }
    const body = table.createTBody();
    for (const [field, delta] of Object.entries(data.changes)) {
      const row = body.insertRow();
      row.append(element("td", field), element("td", delta.before), element("td", delta.after));
    }
    details.append(table);
  } else {
    details.append(element("pre", JSON.stringify(data.changes, null, 2)));
  }
  if (data.status !== "pending_approval" || !data.approval_url) return;
  try {
    const url = new URL(data.approval_url);
    if (url.protocol === "https:" && !url.username && !url.password &&
        !url.search && !url.hash && url.pathname === "/gtm/approvals/" + data.plan_id) {
      approvalUrl = url.href;
      approval.href = approvalUrl;
      approval.hidden = false;
    }
  } catch {
    // The text result carries the original link if a host cannot display the card.
  }
}

function reportSize() {
  const width = document.documentElement.clientWidth;
  const height = Math.ceil(document.body.getBoundingClientRect().height);
  const size = width + "x" + height;
  if (size === lastSize) return;
  lastSize = size;
  send({method: "ui/notifications/size-changed", params: {width, height}});
}

approval.addEventListener("click", event => {
  if (!event.isTrusted || !approvalUrl) {
    event.preventDefault();
    return;
  }
  if (canOpenLinks) {
    event.preventDefault();
    send({id: 2, method: "ui/open-link", params: {url: approvalUrl}});
  }
});

window.addEventListener("message", event => {
  const message = event.data;
  if (event.source !== window.parent || message?.jsonrpc !== "2.0") return;
  if (message.id === 1 && message.result) {
    canOpenLinks = Boolean(message.result.hostCapabilities?.openLinks);
    send({method: "ui/notifications/initialized"});
    new ResizeObserver(reportSize).observe(document.body);
    reportSize();
  } else if (message.method === "ui/notifications/tool-result") {
    render(message.params);
  } else if (message.id === 2 && message.error) {
    canOpenLinks = false;
    linkStatus.textContent = "Claude kunde inte öppna länken. Klicka igen eller använd länken i chatten.";
    linkStatus.hidden = false;
  } else if (message.method === "ui/resource-teardown" || message.method === "ping") {
    send({id: message.id, result: {}});
  }
});

send({id: 1, method: "ui/initialize", params: {
  protocolVersion: "2026-01-26",
  appInfo: {name: "Viks GTM-förslag", version: "1.0.0"},
  appCapabilities: {availableDisplayModes: ["inline"]}
}});
</script>
</body>
</html>
"""


def register_gtm_review_card(mcp):
    # Register at the root so the advertised ui:// URI is independent of tool namespaces.
    @mcp.resource(
        REVIEW_URI,
        mime_type="text/html;profile=mcp-app",
        app={"prefersBorder": True},
    )
    def tracking_review_card() -> str:
        """Render the GTM proposal card."""
        return REVIEW_HTML
