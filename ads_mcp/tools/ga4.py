"""GA4 property discovery and aggregate reports using each member's grant."""

from datetime import date
from typing import Annotated
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ads_mcp import google_grant, google_media
from ads_mcp.write_policy import WriteBlocked, principal

ga4_mcp = FastMCP("ga4")
READ = ToolAnnotations(readOnlyHint=True)
Property = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_:]{0,127}$")]
Metrics = Annotated[list[Name], Field(min_length=1, max_length=10)]
Dimensions = Annotated[list[Name], Field(max_length=9)]
Limit = Annotated[int, Field(ge=1, le=1000)]
Offset = Annotated[int, Field(ge=0, le=10000000)]
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
DATA = "https://analyticsdata.googleapis.com/v1beta/properties"


def request(method, url, **kwargs):
    try:
        with google_media.google_session(
            principal(), google_grant.GA4_READ
        ) as client:
            return google_media.checked(
                client.request(
                    method, url, timeout=20, allow_redirects=False, **kwargs
                )
            )
    except WriteBlocked as exc:
        raise ToolError(str(exc)) from None


@ga4_mcp.tool(annotations=READ)
def list_properties(
    page_token: Annotated[str, Field(max_length=4096)] | None = None,
) -> dict:
    """List GA4 accounts and properties visible to the member. Continue with nextPageToken."""
    return request(
        "GET",
        ADMIN + "/accountSummaries",
        params={"pageSize": 200, "pageToken": page_token},
    )


@ga4_mcp.tool(annotations=READ)
def run_report(
    property_id: Property,
    start_date: date,
    end_date: date,
    metrics: Metrics,
    dimensions: Dimensions,
    limit: Limit = 100,
    offset: Offset = 0,
) -> dict:
    """Read an aggregate GA4 report for inclusive ISO dates. Examples include sessions by sessionSourceMedium, eventCount by eventName, or purchaseRevenue by date. Use rowCount and offset for pagination."""
    if end_date < start_date:
        raise ToolError("Slutdatum måste ligga på eller efter startdatum.")
    return request(
        "POST",
        f"{DATA}/{property_id}:runReport",
        json={
            "dateRanges": [
                {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                }
            ],
            "dimensions": [{"name": name} for name in dimensions],
            "metrics": [{"name": name} for name in metrics],
            "limit": str(limit),
            "offset": str(offset),
        },
    )


@ga4_mcp.tool(annotations=READ)
def realtime_events(property_id: Property) -> dict:
    """Read aggregate event counts by event name for the GA4 realtime window."""
    return request(
        "POST",
        f"{DATA}/{property_id}:runRealtimeReport",
        json={
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
            "limit": "1000",
        },
    )
