---
status: accepted
---

# Notes dùng ChatGPT hoặc OMP + Google Antigravity, giữ ASR local

Người vận hành muốn tận dụng subscription đã có và dành GPU 16 GB cho chuyển âm thanh thành văn bản/phân biệt người nói. V1 hỗ trợ hai lựa chọn tạo notes: **ChatGPT** và **OMP + Google Antigravity**. Người dùng chọn phương án khi chạy và kết nối tài khoản bên đó từ UI; không buộc đăng nhập cả hai hoặc cố định Google làm mặc định. Hoãn Qwen và runtime tạo notes local; training nhận diện danh tính người nói vẫn thuộc phase sau.

Quyết định này thay yêu cầu không dùng AI cloud/toàn bộ pipeline offline trong [ADR 0001](0001-browser-ui-container-backend.md) và phần Qwen của Q7. Âm thanh, ASR/diarization, dữ liệu lưu và logic ứng dụng vẫn trên máy tự quản. Khi tạo notes, transcript cùng thông tin nguồn cần thiết được gửi tới provider đã chọn; không gửi âm thanh. Tạo notes mới cần Internet và quyền sử dụng còn hiệu lực; đọc/sửa, nghe lại và xuất kết quả đã lưu vẫn local.

Đánh đổi là phụ thuộc model access, quota, thời gian phản hồi và điều khoản provider thay cho VRAM chạy LLM local. Người dùng vẫn chọn giữ OMP + Antigravity sau khi được thông báo mục 6 điều khoản Google cấm truy cập dịch vụ qua phần mềm bên thứ ba và có thể đình chỉ Antigravity/Gemini CLI. Hỗ trợ kỹ thuật trong OMP không đồng nghĩa được Google hỗ trợ. [Điều khoản Google](https://www.antigravity.google/terms)

Hai lựa chọn dùng chung quy tắc khóa nguồn, kiểm tra đầu ra và lưu bản notes mới. Đăng nhập provider độc lập với token truy cập localhost. Q13–Q15 chốt lưu một tài khoản/phiên mỗi provider trên bản cài để dùng lại và có ngắt kết nối; cho chạy ASR/diarization rồi lưu transcript/kết thúc tác vụ khi provider chưa dùng được. Khi notes lỗi/quota, giữ transcript và notes cũ để người dùng chủ động chọn bên khác rồi tạo lại; không tự chuyển provider/tài khoản hay bật API billing. Đánh đổi là người dùng cần khởi động lại bước notes, đổi lại kiểm soát được nơi gửi nguồn và hạn mức sử dụng.

Q16 chọn thử direct Sign in with ChatGPT → Responses API cho ChatGPT, còn Antigravity qua OMP RPC. Ứng dụng tự quản OAuth/refresh cho ChatGPT để không cần thêm coding-agent runtime ở nhánh này. Q22 chặn ngắt/đổi tài khoản provider đã chọn cho tác vụ đang hoạt động; phải hủy và chờ xác nhận dừng trước, provider không thuộc tác vụ vẫn ngắt được.

Callback Docker, refresh/revoke, hủy auth và giới hạn thời gian tiếp tục được làm rõ trong [backend design v1](../backend-design.md); chưa có đăng nhập hay inference được kiểm thử. Xem [khảo sát harness](../harness-notes.md) và [subscription](../subscription-notes-options.md) để phân biệt khả năng kỹ thuật với quyền lợi tài khoản.
