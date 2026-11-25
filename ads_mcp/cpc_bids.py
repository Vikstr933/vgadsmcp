"""Explicit manual CPC updates for existing Search ad groups and keywords."""

from decimal import Decimal

from ads_mcp.write_policy import WriteBlocked


def bid_target(gateway, change):
    fields = [
        "campaign.id",
        "campaign.name",
        "campaign.status",
        "campaign.bidding_strategy_type",
        "campaign.advertising_channel_type",
        "ad_group.id",
        "ad_group.name",
        "ad_group.status",
        "ad_group.cpc_bid_micros",
    ]
    table = "ad_group"
    where = f"campaign.id = {change.campaign_id} AND ad_group.id = {change.ad_group_id}"
    if change.criterion_id:
        table = "ad_group_criterion"
        fields.extend(
            table + "." + field
            for field in (
                "criterion_id",
                "status",
                "type",
                "negative",
                "keyword.text",
                "keyword.match_type",
                "cpc_bid_micros",
            )
        )
        where += f" AND ad_group_criterion.criterion_id = {change.criterion_id}"
    rows = gateway.rows(
        change.customer_id, f"SELECT {', '.join(fields)} FROM {table} WHERE {where}"
    )
    if len(rows) != 1:
        raise WriteBlocked(
            "No unique matching target in the selected campaign and ad group."
        )
    row = rows[0]
    group, campaign = row.ad_group, row.campaign
    target = {
        "campaign_id": str(campaign.id),
        "campaign_name": campaign.name,
        "campaign_status": campaign.status.name,
        "strategy": campaign.bidding_strategy_type.name,
        "channel": campaign.advertising_channel_type.name,
        "ad_group_id": str(group.id),
        "ad_group_name": group.name,
        "ad_group_status": group.status.name,
        "group_bid_micros": group.cpc_bid_micros,
        "resource_name": group.resource_name,
        "cpc_bid_micros": group.cpc_bid_micros,
    }
    if change.criterion_id:
        criterion = row.ad_group_criterion
        target.update(
            criterion_id=str(criterion.criterion_id),
            negative=criterion.negative,
            criterion_type=criterion.type_.name,
            criterion_status=criterion.status.name,
            keyword=criterion.keyword.text,
            match_type=criterion.keyword.match_type.name,
            cpc_bid_micros=criterion.cpc_bid_micros or None,
            resource_name=criterion.resource_name,
        )
    return target


def check_target(change, snapshot):
    t = snapshot["target"]
    customer = f"customers/{change.customer_id}"
    resource = (
        f"{customer}/adGroupCriteria/{change.ad_group_id}~{change.criterion_id}"
        if change.criterion_id
        else f"{customer}/adGroups/{change.ad_group_id}"
    )
    if (
        t["resource_name"] != resource
        or t["campaign_id"] != change.campaign_id
        or t["ad_group_id"] != change.ad_group_id
        or t.get("criterion_id") != change.criterion_id
    ):
        raise WriteBlocked(
            "Bid target differs from the requested account, campaign, ad group or keyword."
        )
    if t["channel"] != "SEARCH" or t["strategy"] != "MANUAL_CPC":
        raise WriteBlocked(
            "Manual CPC bids require a Search campaign using MANUAL_CPC."
        )
    allowed = ("ENABLED", "PAUSED")
    if t["campaign_status"] not in allowed or t["ad_group_status"] not in allowed:
        raise WriteBlocked("The campaign and ad group must be enabled or paused.")
    if change.criterion_id and (
        t["criterion_status"] not in allowed
        or t["negative"]
        or t["criterion_type"] != "KEYWORD"
    ):
        raise WriteBlocked("Select an enabled or paused positive keyword.")


def review_rows(plan):
    t = plan["snapshot"]["target"]
    currency = plan["snapshot"]["account"]["currency"]

    def money(value):
        return f"{Decimal(value) / 1_000_000:f} {currency}"

    rows = [
        ("Google Ads-konto", plan["customer_id"]),
        ("Kampanj", f"{t['campaign_name']} ({t['campaign_id']})"),
        ("Annonsgrupp", f"{t['ad_group_name']} ({t['ad_group_id']})"),
    ]
    if t.get("criterion_id"):
        rows.append(
            ("Sökord", f"{t['keyword']} · {t['match_type']} ({t['criterion_id']})")
        )
    before = (
        money(t["cpc_bid_micros"])
        if t["cpc_bid_micros"] is not None
        else "Ärver " + money(t["group_bid_micros"]) + " från annonsgruppen"
    )
    rows.extend(
        [
            ("Nuvarande max-CPC", before),
            ("Nytt max-CPC", money(plan["change"]["cpc_bid_micros"])),
        ]
    )
    return rows
