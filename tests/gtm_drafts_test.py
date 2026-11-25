"""Guard the approval, workspace isolation, and replay boundary for GTM drafts."""

import time
from unittest.mock import patch

import pytest

from ads_mcp import gtm_drafts as gtm
from ads_mcp.write_policy import WriteBlocked, digest


def test_gtm_draft_cannot_apply_early_and_only_writes_a_new_workspace():
    owner = "member"
    plan_id = "1" * 32
    draft = gtm.TrackingDraft.model_validate(
        {
            "data_layer_event": "purchase",
            "tag": {
                "destination": "google_ads",
                "conversion_id": "AW-123456",
                "conversion_label": "Label",
            },
            "value_key": "ecommerce.value",
            "currency_key": "ecommerce.currency",
            "transaction_id_key": "ecommerce.transaction_id",
        }
    )
    changes = gtm.build_changes(draft, plan_id)
    container = {"name": "Web", "publicId": "GTM-TEST"}
    change = {
        "customer_id": plan_id,
        "container_path": "accounts/1/containers/2",
        "container": container,
        "workspace_name": "Viks Test",
        "changes": changes,
    }
    record = {
        "id": plan_id,
        "owner": owner,
        "status": "pending_approval",
        "expires_at": time.time() + 60,
        "change": change,
        "review_digest": digest(change),
    }
    with (
        patch.object(gtm, "GTMStore") as store,
        patch.object(gtm, "policy"),
        patch.object(gtm.google_grant, "credentials") as credentials,
        patch.object(gtm, "AuthorizedSession"),
        patch.object(gtm, "target_container", return_value=container),
        patch.object(
            gtm,
            "api_request",
            side_effect=[{"workspaceId": "3"}, {"changes": changes}],
        ) as api,
    ):
        store.return_value.get.return_value = record
        with pytest.raises(WriteBlocked):
            gtm.apply("someone else", plan_id)
        with pytest.raises(WriteBlocked, match="Godkänn"):
            gtm.apply(owner, plan_id)
        credentials.assert_not_called()
        record["status"] = "approved"
        result = gtm.apply(owner, plan_id)
        assert result["workspace_id"] == "3" and result["status"] == "applied"
        assert [x.args[1:] for x in api.call_args_list] == [
            ("POST", "accounts/1/containers/2/workspaces"),
            ("POST", "accounts/1/containers/2/workspaces/3/bulk_update"),
        ]
        payload = api.call_args.kwargs["payload"]["changes"]
        assert (
            payload[0]["trigger"]["triggerId"]
            == payload[-1]["tag"]["firingTriggerId"][0]
        )
        assert (
            payload[1]["variable"]["parameter"][0]["value"] == "ecommerce.value"
        )
        record["status"] = "applied"
        assert gtm.apply(owner, plan_id)["status"] == "applied"
        assert api.call_count == 2
