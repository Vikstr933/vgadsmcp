"""Compile bounded campaign packages and verify every configured resource.

Creation is one atomic mutate. System-generated conversion goal objects are
configured in a second mutate while the new campaign remains PAUSED. A failure
after the first mutate keeps the existing workflow's unresolved account lock.
"""

import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from ads_mcp.conversion_writes import blocked


def snapshot_campaign_package(gateway, change):
    spec = change.campaign_package
    customers = gateway.rows(
        change.customer_id,
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager, customer.status, customer.conversion_tracking_setting.google_ads_conversion_customer FROM customer",
    )
    if len(customers) != 1:
        blocked("Customer information unavailable.")
    c = customers[0].customer
    account = dict(
        id=str(c.id),
        name=c.descriptive_name,
        currency=c.currency_code,
        time_zone=c.time_zone,
        manager=c.manager,
        status=c.status.name,
        conversion_customer=c.conversion_tracking_setting.google_ads_conversion_customer,
    )
    if account["manager"] or account["status"] != "ENABLED":
        blocked("Select an enabled client account, not an MCC.")
    if account["conversion_customer"] != f"customers/{change.customer_id}":
        blocked(
            "This builder requires client-owned conversions. Shared MCC conversions need separate support."
        )
    today = datetime.now(ZoneInfo(account["time_zone"])).date()
    if date.fromisoformat(spec.start_date) < today:
        blocked(
            "Start date is in the past in the account time zone. Supply explicit future dates including year."
        )
    existing = gateway.rows(
        change.customer_id,
        "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
        "WHERE campaign.status IN ('ENABLED', 'PAUSED', 'REMOVED')",
    )
    if any(
        r.campaign.name.casefold().strip() == spec.name.casefold().strip()
        for r in existing
    ):
        blocked(
            "A campaign with this name already exists (including removed campaigns). Inspect it before creating another."
        )
    conversions = gateway.rows(
        change.customer_id,
        "SELECT conversion_action.resource_name, conversion_action.id, conversion_action.name, conversion_action.status, conversion_action.owner_customer, conversion_action.primary_for_goal, conversion_action.category, conversion_action.origin FROM conversion_action",
    )
    chosen = []
    for row in conversions:
        action = row.conversion_action
        if str(action.id) not in spec.conversion_action_ids:
            continue
        if (
            action.status.name != "ENABLED"
            or action.owner_customer != f"customers/{change.customer_id}"
        ):
            blocked(
                "Every selected conversion must be enabled and owned by the client."
            )
        chosen.append(
            dict(
                id=str(action.id),
                resource_name=action.resource_name,
                name=action.name,
                primary_for_goal=action.primary_for_goal,
                category=action.category.name,
                origin=action.origin.name,
            )
        )
    if len(chosen) != len(spec.conversion_action_ids):
        blocked("One or more selected conversion actions were not found.")
    goals = gateway.rows(
        change.customer_id,
        "SELECT customer_conversion_goal.category, customer_conversion_goal.origin, customer_conversion_goal.biddable FROM customer_conversion_goal",
    )
    goal_state = sorted(
        [
            dict(
                category=r.customer_conversion_goal.category.name,
                origin=r.customer_conversion_goal.origin.name,
                biddable=r.customer_conversion_goal.biddable,
            )
            for r in goals
        ],
        key=lambda r: (r["category"], r["origin"]),
    )
    if not goal_state or len(goal_state) > 200:
        blocked("Conversion goals are unavailable or too numerous for this review.")
    geo_ids = spec.geo_target_ids + spec.excluded_geo_target_ids
    geo_rows = gateway.rows(
        change.customer_id,
        "SELECT geo_target_constant.id, geo_target_constant.name, geo_target_constant.country_code, geo_target_constant.status FROM geo_target_constant WHERE geo_target_constant.id IN ("
        + ",".join(geo_ids)
        + ")",
    )
    geos = sorted(
        [
            dict(
                id=str(r.geo_target_constant.id),
                name=r.geo_target_constant.name,
                country=r.geo_target_constant.country_code,
                status=r.geo_target_constant.status.name,
            )
            for r in geo_rows
        ],
        key=lambda r: r["id"],
    )
    if {g["id"] for g in geos} != set(geo_ids) or any(
        g["status"] != "ENABLED" for g in geos
    ):
        blocked(
            "A selected location is unavailable. Resolve real geo target IDs first."
        )
    lang_rows = gateway.rows(
        change.customer_id,
        "SELECT language_constant.id, language_constant.name, language_constant.targetable FROM language_constant WHERE language_constant.id IN ("
        + ",".join(spec.language_ids)
        + ")",
    )
    languages = sorted(
        [
            dict(
                id=str(r.language_constant.id),
                name=r.language_constant.name,
                targetable=r.language_constant.targetable,
            )
            for r in lang_rows
        ],
        key=lambda r: r["id"],
    )
    if {l["id"] for l in languages} != set(spec.language_ids) or not all(
        l["targetable"] for l in languages
    ):
        blocked("A selected language cannot be targeted.")
    snapshot = dict(
        account=account,
        conversions=sorted(chosen, key=lambda r: r["id"]),
        customer_goals=goal_state,
        locations=geos,
        languages=languages,
    )
    selected_actions = sorted(c["resource_name"] for c in chosen)
    matching_goals = []
    for row in gateway.rows(
        change.customer_id,
        "SELECT custom_conversion_goal.resource_name, custom_conversion_goal.name, "
        "custom_conversion_goal.status, custom_conversion_goal.conversion_actions "
        "FROM custom_conversion_goal WHERE custom_conversion_goal.status = 'ENABLED'",
    ):
        goal = row.custom_conversion_goal
        if (
            goal.status.name == "ENABLED"
            and sorted(goal.conversion_actions) == selected_actions
        ):
            if not re.fullmatch(
                rf"customers/{change.customer_id}/customConversionGoals/[0-9]+",
                goal.resource_name,
            ):
                blocked(
                    "Matching custom conversion goal is outside the client account."
                )
            matching_goals.append(
                dict(resource_name=goal.resource_name, name=goal.name)
            )
    if len(matching_goals) > 1:
        blocked("Multiple enabled custom conversion goals match the selected actions.")
    if matching_goals:
        snapshot["existing_custom_goal"] = matching_goals[0]
    return snapshot


