# Chạy lại nghiệm thu UX v2

Dùng một checkout tách biệt ở commit cần kiểm tra. Chuẩn bị Node 24, Python 3.12, Chromium và FFmpeg theo hướng dẫn của repository. Các fixture tạo database, hồ sơ trình duyệt và audio mẫu riêng. Kết nối AI dùng adapter tổng hợp trong fixture. [Môi trường development](../../agents/development.md)

```sh
git rev-parse HEAD
uv sync --all-groups --locked

ASR_RUN_BROWSER_TESTS=1 ASR_REQUIRE_MEDIA_TOOLS=1 uv run --no-sync pytest -q \
  tests/test_browser_connections.py \
  tests/test_browser_extension_devices.py \
  tests/test_browser_asr_install.py \
  tests/test_browser_create_flow.py \
  tests/test_browser_notes.py \
  tests/test_browser_notes_provider.py \
  tests/test_browser_notes_editor.py \
  tests/test_browser_asr_model.py \
  tests/test_browser_automatic_processing.py \
  tests/test_browser_history.py \
  tests/test_browser_settings.py \
  tests/test_recordings.py \
  tests/test_model_install.py::test_install_then_poll_then_cancel_through_the_api \
  tests/test_extension_devices.py::test_pairing_code_expiry_precedes_device_activation \
  tests/test_extension_devices.py::test_revoke_invalidates_pending_code_and_fresh_explicit_repair_reactivates

uv run --no-sync pytest -q \
  tests/test_provider_routes.py \
  tests/test_antigravity_routes.py \
  tests/test_chatgpt_provider.py::test_cancel_and_replay_cannot_exchange_or_commit_callback \
  tests/test_chatgpt_provider.py::test_expired_login_cannot_be_replayed_or_exchanged \
  tests/test_chatgpt_provider.py::test_pasted_callback_rejects_broken_encoding_before_consuming_session \
  tests/test_chatgpt_provider.py::test_notes_http_client_bounds_silent_reads_but_keeps_long_task_budget \
  tests/test_antigravity_provider.py::test_prompt_deadline_cuts_a_stream_that_keeps_producing_frames \
  tests/test_antigravity_provider.py::test_generate_and_verify_use_their_own_inference_deadlines

ASR_RUN_EXTENSION_TESTS=1 ASR_EXTENSION_DISPOSABLE_XVFB=1 \
  xvfb-run -a -s '-screen 0 1280x1024x24' \
  uv run --no-sync pytest -q tests/test_browser_extension_capture.py \
  -k 'native_capture_keeps_recording_without_controls_and_finishes_at_raw_receipt or native_capture_runs_transcript_and_notes_after_client_receipt'
```

| Kiểm tra | Bằng chứng cần quan sát |
| --- | --- |
| Đăng nhập và Verify | Thử hủy và hết hạn phiên ở cả hai provider, nhập callback sai và phát lại ở OpenAI, lỗi input và kết nối lại ở Antigravity. Fixture Verify kiểm tra quota, trạng thái bận và số lần gọi. Ca OMP deadline dùng luồng frame giả và đồng hồ điều khiển, còn OpenAI dùng HTTP transport giả để kiểm tra giới hạn đọc. |
| Ghép và thu hồi thiết bị | Ghép hai thiết bị, thu hồi một thiết bị và xác minh chỉ thiết bị còn lại giữ quyền cấp phiên. Thử code hết hạn và re-pair rõ ràng bằng fixture Extension API. |
| Cài model | Fixture installer tạo lỗi, hủy khi đang tải và retry có chủ ý. Mở lại Settings để đọc readiness từ server, rồi kiểm tra lựa chọn mặc định riêng với cài đặt. |
| Hủy upload | Giữ transfer đang chạy, bấm Hủy và kiểm tra form còn thông tin để sửa hoặc thử lại. Đếm yêu cầu theo bước và kiểm tra validation sau khi form thay đổi. Fixture backend kiểm tra policy file, 4 GiB, 4 giờ, tiêu đề 200 ký tự và ngày tùy chọn. Các ca browser kiểm tra checkbox auto-ASR và model theo lượt. |
| Notes nguồn và phiên bản | Lưu bản Notes sửa tay, Regenerate bằng một nguồn transcript đã lưu, rồi mở lại bản cũ. So sánh nội dung và citation của từng bản. |
| Transcript rerun | Chọn model sẵn sàng, gợi ý ngôn ngữ và chế độ được server cấp. Xác nhận tạo generation mới, rồi mở lại generation trước và đối chiếu sửa tay, mốc thời gian và provenance. |
| Capture → Notes | Chrome Extension ghi audio mẫu qua HTTP loopback và HTTPS fixture. Đối chiếu raw receipt đã lưu trước khi derivative sẵn sàng, sau đó kiểm tra ASR, diarization, Notes revision và source snapshot. |
| Bàn phím, mobile và giao diện | Thử mở, Tab, Shift+Tab, Escape và trả focus cho Settings và New Meeting. Kiểm tra menu Settings, Calendar và editor ở light/dark, VI/EN, desktop và mobile. |

Bộ kiểm tra phát hành chạy các suite Node, Chromium và Chrome Extension rồi build ảnh container cho hai kiến trúc trên cùng commit. Kết quả fixture thể hiện contract và luồng tương tác với dữ liệu mẫu. Độ chính xác suy luận, microphone vật lý, tài khoản provider và kết nối qua LAN, Tailscale hoặc tunnel cần kiểm chứng ở deployment tương ứng. [Workflow](../../../.github/workflows/pipeline.yml)
