"""Profile photos: every account type manages its OWN photo; content is validated and encrypted."""

from pathlib import Path

from app.core.config import settings
from tests.helpers import bearer, login, make_company, make_user, new_driver, staff_headers
from tests.test_passenger_portal import passenger_headers

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64 + b"PNG-MARKER-777"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 64 + b"JPEG-MARKER-888"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64


async def put(client, h, data: bytes, ctype: str, name: str = "me.png"):
    return await client.put("/users/me/avatar", headers=h, files={"file": (name, data, ctype)})


async def test_each_role_uploads_views_replaces_and_removes_its_own_photo(client, super_token):
    c = await make_company(client, super_token, "alpha")
    drv = await new_driver(client, c["admin_headers"])
    accounts = {
        "super_admin": bearer(super_token),
        "company_admin": c["admin_headers"],
        "dispatcher": await staff_headers(client, c, "dispatcher"),
        "fleet_manager": await staff_headers(client, c, "fleet_manager"),
        "driver": bearer((await login(client, "alpha", drv["email"]))["access_token"]),
        "passenger": await passenger_headers(client, "alpha", "rider@example.com"),
    }
    for role, h in accounts.items():
        me = (await client.get("/users/me", headers=h)).json()
        assert me["role"] == role and me["has_avatar"] is False
        assert (await client.get("/users/me/avatar", headers=h)).status_code == 404

        r = await put(client, h, PNG, "image/png")
        assert r.status_code == 200, (role, r.text)
        assert r.json()["has_avatar"] is True and r.json()["avatar_updated_at"]
        assert "avatar_key" not in r.json() and "storage" not in r.text
        got = await client.get("/users/me/avatar", headers=h)
        assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
        assert got.headers["x-content-type-options"] == "nosniff"

        assert (await put(client, h, JPEG, "image/jpeg", "me.jpg")).status_code == 200
        got = await client.get("/users/me/avatar", headers=h)
        assert got.content == JPEG and got.headers["content-type"] == "image/jpeg"

    # every account still sees ITS OWN photo (no cross-wiring between users)
    for h in accounts.values():
        assert (await client.get("/users/me/avatar", headers=h)).content == JPEG
    await put(client, accounts["driver"], WEBP, "image/webp", "d.webp")
    assert (await client.get("/users/me/avatar", headers=accounts["driver"])).content == WEBP
    assert (await client.get("/users/me/avatar", headers=accounts["passenger"])).content == JPEG

    h = accounts["passenger"]
    assert (await client.delete("/users/me/avatar", headers=h)).status_code == 204
    assert (await client.get("/users/me/avatar", headers=h)).status_code == 404
    assert (await client.get("/users/me", headers=h)).json()["has_avatar"] is False


async def test_photo_is_encrypted_at_rest_and_old_files_are_removed(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    await put(client, h, PNG, "image/png")
    root = Path(settings.storage_dir) / c["company"]["id"]  # this test's company only
    files = list(root.rglob("*.bin"))
    assert len(files) == 1 and b"PNG-MARKER-777" not in files[0].read_bytes()
    await put(client, h, JPEG, "image/jpeg")
    files = list(root.rglob("*.bin"))
    assert len(files) == 1 and b"JPEG-MARKER-888" not in files[0].read_bytes()   # replaced, not piled up
    await client.delete("/users/me/avatar", headers=h)
    assert list(root.rglob("*.bin")) == []


async def test_bad_files_are_rejected(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    pdf = b"%PDF-1.4\n%%EOF"
    for data, ctype in ((pdf, "image/png"),                 # wrong bytes for the claimed type
                        (b"<svg onload=alert(1)>", "image/svg+xml"),
                        (b"GIF89a....", "image/gif"),
                        (PNG, "text/html"),                 # right bytes, disallowed declared type
                        (b"hello", "image/jpeg")):
        r = await put(client, h, data, ctype)
        assert r.status_code == 415, (ctype, r.status_code)
    assert (await put(client, h, b"", "image/png")).status_code == 400
    big = PNG + b"\x00" * (settings.max_avatar_kb * 1024)
    assert (await put(client, h, big, "image/png")).status_code == 413
    assert (await client.get("/users/me", headers=h)).json()["has_avatar"] is False
    assert (await client.put("/users/me/avatar", files={"file": ("a.png", PNG, "image/png")})).status_code == 401


async def test_photo_endpoints_cannot_target_another_user(client, super_token):
    c = await make_company(client, super_token, "alpha")
    other = await make_user(client, c, "other@alpha.example.com", "dispatcher")
    h = c["admin_headers"]
    # there is no id in the path; extra form fields or query params can't redirect the write
    r = await client.put(f"/users/me/avatar?user_id={other['id']}", headers=h,
                         data={"user_id": other["id"]}, files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 200 and r.json()["id"] != other["id"]
    o = bearer((await login(client, "alpha", "other@alpha.example.com"))["access_token"])
    assert (await client.get("/users/me", headers=o)).json()["has_avatar"] is False
    assert (await client.get(f"/users/{other['id']}/avatar", headers=h)).status_code in (404, 405)
