"""Focused checks for frontend-only assets added to the server allowlist."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from asr_studio.auth import AuthStore

pytestmark = pytest.mark.anyio


async def test_extension_devices_core_is_served_from_the_authenticated_asset_allowlist(
    client: AsyncClient, settings
):
    state = AuthStore(settings.auth_dir).load()
    session = await client.post(
        "/api/access/session",
        headers={"Origin": settings.public_url},
        json={"token": state.token},
    )
    assert session.status_code == 204

    response = await client.get("/assets/extension-devices-core.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "export const initialState" in response.text
    assert "device_token" not in response.text