class Compiler:
    def __init__(self, client, customer_id):
        self.client = client
        self.root = f"customers/{customer_id}"
        self.next_id = -1
        self.operations = []
        self.expected = []
        self.reused_goal_resource = None

    def resource(self, collection):
        value = f"{self.root}/{collection}/{self.next_id}"
        self.next_id -= 1
        return value

    def create(self, kind, fields):
        operation = self.client.get_type("MutateOperation")
        obj = getattr(operation, kind + "_operation").create
        for path, value in fields.items():
            target = obj
            parts = path.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            old = getattr(target, parts[-1])
            if isinstance(value, list):
                old.extend(value)
            else:
                setattr(target, parts[-1], value)
        self.operations.append(operation)
        self.expected.append(dict(kind=kind, fields=dict(fields)))
        return obj


def campaign_fields(builder, change, channel):
    spec = change.campaign_package
    budget = builder.resource("campaignBudgets")
    campaign = builder.resource("campaigns")
    budget_amount = (
        dict(period="CUSTOM_PERIOD", total_amount_micros=spec.total_budget_micros)
        if spec.total_budget_micros is not None
        else dict(period="DAILY", amount_micros=spec.daily_budget_micros)
    )
    builder.create(
        "campaign_budget",
        dict(
            resource_name=budget,
            name=spec.name + " budget",
            explicitly_shared=False,
            delivery_method="STANDARD",
            **budget_amount,
        ),
    )
    fields = {
        "resource_name": campaign,
        "name": spec.name,
        "status": "PAUSED",
        "advertising_channel_type": channel,
        "campaign_budget": budget,
        "geo_target_type_setting.positive_geo_target_type": "PRESENCE",
        "geo_target_type_setting.negative_geo_target_type": "PRESENCE",
        "start_date_time": spec.start_date + " 00:00:00",
        "end_date_time": spec.end_date + " 23:59:59",
        "contains_eu_political_advertising": (
            "CONTAINS_EU_POLITICAL_ADVERTISING"
            if change.eu_political_advertising == "contains"
            else "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
        ),
    }
    if spec.final_url_suffix:
        fields["final_url_suffix"] = spec.final_url_suffix
    return fields


