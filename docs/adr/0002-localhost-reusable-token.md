---
status: accepted
---

# Token dùng lại không tự hết hạn trên localhost

V1 chạy trên máy cá nhân và chỉ cho phép truy cập qua localhost. Dùng token bắt buộc, dùng lại nhiều lần và không tự hết hạn theo thời gian để người vận hành mở ứng dụng thuận tiện; khi khởi chạy cung cấp cả QR và link có thể bấm/copy trong terminal. Phạm vi này không bao gồm điện thoại, LAN hoặc Internet.

Backend kiểm tra quyền truy cập cả API, âm thanh, kết quả tải xuống và thao tác thay đổi dữ liệu. Token không được công khai trên trang chưa xác thực; tài khoản hệ điều hành và quyền quản trị Docker thuộc môi trường tin cậy của người vận hành.

Token được sinh ngẫu nhiên ở lần chạy đầu và lưu cùng vùng dữ liệu của bản cài. Restart hoặc tạo lại container với vùng dữ liệu được giữ nguyên không đổi token; từng bản cài có token riêng và source/image công khai không mang token dùng chung.

Trình duyệt giữ trạng thái xác thực bằng cookie và xóa token khỏi thanh địa chỉ sau đăng nhập. Ứng dụng không đặt thời hạn tự hết hiệu lực; nếu cookie bị mất, người vận hành mở lại cùng link để đăng nhập. Có lệnh local để chủ động thay token, làm mất hiệu lực token cũ và toàn bộ trạng thái xác thực dựa trên nó, trong khi dữ liệu và tác vụ đã nhận được giữ nguyên.

Quyết định ưu tiên thao tác thuận tiện trong môi trường máy cá nhân chỉ mở localhost. Khi mở truy cập LAN hoặc Internet ở phiên bản sau, cần đánh giá lại phạm vi tin cậy và cách quản lý truy cập. Các tiêu chí kiểm tra nằm trong [specification](../specification.md).
