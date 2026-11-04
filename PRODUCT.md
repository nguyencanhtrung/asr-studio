# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Người vận hành làm việc một mình trên máy tự quản. Họ nhập bản ghi cuộc họp, theo dõi xử lý local, nghe lại nguồn và duyệt kết quả có thể đối chiếu.

## Product Purpose

ASR Studio biến bản ghi cuộc họp tiếng Anh hoặc tiếng Việt thành transcript có timestamp và người nói để người vận hành rà soát. Thành công là kết quả vẫn rõ nguồn, bền qua restart và dùng được khi phần xử lý local không có Internet.

## Positioning

Ứng dụng kết hợp một workspace cuộc họp local-first với ASR, phân biệt người nói, lịch sử thế hệ và chỉnh sửa an toàn; mỗi kết quả vận hành vẫn truy ngược được về bản ghi thay vì chỉ xuất một khối văn bản dùng một lần.

## Operating Context

- Ứng dụng chạy bằng Docker Compose trên localhost và được mở bằng link token từ terminal.
- Người vận hành nhập audio/video, đặt tên và ngày, chọn gợi ý ngôn ngữ rồi tạo cuộc họp bằng một nút; chép lời tự bắt đầu trừ khi tắt. Sau đó họ theo dõi tác vụ rồi duyệt transcript trong cùng workspace.
- Lịch tháng, danh sách gần đây và màn hình chi tiết cuộc họp là các đường truy cập chính tới dữ liệu đã lưu.
- Rail workspace giữ wordmark cùng EN/VI và theme ở masthead; launcher OpenAI và Google nằm ngay trên khối GitHub/version ở footer dính của rail, còn nút Cài đặt (bánh răng) nằm cạnh GitHub; trên điện thoại hai launcher thành nút logo kèm đèn trạng thái và nút Cài đặt đứng sau chúng, cùng một hàng với brand, EN/VI và theme ở top bar (wordmark chỉ còn tên truy cập khi hàng không đủ chỗ). Cài đặt mở một hộp thoại duy nhất: tài khoản và model ghi chú của từng provider, cùng trạng thái các model chép lời trên máy. Header workspace chỉ giữ hành động tạo cuộc họp (và Lịch trên điện thoại), với mép phải thẳng hàng cột nội dung.
- Trang cuộc họp chia hai tab: **Meeting notes** (kết quả chính) và **Transcript & bản ghi** (trình phát và transcript luôn đi cùng nhau để đối chiếu). Phần đầu trang chỉ hiện tên cuộc họp một lần, kèm dải trạng thái ngắn **Bản ghi · Transcript · Notes** cho biết cuộc họp đã đi tới bước nào. Tab mở sẵn là Meeting notes khi cuộc họp có ít nhất một bản notes đã lưu hoặc đang tạo notes, ngược lại là Transcript; `#notes` và `#transcript` chọn tab, đổi tab thêm một mục lịch sử nên Back quay lại tab trước và tải lại trang giữ tab. Khi chưa có transcript, tab Meeting notes chỉ nói **Cần transcript trước** và dẫn sang tab Transcript; bấm tạo notes ở đâu cũng đưa người vận hành tới tab Meeting notes để theo dõi tiến độ.
- Khi đối chiếu transcript với audio, trình phát dính ở mép trên tab **Transcript & bản ghi** và đoạn đang phát tự cuộn vào tầm nhìn; người vận hành không phải cuộn tìm trình phát hay đoạn đang nghe.
- Transcript mở ở chế độ đọc để nghe và đọc nhanh: mỗi đoạn chỉ hiện mốc thời gian, người nói, nội dung và dấu cần kiểm tra. Nút **Sửa** mở ô sửa cho đúng một đoạn tại một lúc; bản nháp chưa lưu của đoạn vừa rời đi được giữ và đánh dấu. Gán người nói ban đầu và chỉ số model nằm trong phần **Chi tiết** của đoạn đang sửa.
- Người nói hiện thành chip; chip còn tên mặc định do hệ thống đặt (Speaker N) có dấu **đặt tên** và đổi tên ngay tại chỗ.
- Tạo notes là một hành động rõ ràng trong tab **Meeting notes**: một nút chính nêu đúng lựa chọn đã lưu, ví dụ **Tạo notes · English · ChatGPT GPT 6.1 Sol**; khi đã có notes, nút là **Tạo lại notes · …**, kiểu nút phụ trong thanh công cụ notes. **Đổi…** mở ba ô chọn ngôn ngữ đầu ra, nhà cung cấp và model cùng mô tả dữ liệu gửi đi, không luôn hiển thị. Nhà cung cấp đã chọn chưa kết nối hoặc model chưa sẵn sàng thì nút thành **Kết nối ChatGPT để tạo notes** (hoặc **Google Antigravity**) và mở hộp thoại kết nối ở đúng phần của nhà cung cấp đó; hệ thống không tự đổi nhà cung cấp hay model.
- Notes dùng tên người nói tại thời điểm tạo, nên khi transcript hiện tại còn người nói mang tên mặc định, khu vực tạo notes nhắc **Notes sẽ dùng tên người nói hiện tại: …** kèm link **Đặt tên trước** (chuyển sang tab Transcript, focus ở danh mục người nói). Lời nhắc không chặn việc tạo notes.
- Trạng thái worker, model, tác vụ, lỗi và dữ liệu cần nghe lại phải quét nhanh được trong phiên làm việc dài.
- Khi chép lời, thanh tiến độ cho biết đã chép tới đâu so với độ dài bản ghi và đang ở giai đoạn nào; khi tạo notes, danh sách bước cho biết đang ở bước nào, đã chạy bao lâu và bằng nhà cung cấp · model nào. Nút hủy nói rõ đang hủy chép lời hay tạo notes. Chi tiết kỹ thuật như compute backend nằm trong phần **Chi tiết xử lý** thu gọn.
- Meeting notes trong tab **Meeting notes** mở ở chế độ **Xem** theo mặc định để đọc toàn bề rộng và trình duyệt nhớ lựa chọn cuối cùng; **Viết** dùng để sửa Markdown. Bản nháp chưa lưu luôn được báo rõ và trình duyệt cảnh báo trước khi đóng hoặc tải lại trang.
- Mục notes có nguồn hiện chip mốc thời gian (`mm:ss`) sau nội dung ở chế độ **Xem**; Markdown, chế độ **Viết**, bản sao chép và file tải về không có link timestamp. Bấm chip mở tab **Transcript & bản ghi**: thẻ **Nguồn đã lưu lúc tạo notes** ngay dưới trình phát, audio tua và phát từ mốc đó, đoạn tương ứng được đánh dấu và đưa vào tầm nhìn; Back quay lại **Meeting notes**. Chip chỉ gắn vào mục còn nguyên nội dung như lúc tạo, nên mục đã sửa không có chip thay vì bị gắn nhầm nguồn.

