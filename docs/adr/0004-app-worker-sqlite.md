---
status: accepted
---

# Hai service app/worker với SQLite và file trên máy local

V1 phục vụ một người vận hành trên một máy, chỉ nhận một tác vụ xử lý tại một thời điểm nhưng vẫn phải cho xem/sửa lịch sử khi model đang chạy. Chọn hai service Docker Compose: `app` phục vụ UI/API và `worker` chạy model; dùng SQLite cho dữ liệu nghiệp vụ/trạng thái tác vụ và file riêng cho âm thanh trong vùng lưu bền trên ổ đĩa local. Lựa chọn này tách vòng đời tiến trình model khỏi UI/API, đồng thời giữ việc cài đặt gọn hơn phương án thêm PostgreSQL; đổi lại phải thiết kế rõ quyền nhận tác vụ, phối hợp ghi dữ liệu và phát hiện worker bị ngắt.

SQLite chỉ có một writer tại một thời điểm, nên giao dịch ghi phải ngắn và không bao trùm suy luận model. Lựa chọn database dựa trên phạm vi một máy và ít ghi đồng thời, phù hợp với [hướng dẫn chính thức của SQLite](https://www.sqlite.org/whentouse.html). Vòng 2 chốt dùng chính SQLite lưu trạng thái và quyền nhận tác vụ: `app` ghi nhận khi rảnh, `worker` nhận việc nguyên tử và cập nhật tiến độ/kết quả; không nhận tác vụ thứ hai để chờ. Đánh đổi là hai service cùng phụ thuộc schema. Ownership, hủy và phục hồi đã được cụ thể hóa trong [backend contract](../backend-contract.md); cấu hình journal, timeout và hành vi khi crash cần được kiểm chứng khi triển khai. Các quyết định sản phẩm đã thống nhất ở [backend design v1](../backend-design.md).