def set_campaign_bidding(builder, obj, bidding, automation_types):
    if bidding == "MANUAL_CPC":
        obj.manual_cpc = builder.client.get_type("ManualCpc")
    elif bidding == "MAXIMIZE_CONVERSIONS":
        obj.maximize_conversions = builder.client.get_type("MaximizeConversions")
    else:
        obj.maximize_conversion_value = builder.client.get_type(
            "MaximizeConversionValue"
        )
    builder.expected[-1]["fields"]["bidding_strategy_type"] = bidding
    for kind in automation_types:
        setting = obj.AssetAutomationSetting()
        setting.asset_automation_type = kind
        setting.asset_automation_status = "OPTED_OUT"
        obj.asset_automation_settings.append(setting)
    builder.expected[-1]["fields"]["asset_automation_settings"] = [
        (kind, "OPTED_OUT") for kind in automation_types
    ]


TEXT_AUTOMATION = (
    "TEXT_ASSET_AUTOMATION",
    "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
)


def add_targeting(builder, spec, campaign):
    for ids, negative in (
        (spec.geo_target_ids, False),
        (spec.excluded_geo_target_ids, True),
    ):
        for geo in ids:
            builder.create(
                "campaign_criterion",
                {
                    "campaign": campaign,
                    "negative": negative,
                    "location.geo_target_constant": f"geoTargetConstants/{geo}",
                },
            )
    for language in spec.language_ids:
        builder.create(
            "campaign_criterion",
            {
                "campaign": campaign,
                "language.language_constant": f"languageConstants/{language}",
            },
        )


