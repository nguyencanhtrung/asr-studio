---
status: accepted
---

# UI trên trình duyệt và backend tự vận hành trong Docker

> Phần không dùng AI cloud và yêu cầu toàn bộ pipeline offline dưới đây đã được thay bởi [ADR 0007](0007-cloud-notes-providers.md): ASR/diarization và lưu trữ vẫn local, tạo notes dùng ChatGPT hoặc OMP + Google Antigravity do người dùng chọn. Các quyết định web, Docker và localhost tiếp tục có hiệu lực.

Chọn UI trên trình duyệt và backend tự vận hành trong Docker Compose để tách thiết bị tương tác khỏi máy xử lý, đồng thời giúp cài mới trên máy Linux có NVIDIA GPU và cấu hình tương thích. Một người vận hành dùng hệ thống; model, logic và dữ liệu nằm trên máy do người đó quản lý, cho phép trao đổi bản ghi và kết quả với trình duyệt được cấp quyền bằng token. Phạm vi v1 là dễ cài mới bằng build từ source hoặc pull image đã build từ registry, không gồm migration hoặc chuyển dữ liệu từ bản cài cũ sang máy khác; xem [specification v1](../specification.md).

## Hệ quả

“Local” chỉ việc xử lý và lưu trữ trên máy tự quản. V1 mở UI qua localhost trên chính máy chạy backend, không mở LAN hoặc Internet; tiếp tục yêu cầu không dùng AI cloud, không gửi telemetry và dùng được khi ngắt Internet sau thiết lập. Khả năng truy cập từ xa thuộc hướng phát triển sau v1.

Truy cập chức năng và dữ liệu yêu cầu xác thực bằng token dùng lại không tự hết hạn; khi khởi chạy cung cấp QR và link có token trong terminal. Backend phải thực thi kiểm soát trên cả API và nội dung được tải, không chỉ ẩn giao diện. Xem [quyết định truy cập](0002-localhost-reusable-token.md).

Đóng gói thành một bộ Docker Compose, cho phép tách phần phục vụ UI/API và phần chạy model. Khi lập ADR này, số service cụ thể chưa chốt; quyết định tiếp theo tại [ADR 0004](0004-app-worker-sqlite.md) chọn hai service `app`/`worker`. Sau thiết lập, bộ triển khai khởi động bằng một lệnh. Sau khi upload hoàn tất và backend đã nhận tác vụ, đóng tab hoặc mất kết nối trình duyệt không hủy xử lý; mở lại sẽ xem được tiến độ hoặc kết quả.

Docker giúp đóng gói phần mềm nhưng máy chạy xử lý vẫn cần phần cứng và runtime GPU tương thích. Cài đặt trên host cần NVIDIA driver và cấu hình container runtime; việc có Docker chưa chứng minh model chạy được trên GPU. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), [Docker Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/).

Image ứng dụng và dữ liệu lưu bền là hai phần riêng. Volumes có vòng đời độc lập với container, phù hợp để giữ lịch sử trong cùng một bản cài khi container được thay thế. [Docker volumes](https://docs.docker.com/engine/storage/volumes/).

Người dùng đã làm rõ rằng dễ cài trên máy khác không có nghĩa là phải chuyển dữ liệu sang máy đó. Bản cài mới có kho dữ liệu mới; yêu cầu lịch sử trên bản cài đang sử dụng vẫn được giữ theo specification. Quy trình migration, sao lưu/khôi phục sang máy khác và đồng bộ nhiều bản cài nằm ngoài phạm vi v1.

Source và image dự kiến được công khai để người khác có thể tự build hoặc pull image. Chính sách cho phép dùng, sửa và chia sẻ phi thương mại; sử dụng phục vụ kinh doanh cần được chủ sở hữu cho phép riêng. Xem [quyết định phân phối](0003-public-source-noncommercial.md); văn bản license chưa được áp dụng và chưa thực hiện phát hành.

## Lựa chọn triển khai

Các hành vi về token, phục hồi tác vụ, upload, giới hạn một tác vụ, cài đặt qua terminal và trình duyệt nghiệm thu đã được chốt. [ADR 0004](0004-app-worker-sqlite.md) sau đó chọn số service và SQLite cùng file âm thanh local; [thiết kế backend](../backend-design.md) tiếp tục chốt Python/FastAPI và bộ model thử nghiệm. Framework frontend, cấu hình model phát hành và chi tiết registry còn được làm rõ trong phạm vi specification.
