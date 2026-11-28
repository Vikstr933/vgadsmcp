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