def compile_search(gateway, change, before):
    spec = change.search_campaign
    builder = Compiler(gateway.client, change.customer_id)
    fields = campaign_fields(builder, change, "SEARCH")
    fields.update(
        {
            "network_settings.target_google_search": True,
            "network_settings.target_search_network": False,
            "network_settings.target_content_network": False,
            "network_settings.target_partner_search_network": False,
            "ai_max_setting.enable_ai_max": False,
            "excluded_parent_asset_field_types": ["SITELINK", "CALLOUT"],
        }
    )
    obj = builder.create("campaign", fields)
    set_campaign_bidding(builder, obj, spec.bidding, TEXT_AUTOMATION)
    campaign = fields["resource_name"]
    add_targeting(builder, spec, campaign)
    for keyword in spec.negative_keywords:
        builder.create(
            "campaign_criterion",
            {
                "campaign": campaign,
                "negative": True,
                "keyword.text": keyword.text,
                "keyword.match_type": keyword.match_type,
            },
        )
    for group in spec.ad_groups:
        group_resource = builder.resource("adGroups")
        fields = {
            "resource_name": group_resource,
            "campaign": campaign,
            "name": group.name,
            "status": "ENABLED",
            "type_": "SEARCH_STANDARD",
        }
        if group.cpc_bid_micros is not None:
            fields["cpc_bid_micros"] = group.cpc_bid_micros
        builder.create("ad_group", fields)
        for values, negative in (
            (group.keywords, False),
            (group.negative_keywords, True),
        ):
            for keyword in values:
                builder.create(
                    "ad_group_criterion",
                    {
                        "ad_group": group_resource,
                        "negative": negative,
                        "status": "ENABLED",
                        "keyword.text": keyword.text,
                        "keyword.match_type": keyword.match_type,
                    },
                )
        for ad in group.ads:
            fields = {
                "ad_group": group_resource,
                "status": "ENABLED",
                "ad.final_urls": [ad.final_url],
            }
            if ad.path1:
                fields["ad.responsive_search_ad.path1"] = ad.path1
            if ad.path2:
                fields["ad.responsive_search_ad.path2"] = ad.path2
            obj = builder.create("ad_group_ad", fields)
            for field in ("headlines", "descriptions"):
                for value in getattr(ad, field):
                    text = gateway.client.get_type("AdTextAsset")
                    text.text = value
                    getattr(obj.ad.responsive_search_ad, field).append(text)
                builder.expected[-1]["fields"]["ad.responsive_search_ad." + field] = (
                    list(getattr(ad, field))
                )
    for link in spec.sitelinks:
        asset = builder.resource("assets")
        fields = {
            "resource_name": asset,
            "sitelink_asset.link_text": link.text,
            "final_urls": [link.final_url],
        }
        if link.description1:
            fields["sitelink_asset.description1"] = link.description1
            fields["sitelink_asset.description2"] = link.description2
        builder.create("asset", fields)
        builder.create(
            "campaign_asset",
            {
                "campaign": campaign,
                "asset": asset,
                "field_type": "SITELINK",
                "status": "ENABLED",
            },
        )
    for text in spec.callouts:
        asset = builder.resource("assets")
        builder.create(
            "asset",
            {"resource_name": asset, "callout_asset.callout_text": text},
        )
        builder.create(
            "campaign_asset",
            {
                "campaign": campaign,
                "asset": asset,
                "field_type": "CALLOUT",
                "status": "ENABLED",
            },
        )
    add_custom_goal(builder, spec, before)
    if len(builder.operations) > 1000:
        blocked("Too many operations for one approval package.")
    return builder


def add_custom_goal(builder, spec, before):
    existing_goal = before.get("existing_custom_goal")
    if existing_goal:
        builder.reused_goal_resource = existing_goal["resource_name"]
    else:
        builder.create(
            "custom_conversion_goal",
            {
                "name": spec.name + " conversions",
                "status": "ENABLED",
                "conversion_actions": [
                    c["resource_name"] for c in before["conversions"]
                ],
            },
        )


def compile_package(gateway, change, before):
    if change.action == "create_pmax_campaign":
        from ads_mcp.pmax_campaigns import compile_pmax

        return compile_pmax(gateway, change, before)
    return compile_search(gateway, change, before)


COLLECTIONS = {
    "campaign_budget": "campaignBudgets",
    "campaign": "campaigns",
    "campaign_criterion": "campaignCriteria",
    "ad_group": "adGroups",
    "ad_group_criterion": "adGroupCriteria",
    "ad_group_ad": "adGroupAds",
    "asset": "assets",
    "asset_group": "assetGroups",
    "asset_group_asset": "assetGroupAssets",
    "campaign_asset": "campaignAssets",
    "custom_conversion_goal": "customConversionGoals",
}


def creation_receipt(change, builder, response):
    responses = response.mutate_operation_responses
    if len(responses) != len(builder.expected):
        blocked("Creation returned an unexpected operation count. Do not retry.")
    resources, mapping = [], {}
    for expected, result in zip(builder.expected, responses):
        kind = expected["kind"]
        resource = getattr(result, kind + "_result").resource_name
        pattern = rf"customers/{change.customer_id}/{COLLECTIONS[kind]}/[0-9]+(?:~[A-Za-z0-9_]+)*"
        if not re.fullmatch(pattern, resource):
            blocked(
                "Creation returned a resource outside the expected client or resource type."
            )
        temporary = expected["fields"].get("resource_name")
        if temporary:
            mapping[temporary] = resource
        resources.append(dict(kind=kind, resource_name=resource))
    return dict(
        stage="created_paused_pending_goal_configuration",
        resources=resources,
        mapping=mapping,
        campaign_resource=next(
            r["resource_name"] for r in resources if r["kind"] == "campaign"
        ),
        custom_goal_resource=builder.reused_goal_resource
        or next(
            r["resource_name"]
            for r in resources
            if r["kind"] == "custom_conversion_goal"
        ),
    )


