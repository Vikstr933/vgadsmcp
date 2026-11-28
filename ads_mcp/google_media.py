"""Read-only Google product sessions and member-selected Drive folders."""

import re
from contextlib import contextmanager

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from requests.exceptions import RequestException

from ads_mcp import google_grant
from ads_mcp.write_policy import WriteBlocked

DRIVE = "https://www.googleapis.com/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
FILE_ID = r"[A-Za-z0-9_-]{1,200}"
FILE_FIELDS = "id,name,mimeType,parents,trashed,driveId,size,modifiedTime,imageMediaMetadata(width,height)"


@contextmanager
def google_session(owner, scope):
    try:
        with AuthorizedSession(
            google_grant.credentials(owner, scope), refresh_timeout=12
        ) as client:
            yield client
    except (GoogleAuthError, RequestException):
        raise WriteBlocked(
            "Google kunde inte nås. Kontrollera anslutningen i Min översikt."
        ) from None


def checked(response):
    if response.status_code != 200:
        raise WriteBlocked(
            f"Google returnerade HTTP {response.status_code}. Kontrollera API och läsåtkomst."
        )
    return response.json()


def folders(owner):
    return google_grant.grant_ref(owner, google_grant.DRIVE_READ).collection(
        "folders"
    )


def folder_id(link):
    match = re.fullmatch(
        r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/("
        + FILE_ID
        + r")/?(?:\?[^#\s]*)?",
        link,
    )
    if not match:
        raise WriteBlocked("Ange en mapplänk från drive.google.com.")
    return match[1]


def metadata(client, file_id):
    if not re.fullmatch(FILE_ID, file_id):
        raise WriteBlocked("Ogiltigt Drive-ID.")
    data = checked(
        client.get(
            f"{DRIVE}/{file_id}",
            params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
            timeout=15,
        )
    )
    if data.get("trashed") or data.get("id") != file_id:
        raise WriteBlocked("Drive-objektet är inte tillgängligt.")
    return data


def link_folder(owner, link):
    root = folder_id(link.strip())
    with google_session(owner, google_grant.DRIVE_READ) as client:
        data = metadata(client, root)
    if data["mimeType"] != FOLDER:
        raise WriteBlocked("Välj en mapp i Drive.")
    folders(owner).document(root).set({"name": data["name"]})
    return {"root_id": root, "name": data["name"]}


def selected_folder(client, owner, root, subfolders):
    if (
        not re.fullmatch(FILE_ID, root)
        or not folders(owner).document(root).get().exists
    ):
        raise WriteBlocked(
            "Klistra in kundens Drive-mapplänk i chatten och be Viks koppla mappen."
        )
    current = metadata(client, root)
    for child_id in subfolders:
        child = metadata(client, child_id)
        if child["mimeType"] != FOLDER or current["id"] not in child.get(
            "parents", []
        ):
            raise WriteBlocked("Undermappen ligger utanför den valda sökvägen.")
        current = child
    return current
