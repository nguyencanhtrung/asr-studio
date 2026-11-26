---
status: accepted
---

# Markdown là nội dung chính của mỗi bản notes

V1 cần cho người dùng sửa tự do meeting notes, giữ các bản trước khi tạo lại và copy/xuất đúng bản được chọn. Chọn Markdown làm nội dung chính được lưu, chỉnh sửa và dùng để hiển thị/copy/xuất, phù hợp vùng soạn thảo trong prototype; không chọn mô hình bắt buộc người dùng sửa từng trường action item. Vòng 2 chốt model sinh cấu trúc trung gian có ID đoạn nguồn để backend kiểm tra trước khi chuyển thành Markdown, nhưng sau chỉnh sửa tự do hệ thống không bảo đảm mọi action item còn có đủ trường có cấu trúc và không tự ghi đè nội dung để khôi phục schema.

Nếu sau này cần quản lý công việc theo người phụ trách, deadline hoặc trạng thái độc lập với văn bản, phải thiết kế lại ranh giới giữa tài liệu người dùng sửa và dữ liệu có cấu trúc; không coi Markdown hiện có là một nguồn trường luôn trích xuất chính xác. Xem [backend design v1](../backend-design.md).

Q26 làm rõ quyền sửa tự do: liên kết nguồn chỉnh tay không hợp lệ không chặn lưu Markdown, nhưng phải cảnh báo và không mở nguồn thay thế. Giữ nguyên văn bản cho copy/xuất; đầu ra model vẫn phải qua kiểm tra trước công bố, không dùng quyền chỉnh tay để bỏ qua kiểm tra đó.

## Phiên bản đã lưu trong một Notes revision

Mỗi lần sinh Notes tạo một revision có nguồn transcript cố định. Lần lưu của người dùng cập nhật Markdown hiện tại và tăng edit version. Bảng lịch sử lưu Markdown, phiên bản, thời điểm, nguồn và loại tác nhân của từng lần lưu. Nó có thể xác định lượt AI gốc khi edit version cũ là 1. Với revision cũ đã qua lần chỉnh, lịch sử bắt đầu từ bản còn lưu ở thời điểm nâng cấp.

Link và export theo Notes revision tiếp tục mở bản Save mới nhất của chính revision đó. Link theo edit version và các thao tác Copy, email preview, Export trên bản lịch sử dùng đúng bản được chọn. ID phiên bản có phạm vi trong Notes revision. Các snapshot transcript giữ danh tính và khả năng đọc cũ.

Migration thêm bảng và trigger ghi lịch sử trong cùng giao dịch với publish hoặc Save. Bản rollback tương thích giữ migration trong `storage.py`, lịch sử và trigger trong `notes_history.py`, cùng bước dọn lịch sử trong `meeting_deletion.py`. Giao diện cũ vẫn dùng luồng Save và publish hiện có, còn SQLite ghi lịch sử. Xóa một cuộc họp dọn đúng các phiên bản Notes của cuộc họp đó trong cùng giao dịch, kể cả khi trình dọn dữ liệu tạm tắt kiểm tra khóa ngoại.

Binary trước khi nâng schema sẽ từ chối mở cơ sở dữ liệu mới. Rollback giao diện giữ bảng, trigger cùng dữ liệu đã lưu. Nâng cấp có đổi schema theo quy trình sao lưu nguội.