def goal_rows(gateway, change, campaign_resource):
    return gateway.rows(
        change.customer_id,
        "SELECT campaign_conversion_goal.resource_name, campaign_conversion_goal.campaign, campaign_conversion_goal.category, campaign_conversion_goal.origin, campaign_conversion_goal.biddable FROM campaign_conversion_goal WHERE campaign_conversion_goal.campaign = '"
        + campaign_resource
        + "'",
    )


def configure_campaign_goals(
    gateway, change, before, receipt, before_write=lambda: None
):
    campaign = receipt["campaign_resource"]
    rows = goal_rows(gateway, change, campaign)
    expected = {(g["category"], g["origin"]) for g in before["customer_goals"]}
    if {
        (
            r.campaign_conversion_goal.category.name,
            r.campaign_conversion_goal.origin.name,
        )
        for r in rows
    } != expected:
        blocked(
            "Conversion goal categories changed during creation. Campaign is paused; reconcile before retrying."
        )
    operations = []
    for row in rows:
        goal = row.campaign_conversion_goal
        if goal.campaign != campaign:
            blocked("Unexpected campaign goal owner.")
        op = gateway.client.get_type("MutateOperation")
        op.campaign_conversion_goal_operation.update.resource_name = goal.resource_name
        op.campaign_conversion_goal_operation.update.biddable = False
        op.campaign_conversion_goal_operation.update_mask.paths.append("biddable")
        operations.append(op)
    op = gateway.client.get_type("MutateOperation")
    config = op.conversion_goal_campaign_config_operation
    config.update.resource_name = campaign.replace(
        "/campaigns/", "/conversionGoalCampaignConfigs/"
    )
    config.update.custom_conversion_goal = receipt["custom_goal_resource"]
    config.update_mask.paths.append("custom_conversion_goal")
    operations.append(op)
    for validate in (True, False):
        # Check the newly created campaign is still paused before touching its goals.
        current = gateway.rows(
            change.customer_id,
            "SELECT campaign.status FROM campaign WHERE campaign.resource_name = '"
            + campaign
            + "'",
        )
        if len(current) != 1 or current[0].campaign.status.name != "PAUSED":
            blocked("New campaign no longer paused. Reconcile manually.")
        request = gateway.client.get_type("MutateGoogleAdsRequest")
        request.customer_id = change.customer_id
        request.mutate_operations = operations
        request.validate_only = validate
        request.partial_failure = False
        before_write()
        gateway.service.mutate(request=request, retry=None, timeout=30)


def read_value(obj, path):
    for part in path.split("."):
        obj = getattr(obj, part)
    if hasattr(obj, "name"):
        return obj.name
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if path.endswith(("headlines", "descriptions")):
        return sorted(x.text for x in obj)
    if path == "asset_automation_settings":
        return sorted(
            (x.asset_automation_type.name, x.asset_automation_status.name) for x in obj
        )
    return sorted(x.name if hasattr(x, "name") else x for x in obj)


