"""Build and apply reviewed tracking drafts to an isolated GTM web workspace."""

import os
import re
import time
import uuid
from dataclasses import replace
from typing import Annotated, Literal

from google.auth.transport.requests import AuthorizedSession
from pydantic import BaseModel, ConfigDict, Field

from ads_mcp import google_grant, gtm_tag_edits
from ads_mcp.write_policy import WriteBlocked, WritePolicy, digest
from ads_mcp.write_store import FirestorePlanStore, check_plan

BASE = "https://tagmanager.googleapis.com/tagmanager/v2/"
EventName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")]
DataLayerKey = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")]


class AnalyticsTag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: Literal["ga4"]
    measurement_id: str = Field(pattern=r"^G-[A-Z0-9]{4,24}$")
    event_name: EventName


class AdsTag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: Literal["google_ads"]
    conversion_id: str = Field(pattern=r"^(AW-)?[0-9]{5,24}$")
    conversion_label: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class TrackingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_layer_event: EventName
    tag: AnalyticsTag | AdsTag = Field(discriminator="destination")
    value_key: DataLayerKey | None = None
    currency_key: DataLayerKey | None = None
    transaction_id_key: DataLayerKey | None = None


def parameter(key, value, kind="template"):
    return {"key": key, "value": value, "type": kind}


def build_changes(draft, plan_id):
    suffix = plan_id[:8]
    changes = [
        {
            "changeStatus": "added",
            "trigger": {
                "triggerId": "new_1",
                "name": f"Viks {suffix} – {draft.data_layer_event}",
                "type": "customEvent",
                "customEventFilter": [
                    {
                        "type": "equals",
                        "parameter": [
                            parameter("arg0", "{{_event}}"),
                            parameter("arg1", draft.data_layer_event),
                        ],
                    }
                ],
            },
        }
    ]
    mapping = {}
    for key in ("value", "currency", "transaction_id"):
        source = getattr(draft, key + "_key")
        if source:
            name = f"Viks {suffix} – {key}"
            changes.append(
                {
                    "changeStatus": "added",
                    "variable": {
                        "variableId": f"new_{len(changes) + 1}",
                        "name": name,
                        "type": "v",
                        "parameter": [
                            parameter("name", source),
                            parameter("dataLayerVersion", "2", "integer"),
                        ],
                    },
                }
            )
            mapping[key] = "{{" + name + "}}"

    tag = draft.tag
    if isinstance(tag, AnalyticsTag):
        kind = "gaawe"
        params = [
            parameter("measurementIdOverride", tag.measurement_id),
            parameter("eventName", tag.event_name),
        ]
        if mapping:
            params.append(
                {
                    "type": "list",
                    "key": "eventSettingsTable",
                    "list": [
                        {
                            "type": "map",
                            "map": [
                                parameter("parameter", key),
                                parameter("parameterValue", variable),
                            ],
                        }
                        for key, variable in mapping.items()
                    ],
                }
            )
    else:
        kind = "awct"
        params = [
            parameter("conversionId", tag.conversion_id.removeprefix("AW-")),
            parameter("conversionLabel", tag.conversion_label),
            parameter("enableConversionLinker", "true", "boolean"),
        ]
        ads_keys = {
            "value": "conversionValue",
            "currency": "currencyCode",
            "transaction_id": "orderId",
        }
        params.extend(
            parameter(ads_keys[key], variable)
            for key, variable in mapping.items()
        )
    changes.append(
        {
            "changeStatus": "added",
            "tag": {
                "tagId": f"new_{len(changes) + 1}",
                "type": kind,
                "name": f"Viks {suffix} – {tag.destination} – {draft.data_layer_event}",
                "parameter": params,
                "firingTriggerId": ["new_1"],
                "tagFiringOption": "oncePerEvent",
            },
        }
    )
    return changes


class GTMStore(FirestorePlanStore):
    def __init__(self, policy):
        super().__init__(policy)
        self.plans = self.client.collection("viks_gtm_plans")
        self.locks = self.client.collection("viks_gtm_plan_locks")


def policy():
    # Keep draft writes independently switchable while sharing the existing secret and storage.
    result = replace(
        WritePolicy.load(),
        mode=os.getenv("GTM_MCP_WRITE_MODE", "approval_required"),
    )
    result.require_writes()
    return result


def checked_plan(store, plan_id, owner):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise WriteBlocked("Ogiltigt förslags-ID.")
    plan = store.get(plan_id)
    if not plan or plan["owner"] != owner:
        raise WriteBlocked(
            "Förslaget tillhör ett annat konto eller har tagits bort."
        )
    if digest(plan["change"]) != plan["review_digest"]:
        raise WriteBlocked("Förslaget har ändrats.")
    return plan


def api_request(
    client, method, path, *, payload=None, fields=None, fingerprint=None
):
    params = {}
    if fields:
        params["fields"] = fields
    if fingerprint:
        params["fingerprint"] = fingerprint
    response = client.request(
        method,
        BASE + path,
        json=payload,
        params=params or None,
        timeout=15,
        allow_redirects=False,
    )
    if response.status_code in (401, 403):
        raise WriteBlocked(
            "Google nekade GTM-åtkomst. Kontrollera utkastbehörigheten och containerns åtkomst."
        )
    if not 200 <= response.status_code < 300:
        raise WriteBlocked(
            f"Google Tag Manager returnerade HTTP {response.status_code}."
        )
    data = response.json()
    if not isinstance(data, dict):
        raise WriteBlocked("Google Tag Manager returnerade ett oväntat svar.")
    return data


