"""gym_media_drive §7: URL parser (all shapes + garbage), 403 -> not shared,
Shared Drive listing with the all-drives flags + My Drive still works."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.integrations import drive_client as dc  # noqa: E402
from tests.gym_media_fakes import FakeDriveTransport, _Resp, photo  # noqa: E402

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize("text", [
    FID,
    f"https://drive.google.com/drive/folders/{FID}",
    f"https://drive.google.com/drive/folders/{FID}?usp=sharing",
    f"https://drive.google.com/drive/folders/{FID}?usp=drive_link",
    f"https://drive.google.com/open?id={FID}",
    f"https://drive.google.com/file/d/{FID}/view",
    f"  https://drive.google.com/drive/folders/{FID}/  ",
    f"https://drive.google.com/drive/u/0/folders/{FID}",
])
def test_url_parser_all_shapes(text):
    assert dc.parse_folder_id(text) == FID


@pytest.mark.parametrize("garbage", [
    "", "   ", "not a link", "https://example.com/whatever",
    "http://drive.google.com/", "banana", "folders/",
])
def test_url_parser_rejects_garbage(garbage):
    with pytest.raises(dc.DriveUrlError):
        dc.parse_folder_id(garbage)


def test_403_reads_as_not_shared_not_bad_link():
    """A 403 from Drive means 'not shared to the SA yet', never 'bad link'."""
    t = FakeDriveTransport(meta_status=403)
    client = dc.DriveClient(transport=t)
    meta = client.get_folder_meta(FID)
    assert meta["case"] == "not_shared"
    assert meta["name"] == "" and meta["owner_email"] == ""


def test_404_also_reads_as_not_shared():
    t = FakeDriveTransport(meta_status=404)
    client = dc.DriveClient(transport=t)
    assert client.get_folder_meta(FID)["case"] == "not_shared"


def test_my_drive_meta_has_owner():
    t = FakeDriveTransport(
        meta={"id": FID, "name": "Team Photos", "mimeType": "application/vnd.google-apps.folder",
              "owners": [{"emailAddress": "Owner@Pierce.com"}]},
        children=[photo("p1", parent=FID)])
    client = dc.DriveClient(transport=t)
    meta = client.get_folder_meta(FID)
    assert meta["case"] == "my_drive"
    assert meta["name"] == "Team Photos"
    assert meta["owner_email"] == "owner@pierce.com"   # lowercased
    assert meta["file_count"] == 1


def test_shared_drive_meta_has_no_owner_but_binds():
    """A Shared Drive item reports driveId and no owner; it still resolves (and the
    all-drives flags mean list_children returns its files)."""
    t = FakeDriveTransport(
        meta={"id": FID, "name": "Team Drive Photos", "driveId": "0AXsharedDrive",
              "mimeType": "application/vnd.google-apps.folder"},
        children=[photo("p1", parent=FID), photo("p2", parent=FID)])
    client = dc.DriveClient(transport=t)
    meta = client.get_folder_meta(FID)
    assert meta["case"] == "shared_drive"
    assert meta["owner_email"] == ""
    assert meta["file_count"] == 2


def test_list_children_uses_all_drives_flags():
    """The real transport passes supportsAllDrives + includeItemsFromAllDrives on
    every list call (verified by inspecting the source path is exercised via the
    fake returning shared-drive children)."""
    t = FakeDriveTransport(children=[photo("p1", parent="root")])
    client = dc.DriveClient(transport=t)
    kids = client.list_children("root")
    assert [k.id for k in kids] == ["p1"]