## Capabilities and Constraints

- Giữ nguyên domain language, route, API, focus management, task semantics và dữ liệu hiện có.
- Audio, ASR, diarization, lưu trữ và frontend assets chạy local; UI không tải font, script, CSS, telemetry hoặc visual asset từ bên thứ ba.
- Transcript giữ ngôn ngữ đã nói; nhãn điều khiển dùng tiếng Việt và một số thuật ngữ đã chốt như Transcript, Meeting notes, Speaker và English.
- V1 là single-operator, không có tài khoản nhiều người dùng và không mở truy cập LAN/Internet.
- Thiết kế phải hoạt động trên desktop và mobile mà không che control, mất trạng thái hoặc tạo horizontal overflow.

## Brand Commitments

- Tên sản phẩm là **ASR Studio** (`asr-studio`); dấu nhận diện kết hợp sóng âm nét đơn với thanh điều chỉnh studio.
- `https://bibo.id.vn/` là visual authority cho ngôn ngữ nhận diện dùng chung. ASR vẫn là công cụ vận hành, không sao chép bố cục portfolio hoặc biến thành landing page.
- Giọng giao diện ngắn gọn, chính xác, bình tĩnh và thiên về kỹ thuật; không đổi product copy hoặc tạo claim mới ngoài phạm vi đã xác nhận.

