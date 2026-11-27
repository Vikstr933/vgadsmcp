"""Read GTM configuration with the signed-in member's dedicated Google grant."""

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from mcp.types import ToolAnnotations
from pydantic import Field
from requests.exceptions import RequestException

from ads_mcp import google_grant, gtm_drafts, gtm_tag_edits
from ads_mcp.write_policy import WriteBlocked, principal

gtm_mcp = FastMCP("gtm")
READ = ToolAnnotations(readOnlyHint=True)
ID = Annotated[str, Field(pattern=r"^[0-9]{1,24}$")]
PAGE = Annotated[str | None, Field(max_length=4096)]
BASE = gtm_drafts.BASE
ENTITY_FIELDS = {
    "tags": (
        "tag",
        "tagId,name,type,paused,firingTriggerId,blockingTriggerId,consentSettings",
    ),
    "triggers": ("trigger", "triggerId,name,type"),
    "variables": ("variable", "variableId,name,type"),
}


def read(path, *, fields, page_token=None):
    try:
        credentials = google_grant.credentials(
            principal(), google_grant.GTM_READ
        )
        params = {"fields": fields}
        if page_token:
            params["pageToken"] = page_token
        with AuthorizedSession(credentials, refresh_timeout=12) as client:
            response = client.get(
                BASE + path, params=params, timeout=15, allow_redirects=False
            )
    except WriteBlocked as exc:
        raise ToolError(str(exc)) from None
    except RefreshError:
        raise ToolError(
            "GTM-åtkomsten behöver förnyas i Min översikt."
        ) from None
    except (RequestException, TransportError):
        raise ToolError("Google Tag Manager svarar inte just nu.") from None
    if response.status_code == 401:
        raise ToolError("GTM-åtkomsten behöver förnyas i Min översikt.")
    if response.status_code == 403:
        raise ToolError(
            "GTM nekade åtkomst. Kontrollera att Tag Manager API är aktiverat och att ditt Google-konto har åtkomst till containern."
        )
    if response.status_code == 404:
        raise ToolError(
            "GTM-resursen hittades inte. Kontrollera ID:t och din åtkomst."
        )
    if response.status_code == 429:
        raise ToolError(
            "Google Tag Managers anropsgräns nåddes. Försök igen senare."
        )
    if response.status_code != 200:
        raise ToolError(
            f"Google Tag Manager returnerade HTTP {response.status_code}."
        )
    try:
        return response.json()
    except ValueError:
        raise ToolError(
            "Google Tag Manager returnerade ett oväntat svar."
        ) from None


@gtm_mcp.tool(annotations=READ)
def list_accounts(page_token: PAGE = None) -> dict:
    """List GTM accounts accessible to the connected Google user. Pass nextPageToken to continue."""
    return read(
        "accounts",
        fields="account(accountId,name,path),nextPageToken",
        page_token=page_token,
    )


@gtm_mcp.tool(annotations=READ)
def list_containers(account_id: ID, page_token: PAGE = None) -> dict:
    """List containers for a GTM account. The numeric containerId is used by other tools."""
    return read(
        f"accounts/{account_id}/containers",
        fields="container(containerId,publicId,name,path,usageContext),nextPageToken",
        page_token=page_token,
    )


@gtm_mcp.tool(annotations=READ)
def list_workspaces(
    account_id: ID, container_id: ID, page_token: PAGE = None
) -> dict:
    """List workspaces in a GTM container, including their numeric IDs."""
    return read(
        f"accounts/{account_id}/containers/{container_id}/workspaces",
        fields="workspace(workspaceId,path,name,description),nextPageToken",
        page_token=page_token,
    )


@gtm_mcp.tool(annotations=READ)
def list_entities(
    account_id: ID,
    container_id: ID,
    workspace_id: ID,
    kind: Literal["tags", "triggers", "variables"],
    page_token: PAGE = None,
) -> dict:
    """Inventory workspace tags, triggers or variables. Includes tag firing, blocking and consent references."""
    name, fields = ENTITY_FIELDS[kind]
    return read(
        f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/{kind}",
        fields=f"{name}({fields}),nextPageToken",
        page_token=page_token,
    )


@gtm_mcp.tool(annotations=READ)
def live_version(account_id: ID, container_id: ID) -> dict:
    """Read the published version and its tag, trigger and variable inventory."""
    entities = ",".join(
        f"{name}({fields})" for name, fields in ENTITY_FIELDS.values()
    )
    return read(
        f"accounts/{account_id}/containers/{container_id}/versions:live",
        fields=f"containerVersionId,name,fingerprint,{entities}",
    )


@gtm_mcp.tool(annotations=READ)
def workspace_status(
    account_id: ID, container_id: ID, workspace_id: ID
) -> dict:
    """Read workspace changes and merge conflicts. Does not sync or modify the workspace."""
    return read(
        f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/status",
        fields="workspaceChange(changeStatus,tag(tagId,name),trigger(triggerId,name),variable(variableId,name)),mergeConflict(entityInWorkspace(changeStatus,tag(tagId,name),trigger(triggerId,name),variable(variableId,name))",
    )


def draft_call(operation):
    try:
        return operation()
    except WriteBlocked as exc:
        raise ToolError(str(exc)) from None
    except RefreshError:
        raise ToolError(
            "Förnya utkaståtkomsten för GTM i Min översikt."
        ) from None
    except Exception:
        raise ToolError(
            "GTM-utkastet kunde inte hanteras. Kontrollera förslagets status före ett nytt försök."
        ) from None


@gtm_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def propose_tracking_draft(
    account_id: ID, container_id: ID, draft: gtm_drafts.TrackingDraft
) -> dict:
    """Preview a new GTM web workspace with a GA4 event or Google Ads conversion tag and a data-layer trigger. Use verified IDs and data-layer keys. Returns an owner approval URL; no workspace is created yet."""
    return draft_call(
        lambda: gtm_drafts.propose(principal(), account_id, container_id, draft)
    )


@gtm_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True
    )
)
def apply_tracking_draft(plan_id: str) -> dict:
    """Create the exact reviewed GTM draft in its own workspace after browser approval. Never creates or publishes a container version. Check draft status after an uncertain result."""
    return draft_call(lambda: gtm_drafts.apply(principal(), plan_id))


@gtm_mcp.tool(annotations=READ)
def tracking_draft_status(plan_id: str) -> dict:
    """Check a tracking proposal's approval and execution status, including a known workspace ID."""
    return draft_call(lambda: gtm_drafts.status(principal(), plan_id))


@gtm_mcp.tool(annotations=READ)
def inspect_published_tag(account_id: ID, container_id: ID, tag_id: ID) -> dict:
    """Inspect an existing published tag, its editable fields and available user-defined triggers. The numeric tag_id comes from live_version."""
    version = read(
        f"accounts/{account_id}/containers/{container_id}/versions:live",
        fields="containerVersionId,tag(tagId,name,type,paused,firingTriggerId,blockingTriggerId,parameter(key,type,value)),trigger(triggerId,name)",
    )
    return draft_call(lambda: gtm_tag_edits.inspect_tag(version, tag_id))


@gtm_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def propose_tag_edit(
    account_id: ID, container_id: ID, tag_id: ID, edit: gtm_tag_edits.TagEdit
) -> dict:
    """Preview edits to a published tag in a new GTM web workspace. Use inspect_published_tag first for its fields and trigger IDs. Returns a before/after diff and browser approval URL. After approval, use apply_tracking_draft with the same plan ID. No production publishing."""
    return draft_call(
        lambda: gtm_drafts.propose_tag_edit(
            principal(), account_id, container_id, tag_id, edit
        )
    )