def target_container(client, path):
    container = api_request(
        client, "GET", path, fields="name,publicId,usageContext"
    )
    if "web" not in container.get("usageContext", []):
        raise WriteBlocked("GTM-utkast stöds för webbcontainrar.")
    return {key: container.get(key, "") for key in ("name", "publicId")}


def container_path_for(account_id, container_id):
    if not all(
        re.fullmatch(r"[0-9]{1,24}", item)
        for item in (account_id, container_id)
    ):
        raise WriteBlocked("Använd numeriska GTM-ID:n för konto och container.")
    return f"accounts/{account_id}/containers/{container_id}"


def propose(owner, account_id, container_id, draft):
    container_path = container_path_for(account_id, container_id)
    settings = policy()
    credentials = google_grant.credentials(owner, google_grant.GTM_EDIT)
    with AuthorizedSession(credentials, refresh_timeout=12) as client:
        container = target_container(client, container_path)
    plan_id = uuid.uuid4().hex
    change = {
        # The workspace is unique to this proposal. Its lock prevents replay of this proposal.
        "customer_id": plan_id,
        "container_path": container_path,
        "container": container,
        "workspace_name": f"Viks – {draft.data_layer_event} – {plan_id[:8]}",
        "changes": build_changes(draft, plan_id),
    }
    return save_plan(settings, owner, plan_id, change)


def propose_tag_edit(owner, account_id, container_id, tag_id, edit):
    container_path = container_path_for(account_id, container_id)
    if not re.fullmatch(r"[0-9]{1,24}", tag_id):
        raise WriteBlocked("Använd taggens numeriska ID.")
    settings = policy()
    credentials = google_grant.credentials(owner, google_grant.GTM_EDIT)
    with AuthorizedSession(credentials, refresh_timeout=12) as client:
        container = target_container(client, container_path)
        version = api_request(
            client,
            "GET",
            container_path + "/versions:live",
            fields="containerVersionId,tag,trigger(triggerId,fingerprint)",
        )
    tag = gtm_tag_edits.select_tag(version, tag_id)
    version_id = version.get("containerVersionId")
    if not version_id:
        raise WriteBlocked(
            "Google returnerade ingen publicerad version att granska."
        )
    _, diff = gtm_tag_edits.patch_tag(tag, edit)
    plan_id = uuid.uuid4().hex
    change = {
        "customer_id": plan_id,
        "container_path": container_path,
        "container": container,
        "workspace_name": f"Viks – redigera tagg {tag_id} – {plan_id[:8]}",
        "changes": diff,
        "edit": {
            "tag_id": tag_id,
            "tag_name": tag["name"],
            "tag_type": tag["type"],
            "version_id": version_id,
            "source_digest": gtm_tag_edits.tag_digest(tag),
            "trigger_fingerprints": gtm_tag_edits.reviewed_triggers(
                version, tag, diff
            ),
            "patch": edit.model_dump(exclude_none=True),
        },
    }
    return save_plan(settings, owner, plan_id, change)


def save_plan(settings, owner, plan_id, change):
    plan = {
        "id": plan_id,
        "owner": owner,
        "status": "pending_approval",
        "expires_at": time.time() + 3600,
        "change": change,
        "review_digest": digest(change),
    }
    GTMStore(settings).create(plan)
    return review_data(settings, plan)


def review(owner, plan_id):
    settings = policy()
    return review_data(
        settings, checked_plan(GTMStore(settings), plan_id, owner)
    )


def review_data(settings, plan):
    change = plan["change"]
    plan_id = plan["id"]
    status = plan["status"]
    if (
        status in ("pending_approval", "approved")
        and plan["expires_at"] <= time.time()
    ):
        status = "expired"
    return {
        "plan_id": plan_id,
        "status": status,
        "container": change["container"],
        "workspace": change["workspace_name"],
        **(
            {
                "tag": {
                    key: change["edit"][key]
                    for key in ("tag_id", "tag_name", "tag_type", "version_id")
                }
            }
            if "edit" in change
            else {}
        ),
        "changes": change["changes"],
        "approval_url": (
            f"{settings.base_url}/gtm/approvals/{plan_id}"
            if status == "pending_approval"
            else None
        ),
    }


def check_live_tag(client, container_path, edit):
    version = api_request(
        client,
        "GET",
        container_path + "/versions:live",
        fields="containerVersionId,tag",
    )
    if version.get("containerVersionId") != edit["version_id"]:
        raise WriteBlocked(
            "En annan containerversion har publicerats. Skapa ett nytt förslag."
        )
    tag = gtm_tag_edits.select_tag(version, edit["tag_id"])
    if gtm_tag_edits.tag_digest(tag) != edit["source_digest"]:
        raise WriteBlocked(
            "Den publicerade taggen har ändrats. Skapa ett nytt förslag."
        )


