"""Check reviewed GTM tag edits and the workspace fingerprint boundary."""

import time
from unittest.mock import patch

import pytest

from ads_mcp import gtm_drafts as gtm, gtm_tag_edits as edits
from ads_mcp.write_policy import WriteBlocked, digest

TAG = {
    "tagId": "7",
    "name": "Data Tag - Purchase",
    "type": "cvt_123_4",
    "firingTriggerId": ["8"],
    "parameter": [
        {
            "key": "gtm_server_domain",
            "type": "template",
            "value": "https://tracking.example.com",
        },
        {"key": "event_type", "type": "template", "value": "standard"},
        {"key": "event_name_standard", "type": "template", "value": "purchase"},
        {
            "key": "custom_data",
            "type": "list",
            "list": [{"type": "map", "map": []}],
        },
    ],
    "consentSettings": {
        "consentStatus": "needed",
        "consentType": {"type": "list", "list": []},
    },
    "fingerprint": "published-tag",
}
VERSION = {
    "containerVersionId": "79",
    "tag": [TAG],
    "trigger": [
        {"triggerId": "9", "name": "Test checkout", "fingerprint": "trigger-9"}
    ],
}


def test_data_tag_edit_preserves_other_config_and_has_a_reviewable_diff():
    edit = edits.TagEdit(
        paused=True,
        parameters={
            "event_type": "custom",
            "event_name_custom": "purchase_test",
        },
    )
    payload, diff = edits.patch_tag(TAG, edit)
    assert diff["paused"] == {"before": False, "after": True}
    assert diff["parameter.event_name_custom"] == {
        "before": None,
        "after": "purchase_test",
    }
    assert payload["parameter"][0] == TAG["parameter"][0]
    assert payload["parameter"][3] == TAG["parameter"][3]
    assert payload["consentSettings"] == TAG["consentSettings"]
    assert "fingerprint" not in payload
    inspected = edits.inspect_tag(VERSION, "7")
    assert inspected["editable_parameters"]["event_type"] == "standard"
    assert "gtm_server_domain" not in inspected["editable_parameters"]
    with pytest.raises(WriteBlocked, match="ingen faktisk"):
        edits.patch_tag(TAG, edits.TagEdit(paused=False))


@pytest.mark.parametrize("workspace_matches", [True, False])
def test_applied_tag_edit_only_updates_the_existing_id_in_new_workspace(
    workspace_matches,
):
    owner = "member"
    plan_id = "1" * 32
    edit = edits.TagEdit(paused=True, firing_trigger_ids=["9"])
    payload, diff = edits.patch_tag(TAG, edit)
    container = {"name": "Web", "publicId": "GTM-TEST"}
    container_path = "accounts/1/containers/2"
    tag_path = container_path + "/workspaces/80/tags/7"
    fields = {
        "tag_id": "7",
        "tag_name": TAG["name"],
        "tag_type": TAG["type"],
        "version_id": "79",
        "source_digest": edits.tag_digest(TAG),
        "trigger_fingerprints": edits.reviewed_triggers(VERSION, TAG, diff),
        "patch": edit.model_dump(exclude_none=True),
    }
    change = {
        "customer_id": plan_id,
        "container_path": container_path,
        "container": container,
        "workspace_name": "Viks Test",
        "changes": diff,
        "edit": fields,
    }
    record = {
        "id": plan_id,
        "owner": owner,
        "status": "approved",
        "expires_at": time.time() + 60,
        "change": change,
        "review_digest": digest(change),
    }
    current_tag = {**TAG, "workspaceId": "80", "fingerprint": "workspace-tag"}
    if not workspace_matches:
        current_tag["name"] = "Already changed"
    replies = [VERSION, {"workspaceId": "80"}, current_tag]
    if workspace_matches:
        replies.extend(
            [
                VERSION["trigger"][0],
                {**payload, "tagId": "7", "fingerprint": "updated-tag"},
            ]
        )
    with (
        patch.object(gtm, "GTMStore") as store,
        patch.object(gtm, "policy"),
        patch.object(gtm.google_grant, "credentials"),
        patch.object(gtm, "AuthorizedSession"),
        patch.object(gtm, "target_container", return_value=container),
        patch.object(gtm, "api_request", side_effect=replies) as api,
    ):
        store.return_value.get.return_value = record
        result = gtm.apply(owner, plan_id)
        calls = [call.args[1:] for call in api.call_args_list]
        assert calls[:3] == [
            ("GET", container_path + "/versions:live"),
            ("POST", container_path + "/workspaces"),
            ("GET", tag_path),
        ]
        if workspace_matches:
            assert calls[3:] == [
                ("GET", container_path + "/workspaces/80/triggers/9"),
                ("PUT", tag_path),
            ]
            assert api.call_args.kwargs == {
                "payload": payload,
                "fingerprint": "workspace-tag",
            }
            assert result["status"] == "applied" and result["tag_id"] == "7"
        else:
            assert len(calls) == 3
            assert (
                result["status"] == "unknown"
                and "Ingen taggändring skickades" in result["message"]
            )
