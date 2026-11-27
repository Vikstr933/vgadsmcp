"""Privacy, low-volume counting and authorization of the activity dashboard."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ads_mcp import activity as a


def test_counts_tools_only_once_across_a_mount_and_survives_a_storage_failure():
    outer = FastMCP("outer")
    outer.add_middleware(a.ActivityCounts({"inner_example": "gtm"}))
    inner = FastMCP("inner")

    @inner.tool
    def example():
        return "ok"

    outer.mount(inner, namespace="inner")

    async def run():
        async with Client(outer) as client:
            await client.list_tools()
            counter.assert_not_called()
            await client.call_tool("inner_example", {})
            assert counter.call_count == 1
            args = counter.call_args.args
            assert args[:2] == ("123", "member@example.com") and isinstance(
                args[2], date
            )
            assert args[3] == "gtm"
            counter.side_effect = RuntimeError()
            result = await client.call_tool("inner_example", {})
            assert not result.is_error and result.content[0].text == "ok"

    claims = {
        "sub": "123",
        "email": "member@example.com",
        "email_verified": True,
    }
    with (
        patch.object(
            a, "get_access_token", return_value=SimpleNamespace(claims=claims)
        ),
        patch.object(a, "count_call") as counter,
    ):
        asyncio.run(run())


def test_daily_summary_and_one_through_nine_calls():
    client = MagicMock()
    c = MagicMock()
    cipher = Fernet(Fernet.generate_key())
    day = date(2026, 11, 25)
    with patch.object(a, "storage", return_value=(client, c, cipher)):
        a.count_call("private-id", "a@example.com", day, "gtm")
        batch = client.batch.return_value
        user_write, product_write = batch.set.call_args_list
        value = user_write.args[1]
        product = product_write.args[1]
        assert set(product) == {"day", "product", "calls", "delete_after"}
        assert product["product"] == "gtm" and product["calls"].value == 1
        assert c.document.call_args.args[0] == "product_2026-11-25_gtm"
        batch.commit.assert_called_once_with(retry=None, timeout=2)
        assert set(value) == {"day", "user", "calls", "label", "delete_after"}
        assert value["calls"].value == 1
        assert "private-id" not in str(value) and "a@example.com" not in str(
            value
        )
        assert value["delete_after"].date() == date(2026, 12, 25)

        def doc(uid, day, n):
            return SimpleNamespace(
                to_dict=lambda: dict(value, user=uid, day=day, calls=n)
            )

        c.where.return_value.where.return_value.stream.return_value = [
            doc("a", "2026-11-24", 1),
            doc("a", "2026-11-25", 1),
            doc("b", "2026-11-23", 9),
            doc("c", "2026-11-25", 1),
            SimpleNamespace(
                to_dict=lambda: {
                    "day": "2026-11-25",
                    "product": "gtm",
                    "calls": 5,
                }
            ),
            SimpleNamespace(
                to_dict=lambda: {
                    "day": "2026-11-25",
                    "product": "google_ads",
                    "calls": 2,
                }
            ),
        ]
        stats, products = a.activity_summary(day)
        assert products == {
            "google_ads": 2,
            "gtm": 5,
            "linkedin": 0,
            "other": 0,
        }
        html = a.product_summary_html(products, 12)
        assert html.index("Google Tag Manager") < html.index(">Google Ads<")
        assert "5 tidigare anrop" in html
        assert [(r["days"], r["calls"], r["last"]) for r in stats] == [
            (2, 2, "2026-11-25"),
            (1, 9, "2026-11-23"),
            (1, 1, "2026-11-25"),
        ]
        assert all(r["name"] == "a@example.com" for r in stats)


def test_admin_page_rejects_unverified_other_or_anonymous_users(monkeypatch):
    import hashlib

    admins = ("first@example.com", "second@example.com")
    monkeypatch.setattr(
        a,
        "ADMIN_EMAIL_DIGESTS",
        {hashlib.sha256(e.encode()).hexdigest() for e in admins},
    )
    client = TestClient(Starlette(routes=[Route(a.PATH, a.activity_page)]))
    with (
        patch("ads_mcp.connections.session") as session,
        patch.object(a, "activity_summary", return_value=([], {})) as summary,
    ):
        for user, status in (
            (None, 401),
            ({"email": "first@example.com"}, 403),
            ({"email_verified": True, "email": "other@example.com"}, 403),
        ):
            session.return_value = user
            assert client.get(a.PATH).status_code == status
        summary.assert_not_called()
        for email in admins:
            session.return_value = {"email_verified": True, "email": email}
            result = client.get(a.PATH)
            assert (
                result.status_code == 200
                and "Inga verktygsanrop" in result.text
                and "Anrop per produkt" in result.text
            )
            assert result.headers["cache-control"] == "no-store"
