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
  tests/test_browser_asr_model.py \
  tests/test_browser_settings.py

ASR_RUN_EXTENSION_TESTS=1 ASR_EXTENSION_DISPOSABLE_XVFB=1 \
  xvfb-run -a -s '-screen 0 1280x1024x24' \
  uv run --no-sync pytest -q tests/test_browser_extension_capture.py \
  -k 'native_capture_keeps_recording_without_controls_and_finishes_at_raw_receipt or native_capture_runs_transcript_and_notes_after_client_receipt'
```

| Kiểm tra | Bằng chứng cần quan sát |
| --- | --- |
| Đăng nhập và Verify | Thử callback hết hạn, phát lại hoặc bị hủy trong Connections. Quota, timeout, trạng thái bận và yêu cầu đăng nhập lại có thông báo tương ứng. Đếm số lần fixture gọi adapter. |
| Ghép và thu hồi thiết bị | Ghép hai thiết bị, thu hồi một thiết bị và xác minh chỉ thiết bị còn lại giữ quyền cấp phiên. Thử code hết hạn và re-pair rõ ràng bằng fixture Extension API. |
| Cài model | Fixture installer tạo lỗi, hủy khi đang tải và retry có chủ ý. Mở lại Settings để đọc readiness từ server, rồi kiểm tra lựa chọn mặc định riêng với cài đặt. |
| Hủy upload | Giữ transfer đang chạy, bấm Hủy và kiểm tra form còn thông tin để sửa hoặc thử lại. Đếm yêu cầu theo bước và kiểm tra validation sau khi form thay đổi. |
| Notes nguồn và phiên bản | Lưu bản Notes sửa tay, Regenerate bằng một nguồn transcript đã lưu, rồi mở lại bản cũ. So sánh nội dung và citation của từng bản. |
| Transcript rerun | Chọn model sẵn sàng, gợi ý ngôn ngữ và chế độ được server cấp. Xác nhận tạo generation mới, rồi mở lại generation trước và đối chiếu sửa tay, mốc thời gian và provenance. |
| Capture → Notes | Chrome Extension ghi audio mẫu qua HTTP loopback và HTTPS fixture. Đối chiếu raw receipt đã lưu trước khi derivative sẵn sàng, sau đó kiểm tra ASR, diarization, Notes revision và source snapshot. |
| Bàn phím, mobile và giao diện | Thử mở, Tab, Shift+Tab, Escape và trả focus cho Settings và New Meeting. Kiểm tra menu Settings, Calendar và editor ở light/dark, VI/EN, desktop và mobile. |

Bộ kiểm tra phát hành chạy các suite Node, Chromium và Chrome Extension rồi build ảnh container cho hai kiến trúc trên cùng commit. Kết quả fixture thể hiện contract và luồng tương tác với dữ liệu mẫu. Độ chính xác suy luận, microphone vật lý, tài khoản provider và kết nối qua LAN, Tailscale hoặc tunnel cần kiểm chứng ở deployment tương ứng. [Workflow](../../../.github/workflows/pipeline.yml)
