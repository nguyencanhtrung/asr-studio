# UX v2 và phạm vi được server hỗ trợ

Handoff cung cấp mẫu bố cục và tương tác. Audit trước khi sửa ghi lại baseline, đường dẫn code, contract và rủi ro. Báo cáo này mô tả quyết định triển khai theo từng workflow. [Audit](audit-before-changes.md) · [Cách chạy fixture](reproduce-fixtures.md)

| Khu vực | Hành vi trong ASR Studio | Điều kiện hoặc phần việc riêng |
| --- | --- | --- |
| Sidebar và Calendar | Mini Calendar truy cập được trong điều hướng. Full Calendar có sự kiện và danh sách cuộc họp theo ngày. Tìm kiếm, gần đây, ngày đang chọn, Back và mục chưa đặt ngày giữ workflow hiện có. | Kiểm tra bàn phím và giao diện light/dark, VI/EN ở desktop và mobile. |
| Settings và AI | Settings tiếp tục mở dạng modal. OpenAI và Google Antigravity có biểu mẫu kết nối, chọn model và effort, Save, Verify tổng hợp, trạng thái tài khoản và thao tác kết nối lại hoặc ngắt. Các yêu cầu callback và input theo URL hoặc code đến từ provider thực tế. | OpenAI giữ đường dẫn usage. Panel Antigravity dùng contract OMP và để người dùng quyết định gửi lại sau lỗi. Một trang usage riêng cần endpoint tương ứng. |
| Model ASR | Catalog và readiness đến từ server. Settings dùng installer trên server để cài, hủy và thử lại. Lựa chọn mặc định tách khỏi cài đặt và theo model sẵn sàng. | Whisper small là lựa chọn nhẹ được hướng dẫn cho cài đặt mới. File model nằm trên host bind mount, app và worker đọc file đã chuẩn bị. Uninstall được hoãn. [Hướng dẫn model](../../user-guide.md) |
| Diarization | Community-1 dùng file do operator chuẩn bị và chạy theo contract cũ. Người dùng có thể chọn Off khi rerun thủ công từ một transcript hoàn chỉnh nếu server báo khả dụng. | Rerun Off giữ câu chữ, mốc và dấu cần nghe lại. Kết quả mới chưa gán người nói. Catalog và trình quản lý diarization khác cần capability thực. [Rerun và lịch sử transcript](../../adr/0006-transcript-generations.md) |
| Thiết bị và Extension | Settings giữ endpoint, code và thời hạn ghép, danh sách thiết bị, last seen, re-pair và thu hồi. Extension dùng quyền mic/tab và các thao tác Start, Stop, Save của người dùng. Gửi dữ liệu, raw receipt đã lưu và media sẵn sàng là những trạng thái riêng. | Bằng chứng dùng Chrome qua HTTP loopback và HTTPS fixture. LAN, Tailscale và HTTPS tunnel cần cấu hình hostname, Chrome permissions, CORS và TLS của deployment. |
| New Meeting | Popup lấy loại file và giới hạn từ upload policy của server. Mặc định là 4 GiB và 4 giờ. Title tối đa 200 ký tự, ngày tùy chọn, có gợi ý ngôn ngữ, checkbox auto-ASR và lựa chọn model theo lượt. | Fixture dùng media nhỏ để kiểm tra các nhánh validation, hủy và thử lại. Giá trị cấu hình riêng do server cấp vẫn được dùng. |
| Meeting Notes | Editor giữ Save, Cancel, Markdown, HTML/plain/email và export. Regenerate cho chọn recipe và nguồn transcript hợp lệ, tạo một Notes revision riêng. Citation tiếp tục mở snapshot và mốc nguồn. | Save nội dung tăng edit version của bản đang mở. Lịch sử từng lần Save và link cố định theo từng Save thuộc #265. Thùng rác và khôi phục từng Save thuộc #275. |
| Transcript | Tìm kiếm, timestamp, seek, sửa đoạn và speaker, cờ cần kiểm tra và lịch sử generation giữ contract hiện có. Rerun lưu một generation mới và giữ dữ liệu của generation trước. | Lịch sử chỉnh theo generation, gộp speaker, Undo và đánh dấu thủ công thuộc #266. |
| Tự động xử lý | Recipe Extension đi tiếp sau raw receipt và media ready, dùng snapshot nguồn. Import giữ checkbox auto-ASR đã nhớ trong browser và lựa chọn model/ngôn ngữ của từng cuộc họp. | Thiết kế stage policy ở #267. Công tắc Extension và server admission tương ứng thuộc #277. |
| Footer và About | Desktop giữ Settings và GitHub cân giữa trên dòng version. Trạng thái provider, mặc định và khả năng kết nối có vai trò riêng. Link và version dùng cấu hình và metadata bản build. | About giữ trang thông tin sản phẩm và website. Bằng chứng hiển thị dùng tên và dữ liệu mẫu. |

## Các phần triển khai và nghiệm thu riêng

| Issue | Kết quả và phần mở rộng |
| --- | --- |
| #260 | Hiển thị readiness và dependency thực tế của Community-1 chuẩn bị trên server. Thêm lựa chọn catalog và installer diarization cần capability được server công bố. |
| #261 | Cho chọn Off ở rerun thủ công khi server sẵn sàng và đã có transcript hoàn chỉnh. Giữ Community-1 trong các luồng mặc định. |
| #262 | Hướng dẫn Whisper small cho host mới và kiểm tra giữ trạng thái khi dùng lại volume và đường dẫn hiện tại. Uninstall chờ thiết kế bảo vệ default, installer và các job. |
| #265 và #275 | Lịch sử từng lần Save, link cố định, thùng rác và dọn vĩnh viễn theo từng cuộc họp có checklist riêng. |
| #266 | Lịch sử chỉnh Transcript, gộp người nói, Undo và đánh dấu đã kiểm tra có checklist riêng. |
| #267 và #277 | Đặc tả và triển khai công tắc từng bước cho Extension có checklist riêng. |

#263 nghiệm thu parity và các khả năng đã giao trong bảng trên. Các phần mở rộng còn mở được nghiệm thu ở issue tương ứng. Prototype dùng trạng thái mẫu cho callback, kết nối, cài model và Extension. Ứng dụng triển khai chỉ hiển thị trạng thái tương ứng khi server hoặc Extension xác nhận dữ liệu.

## Phạm vi bằng chứng

Fixture chạy app, Extension, API routes, admission, SQLite, receipt, snapshot, worker và publication. Audio, model output và phản hồi provider dùng dữ liệu tổng hợp. Fixture HTTPS dùng cấu hình riêng của test. Tải trọng số thật, chạy GPU/CUDA, độ chính xác suy luận, microphone vật lý, tài khoản và quota provider thật, chứng chỉ công khai và mạng triển khai cần kiểm chứng ở môi trường sử dụng tương ứng.

Các mục phát triển tiếp theo có checklist riêng. Báo cáo nghiệm thu ghi SHA và kết quả của bộ fixture, ảnh và CI trên phiên bản được đưa ra merge.
