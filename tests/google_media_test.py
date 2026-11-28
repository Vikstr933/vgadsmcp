from datetime import date
from unittest.mock import MagicMock, patch
import pytest
from fastmcp.exceptions import ToolError
from ads_mcp import google_media as media
from ads_mcp.tools import ga4
from ads_mcp.write_policy import WriteBlocked


def test_folder_link_and_traversal_are_owner_bound():
    assert (
        media.folder_id(
            "https://drive.google.com/drive/u/1/folders/customer?usp=sharing"
        )
        == "customer"
    )
    with pytest.raises(WriteBlocked):
        media.folder_id("https://other.example/drive/folders/customer")
    client = MagicMock()
    with (
        patch.object(media, "folders") as roots,
        patch.object(media, "metadata") as meta,
    ):
        roots.return_value.document.return_value.get.return_value.exists = False
        with pytest.raises(WriteBlocked):
            media.selected_folder(client, "member", "customer", [])
        roots.assert_called_once_with("member")
        meta.assert_not_called()
        roots.return_value.document.return_value.get.return_value.exists = True
        meta.side_effect = [
            {"id": "customer"},
            {"id": "december", "mimeType": media.FOLDER, "parents": ["other"]},
        ]
        with pytest.raises(WriteBlocked):
            media.selected_folder(client, "member", "customer", ["december"])


def test_ga4_report_is_aggregate_and_date_bounded():
    start = date(2026, 11, 1)
    end = date(2026, 11, 28)
    with patch.object(ga4, "request") as read:
        with pytest.raises(ToolError):
            ga4.run_report("123", end, start, ["sessions"], [])
        read.assert_not_called()
        ga4.run_report("123", start, end, ["sessions"], ["sessionSourceMedium"])
        assert read.call_args.args == ("POST", ga4.DATA + "/123:runReport")
        assert read.call_args.kwargs["json"]["dateRanges"] == [
            {"startDate": "2026-11-01", "endDate": "2026-11-28"}
        ]


def test_drive_preview_reads_only_direct_children_of_selected_folder():
    from contextlib import nullcontext
    from io import BytesIO
    from PIL import Image
    from ads_mcp.tools import drive

    original = BytesIO()
    Image.new("RGB", (8, 8), color="red").save(original, "GIF")
    client = MagicMock()
    response = client.get.return_value.__enter__.return_value
    response.status_code = 200
    response.iter_content.return_value = [original.getvalue()]
    with (
        patch.object(drive, "principal", return_value="member"),
        patch.object(media, "google_session", return_value=nullcontext(client)),
        patch.object(media, "selected_folder", return_value={"id": "december"}),
        patch.object(
            media,
            "metadata",
            return_value={"mimeType": "image/gif", "parents": ["other"]},
        ) as info,
    ):
        with pytest.raises(ToolError):
            drive.preview_image("customer", ["december"], "image")
        client.get.assert_not_called()
        info.return_value["parents"] = ["december"]
        preview = drive.preview_image("customer", ["december"], "image")
        assert Image.open(BytesIO(preview.data)).format == "JPEG"


def test_chat_link_checks_member_access_and_returns_usable_root():
    from contextlib import nullcontext
    from ads_mcp.tools import drive

    url = " https://drive.google.com/drive/folders/customer?usp=sharing "
    client = MagicMock()
    with (
        patch.object(drive, "principal", return_value="member"),
        patch.object(
            media, "google_session", return_value=nullcontext(client)
        ) as session,
        patch.object(
            media,
            "metadata",
            return_value={"mimeType": media.FOLDER, "name": "Folkness"},
        ) as meta,
        patch.object(media, "folders") as roots,
    ):
        assert drive.link_customer_folder(url) == {
            "root_id": "customer",
            "name": "Folkness",
        }
        session.assert_called_once_with("member", media.google_grant.DRIVE_READ)
        meta.assert_called_once_with(client, "customer")
        roots.assert_called_once_with("member")
        roots.return_value.document.assert_called_once_with("customer")
        roots.return_value.document.return_value.set.assert_called_once_with(
            {"name": "Folkness"}
        )
        roots.reset_mock()
        meta.side_effect = WriteBlocked("Google returnerade HTTP 403.")
        with pytest.raises(ToolError, match="403"):
            drive.link_customer_folder(url)
        roots.assert_not_called()
