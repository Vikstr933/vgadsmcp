"""Validate and review edits to an existing GTM tag without storing its full configuration."""

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ads_mcp.write_policy import WriteBlocked, digest

NumericId = Annotated[str, Field(pattern=r"^[0-9]{1,24}$")]
EventValue = Annotated[
    str,
    Field(pattern=r"^(?:[A-Za-z][A-Za-z0-9_.-]{0,79}|\{\{[^{}<>]{1,100}\}\})$"),
]
TagParameter = Literal[
    "event_type",
    "event_name_standard",
    "event_name_custom",
    "eventName",
    "measurementIdOverride",
    "conversionId",
    "conversionLabel",
    "conversionValue",
    "currencyCode",
    "orderId",
]
TagValue = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f<>]+$")
]
EVENT_VALUE = TypeAdapter(EventValue)
IDENTITY_FIELDS = {
    "path",
    "accountId",
    "containerId",
    "workspaceId",
    "tagId",
    "fingerprint",
    "tagManagerUrl",
}

# These are scalar tracking fields. Destination URLs, script, consent and user data fields stay as configured.
DATA_TAG_KEYS = {"event_type", "event_name_standard", "event_name_custom"}
GA4_KEYS = {"eventName", "measurementIdOverride"}
ADS_KEYS = {
    "conversionId",
    "conversionLabel",
    "conversionValue",
    "currencyCode",
    "orderId",
}
TRIGGER_FIELDS = {
    "firing_trigger_ids": "firingTriggerId",
    "blocking_trigger_ids": "blockingTriggerId",
}


class TagEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(
        default=None, min_length=1, max_length=120, pattern=r".*\S.*"
    )
    paused: bool | None = None
    firing_trigger_ids: list[NumericId] | None = Field(
        default=None, max_length=10
    )
    blocking_trigger_ids: list[NumericId] | None = Field(
        default=None, max_length=10
    )
    parameters: dict[TagParameter, TagValue] = Field(
        default_factory=dict, max_length=10
    )


def parameter_keys(tag):
    if tag.get("type") == "gaawe":
        return GA4_KEYS
    if tag.get("type") == "awct":
        return ADS_KEYS
    # Custom tag template IDs are container-specific. Recognize Data Tag by its existing configuration fields.
    keys = {item.get("key") for item in tag.get("parameter", [])}
    if (
        tag.get("type", "").startswith("cvt_")
        and {"gtm_server_domain", "event_type"} <= keys
    ):
        return DATA_TAG_KEYS
    return set()


def settings(tag):
    return {
        key: value for key, value in tag.items() if key not in IDENTITY_FIELDS
    }


def tag_digest(tag):
    body = settings(tag)
    body.setdefault("paused", False)
    for field in ("firingTriggerId", "blockingTriggerId"):
        body.setdefault(field, [])
    return digest(body)


def select_tag(version, tag_id):
    for tag in version.get("tag", []):
        if tag.get("tagId") == tag_id:
            return tag
    raise WriteBlocked(
        "Taggen hittades inte i den publicerade containerversionen."
    )


def inspect_tag(version, tag_id):
    tag = select_tag(version, tag_id)
    keys = parameter_keys(tag)
    return {
        "version_id": version.get("containerVersionId"),
        "tag_id": tag_id,
        "name": tag.get("name"),
        "type": tag.get("type"),
        "paused": tag.get("paused", False),
        "firing_trigger_ids": tag.get("firingTriggerId", []),
        "blocking_trigger_ids": tag.get("blockingTriggerId", []),
        "editable_parameters": {
            item["key"]: item["value"]
            for item in tag.get("parameter", [])
            if item.get("key") in keys
            and item.get("type") == "template"
            and "value" in item
        },
        "available_parameter_keys": sorted(keys),
        "published_triggers": [
            {"id": trigger["triggerId"], "name": trigger.get("name")}
            for trigger in version.get("trigger", [])
            if "triggerId" in trigger
        ],
    }


def patch_tag(tag, edit):
    payload = deepcopy(settings(tag))
    diff = {}
    for field, remote in {
        "name": "name",
        "paused": "paused",
        **TRIGGER_FIELDS,
    }.items():
        value = getattr(edit, field)
        if value is None:
            continue
        if isinstance(value, list) and len(value) != len(set(value)):
            raise WriteBlocked("En trigger finns med flera gånger.")
        before = payload.get(
            remote,
            (
                []
                if field in TRIGGER_FIELDS
                else False if field == "paused" else None
            ),
        )
        if value != before:
            payload[remote] = value
            diff[remote] = {"before": before, "after": value}
    if TRIGGER_FIELDS.values() & diff.keys() and set(
        payload.get("firingTriggerId", [])
    ) & set(payload.get("blockingTriggerId", [])):
        raise WriteBlocked(
            "Samma trigger kan inte vara både utlösande och blockerande."
        )

    if edit.parameters:
        if not edit.parameters.keys() <= parameter_keys(tag):
            raise WriteBlocked(
                "Någon parameter stöds inte för den valda taggen."
            )
        parameters = payload.setdefault("parameter", [])
        for key, value in edit.parameters.items():
            if key == "event_type" and value not in ("custom", "standard"):
                raise WriteBlocked(
                    "Data Tag-eventtypen måste vara custom eller standard."
                )
            if key in ("event_name_custom", "event_name_standard", "eventName"):
                try:
                    EVENT_VALUE.validate_python(value)
                except ValidationError:
                    raise WriteBlocked(
                        "Använd ett giltigt eventnamn eller en GTM-variabel."
                    ) from None
            candidates = [
                parameter
                for parameter in parameters
                if parameter.get("key") == key
            ]
            if len(candidates) > 1 or (
                candidates and candidates[0].get("type") != "template"
            ):
                raise WriteBlocked(
                    "Parametern har en konfiguration som kräver redigering i GTM."
                )
            if candidates:
                before = candidates[0].get("value")
                candidates[0]["value"] = value
            else:
                before = None
                parameters.append(
                    {"key": key, "type": "template", "value": value}
                )
            if before != value:
                diff[f"parameter.{key}"] = {"before": before, "after": value}
    if not diff:
        raise WriteBlocked("Förslaget innehåller ingen faktisk ändring.")
    return payload, diff


def reviewed_triggers(version, tag, diff):
    fingerprints = {
        t["triggerId"]: t.get("fingerprint")
        for t in version.get("trigger", [])
        if "triggerId" in t
    }
    reviewed = {}
    for field in TRIGGER_FIELDS.values():
        if field in diff:
            previous = set(tag.get(field, []))
            for trigger_id in diff[field]["after"]:
                if trigger_id not in fingerprints and trigger_id in previous:
                    # An existing built-in trigger has no standalone resource.
                    continue
                if not fingerprints.get(trigger_id):
                    raise WriteBlocked(
                        "En ny triggerreferens måste finnas i den publicerade containerversionen."
                    )
                reviewed[trigger_id] = fingerprints[trigger_id]
    return reviewed