## Evidence on Hand

- Product terminology: `CONTEXT.md`.
- Product and workflow constraints: `docs/specification-v1.md`, `docs/backend-design-v1.md` and `docs/backend-contract-v1.md`.
- Existing implemented surfaces and states: `src/asr_studio/web/`.
- Existing real-browser behavior coverage: `tests/test_browser_access.py`, `tests/test_browser_workspace.py` and `tests/test_browser_history.py`.
- The repository contains no customer claims, testimonials or marketing proof; future UI must not fabricate them.

## Product Principles

1. Nguồn và trạng thái phải rõ trước khi giao diện tạo cảm giác trau chuốt.
2. Xử lý local và quyền riêng tư là hành vi có thể kiểm chứng, không phải khẩu hiệu trang trí.
3. Mọi chỉnh sửa giữ được lịch sử và ngữ cảnh để người vận hành đối chiếu.
4. Một hệ thống nhận diện chung không được làm giảm mật độ thông tin hoặc tốc độ thao tác.

## Accessibility & Inclusion

- Hỗ trợ đầy đủ tiếng Việt và tiếng Anh bằng font đóng gói local.
- Hai tab của trang cuộc họp theo mẫu WAI-ARIA Tabs: chỉ tab đang chọn nằm trong thứ tự Tab, mũi tên trái/phải, Home và End đổi tab; tab đang chọn khác tab còn lại bằng chữ đậm và gạch chân, không chỉ bằng màu, và nhãn tab Transcript ghi số đoạn cùng số đoạn cần kiểm tra bằng chữ.
- Tất cả control phải dùng được bằng bàn phím, có focus indicator rõ và không truyền trạng thái chỉ bằng màu. Thanh tiến độ có tên truy cập; mỗi bước tạo notes có chữ trạng thái; hiệu ứng nhấp nháy của thanh không xác định tắt khi người dùng chọn giảm chuyển động.
- Nút tạo notes lấy tên truy cập từ chữ hiển thị; **Đổi…** có `aria-expanded` và `aria-controls`; khi nút chuyển vào thanh công cụ notes, focus bàn phím đi cùng nhóm control đó. **Đặt tên trước** là link thật (`#transcript`) và đưa focus tới tiêu đề danh mục người nói.
- Mỗi đoạn transcript ở chế độ đọc có tối đa hai điểm dừng Tab (mốc thời gian và **Sửa**). Esc hủy và Ctrl/⌘+Enter lưu trong ô sửa; sau khi lưu hoặc hủy, focus quay lại **Sửa** của đoạn đó. Thông báo lưu của mọi đoạn đi qua một vùng `aria-live` chung. Khi nguồn bị khóa, **Sửa** và chip bị tắt kèm lý do.
- Mỗi chip mốc thời gian của notes là một link có điểm dừng Tab riêng, focus nhìn thấy và tên truy cập “Nghe lại đoạn nguồn lúc …”; đoạn nguồn được đánh dấu bằng nhãn chữ **Nguồn của mục notes**, không chỉ bằng màu, và việc cuộn tới nó tức thì khi người dùng chọn giảm chuyển động.
- Light/dark theme, contrast và reduced motion phải theo semantic state và sở thích hệ điều hành/người dùng.
- Chữ hiển thị không nhỏ hơn 12px, control từ 14px, prose đọc từ 16px; cỡ chữ theo rem nên cài đặt cỡ chữ của trình duyệt có tác dụng.
- Tự cuộn theo đoạn đang phát chỉ chạy khi còn thấy một phần danh sách đoạn bên dưới thanh trình phát, nên người đang đọc notes (tab Meeting notes, khi danh sách đoạn đang ẩn) không bị kéo về transcript; nó cũng dừng khi audio tạm dừng, khi người dùng vừa cuộn tay hoặc đang sửa, và dùng cuộn tức thì khi người dùng chọn giảm chuyển động; thanh trình phát dính không che đoạn được cuộn tới, còn control nhận focus bàn phím được cuộn xuống dưới thanh (ô nhập văn bản có thể chỉ lộ dòng chứa con trỏ).
