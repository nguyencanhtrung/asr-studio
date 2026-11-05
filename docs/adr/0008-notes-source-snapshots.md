---
status: accepted
---

# Giữ bản chụp nguồn bất biến cho mỗi bản notes

Sau khi notes đã tạo xong, transcript và tên người nói có thể tiếp tục được sửa ngay trong cùng thế hệ; chỉ giữ các thế hệ transcript chưa đủ để đối chiếu đúng nguồn trước chỉnh sửa. Chọn gắn mỗi bản notes với bản chụp bất biến của transcript, ID/mốc nguồn, tên người nói và ngày họp đã dùng, thay vì luôn trỏ về nội dung đang sửa. Đánh đổi là lưu thêm văn bản để giữ bằng chứng đúng thời điểm tạo notes.

Bản chụp không thay quy tắc khóa nguồn trong lúc tạo notes. Sửa nguồn về sau làm notes cần cập nhật nhưng không đổi bản chụp hoặc notes cũ; sửa tự do nội dung notes cũng không viết lại nguồn mà model đã dùng. Bản chụp được giữ bất biến ở tầng cơ sở dữ liệu: trigger `notes_sources_update_guard` (migration schema SQLite v11) từ chối mọi `UPDATE`, nên chỉ `INSERT` khi nhận tác vụ mới tạo ra bản chụp. Không yêu cầu lưu lịch sử từng phím gõ. Xem [thiết kế backend](../backend-design.md), [thế hệ transcript](0006-transcript-generations.md) và [Markdown notes](0005-markdown-notes.md).
