"""Only these explicit Google Ads mutations are reachable from the write tools."""

from ads_mcp import utils
from ads_mcp.write_policy import Change, WriteBlocked


class GoogleAdsWriteGateway:
    def __init__(self, login_customer_id=None):
        self.service = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=login_customer_id
        )
        self.client = utils.get_googleads_client(login_customer_id=login_customer_id)

    def rows(self, customer_id, query):
        return list(
            self.service.search(
                customer_id=customer_id, query=query, retry=None, timeout=30
            )
        )

    def snapshot(self, change: Change):
        change.check_shape()
        if change.action in (
            "create_conversion",
            "update_conversion",
            "set_conversion_primary",
        ):
            from ads_mcp.conversion_writes import snapshot_conversion

            return snapshot_conversion(self, change)
        customer = self.rows(
            change.customer_id,
            "SELECT customer.id, customer.currency_code, customer.time_zone FROM customer",
        )[0].customer
        result = {
            "account": {
                "id": str(customer.id),
                "currency": customer.currency_code,
                "time_zone": customer.time_zone,
            }
        }
        if change.action == "create_paused_search":
            return result
        rows = self.rows(
            change.customer_id,
            "SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget, "
            "campaign.advertising_channel_type, campaign_budget.amount_micros, "
            "campaign_budget.period, campaign_budget.explicitly_shared "
            f"FROM campaign WHERE campaign.id = {change.campaign_id}",
        )
        if len(rows) != 1:
            raise WriteBlocked("Campaign not found in the selected account.")
        row = rows[0]
        campaign, budget = row.campaign, row.campaign_budget
        result["campaign"] = {
            "id": str(campaign.id),
            "name": campaign.name,
            "resource_name": campaign.resource_name,
            "status": campaign.status.name,
            "budget_resource": campaign.campaign_budget,
            "amount_micros": budget.amount_micros,
            "period": budget.period.name,
            "shared": budget.explicitly_shared,
            "channel": campaign.advertising_channel_type.name,
        }
        affected = self.rows(
            change.customer_id,
            "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
            f"WHERE campaign.campaign_budget = '{campaign.campaign_budget}' "
            "AND campaign.status != 'REMOVED'",
        )
        result["affected_campaigns"] = sorted(
            [
                {
                    "id": str(r.campaign.id),
                    "name": r.campaign.name,
                    "status": r.campaign.status.name,
                }
                for r in affected
            ],
            key=lambda c: c["id"],
        )
        return result

    def manager_scope(self, manager_id, customer_id):
        # The caller provides validated IDs from server policy/typed Change.
        # customer_client includes all transitive descendants, not just level 1.
        rows = self.rows(
            manager_id,
            "SELECT customer_client.id, customer_client.manager, "
            "customer_client.level, customer_client.status FROM customer_client "
            f"WHERE customer_client.id = {customer_id}",
        )
        if len(rows) != 1:
            raise WriteBlocked("Customer is not linked to the configured MCC.")
        client = rows[0].customer_client
        if (
            str(client.id) != customer_id
            or client.manager
            or client.level <= 0
            or client.status.name != "ENABLED"
        ):
            raise WriteBlocked("Select an enabled advertising account under the MCC.")
        return {"manager_id": manager_id, "customer_id": customer_id}

    def operations(self, change: Change, snapshot: dict):
        # Never accept protobuf operations, field masks or status from the agent.
        change.check_shape()
        if change.action in (
            "create_conversion",
            "update_conversion",
            "set_conversion_primary",
        ):
            from ads_mcp.conversion_writes import conversion_operations

            return conversion_operations(self, change, snapshot)
        operation = self.client.get_type("MutateOperation")
        if change.action in ("pause", "activate"):
            update = operation.campaign_operation
            update.update.resource_name = snapshot["campaign"]["resource_name"]
            update.update.status = "PAUSED" if change.action == "pause" else "ENABLED"
            update.update_mask.paths.append("status")
            return [operation]
        if change.action == "set_budget":
            update = operation.campaign_budget_operation
            update.update.resource_name = snapshot["campaign"]["budget_resource"]
            update.update.amount_micros = change.amount_micros
            update.update_mask.paths.append("amount_micros")
            return [operation]
        budget_resource = f"customers/{change.customer_id}/campaignBudgets/-1"
        budget = operation.campaign_budget_operation.create
        budget.resource_name = budget_resource
        budget.name = change.name + " budget"
        budget.amount_micros = change.amount_micros
        budget.explicitly_shared = False
        budget.delivery_method = "STANDARD"
        budget.period = "DAILY"
        campaign_operation = self.client.get_type("MutateOperation")
        campaign = campaign_operation.campaign_operation.create
        campaign.name = change.name
        campaign.status = "PAUSED"  # Invariant, never an input parameter.
        campaign.advertising_channel_type = "SEARCH"
        campaign.campaign_budget = budget_resource
        campaign.manual_cpc = self.client.get_type("ManualCpc")
        campaign.network_settings.target_google_search = True
        campaign.network_settings.target_search_network = False
        campaign.network_settings.target_content_network = False
        campaign.network_settings.target_partner_search_network = False
        campaign.contains_eu_political_advertising = (
            "CONTAINS_EU_POLITICAL_ADVERTISING"
            if change.eu_political_advertising == "contains"
            else "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
        )
        return [operation, campaign_operation]

    def mutate(self, change, snapshot, *, validate_only):
        request = self.client.get_type("MutateGoogleAdsRequest")
        request.customer_id = change.customer_id
        request.mutate_operations = self.operations(change, snapshot)
        request.validate_only = validate_only
        request.partial_failure = False
        # No automatic retry: a lost response is not proof that a write failed.
        return self.service.mutate(request=request, retry=None, timeout=30)

    def verify(self, change, before, response):
        if change.action in (
            "create_conversion",
            "update_conversion",
            "set_conversion_primary",
        ):
            from ads_mcp.conversion_writes import verify_conversion

            return verify_conversion(self, change, before, response)
        if change.action == "create_paused_search":
            campaign_resources = [
                r.campaign_result.resource_name
                for r in response.mutate_operation_responses
                if r.campaign_result.resource_name
            ]
            if len(campaign_resources) != 1:
                raise WriteBlocked(
                    "Unexpected creation result; reconcile before retrying."
                )
            probe = Change(
                action="pause",
                customer_id=change.customer_id,
                login_customer_id=change.login_customer_id,
                campaign_id=campaign_resources[0].split("/")[-1],
            )
            actual = self.snapshot(probe)
            good = (
                actual["campaign"]["status"] == "PAUSED"
                and actual["campaign"]["amount_micros"] == change.amount_micros
            )
        else:
            actual = self.snapshot(change)
            if change.action == "set_budget":
                good = (
                    actual["campaign"]["amount_micros"] == change.amount_micros
                    and actual["campaign"]["status"] == before["campaign"]["status"]
                )
            else:
                good = actual["campaign"]["status"] == (
                    "PAUSED" if change.action == "pause" else "ENABLED"
                )
        if not good:
            raise WriteBlocked(
                "Read-back did not match the approved change; reconcile before retrying."
            )
        return actual
