# Release notes v1 — bản dự thảo, chưa công bố

Phiên bản ứng dụng: `0.1.0`; tag dự kiến: `v0.1.0`.
Image dự kiến: `ghcr.io/nguyencanhtrung/asr-en-vi:v0.1.0` (`linux/amd64`,
`linux/arm64`), cùng tag `latest` sau khi kiểm tra thành công.
Chưa công bố artifact này. Trước công bố, điền source commit, manifest digest,
digest từng kiến trúc và kết quả smoke bên dưới.

## Phạm vi

- Workspace tự host trên localhost: nhập recording, transcript EN/VI, diarization,
  sửa transcript/người nói, chạy lại và giữ lịch sử.
- Notes qua provider người dùng chọn; review/sửa, trích dẫn timestamp, lịch sử,
  copy và export Markdown.
- ASR/diarization local, GPU NVIDIA hoặc CPU; model được tải riêng khi setup.
  Notes cloud cần kết nối mạng và tài khoản của người dùng.
- Cài từ source hoặc image qua Docker Compose, volume dữ liệu, token và provider
  riêng cho từng bản cài.

## License

[PolyForm Noncommercial 1.0.0 tiếng Anh](../../../../LICENSE) là bản chính;
[tiếng Việt](https://github.com/nguyencanhtrung/asr-studio/blob/fe1e685/LICENSE.vi) chỉ để tham khảo. Mục đích thương mại ngoài các
quyền được license cho phép cần có chấp thuận riêng của chủ sở hữu. Dependency,
font, base image và model giữ điều khoản riêng; xem [hồ sơ thành phần](../../README.md).

## Nghiệm thu và giới hạn đã biết

[Báo cáo #16](../../../acceptance-report.md) ghi 31/49 AC và 10/16 FE đạt trọn trên
candidate lịch sử `151f3e4`, không phải chứng nhận candidate hiện tại. Lần đo lại
#99 tại `4a88ec8` có transcript mẫu ghép hai giờ sau 10,4 phút, nhưng notes lỗi
`provider_output_too_large`; chưa có mốc transcript và notes cùng sẵn, chưa chứng
minh mục tiêu 30 phút. Các sửa đổi sau đó phải có phép đo lại mới được đổi kết luận.

Bộ nghiệm thu còn thiếu họp toàn tiếng Anh, mẫu 2–3 người nói và một cuộc họp
hai giờ thực. Thời gian sửa draft mới có ước lượng, chưa đủ chứng minh mục tiêu
5–10 phút mỗi giờ họp. Chất lượng speaker, timestamp, quyết định/action và mơ hồ
cần người dùng review. Không coi đầu ra notes là biên bản đã được xác nhận.

Các phép thử browser stable hiện hành, đăng nhập provider trên bản cài mới,
quota/phiên mất hiệu lực và anonymous pull còn phải chốt theo artifact cuối.
CPU dùng model small có độ chính xác khác large-v3 trên GPU; không cam kết cùng
hiệu năng giữa các cấu hình. Danh sách model/quota cloud phụ thuộc tài khoản.

## Runtime và chẩn đoán

Log mặc định chỉ ghi mã sự kiện/giai đoạn/lỗi cố định. Stderr của tiến trình con
được đọc và bỏ; chi tiết vendor không được lưu trong log mặc định. Dùng
`runtime check --verbose`, `task diagnose` hoặc `access show` theo chủ ý khi cần
chẩn đoán; các lệnh này có thể hiển thị dữ liệu riêng của bản cài.

Image dùng runtime GNU của Ubuntu cho các wheel, thay các bản OpenMP/Fortran/
quadmath nhúng không có nguồn build xác định. Tên thư viện được giữ và symbol
export được kiểm tra trước khi thay. PyAV dùng FFmpeg 8.1.2 LGPL nguyên bản,
không nhúng x264/x265 từ vendor wheel. Hash trước/sau nằm trong `openmp-runtime.json` ở thư mục license của
image. TorchCodec `0.11.1` dùng wheel PyPI trên amd64 và wheel `+cu128` chính thức
trên Linux aarch64 để khớp Torch/Torchaudio CUDA 12.8; không thêm CUDA 13. Đây là
thay đổi runtime; số đo hiệu năng lịch sử không xác nhận hiệu năng của artifact mới.

Mốc #104 ghi **733,2 s (12,2 phút)** cho transcript timed 651,7 s, khoảng 0,2 s và
notes 81,3 s. Đây là số đo composed, không phải một lần chạy liên tục; không phải
phép đo lại artifact release này.

## Google Antigravity

OMP là adapter kỹ thuật; license MIT của OMP không phải sự cho phép từ Google.
[Điều khoản Antigravity, mục 6](https://www.antigravity.google/terms), được đọc
ngày 2026-10-26, cấm truy cập bằng công cụ bên thứ ba và nêu khả năng đình chỉ
hoặc chấm dứt tài khoản. Trang điều khoản cũng nêu một số trường hợp enterprise
chịu điều khoản riêng. Không quảng bá đường OMP như tích hợp được Google hỗ trợ
hoặc bảo đảm dùng được với mọi tài khoản. Transcript gửi tới provider được chọn;
recording và model ASR ở local.

## Ngoài phạm vi

Không hỗ trợ dịch vụ nhiều người dùng, triển khai public/LAN như một cấu hình
được nghiệm thu, ghi âm cuộc họp trực tiếp, tài khoản cloud dùng chung, model cloud
offline, hay bảo đảm quota/chi phí miễn phí. V1 không kèm dữ liệu cuộc họp mẫu
nghiệm thu hoặc credential có sẵn.

## Artifact và kiểm tra cuối — chưa điền

| Bằng chứng | Kết quả |
| --- | --- |
| Tag và source commit | Chưa chốt |
| Registry manifest digest / amd64 / arm64 | Chưa chốt |
| Danh mục license đúng từng image digest | Chưa duyệt xong |
| Source mới tải từ tag công bố, volume mới | Chưa chạy |
| Pull bằng DOCKER_CONFIG rỗng, volume mới | Chưa chạy |
| Version CLI/UI, revision và digest khớp | Chưa chạy trên artifact công bố |
| Kết nối provider thật và tạo notes trên dữ liệu được phép | Chưa chạy trên bản cài mới |
| Smoke offline / restart / recreate | Chưa chạy trên artifact công bố |

Chạy `scripts/offline-smoke.py --mode source` và `--mode pull` từ checkout tag
đã công bố, với thư mục evidence riêng cho mỗi lượt. Đường pull kiểm tra anonymous
pull bằng Docker config rỗng. Smoke dùng fixture provider tổng hợp để kiểm tra
persistence, không chứng minh đăng nhập/inference thật; phần đó phải được đo riêng.
Giữ evidence có token hoặc nội dung họp ở ngoài repo và chỉ công bố bản tổng hợp.

## Công việc còn mở sau các phép đo lịch sử

Việc mở liên quan tới các thay đổi trên. Khi PR của một việc tới giai đoạn documentation, nó thêm mục của mình ở mục 3 (ví dụ #100) và bỏ hàng ở đây.

| # | Việc | Số đo nền | Tag dự kiến | Quan hệ |
| --- | --- | --- | --- | --- |
| #110 | Notes vẫn lặp ý giữa các mục sau #100 | Theo thứ tự A, C, B: sau #100: 12/19/15 ý lặp; c1: 10/14/11; trước #100: 27/38/15 | Notes | AC-2 chưa đạt; strict Found của mẫu B là 26 so với 28, nên AC-4 và điều kiện chồng lấp của AC-3 chưa đạt ở đó; #110 yêu cầu ít nhất hai lượt chạy mỗi cuộc họp |
| #101 | Notes của bản ghi 2 giờ mất hơn 24 phút với 29 lượt gọi tuần tự | notes 1.463,5 s = 70% mốc AC-13; trung bình 50,5 s mỗi lượt; 8 lượt cuối 62–77 s. Hướng (chưa quyết): gọi song song chunk độc lập, giảm số lượt hoặc tầng reduce, rút ngắn output (#100) | Hiệu năng, Notes | chặn #17 |
| #102 | Notes vẫn lỗi vượt trần đầu ra sau khi hết lượt phục hồi | lượt 29: `output_bytes` 10.701 so với trần 10.700 (cận dưới); mục tiêu 8000 → 6000 → 4500 không giới hạn được độ dài thật | Notes | chặn #17 |
| #73 | Notes không tự chạy sau transcript: quyết định đặc tả cho cách đo AC-13 | người vận hành phải bấm "Tạo notes"; công cụ đo bấm sau 0,2–1,1 s, người thật không có giới hạn | Kiến trúc | quyết định đổi mốc đo AC-13 |
| #97 | RSS đỉnh của diarization tăng bậc hai ở clustering | 4,93 GiB ở 4 giờ (mẫu tổng hợp) so với 4 GiB; mô hình 1,98 + 0,35·d + 0,105·d² GiB (d tính bằng giờ), vượt 4 GiB ở khoảng 3 giờ | Hiệu năng | từ #71 |

Nguồn: chính các issue #104, #110, #101, #102, #73, #97; #101 và #102 khớp `docs/acceptance-report.md` mục 6.5. #101 và #102 chặn #17 (release).
