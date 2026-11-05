---
status: accepted
---

# Markdown là nội dung chính của mỗi bản notes

V1 cần cho người dùng sửa tự do meeting notes, giữ các bản trước khi tạo lại và copy/xuất đúng bản được chọn. Chọn Markdown làm nội dung chính được lưu, chỉnh sửa và dùng để hiển thị/copy/xuất, phù hợp vùng soạn thảo trong prototype; không chọn mô hình bắt buộc người dùng sửa từng trường action item. Vòng 2 chốt model sinh cấu trúc trung gian có ID đoạn nguồn để backend kiểm tra trước khi chuyển thành Markdown, nhưng sau chỉnh sửa tự do hệ thống không bảo đảm mọi action item còn có đủ trường có cấu trúc và không tự ghi đè nội dung để khôi phục schema.

Nếu sau này cần quản lý công việc theo người phụ trách, deadline hoặc trạng thái độc lập với văn bản, phải thiết kế lại ranh giới giữa tài liệu người dùng sửa và dữ liệu có cấu trúc; không coi Markdown hiện có là một nguồn trường luôn trích xuất chính xác. Xem [backend design v1](../backend-design.md).

Q26 làm rõ quyền sửa tự do: liên kết nguồn chỉnh tay không hợp lệ không chặn lưu Markdown, nhưng phải cảnh báo và không mở nguồn thay thế. Giữ nguyên văn bản cho copy/xuất; đầu ra model vẫn phải qua kiểm tra trước công bố, không dùng quyền chỉnh tay để bỏ qua kiểm tra đó.
