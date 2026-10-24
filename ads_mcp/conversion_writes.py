"""Bounded native website conversion changes, using the shared approval flow."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category: (
        Literal[
            "DEFAULT",
            "PURCHASE",
            "SIGNUP",
            "SUBMIT_LEAD_FORM",
            "CONTACT",
            "PAGE_VIEW",
            "ADD_TO_CART",
            "BEGIN_CHECKOUT",
            "BOOK_APPOINTMENT",
            "REQUEST_QUOTE",
            "QUALIFIED_LEAD",
            "CONVERTED_LEAD",
        ]
        | None
    ) = None
    counting_type: Literal["ONE_PER_CLICK", "MANY_PER_CLICK"] | None = None
    click_through_lookback_window_days: int | None = Field(default=None, ge=1, le=90)
    view_through_lookback_window_days: int | None = Field(default=None, ge=1, le=30)
    default_value: float | None = Field(default=None, ge=0, le=1e9, allow_inf_nan=False)
    default_currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    always_use_default_value: bool | None = None

    @field_validator("name")
    @classmethod
    def name_is_printable(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Use a non-empty printable conversion name.")
        return value


def blocked(message):
    from ads_mcp.write_policy import WriteBlocked

    raise WriteBlocked(message)


def check_conversion_shape(change):
    if any(
        v is not None
        for v in (
            change.campaign_id,
            change.amount_micros,
            change.name,
            change.eu_political_advertising,
        )
    ):
        blocked("Campaign parameters are not accepted for conversion changes.")
    create = change.action == "create_conversion"
    if create != (change.conversion_action_id is None):
        blocked("Creation cannot have an ID; conversion updates require an ID.")
    if change.action == "set_conversion_primary":
        if change.primary_for_goal is None or change.conversion_settings is not None:
            blocked(
                "Primary/secondary changes require only an explicit primary_for_goal value."
            )
        return
    if change.primary_for_goal is not None:
        blocked("Use a separate primary/secondary preview and approval.")
    if change.conversion_settings is None:
        blocked("Conversion settings are required.")
    settings = change.conversion_settings.model_dump(exclude_none=True)
    if not settings:
        blocked("Provide at least one supported setting.")
    if create and not all(k in settings for k in ("name", "category", "counting_type")):
        blocked("Creation requires name, category and counting type.")


def check_conversion_policy(change, snapshot):
    account = snapshot["account"]
    if account.get(
        "conversion_customer"
    ) != f"customers/{change.customer_id}" or account.get("manager"):
        blocked(
            "This account uses shared/MCC-owned conversions or is a manager. Shared conversion writes need a separately scoped implementation; no write sent."
        )
    settings = (
        change.conversion_settings.model_dump(exclude_none=True)
        if change.conversion_settings
        else {}
    )
    if (
        settings.get("default_currency_code", account["currency"])
        != account["currency"]
    ):
        blocked("Default conversion currency must match the account policy currency.")
    action = snapshot.get("conversion")
    if change.action != "create_conversion":
        expected = f"customers/{change.customer_id}/conversionActions/{change.conversion_action_id}"
        if (
            not action
            or action["resource_name"] != expected
            or action["id"] != change.conversion_action_id
        ):
            blocked("Conversion snapshot belongs to another action or account.")
        if action["owner_customer"] != f"customers/{change.customer_id}":
            blocked("Only conversions owned by this client account are writable.")
        if action["type"] != "WEBPAGE" or action["status"] != "ENABLED":
            blocked(
                "Only enabled native WEBPAGE conversions are writable. GA4 imports and system conversions are excluded."
            )
    name = settings.get("name")
    if name and any(
        item["name"].casefold() == name.casefold()
        and item["id"] != change.conversion_action_id
        for item in snapshot["conversion_inventory"]
    ):
        blocked(
            "Another conversion already has that name. Select the existing action or choose a unique name."
        )


FIELDS = (
    "id",
    "resource_name",
    "name",
    "type",
    "status",
    "owner_customer",
    "category",
    "primary_for_goal",
    "counting_type",
    "click_through_lookback_window_days",
    "view_through_lookback_window_days",
)
VALUE_FIELDS = ("default_value", "default_currency_code", "always_use_default_value")


def conversion_dict(action):
    result = {}
    for name in FIELDS:
        value = getattr(action, name)
        result[name] = (
            value.name
            if name in ("type", "status", "category", "counting_type")
            else value
        )
    result["id"] = str(result["id"])
    for name in VALUE_FIELDS:
        result[name] = getattr(action.value_settings, name)
    return result


def snapshot_conversion(gateway, change):
    change.check_shape()
    customers = gateway.rows(
        change.customer_id,
        "SELECT customer.id, customer.currency_code, customer.time_zone, customer.manager, customer.conversion_tracking_setting.google_ads_conversion_customer FROM customer",
    )
    if len(customers) != 1:
        blocked("Customer information unavailable.")
    customer = customers[0].customer
    account = {
        "id": str(customer.id),
        "currency": customer.currency_code,
        "time_zone": customer.time_zone,
        "manager": customer.manager,
        "conversion_customer": customer.conversion_tracking_setting.google_ads_conversion_customer,
    }
    # Never silently redirect a write to an MCC conversion owner.
    if (
        account["conversion_customer"] != f"customers/{change.customer_id}"
        or account["manager"]
    ):
        blocked(
            "Conversions are owned by a different account or this is an MCC. Shared conversions are read-only in this version."
        )
    columns = ["conversion_action." + key for key in FIELDS] + [
        "conversion_action.value_settings." + key for key in VALUE_FIELDS
    ]
    rows = gateway.rows(
        change.customer_id, "SELECT " + ", ".join(columns) + " FROM conversion_action"
    )
    inventory = sorted(
        [conversion_dict(row.conversion_action) for row in rows],
        key=lambda item: item["id"],
    )
    if len(inventory) > 5000:
        blocked("Conversion inventory is too large for an explicit review.")
    result = {"account": account, "conversion_inventory": inventory}
    if change.action != "create_conversion":
        matches = [
            item
            for item in inventory
            if item["id"] == change.conversion_action_id
            and item["resource_name"]
            == f"customers/{change.customer_id}/conversionActions/{change.conversion_action_id}"
        ]
        if len(matches) != 1:
            blocked("Conversion not found in the selected account.")
        result["conversion"] = matches[0]
    campaigns = gateway.rows(
        change.customer_id,
        "SELECT campaign.id, campaign.name, campaign.status FROM campaign WHERE campaign.status != 'REMOVED'",
    )
    result["potentially_affected_campaigns"] = sorted(
        [
            {
                "id": str(r.campaign.id),
                "name": r.campaign.name,
                "status": r.campaign.status.name,
            }
            for r in campaigns
        ],
        key=lambda x: x["id"],
    )
    goals = gateway.rows(
        change.customer_id,
        "SELECT custom_conversion_goal.resource_name, custom_conversion_goal.name, custom_conversion_goal.status, custom_conversion_goal.conversion_actions FROM custom_conversion_goal",
    )
    result["custom_goals"] = sorted(
        [
            {
                "resource_name": r.custom_conversion_goal.resource_name,
                "name": r.custom_conversion_goal.name,
                "status": r.custom_conversion_goal.status.name,
                "conversion_actions": sorted(
                    r.custom_conversion_goal.conversion_actions
                ),
            }
            for r in goals
        ],
        key=lambda x: x["resource_name"],
    )
    return result


def desired_conversion(change, before):
    if change.action == "create_conversion":
        desired = {
            "type": "WEBPAGE",
            "status": "ENABLED",
            "primary_for_goal": False,
            "click_through_lookback_window_days": 30,
            "view_through_lookback_window_days": 1,
            "default_value": 0.0,
            "default_currency_code": before["account"]["currency"],
            "always_use_default_value": False,
        }
        desired.update(change.conversion_settings.model_dump(exclude_none=True))
        return desired
    if change.action == "set_conversion_primary":
        return {"primary_for_goal": change.primary_for_goal}
    return change.conversion_settings.model_dump(exclude_none=True)


def conversion_operations(gateway, change, before):
    check_conversion_shape(change)
    check_conversion_policy(change, before)
    operation = gateway.client.get_type("MutateOperation")
    op = operation.conversion_action_operation
    create = change.action == "create_conversion"
    target = op.create if create else op.update
    if not create:
        target.resource_name = before["conversion"]["resource_name"]
    for key, value in desired_conversion(change, before).items():
        if key in VALUE_FIELDS:
            setattr(target.value_settings, key, value)
            field = "value_settings." + key
        else:
            setattr(target, key, value)
            field = key
        if not create:
            op.update_mask.paths.append(field)
    return [operation]


def describe_conversion(change, before):
    desired = desired_conversion(change, before)
    old = before.get("conversion", {})
    return {
        "before": (
            {key: old.get(key) for key in desired}
            if old
            else "No conversion created by this plan."
        ),
        "after": desired,
        "effect": "Creates/changes a Google Ads conversion action only; does not install tags, import GA4 events or upload conversions. New actions are ENABLED but SECONDARY (Google Ads has no PAUSED conversion action status). Changes to existing actions can immediately affect reporting and bidding. Custom goals may use secondary actions for bidding too.",
        "blast_radius": "Potentially all campaigns in this client account. The campaign list is conservative, not proof that each campaign uses this action.",
        "rollback": "For updates, preview and separately approve restoring the before values. For creation, leave secondary and do not connect a tag; deletion is not exposed. Historical reporting and bidding effects may not be reversible.",
        "verification": "Read back action settings immediately; verify actual tag events and reporting separately before relying on measurement.",
    }


def verify_conversion(gateway, change, before, response):
    from ads_mcp.write_policy import Change

    if change.action == "create_conversion":
        resources = [
            r.conversion_action_result.resource_name
            for r in response.mutate_operation_responses
            if r.conversion_action_result.resource_name
        ]
        if len(resources) != 1 or not resources[0].startswith(
            f"customers/{change.customer_id}/conversionActions/"
        ):
            blocked("Unexpected conversion result; reconcile before retrying.")
        probe = Change(
            action="set_conversion_primary",
            customer_id=change.customer_id,
            login_customer_id=change.login_customer_id,
            conversion_action_id=resources[0].split("/")[-1],
            primary_for_goal=False,
        )
    else:
        probe = change
    actual = snapshot_conversion(gateway, probe)
    expected = dict(before.get("conversion", {}))
    expected.update(desired_conversion(change, before))
    if any(actual["conversion"].get(key) != value for key, value in expected.items()):
        blocked(
            "Conversion read-back mismatch; reconcile the account lock before any retry."
        )
    return {"account": actual["account"], "conversion": actual["conversion"]}
