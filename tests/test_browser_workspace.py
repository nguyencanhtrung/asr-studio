from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import wave
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.config import Settings


@pytest.mark.browser
def test_managed_recording_upload_saves_and_restores_meeting(tmp_path):
    if os.environ.get("ASR_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set ASR_RUN_BROWSER_TESTS=1 to run the real-browser workspace test")
    chrome_path = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")

    recording_path = tmp_path / "short-meeting.wav"
    with wave.open(str(recording_path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 16_000)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    port = server_socket.getsockname()[1]
    public_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url=public_url,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            log_config=None,
            log_level="warning",
            access_log=False,
        )
    )
    server_thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        daemon=True,
    )
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.025)
        assert server.started, "test server did not start"

        state = AuthStore(settings.auth_dir).load()
        link = f"{public_url}/access#{state.token}"
        api_requests: list[dict[str, object]] = []
        workspace_states: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            launch_options = {"executable_path": chrome_path} if chrome_path else {}
            browser = playwright.chromium.launch(headless=True, **launch_options)
            context = browser.new_context()
            page = context.new_page()
            page.add_init_script(
                """
                window.__workspaceStates = [];
                const stateObserver = new MutationObserver(() => {
                  const dialog = document.querySelector("#meeting-dialog");
                  const createButton = document.querySelector("#create-meeting");
                  if (dialog && createButton) {
                    window.__workspaceStates.push({
                      state: dialog.dataset.state,
                      createDisabled: createButton.disabled,
                    });
                  }
                });
                stateObserver.observe(document, {
                  attributes: true,
                  attributeFilter: ["data-state", "disabled"],
                  childList: true,
                  subtree: true,
                });
                """
            )
            page.goto(link, wait_until="domcontentloaded")
            page.wait_for_url(f"{public_url}/", timeout=10_000)
            assert page.evaluate("window.location.hash") == ""
            cookie = next(
                cookie for cookie in context.cookies() if cookie["name"] == "asr_studio_session"
            )
            assert cookie["httpOnly"] is True
            assert cookie["sameSite"] == "Strict"

            def capture_request(request) -> None:
                if "/api/uploads" not in request.url and "/api/meetings" not in request.url:
                    return
                try:
                    headers = request.all_headers()
                except Exception:
                    headers = request.headers
                api_requests.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "post_data": (
                            None
                            if request.headers.get("content-type", "").startswith(
                                "application/octet-stream"
                            )
                            else request.post_data
                        ),
                        "headers": headers,
                    }
                )

            page.on("request", capture_request)
            page.get_by_role("button", name="Cuộc họp mới").first.click()
            dialog = page.get_by_role("dialog", name="Cuộc họp mới")
            expect(dialog).to_be_visible()
            file_input = page.locator("#recording-file")
            expect(file_input).to_be_enabled(timeout=10_000)
            accept = file_input.get_attribute("accept")
            assert accept and ".wav" in accept.lower(), "upload policy must publish WAV support"
            limits = page.locator("#file-limits").inner_text()
            assert "Tối đa" in limits and "Định dạng" in limits

            file_input.set_input_files(str(recording_path))
            expect(page.locator("#selected-file-name")).to_have_text(recording_path.name)
            page.locator("#meeting-name").fill("Điều phối sản phẩm — tuần 42")
            page.locator("#meeting-date-input").fill("2026-10-15")
            create_button = page.get_by_role("button", name="Tạo cuộc họp")
            expect(create_button).to_be_disabled()  # the language is still missing
            page.locator('input[name="language_hint"][value="en"]').check()
            expect(create_button).to_be_enabled()  # the file and the form are valid
            expect(page.get_by_role("button", name="Tải lên và kiểm tra")).to_have_count(0)

            failed_content_requests = 0

            def fail_upload_after_recovery(route) -> None:
                nonlocal failed_content_requests
                failed_content_requests += 1
                if failed_content_requests == 1:
                    route.abort("connectionfailed")
                else:
                    route.fulfill(
                        status=403,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "detail": {
                                    "code": "invalid_media",
                                    "message": "Test upload refusal",
                                }
                            }
                        ),
                    )

            page.route("**/api/uploads/*/content**", fail_upload_after_recovery)
            create_button.click()
            expect(dialog).to_have_attribute("data-state", "error", timeout=10_000)
            expect(page.locator("#form-error")).to_contain_text("Không tạo cuộc họp")
            retry_button = page.get_by_role("button", name="Tải lên lại")
            expect(retry_button).to_be_visible()
            page.unroute("**/api/uploads/*/content**")

            def commit_meeting_then_drop_response(route) -> None:
                response = route.fetch()
                assert response.status == 201
                route.abort("connectionfailed")

            # Retry re-runs the whole flow, so the dropped response is met at the create step.
            page.route("**/api/meetings", commit_meeting_then_drop_response)
            retry_button.click()
            retry_create = page.get_by_role("button", name="Thử lại")
            expect(retry_create).to_be_enabled(timeout=20_000)
            expect(dialog).to_have_attribute("data-state", "error")
            expect(page.locator("#close-dialog")).to_be_enabled()
            expect(page.locator("#meeting-name")).to_be_disabled()

            state_snapshots = page.evaluate("window.__workspaceStates")
            workspace_states.extend(state_snapshots)
            states = {snapshot["state"] for snapshot in workspace_states}
            assert "uploading" in states
            assert "checking" in states
            assert "creating" in states
            for state_name in ("uploading", "checking", "creating"):
                assert any(
                    snapshot["state"] == state_name and snapshot["createDisabled"]
                    for snapshot in workspace_states
                ), f"Create must stay disabled during {state_name}"
            assert any(
                snapshot["state"] == "selected" and not snapshot["createDisabled"]
                for snapshot in workspace_states
            ), "Create should unlock as soon as the file and the form are valid"
            pre_restart_content_paths = [
                request["url"].removeprefix(public_url)
                for request in api_requests
                if request["method"] == "PUT" and urlsplit(request["url"]).path.endswith("/content")
            ]
            assert len(pre_restart_content_paths) >= 2  # one network retry, then terminal refusal
            interrupted_upload_id = urlsplit(pre_restart_content_paths[0]).path.split("/")[3]
            assert all(
                urlsplit(path).path.split("/")[3] == interrupted_upload_id
                for path in pre_restart_content_paths[:2]
            )
            interrupted_state = context.request.get(
                f"{public_url}/api/uploads/{interrupted_upload_id}"
            )
            assert interrupted_state.status == 200
            assert interrupted_state.json()["status"] == "failed"
            assert not (settings.data_dir / "uploads" / f"{interrupted_upload_id}.part").exists()

            page.unroute("**/api/meetings")
            retry_create.click()
            page.wait_for_url(f"{public_url}/meetings/*", timeout=10_000)
            meeting_id = page.url.rstrip("/").rsplit("/", maxsplit=1)[-1]
            expect(page.locator("#meeting-title")).to_have_text("Điều phối sản phẩm — tuần 42")
            expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-15")
            expect(page.locator("#meeting-language")).to_have_text("English")
            expect(page.locator("#status-recording")).to_contain_text("Bản ghi · Đã lưu")
            expect(page.locator("#status-transcript")).to_have_text("Transcript · Chưa chép lời")
            audio = page.locator("#meeting-audio")
            expect(audio).to_be_visible()
            assert audio.evaluate("element => element.controls") is True
            assert audio.get_attribute("src") == f"/api/meetings/{meeting_id}/audio"
            media_deadline = time.monotonic() + 10
            ready_state = audio.evaluate("element => element.readyState")
            while ready_state < 1 and time.monotonic() < media_deadline:
                time.sleep(0.1)
                ready_state = audio.evaluate("element => element.readyState")
            media_error = audio.evaluate(
                "element => element.error ? element.error.message || element.error.code : null"
            )
            assert ready_state >= 1, (
                f"native audio did not load metadata; media error: {media_error}"
            )
            seek_result = audio.evaluate(
                """
                async element => {
                  let seeked = false;
                  if (Number.isFinite(element.duration) && element.duration > 0.25) {
                    const seekedEvent = new Promise(resolve =>
                      element.addEventListener("seeked", () => resolve(true), { once: true })
                    );
                    element.currentTime = Math.min(0.25, element.duration / 2);
                    seeked = await Promise.race([
                      seekedEvent,
                      new Promise(resolve => setTimeout(() => resolve(false), 1000)),
                    ]);
                  }
                  return { currentTime: element.currentTime, seeked };
                }
                """
            )
            assert seek_result["seeked"] is True
            assert seek_result["currentTime"] > 0

            authenticated_range = context.request.get(
                f"{public_url}/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=0-5"},
            )
            assert authenticated_range.status == 206
            assert len(authenticated_range.body()) == 6
            assert authenticated_range.headers["content-range"].startswith("bytes 0-5/")
            unauthenticated_context = browser.new_context()
            try:
                unauthenticated_audio = unauthenticated_context.request.get(
                    f"{public_url}/api/meetings/{meeting_id}/audio"
                )
                assert unauthenticated_audio.status == 401
            finally:
                unauthenticated_context.close()

            server.should_exit = True
            server_thread.join(timeout=5)
            assert not server_thread.is_alive(), "test server did not stop for restart"
            server_socket.close()
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(("127.0.0.1", port))
            server_socket.listen()
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    log_config=None,
                    log_level="warning",
                    access_log=False,
                )
            )
            server_thread = threading.Thread(
                target=server.run,
                kwargs={"sockets": [server_socket]},
                daemon=True,
            )
            server_thread.start()
            restart_deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < restart_deadline:
                time.sleep(0.025)
            assert server.started, "test server did not restart"

            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#meeting-title")).to_have_text("Điều phối sản phẩm — tuần 42")
            expect(page.locator("#meeting-date")).to_have_attribute("datetime", "2026-10-15")
            expect(page.locator("#meeting-language")).to_have_text("English")
            expect(page.locator("#meeting-audio")).to_be_visible()

            # The meeting made through the real upload/import path is durable history.
            history = context.request.get(f"{public_url}/api/meetings").json()
            assert [item["id"] for item in history["meetings"]] == [meeting_id]
            assert history["recent"][0]["meeting_date"] == "2026-10-15"
            assert 0.5 < history["recent"][0]["duration_seconds"] < 2
            recent_link = page.locator("#recent-list a")
            expect(recent_link).to_have_count(1)
            expect(recent_link).to_contain_text("Điều phối sản phẩm — tuần 42")
            expect(recent_link.locator(".link-date")).to_have_text("15/10/2026 · 1 giây")
            expect(recent_link).to_have_attribute("aria-current", "page")
            page.get_by_label("Tìm cuộc họp theo tên").fill("TUẦN 42")
            expect(recent_link).to_have_count(1)

            methods_and_paths = [
                (request["method"], request["url"].removeprefix(public_url))
                for request in api_requests
            ]
            content_paths = [
                path
                for method, path in methods_and_paths
                if method == "PUT" and urlsplit(path).path.endswith("/content")
            ]
            complete_path = next(
                path
                for method, path in methods_and_paths
                if method == "POST" and path.endswith("/complete")
            )
            upload_id = complete_path.split("/")[3]
            assert ("GET", "/api/uploads/policy") in methods_and_paths
            assert methods_and_paths.count(("POST", "/api/uploads")) == 2
            assert len(content_paths) >= 3
            content_upload_ids = [urlsplit(path).path.split("/")[3] for path in content_paths]
            assert len(set(content_upload_ids)) == 2
            assert content_upload_ids.count(upload_id) == 1
            assert any(
                urlsplit(path).path == f"/api/uploads/{upload_id}/content" for path in content_paths
            )
            assert complete_path == f"/api/uploads/{upload_id}/complete"
            assert methods_and_paths.count(("POST", "/api/meetings")) == 2
            assert ("GET", f"/api/meetings/{meeting_id}") in methods_and_paths
            assert ("GET", f"/api/meetings/{meeting_id}/audio") in methods_and_paths

            meeting_request = next(
                request
                for request in api_requests
                if request["method"] == "POST" and request["url"].endswith("/api/meetings")
            )
            meeting_payload = json.loads(str(meeting_request["post_data"]))
            assert meeting_payload == {
                "upload_id": upload_id,
                "title": "Điều phối sản phẩm — tuần 42",
                "meeting_date": "2026-10-15",
                "language_hint": "en",
            }
            upload_requests = [
                request
                for request in api_requests
                if request["method"] == "POST" and request["url"].endswith("/api/uploads")
            ]
            assert len(upload_requests) == 2
            for upload_request in upload_requests:
                upload_payload = json.loads(str(upload_request["post_data"]))
                assert upload_payload == {
                    "filename": recording_path.name,
                    "size_bytes": recording_path.stat().st_size,
                }

            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        server_socket.close()