def verify_later_edits(gateway, change, before, receipt, created_at, differences):
    zone = ZoneInfo(before["account"]["time_zone"])
    now = datetime.now(zone)
    start = max(datetime.fromtimestamp(created_at, zone), now - timedelta(days=29))
    rows = gateway.rows(
        change.customer_id,
        "SELECT change_event.change_date_time, change_event.change_resource_name, "
        "change_event.changed_fields FROM change_event "
        f"WHERE change_event.campaign = '{receipt['campaign_resource']}' "
        "AND change_event.resource_change_operation = 'UPDATE' "
        f"AND change_event.change_date_time BETWEEN '{start:%Y-%m-%d %H:%M:%S}' "
        f"AND '{now:%Y-%m-%d %H:%M:%S}' "
        "ORDER BY change_event.change_date_time DESC LIMIT 1000",
    )
    evidence = {}
    for row in rows:
        event = row.change_event
        for field in event.changed_fields.paths:
            evidence.setdefault(
                (event.change_resource_name, field.replace("_", "").lower()),
                event.change_date_time,
            )
    for difference in differences:
        key = (
            difference["resource_name"],
            difference["field"].replace("_", "").lower(),
        )
        if key not in evidence:
            blocked("A mismatch has no matching later Google Ads change event.")
        difference["change_date_time"] = evidence[key]


def verify_campaign_package(
    gateway, change, before, receipt, *, reconcile_created_at=None
):
    spec = change.campaign_package
    builder = compile_package(gateway, change, before)
    if [resource["kind"] for resource in receipt["resources"]] != [
        expected["kind"] for expected in builder.expected
    ]:
        blocked("Read-back receipt does not cover the approved resources.")
    grouped = {}
    for expected, resource in zip(builder.expected, receipt["resources"]):
        grouped.setdefault(expected["kind"], []).append(
            (expected, resource["resource_name"])
        )
    if builder.reused_goal_resource:
        grouped["custom_conversion_goal"] = [
            (
                {
                    "fields": {
                        "name": before["existing_custom_goal"]["name"],
                        "status": "ENABLED",
                        "conversion_actions": [
                            c["resource_name"] for c in before["conversions"]
                        ],
                    }
                },
                builder.reused_goal_resource,
            )
        ]
    later_edits = []
    campaign_status = "PAUSED"
    for kind, items in grouped.items():
        fields = {path for expected, _ in items for path in expected["fields"]}
        fields.add("resource_name")
        # Python reserves type; GAQL does not use the SDK's trailing underscore.
        selected = ", ".join(
            kind + "." + path.removesuffix("_") for path in sorted(fields)
        )
        names = ",".join("'" + name + "'" for _, name in items)
        rows = gateway.rows(
            change.customer_id,
            f"SELECT {selected} FROM {kind} WHERE {kind}.resource_name IN ({names})",
        )
        found = {getattr(r, kind).resource_name: getattr(r, kind) for r in rows}
        if set(found) != {name for _, name in items}:
            blocked("Read-back is missing expected resources. Do not retry creation.")
        for expected, name in items:
            for path, value in expected["fields"].items():
                value = (
                    receipt["mapping"].get(value, value)
                    if isinstance(value, str)
                    else value
                )
                if isinstance(value, list):
                    value = sorted(value)
                actual = read_value(found[name], path)
                if path == "asset_automation_settings":
                    # Other unrelated automation enum entries may be returned.
                    good = all(v in actual for v in value)
                elif kind == "campaign_budget" and path == "name":
                    # Google can use the owning campaign's name for a non-shared budget.
                    good = actual in (value, spec.name)
                else:
                    good = actual == value
                if good:
                    continue
                if reconcile_created_at is not None:
                    if kind == "campaign" and path == "status" and actual == "ENABLED":
                        campaign_status = actual
                        later_edits.append(dict(resource_name=name, field=path))
                        continue
                    if kind == "ad_group_ad" and path in (
                        "ad.responsive_search_ad.headlines",
                        "ad.responsive_search_ad.descriptions",
                    ):
                        ad_id = name.rsplit("~", 1)[-1]
                        if ad_id.isascii() and ad_id.isdigit():
                            later_edits.append(
                                dict(
                                    resource_name=f"customers/{change.customer_id}/ads/{ad_id}",
                                    field=path.removeprefix("ad."),
                                )
                            )
                            continue
                blocked(
                    f"Read-back mismatch for {kind}.{path}; manual reconciliation required."
                )
    campaign = receipt["campaign_resource"]
    goals = goal_rows(gateway, change, campaign)
    pairs = {
        (
            r.campaign_conversion_goal.category.name,
            r.campaign_conversion_goal.origin.name,
        )
        for r in goals
    }
    if pairs != {(g["category"], g["origin"]) for g in before["customer_goals"]} or any(
        r.campaign_conversion_goal.biddable for r in goals
    ):
        blocked("Campaign still has unintended standard conversion goals.")
    configs = gateway.rows(
        change.customer_id,
        "SELECT conversion_goal_campaign_config.goal_config_level, conversion_goal_campaign_config.custom_conversion_goal FROM conversion_goal_campaign_config WHERE conversion_goal_campaign_config.campaign = '"
        + campaign
        + "'",
    )
    if (
        len(configs) != 1
        or configs[0].conversion_goal_campaign_config.goal_config_level.name
        != "CAMPAIGN"
        or configs[0].conversion_goal_campaign_config.custom_conversion_goal
        != receipt["custom_goal_resource"]
    ):
        blocked("Campaign-specific conversion selection did not match approval.")
    if later_edits:
        verify_later_edits(
            gateway, change, before, receipt, reconcile_created_at, later_edits
        )
    return dict(
        campaign_resource=campaign,
        campaign_id=campaign.split("/")[-1],
        status=campaign_status,
        later_edits=later_edits,
        resource_count=len(receipt["resources"]),
        resources=receipt["resources"],
        custom_goal_resource=receipt["custom_goal_resource"],
        conversion_action_ids=spec.conversion_action_ids,
        launch_checks_required=[
            "Check account-level automated assets and auto-apply recommendations in Google Ads.",
            "Check landing pages, event dates, billing, conversion measurement and ad policy review.",
            "Activation requires a new, separate approval; API validation is not ad-policy approval.",
        ],
    )


