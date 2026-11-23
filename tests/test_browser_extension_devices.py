"""Real-browser coverage for the operator extension-device Settings surface."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect
from test_browser_connections import browser_runtime as browser_runtime  # noqa: F401
from test_browser_connections import signed_in_page

from asr_studio.extension_auth import EXTENSION_ID

pytestmark = pytest.mark.browser
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"


def extension_json(page, live, method, path, *, body=None, headers=None):
    request_headers = {"Origin": live.url, **(headers or {})}
    data = None if body is None else json.dumps(body)
    response = page.request.fetch(
        f"{live.url}{path}",
        method=method,
        data=data,
        headers=request_headers,
    )
    assert response.ok, (method, path, response.status, response.text())
    return response.json()


def test_delayed_create_failure_restores_focus_to_usable_retry(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )

    held_routes = []

    def hold_request(route):
        held_routes.append(route)

    page.route("**/api/extension/pairing-codes", hold_request)
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    create = page.locator("#extension-device-code-button")
    expect(create).to_be_enabled()
    create.focus()
    expect(create).to_be_focused()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert held_routes
    expect(create).to_be_disabled()
    held_routes[0].fulfill(
        status=429,
        content_type="application/json",
        body=json.dumps(
            {
                "error": {
                    "code": "pending_limit",
                    "message": "internal pending limit details",
                }
            }
        ),
    )
    expect(page.locator("#extension-device-error-message")).to_have_text(
        "Có quá nhiều thiết bị đang chờ ghép. Hãy ghép hoặc thu hồi một thiết bị "
        "trước khi tạo mã mới."
    )
    expect(create).to_be_enabled()
    assert page.evaluate("document.activeElement.id") == "extension-device-code-button"
    assert "internal pending limit details" not in page.locator("#connections-dialog").inner_text()
    context.close()


def test_delayed_repair_failure_restores_focus_to_selected_retry(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )

    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    page.get_by_role("button", name="Tạo mã ghép").click()
    expect(page.locator("#extension-device-code")).to_be_visible()
    pending = page.locator("#extension-device-list > li").first
    repair = pending.get_by_role("button", name="Cấp mã ghép lại cho thiết bị Đang chờ client")
    expect(repair).to_be_enabled()

    held_routes = []

    def hold_request(route):
        held_routes.append(route)

    page.route("**/api/extension/pairing-codes", hold_request)
    repair.focus()
    expect(repair).to_be_focused()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert held_routes
    expect(repair).to_be_disabled()
    held_routes[0].fulfill(
        status=429,
        content_type="application/json",
        body=json.dumps(
            {
                "error": {
                    "code": "pending_limit",
                    "message": "internal pending limit details",
                }
            }
        ),
    )
    expect(page.locator("#extension-device-error-message")).to_have_text(
        "Có quá nhiều thiết bị đang chờ ghép. Hãy ghép hoặc thu hồi một thiết bị "
        "trước khi tạo mã mới."
    )
    expect(repair).to_be_enabled()
    expect(repair).to_be_focused()
    context.close()


def test_delayed_create_failure_respects_section_focus_movement(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )

    held_routes = []

    def hold_request(route):
        held_routes.append(route)

    page.route("**/api/extension/pairing-codes", hold_request)
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    create = page.locator("#extension-device-code-button")
    create.focus()
    expect(create).to_be_focused()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert held_routes
    expect(create).to_be_disabled()

    theme = page.get_by_role("tab", name="Giao diện")
    theme.click()
    expect(theme).to_be_focused()
    held_routes[0].fulfill(
        status=429,
        content_type="application/json",
        body=json.dumps({"error": {"code": "pending_limit", "message": "private"}}),
    )
    expect(theme).to_be_focused()
    assert page.evaluate("document.activeElement.id") == "settings-tab-theme"
    context.close()


def test_delayed_create_failure_does_not_steal_control_focus(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )

    held_routes = []

    def hold_request(route):
        held_routes.append(route)

    page.route("**/api/extension/pairing-codes", hold_request)
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    create = page.locator("#extension-device-code-button")
    create.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert held_routes

    close = page.locator("#connections-close")
    close.focus()
    expect(close).to_be_focused()
    held_routes[0].fulfill(
        status=429,
        content_type="application/json",
        body=json.dumps({"error": {"code": "pending_limit", "message": "private"}}),
    )
    expect(close).to_be_focused()
    expect(page.locator("#extension-device-error-message")).to_have_text(
        "Có quá nhiều thiết bị đang chờ ghép. Hãy ghép hoặc thu hồi một thiết bị "
        "trước khi tạo mã mới."
    )
    context.close()


def test_long_endpoint_copy_fallback_keeps_focus_contract_at_mobile_width(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 390, "height": 844},
    )
    long_endpoint = (
        "https://synthetic.example.test/extension/" + "long-segment/" * 16 + "?mode=mobile"
    )

    def serve_synthetic_list(route):
        if route.request.method != "GET":
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "backend_id": "synthetic-backend",
                    "extension_id": EXTENSION_ID,
                    "server_urls": [live.url, long_endpoint],
                    "devices": [],
                }
            ),
        )

    page.route("**/api/extension/devices", serve_synthetic_list)
    page.evaluate(
        """
        () => {
          window.__clipboardMode = "pending";
          window.__clipboardWrites = 0;
          window.__rejectClipboard = null;
          Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
              writeText: (value) => {
                window.__clipboardWrites += 1;
                window.__lastClipboardValue = value;
                if (window.__clipboardMode === "pending") {
                  return new Promise((_, reject) => { window.__rejectClipboard = reject; });
                }
                if (window.__clipboardMode === "success") return Promise.resolve();
                return Promise.reject(new Error("synthetic clipboard denied"));
              },
            },
          });
        }
        """
    )
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()

    long_input = page.locator("input[data-endpoint-index='1']")
    long_copy = page.locator("[data-endpoint-action='copy'][data-endpoint-index='1']")
    expect(long_input).to_have_value(long_endpoint)
    expect(long_copy).to_have_attribute("aria-label", f"Sao chép endpoint {long_endpoint}")

    # The focused button stays enabled during an in-flight request; a repeated click is ignored by
    # the core.
    long_copy.click()
    expect(long_copy).to_be_enabled()
    expect(long_copy).to_be_focused()
    assert page.evaluate("window.__clipboardWrites") == 1
    long_copy.click()
    assert page.evaluate("window.__clipboardWrites") == 1

    # If the user moves away before the rejected promise settles, fallback does not steal that
    # focus.
    close = page.locator("#connections-close")
    close.focus()
    page.evaluate("window.__rejectClipboard(new Error('synthetic clipboard denied'))")
    expect(page.locator("[data-endpoint-copy-status='1']")).to_have_text(
        "Không sao chép được. Chọn endpoint rồi nhấn Ctrl+C."
    )
    expect(close).to_be_focused()

    # A fresh rejected copy while its button is focused selects the exact long URL for Ctrl+C.
    page.evaluate("window.__clipboardMode = 'reject'")
    long_copy.click()
    expect(long_input).to_be_focused()
    selected = long_input.evaluate(
        "input => ({value: input.value, start: input.selectionStart, end: input.selectionEnd})"
    )
    assert selected == {"value": long_endpoint, "start": 0, "end": len(long_endpoint)}
    metrics = (
        page.locator(".device-endpoint-row")
        .nth(1)
        .evaluate("row => ({client: row.clientWidth, scroll: row.scrollWidth})")
    )
    assert metrics["scroll"] <= metrics["client"]

    # A successful copy keeps the focused button in place while copying the exact other server URL.
    first_copy = page.locator("button[data-endpoint-action='copy'][data-endpoint-index='0']")
    page.evaluate("window.__clipboardMode = 'success'")
    first_copy.click()
    expect(page.locator("[data-endpoint-copy-status='0']")).to_have_text("Đã sao chép endpoint.")
    expect(first_copy).to_be_focused()
    assert page.evaluate("window.__lastClipboardValue") == live.url
    context.close()


def test_expiry_labels_and_endpoint_setup_guidance_are_distinct_in_english(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )
    endpoint = "https://synthetic.example.test/asr"

    def serve_synthetic_list(route):
        if route.request.method != "GET":
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "backend_id": "synthetic-backend",
                    "extension_id": EXTENSION_ID,
                    "server_urls": [endpoint],
                    "devices": [
                        {
                            "id": "expired-device",
                            "device_id": "synthetic-client",
                            "server_url": endpoint,
                            "device_lifetime": "30d",
                            "device_expires_at": "2026-11-01T00:00:00+00:00",
                            "created_at": "2026-10-01T00:00:00+00:00",
                            "last_seen_at": "2026-10-15T00:00:00+00:00",
                            "revoked_at": None,
                            "status": "expired",
                        }
                    ],
                }
            ),
        )

    page.route("**/api/extension/devices", serve_synthetic_list)
    page.evaluate("localStorage.setItem('asr-studio:lang', 'en')")
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Devices").click()
    row = page.locator("#extension-device-list > li[data-device='expired-device']")
    expect(row.locator(".device-status")).to_have_text("Expired")
    expect(row.locator(".extension-device-ledger dt").nth(3)).to_have_text("Expires")
    expect(
        page.locator("section[aria-labelledby='extension-device-endpoints-title'] .device-help")
    ).to_have_text(
        "Copy the exact server URL returned here into Extension Setup. "
        "HTTP is for loopback only; remote endpoints require HTTPS."
    )
    context.close()


def test_rendered_device_list_orders_multiple_devices_newest_first(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )
    devices = [
        {
            "id": "older",
            "device_id": "client-older",
            "server_url": live.url,
            "device_lifetime": "30d",
            "device_expires_at": "2026-12-01T00:00:00+00:00",
            "created_at": "2026-11-01T00:00:00+00:00",
            "last_seen_at": None,
            "revoked_at": None,
            "status": "active",
        },
        {
            "id": "tie-a",
            "device_id": "client-tie-a",
            "server_url": live.url,
            "device_lifetime": "30d",
            "device_expires_at": "2026-12-01T00:00:00+00:00",
            "created_at": "2026-11-10T00:00:00+00:00",
            "last_seen_at": None,
            "revoked_at": None,
            "status": "active",
        },
        {
            "id": "newest",
            "device_id": "client-newest",
            "server_url": live.url,
            "device_lifetime": "30d",
            "device_expires_at": "2026-12-01T00:00:00+00:00",
            "created_at": "2026-11-11T00:00:00+00:00",
            "last_seen_at": None,
            "revoked_at": None,
            "status": "active",
        },
        {
            "id": "tie-z",
            "device_id": "client-tie-z",
            "server_url": live.url,
            "device_lifetime": "30d",
            "device_expires_at": "2026-12-01T00:00:00+00:00",
            "created_at": "2026-11-10T00:00:00+00:00",
            "last_seen_at": None,
            "revoked_at": None,
            "status": "active",
        },
    ]

    def serve_synthetic_list(route):
        if route.request.method != "GET":
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "backend_id": "synthetic-backend",
                    "extension_id": EXTENSION_ID,
                    "server_urls": [live.url],
                    "devices": devices,
                }
            ),
        )

    page.route("**/api/extension/devices", serve_synthetic_list)
    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    rows = page.locator("#extension-device-list > li")
    expect(rows).to_have_count(4)
    assert rows.evaluate_all("items => items.map(item => item.dataset.device)") == [
        "newest",
        "tie-z",
        "tie-a",
        "older",
    ]
    context.close()


def test_failed_revoke_keeps_device_visible_and_restores_keyboard_retry(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1024, "height": 800},
    )

    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    page.get_by_role("button", name="Tạo mã ghép").click()
    row = page.locator("#extension-device-list > li").first
    record_id = row.get_attribute("data-device")
    assert record_id
    revoke = row.get_by_role("button", name=re.compile(r"Thu hồi thiết bị"))

    def fail_revoke(route):
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps(
                {"error": {"code": "service_unavailable", "message": "private details"}}
            ),
        )

    page.route("**/api/extension/devices/*/revoke", fail_revoke)
    revoke.focus()
    revoke.click()
    page.get_by_role("button", name="Thu hồi thiết bị", exact=True).click()

    expect(page.locator(f"#extension-device-list > li[data-device='{record_id}']")).to_have_count(1)
    expect(page.locator("#extension-device-error-message")).to_have_text(
        "Máy chủ chưa thể hoàn tất yêu cầu thiết bị. Hãy thử lại sau."
    )
    expect(revoke).to_be_enabled()
    expect(revoke).to_be_focused()
    assert "private details" not in page.locator("#connections-dialog").inner_text()
    context.close()


def test_real_pairing_lifecycle_keeps_pending_records_visible_in_both_languages(browser_runtime):
    live, browser = browser_runtime
    context, page, _api = signed_in_page(
        browser,
        live,
        viewport={"width": 1280, "height": 900},
    )
    context.grant_permissions(["clipboard-read", "clipboard-write"])

    page.get_by_role("button", name="Cài đặt").click()
    page.get_by_role("tab", name="Thiết bị").click()
    page.locator("#extension-device-lifetime").select_option("30d")
    page.get_by_role("button", name="Tạo mã ghép").click()

    code = page.locator("#extension-device-code")
    expect(code).to_be_visible()
    pairing_code = code.input_value()
    assert pairing_code
    page.get_by_role("button", name="Sao chép mã").click()
    expect(page.locator("#extension-device-code-copy-status")).to_have_text("Đã sao chép mã ghép.")
    assert page.evaluate("navigator.clipboard.readText()") == pairing_code

    # Code issuance creates the server record first. The real operator list must accept its null
    # client identity and endpoint rather than turning the whole read into an invalid-list error.
    pending = page.locator("#extension-device-list > li")
    expect(pending).to_have_count(1)
    record_id = pending.get_attribute("data-device")
    assert record_id
    expect(pending).to_contain_text(record_id)
    expect(pending.locator(".device-status")).to_have_text("Đang chờ ghép")
    expect(pending.locator(".device-status")).to_have_attribute("data-status", "pending")
    expect(pending.locator(".extension-device-id")).to_have_text("Đang chờ client")
    expect(pending).to_contain_text("Chưa xác định")
    expect(pending).to_contain_text("Chờ client kích hoạt")
    expect(page.locator("#extension-device-endpoints input").first).to_have_value(live.url)
    repair_vi = pending.get_by_role("button", name="Cấp mã ghép lại cho thiết bị Đang chờ client")
    expect(repair_vi).to_have_attribute(
        "aria-label", "Cấp mã ghép lại cho thiết bị Đang chờ client"
    )
    page.get_by_role("button", name="Sao chép mã").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#extension-device-endpoints input").first).to_be_focused()
    page.keyboard.press("Tab")
    expect(
        page.locator("button[data-endpoint-action='copy'][data-endpoint-index='0']")
    ).to_be_focused()
    page.keyboard.press("Tab")
    expect(repair_vi).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"

    # Switch through the real Settings language control, then keep the same pending record
    # visible and keyboard-addressable in English, including its endpoint copy controls.
    page.get_by_role("tab", name="Giao diện").click()
    with page.expect_navigation():
        page.get_by_role("radio", name="English").check()
    page.get_by_role("tab", name="Devices").click()
    pending_en = page.locator(f"#extension-device-list > li[data-device='{record_id}']")
    expect(pending_en.locator(".device-status")).to_have_text("Pending pairing")
    expect(pending_en.locator(".extension-device-id")).to_have_text("Waiting for client")
    repair_en = pending_en.get_by_role(
        "button", name="Issue a re-pair code for device Waiting for client"
    )
    expect(repair_en).to_have_attribute(
        "aria-label", "Issue a re-pair code for device Waiting for client"
    )
    assert "[object Object]" not in repair_en.get_attribute("aria-label")
    page.locator("#settings-panel-devices").focus()
    # The intro link to Automatic processing is the first tab stop in the panel; the endpoint
    # field and its Copy button are two additional stops before the device row.
    for _ in range(6):
        page.keyboard.press("Tab")
    expect(repair_en).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    assert page.locator("#extension-device-code-form select").count() == 1

    # Consume the code at the configured endpoint. The operator UI never receives this bearer;
    # it is held only by this browser test to exercise the pending-before-session contract.
    paired = extension_json(
        page,
        live,
        "POST",
        "/api/extension/v1/pair",
        body={"code": pairing_code, "device_id": "client-217"},
        headers={"Content-Type": "application/json", "Origin": EXTENSION_ORIGIN},
    )
    device_token = paired["device_token"]
    before_session = extension_json(page, live, "GET", "/api/extension/devices")
    paired_record = next(
        device for device in before_session["devices"] if device["id"] == record_id
    )
    assert paired_record["device_id"] == "client-217"
    assert paired_record["server_url"] == live.url
    assert paired_record["status"] == "pending"
    assert paired_record["device_expires_at"] is not None

    # Refresh the operator list in the UI: a credential exists, but no session has activated it.
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Devices").click()
    pending_credential = page.locator(f"#extension-device-list > li[data-device='{record_id}']")
    expect(pending_credential.locator(".device-status")).to_have_text("Pending pairing")
    expect(pending_credential.locator(".extension-device-id")).to_have_text("client-217")

    session = extension_json(
        page,
        live,
        "POST",
        "/api/extension/v1/session",
        body={},
        headers={
            "Authorization": f"Bearer {device_token}",
            "Content-Type": "application/json",
            "Origin": EXTENSION_ORIGIN,
        },
    )
    active = extension_json(
        page,
        live,
        "GET",
        "/api/extension/devices",
    )
    active_record = next(device for device in active["devices"] if device["id"] == record_id)
    assert session["session_token"]
    assert active_record["status"] == "active"
    assert active_record["last_seen_at"] is not None

    # The UI can explicitly re-pair the selected record and revoke it afterward; neither operation
    # relies on a client-supplied identity or endpoint choice.
    active_row = page.locator(f"#extension-device-list > li[data-device='{record_id}']")
    active_row.get_by_role(
        "button", name=re.compile(r"Issue a re-pair code for device client-217")
    ).click()
    expect(page.locator("#extension-device-code")).to_have_value(re.compile(r".+"))
    active_row.get_by_role("button", name=re.compile(r"Revoke device client-217")).click()
    page.get_by_role("button", name="Revoke device", exact=True).click()
    revoked = page.locator(f"#extension-device-list > li[data-device='{record_id}']")
    expect(page.locator("#extension-device-notice")).to_have_text(
        "The device was revoked. Updating the list."
    )
    expect(page.locator("#extension-device-notice")).to_be_focused()
    expect(revoked).to_have_count(0)

    show_revoked = page.get_by_role("checkbox", name="Show revoked devices")
    expect(show_revoked).not_to_be_checked()
    show_revoked.check()
    expect(revoked.locator(".device-status")).to_have_text("Revoked")

    page.keyboard.press("Escape")
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Devices").click()
    show_revoked = page.get_by_role("checkbox", name="Show revoked devices")
    expect(show_revoked).not_to_be_checked()
    expect(revoked).to_have_count(0)
    show_revoked.check()
    expect(revoked.locator(".device-status")).to_have_text("Revoked")

    # Tokens are not part of any rendered UI state.
    rendered = page.locator("#connections-dialog").inner_text()
    assert device_token not in rendered
    assert session["session_token"] not in rendered
    context.close()
