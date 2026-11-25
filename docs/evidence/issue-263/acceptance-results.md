# Kết quả nghiệm thu UX v2

Checkout kiểm thử là `3adad3dc084d4a615136062337c9c88eb35c6acc`, tree `107c61acb2d49526327d08be5e6b395075562374`, dựa trên `e1a12ca8371e95b63db3e1f6557b6454c1de6c1a`. Ảnh lấy từ app trong checkout này với dữ liệu mẫu; commit đính kèm báo cáo và ảnh giữ nguyên code chạy và fixture. [Chạy lại các fixture](reproduce-fixtures.md) · [Khác biệt với handoff](handoff-gaps.md)

| Workflow | Bằng chứng |
| --- | --- |
| Extension capture qua HTTP loopback và HTTPS fixture | Native Chrome ghi audio mẫu, hoàn tất upload khi raw receipt đã lưu, rồi theo dõi derivative, ASR, diarization và Notes. Kiểm tra Notes revision cùng snapshot nguồn và citation. Trình phát tua đến mốc 1,1 giây với dung sai 0,1 giây. |
| OAuth và Verify | Fixture kiểm tra phiên bị hủy hoặc hết hạn ở hai provider, callback sai và phát lại ở OpenAI, lỗi nhập và kết nối lại ở Antigravity. Mô phỏng quota và trạng thái bận, kiểm đếm yêu cầu Verify, kiểm tra deadline của OMP và cấu hình giới hạn đọc của OpenAI. |
| Thu hồi thiết bị | Ghép và kích hoạt hai thiết bị. Thu hồi một trong Settings. Server từ chối phiên cũ và yêu cầu cấp phiên mới của thiết bị đó, đồng thời tiếp tục chấp nhận thiết bị còn lại. Kiểm tra ghép bằng code hết hạn và re-pair. |
| Cài model và hủy upload | Dùng installer server với tiến trình tải tổng hợp để kiểm tra progress, dữ liệu ready, hủy và retry. Popup upload giữ dữ liệu đã nhập sau khi hủy và kiểm tra lại form trước khi retry. |
| Notes, Transcript và override | Save tăng edit version của bản đang mở. Regenerate thêm Notes revision với nguồn đã chọn. Rerun thêm transcript generation và giữ sửa tay ở bản trước. Các lựa chọn theo lượt giữ defaults của Settings. |
| Calendar, Settings và bàn phím | Kiểm tra VI/EN, focus trap và trả focus, Mini Calendar, Full Calendar và ngữ cảnh tìm kiếm. Thao tác các panel provider, model, automation và thiết bị bằng fixture. |

**Kết quả tại checkout** 159 ca đạt (42 tài khoản và provider, 5 thiết bị và thu hồi, 22 cài model, 14 upload và validation, 17 Notes và Transcript, 55 điều hướng, giao diện và i18n, 4 Chrome Extension native).

## Ảnh từ app

| Thiết bị | Light · Tiếng Việt | Dark · English |
| --- | --- | --- |
| Desktop 1440 × 900 | [Sidebar và Calendar](3adad3d-desktop-light.png) · [Footer](3adad3d-footer-light.png) | [Sidebar và Calendar](3adad3d-desktop-dark.png) · [Footer](3adad3d-footer-dark.png) |
| Settings với focus bàn phím | [Desktop, focus ở tab Giới thiệu](3adad3d-desktop-focus-light.png) · [Mobile](3adad3d-mobile-focus-light.png) | [Desktop, focus ở tab About](3adad3d-desktop-focus-dark.png) · [Mobile](3adad3d-mobile-focus-dark.png) |
| Mobile 390 × 844 | [Trang chính](3adad3d-mobile-light.png) · [Full Calendar](3adad3d-mobile-calendar-light.png) | [Trang chính](3adad3d-mobile-dark.png) · [Full Calendar](3adad3d-mobile-calendar-dark.png) |

Chrome dùng thiết bị audio tổng hợp và chứng chỉ thử nghiệm. Các kiểm tra deployment cần xác nhận riêng gồm microphone vật lý, tài khoản provider, tải model thật, tốc độ hoặc chất lượng suy luận, LAN, Tailscale, TLS công khai và tunnel. [Phạm vi bằng chứng](handoff-gaps.md#phạm-vi-bằng-chứng)