def describe_search(change, before):
    spec = change.search_campaign
    existing_goal = before.get("existing_custom_goal")
    return dict(
        before="No campaign created by this plan.",
        after=spec.model_dump(),
        status="PAUSED",
        child_status="ENABLED under paused parent: no delivery until separate campaign activation.",
        stages=[
            (
                "Atomically create budget, paused campaign, targeting, groups, keywords, ads and assets. "
                f"Reuse custom conversion goal {existing_goal['name']} ({existing_goal['resource_name']}) unchanged."
                if existing_goal
                else "Atomically create budget, paused campaign, targeting, groups, keywords, ads, assets and a new custom conversion goal."
            ),
            "Configure campaign-only goals while paused; disable all standard campaign goals and select the approved conversion actions.",
            "Read back every configured field and resource before reporting success.",
        ],
        restrictions="Google Search only; presence targeting; exact/phrase keywords; AI Max, text customization and URL expansion off. Manual sitelinks/callouts; account inheritance excluded for these two asset types.",
        warnings=[
            "Custom goals use selected conversions for bidding even when secondary. Account conversion primary flags are not changed.",
            "Account-wide automatic assets, auto-apply recommendations and other inherited asset types are NOT disabled by this tool. Review them before launch.",
            "Responsive search ads still combine supplied texts automatically. No guarantee of placement or serving order.",
            (
                "Campaign total budget caps spend over the scheduled period. It has no individual daily spending limit."
                if spec.total_budget_micros is not None
                else "Daily budget is an average, not a hard daily or total spend cap."
            ),
            "Resolve missing URLs or unconfirmed facts before approval.",
            "A second-stage failure leaves created objects paused and the account locked. Never retry creation blindly.",
        ],
        rollback="Keep the new campaign paused. No existing campaign is changed. Removal and activation are not part of this approval.",
    )
