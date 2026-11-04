"""Read-only planning tools, separate from campaign write approval."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from ads_mcp.keyword_planning import (
    Planner,
    IdeasRequest,
    HistoryRequest,
    ForecastRequest,
    safe_error,
)

keyword_planning_mcp = FastMCP("keyword_planning")
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


def run(method, request):
    try:
        return getattr(Planner(request), method)(request)
    except Exception as error:
        raise ToolError(safe_error(error)) from None


@keyword_planning_mcp.tool(annotations=READ)
def generate_ideas(request: IdeasRequest) -> dict:
    """Requires Basic or Standard API access (Explorer cannot use Keyword Planner). Get real Google Keyword Planner ideas from keyword seeds, a public URL, or both. Requires accessible client customer_id, optional login_customer_id MCC, explicit geo_target_ids and language_id. Google Search only. Returns one bounded page (has_more indicates truncation), approximate historical volume, monthly seasonality and bid ranges in account-currency micros. Optional complete-month historical range. Never invent missing metrics or treat ideas as approved keywords. Plan different event/city combinations separately. No Google Ads mutations or persistent plans."""
    return run("ideas", request)


@keyword_planning_mcp.tool(annotations=READ)
def historical_metrics(request: HistoryRequest) -> dict:
    """Requires Basic or Standard API access (Explorer cannot use Keyword Planner). Get Google historical monthly search volume, seasonality, competition and bid ranges for up to 500 keywords. Explicit language/geography, Google Search only; defaults to past 12 months. Account currency returned; 1 unit=1,000,000 micros. Preserve null vs zero and Google's merged close variants; never add overlapping volumes or promise future demand. Use for event-season research before proposing budgets. No campaign changes."""
    return run("history", request)


@keyword_planning_mcp.tool(annotations=READ)
def forecast(request: ForecastRequest) -> dict:
    """Requires Basic or Standard API access (Explorer cannot use Keyword Planner). Estimate one future keyword scenario using Google's planless forecast API. Requires explicit dates within one year, budget, exact/phrase keywords, language and geos, and MANUAL_CPC with max_cpc_bid_micros or MAXIMIZE_CONVERSIONS. Read-only: budgets are hypothetical, no campaign is created. Returns only available total-period cost/click/conversion metrics; missing is null. API v25 has NO network selector, negative keywords, ad copy, or campaign-specific conversion selection: this is not an exact simulation of the Search campaign builder. Keep event/city scenarios separate; never represent estimates as guarantees or spend approval."""
    return run("forecast", request)