def edit_tag_in_workspace(client, workspace_path, change):
    edit = change["edit"]
    tag_path = f'{workspace_path}/tags/{edit["tag_id"]}'
    current = api_request(client, "GET", tag_path)
    fingerprint = current.get("fingerprint")
    if (
        current.get("tagId") != edit["tag_id"]
        or not fingerprint
        or gtm_tag_edits.tag_digest(current) != edit["source_digest"]
    ):
        raise WriteBlocked(
            "Taggen i den nya arbetsytan matchar inte den granskade versionen. Ingen taggändring skickades."
        )
    for trigger_id, reviewed_fingerprint in edit[
        "trigger_fingerprints"
    ].items():
        trigger = api_request(
            client,
            "GET",
            f"{workspace_path}/triggers/{trigger_id}",
            fields="triggerId,fingerprint",
        )
        if (
            trigger.get("triggerId") != trigger_id
            or trigger.get("fingerprint") != reviewed_fingerprint
        ):
            raise WriteBlocked(
                "En vald trigger har ändrats. Ingen taggändring skickades."
            )
    patch = gtm_tag_edits.TagEdit.model_validate(edit["patch"])
    payload, diff = gtm_tag_edits.patch_tag(current, patch)
    if diff != change["changes"]:
        raise WriteBlocked("Ändringen matchar inte godkännandet.")
    reply = api_request(
        client, "PUT", tag_path, payload=payload, fingerprint=fingerprint
    )
    if reply.get("tagId") != edit["tag_id"] or gtm_tag_edits.tag_digest(
        reply
    ) != gtm_tag_edits.tag_digest(payload):
        raise WriteBlocked(
            "Google bekräftade inte att taggen matchade det godkända utkastet."
        )
    return {"tag_id": edit["tag_id"], "entities": [reply["name"]]}


def apply(owner, plan_id):
    store = GTMStore(policy())
    plan = checked_plan(store, plan_id, owner)
    if plan["status"] in ("applied", "unknown", "executing"):
        return status(owner, plan_id, store=store)
    check_plan(plan, owner)
    if plan["status"] != "approved":
        raise WriteBlocked("Godkänn förslaget i webbläsaren först.")
    change = plan["change"]
    container_path = change["container_path"]
    credentials = google_grant.credentials(owner, google_grant.GTM_EDIT)
    # AuthorizedSession may retry a rejected auth response by default. Write requests are single-attempt.
    with AuthorizedSession(
        credentials, refresh_timeout=12, max_refresh_attempts=0
    ) as client:
        if target_container(client, container_path) != change["container"]:
            raise WriteBlocked(
                "Containern har ändrats sedan granskningen. Skapa ett nytt förslag."
            )
        if "edit" in change:
            check_live_tag(client, container_path, change["edit"])
        store.transition(plan_id, "approved", "executing", owner=owner)
        result = {
            "workspace_name": change["workspace_name"],
            "container": change["container"],
        }
        try:
            workspace = api_request(
                client,
                "POST",
                container_path + "/workspaces",
                payload={
                    "name": change["workspace_name"],
                    "description": f"Godkänt Viks-förslag {plan_id}.",
                },
            )
            wid = workspace.get("workspaceId", "")
            if not isinstance(wid, str) or not re.fullmatch(
                r"[0-9]{1,24}", wid
            ):
                raise WriteBlocked(
                    "Google returnerade inget giltigt arbetsyte-ID."
                )
            workspace_path = f"{container_path}/workspaces/{wid}"
            result["workspace_id"] = wid
            result["workspace_path"] = workspace_path
            store.record_progress(plan_id, owner, result)
            if "edit" in change:
                result.update(
                    edit_tag_in_workspace(client, workspace_path, change)
                )
            else:
                reply = api_request(
                    client,
                    "POST",
                    workspace_path + "/bulk_update",
                    payload={"changes": change["changes"]},
                )
                expected = {
                    entity[key]["name"]
                    for entity in change["changes"]
                    for key in ("tag", "trigger", "variable")
                    if key in entity
                }
                actual = {
                    entity[key]["name"]
                    for entity in reply.get("changes", [])
                    for key in ("tag", "trigger", "variable")
                    if key in entity
                }
                if actual != expected:
                    raise WriteBlocked(
                        "Google bekräftade inte alla delar av utkastet."
                    )
                result["entities"] = sorted(actual)
        except Exception as exc:
            result["message"] = (
                str(exc)
                if isinstance(exc, WriteBlocked)
                else "Google kunde inte bekräfta utfallet."
            )
            store.transition(
                plan_id, "executing", "unknown", owner=owner, result=result
            )
            return {"status": "unknown", **result}
        store.transition(
            plan_id, "executing", "applied", owner=owner, result=result
        )
        return {"status": "applied", **result}


def status(owner, plan_id, *, store=None):
    plan = checked_plan(store or GTMStore(policy()), plan_id, owner)
    return {
        "status": plan["status"],
        "workspace_name": plan["change"]["workspace_name"],
        **plan.get("progress", {}),
        **plan.get("result", {}),
    }
