# ASR Studio Meeting Notes

Từ vựng dùng để mô tả việc chuyển bản ghi cuộc họp thành transcript và meeting notes có thể đối chiếu với nội dung nguồn.

## Language

**Người vận hành (Operator)**:
Người trực tiếp nhập bản ghi, theo dõi xử lý và duyệt kết quả trong ứng dụng. V1 có một người vận hành; những người tham gia cuộc họp không mặc nhiên là người sử dụng ứng dụng.

**Cuộc họp (Meeting)**:
Buổi trao đổi có nội dung cần được tổng kết, khác với lần hệ thống xử lý bản ghi của buổi đó. Ngày họp là ngày diễn ra buổi trao đổi khi đã được xác nhận.

**Bản ghi (Recording)**:
Nguồn âm thanh hoặc video chứa nội dung cuộc họp. File nguồn do người dùng cung cấp và bản sao âm thanh do ứng dụng quản lý là hai đối tượng riêng.

**Transcript**:
Bản chép lời giữ ngôn ngữ đã nói, gắn với mốc thời gian và nhãn người nói khi xác định được; người dùng có thể chỉnh sửa.
_Avoid_: Notes, bản dịch

**Thế hệ transcript (Transcript generation)**:
Transcript xuất phát từ một lần xử lý bản ghi, cùng các chỉnh sửa của kết quả đó. Thế hệ mới nhất đã hoàn tất là bản đang dùng để sửa; các thế hệ trước là nguồn lịch sử chỉ đọc.

**Nhãn người nói (Speaker label)**:
Nhãn phân biệt người phát biểu trong một thế hệ transcript của cuộc họp, chẳng hạn `Speaker 1`. Nhãn không xác nhận danh tính hoặc biểu thị cùng một người qua hai lần xử lý hay nhiều cuộc họp.

**Tên người nói (Speaker display name)**:
Tên do người vận hành đặt cho một nhãn người nói trong một thế hệ transcript của cuộc họp. Tên này không thay đổi nhãn gốc hoặc tự áp dụng sang lần xử lý/cuộc họp khác.

**Gán người nói (Speaker assignment)**:
Liên kết giữa một đoạn transcript và nhãn người nói của đoạn đó. Người vận hành có thể sửa liên kết này khi nhận dạng sai, trong khi nhãn model gán ban đầu được giữ để đối chiếu.
_Avoid_: Đổi tên người nói

**Meeting notes**:
Bản tổng kết bằng tiếng Anh hoặc tiếng Việt theo lựa chọn của người vận hành, gồm các ý quan trọng, lập luận, quyết định, vấn đề còn mở và công việc cần thực hiện, được người dùng duyệt dựa trên nguồn. Ngôn ngữ của mỗi bản notes được chọn khi tạo bản đó, độc lập với ngôn ngữ của transcript.
_Avoid_: Transcript

**Bản notes (Notes revision)**:
Một phiên bản meeting notes có thể được xem, chỉnh sửa và chọn để xuất. Tạo lại notes từ transcript sinh một bản mới và giữ bản trước.

**Nguồn tạo notes (Notes source)**:
Tập nội dung làm cơ sở cho một bản notes, gồm transcript, tên người nói của cuộc họp và ngày họp đã nhập nếu có.
_Avoid_: Bản ghi, notes cũ

**Bản chụp nguồn (Notes source snapshot)**:
Bản giữ nguyên nguồn đã dùng để tạo một bản notes, gồm nội dung transcript với các mốc nguồn, tên người nói và ngày họp nếu có. Bản chụp không thay đổi khi người vận hành sửa transcript hoặc tên người nói về sau.
_Avoid_: Thế hệ transcript, bản notes

**Lần xử lý bản ghi (Recording processing run)**:
Một lần chuyển bản ghi thành transcript, có thể kèm tạo meeting notes. Chạy lại từ bản ghi là một lần xử lý mới, khác với việc tạo notes từ transcript đã có. Mỗi lần xử lý dùng model ASR được chọn riêng cho lần đó; lựa chọn này không đổi mặc định của hệ thống.

**Lần tạo notes (Notes generation run)**:
Một lần tạo bản notes mới từ nguồn tạo notes của cuộc họp. Lần tạo notes có thể thuộc quá trình xử lý bản ghi hoặc do người vận hành yêu cầu sau khi đã có transcript.

**Nhà cung cấp tạo notes (Notes provider)**:
Dịch vụ được người vận hành chọn cho một lần tạo notes, nhận nguồn tạo notes và trả bản nháp theo ngôn ngữ đầu ra đã chọn. Kết nối tài khoản dịch vụ này là thao tác riêng với truy cập ứng dụng; không dùng nó để nhận dạng âm thanh.

**Action item**:
Công việc được nêu trong cuộc họp, có trạng thái đã thống nhất hoặc cần xác nhận và có người phụ trách, thời hạn khi nguồn cung cấp đủ thông tin. Người phụ trách công việc có thể khác người phát biểu.
_Avoid_: Quyết định

**Mốc nguồn (Source timestamp)**:
Vị trí trong bản ghi dùng để nghe lại và kiểm chứng nội dung transcript, quyết định hoặc action item.

**Trích dẫn nguồn (Notes citation)**:
Dữ liệu riêng của một bản notes nối từng mục với các đoạn nguồn của nó (ID và mốc bắt đầu) trong bản chụp nguồn. Trích dẫn không nằm trong Markdown, nên bản đã lưu, chế độ Viết, bản sao chép và file tải về không có link timestamp; chế độ Xem hiện chúng thành chip, và chỉ cho mục còn nguyên nội dung như lúc tạo. Bản notes tạo trước khi có trích dẫn không có dữ liệu này.
_Avoid_: Link timestamp trong Markdown

**Duyệt notes (Review)**:
Việc người dùng đối chiếu bản nháp với nguồn và sửa nội dung để đạt mức dùng được, tập trung vào quyết định, người phụ trách, thời hạn và các đoạn chưa rõ.
