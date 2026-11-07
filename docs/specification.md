# Đặc tả v1 — ASR Studio Meeting Notes

> Phạm vi sản phẩm v1 đã được thống nhất. Các quyết định nền tảng được ghi tại [kiến trúc web và Docker](adr/0001-browser-ui-container-backend.md), [truy cập bằng token](adr/0002-localhost-reusable-token.md) và [phân phối non-commercial](adr/0003-public-source-noncommercial.md). Các lựa chọn triển khai và việc kiểm chứng còn lại được nêu ở mục 9.

Thiết kế giao diện A dark theme đã được duyệt theo prototype và ghi thành chuẩn triển khai tại [Frontend design v1](frontend-design.md).

Các quyết định backend Q1–Q27 đã thống nhất tại [Backend design v1](backend-design.md), được cụ thể hóa thành schema/API và vòng đời tác vụ trong [Backend contract v1](backend-contract.md). Issue [#166](https://github.com/nguyencanhtrung/asr-studio/issues/166) bổ sung `notes-service` cho việc gọi provider; `app`, worker transcription và notes service giao việc qua SQLite/file âm thanh local với cùng `processing_slot`. ASR/phân biệt người nói chạy local; Qwen3-ASR-1.7B là mặc định GPU, Whisper small là mặc định CPU; tạo notes dùng ChatGPT hoặc OMP + Google Antigravity theo [ADR 0007](adr/0007-cloud-notes-providers.md). Benchmark và khả năng đạt toàn bộ nghiệm thu vẫn cần kiểm chứng.

Ứng dụng dùng qua trình duyệt, với backend ASR và logic được đóng gói trong Docker Compose trên máy do người dùng quản lý, để biến bản ghi cuộc họp của một nhóm nội bộ cố định thành meeting notes bằng tiếng Anh hoặc tiếng Việt theo lựa chọn. Người dùng nhập bản ghi, chọn ngôn ngữ chính và nhà cung cấp tạo notes, xử lý, nghe lại và sửa bản nháp, sau đó copy hoặc xuất notes.

Các yêu cầu sản phẩm dưới đây tổng hợp những quyết định đã được người dùng đồng ý trong phiên làm rõ specification. Mục tiêu hiệu năng và chất lượng cần được kiểm chứng bằng bản ghi thực tế trước khi nghiệm thu.

## 1. Người dùng và phạm vi

| Thuộc tính | Yêu cầu v1 |
| --- | --- |
| Người vận hành | Một người trực tiếp sử dụng UI trong v1 |
| Người tham gia cuộc họp | Một nhóm nội bộ cố định; nội dung chủ yếu từ người dùng và boss |
| Thời điểm xử lý | Sau cuộc họp |
| Nguồn | File audio hoặc video đã thu từ Meet hoặc microphone trong phòng |
| Buổi họp điển hình | Dài 1–2 giờ, có 2–3 người nói; người dùng và boss có thể cùng xuất hiện |
| Ngôn ngữ đầu vào | Toàn tiếng Anh; tiếng Việt xen thuật ngữ Anh; hoặc cả hai ngôn ngữ trong một bản ghi |
| Transcript | Giữ ngôn ngữ đã nói, gồm thuật ngữ Anh trong câu Việt |
| Meeting notes | Ngôn ngữ đầu ra do người dùng chọn: English hoặc Tiếng Việt |
| Thiết bị | Máy hiện có với Ryzen 9 và RTX 5080; không mua thêm phần cứng |
| Máy chạy backend | Nghiệm thu trên Ubuntu 24.04 hiện tại; hỗ trợ cài mới trên Linux có NVIDIA GPU và cấu hình tương thích |
| Hình thức ứng dụng | UI trên trình duyệt; backend đóng gói thành một bộ Docker Compose |
| Hướng giao diện | Phương án A: lịch sử bên trái, trang cuộc họp hai tab Meeting notes và Transcript & bản ghi; dark theme |
| Thiết bị dùng UI | Thiết bị chạy trình duyệt; không cần GPU để chạy model |
| Trình duyệt nghiệm thu | Chrome và Firefox bản ổn định tại thời điểm kiểm thử, trên laptop/desktop; chưa tối ưu điện thoại/tablet trong v1 |
| Truy cập | V1 chỉ localhost trên máy chạy backend; token bắt buộc, dùng lại nhiều lần, không tự hết hạn; khi khởi chạy cung cấp QR và link có thể bấm/copy |
| Phạm vi dữ liệu | Âm thanh, ASR/phân biệt người nói và lưu trữ trên máy tự quản; gửi transcript và thông tin nguồn cần thiết tới nhà cung cấp tạo notes do người dùng chọn; dữ liệu đã lưu vẫn duyệt/sửa/xuất được khi ngắt Internet |
| Tạo notes | Chọn ChatGPT hoặc Google Antigravity qua OMP cho mỗi lần chạy; kết nối tài khoản bên được chọn từ UI; hoãn Qwen local cho bước tạo notes |
| Cài đặt trên máy khác | Cài mới với kho dữ liệu mới; migration và chuyển dữ liệu từ bản cài khác nằm ngoài v1 |
| Phân phối | Source và image dự kiến công khai; chính sách cho phép dùng, sửa và chia sẻ phi thương mại; sử dụng phục vụ kinh doanh cần được chủ sở hữu cho phép riêng |

Phạm vi nghiệm thu tập trung vào các bản ghi 1–2 giờ. Khả năng xử lý bản ghi dài hơn sẽ được xác định bằng thử nghiệm, không suy ra từ độ dài ngữ cảnh quảng cáo của model.

V1 không bao gồm tự ghi âm, bot tham gia Meet, chép lời thời gian thực, nhận diện danh tính người nói xuyên nhiều cuộc họp, dịch toàn bộ transcript, đồng bộ cloud hoặc tự gửi notes cho người khác. Migration, chuyển lịch sử sang máy khác, đồng bộ nhiều bản cài, truy cập từ LAN/Internet và sử dụng trên điện thoại nằm ngoài v1. Các định dạng Word/PDF và backend chạy trên Windows hoặc macOS chưa thuộc phạm vi đã chốt.

## 2. Luồng sử dụng

1. Khi khởi chạy, nhận QR và link có token trong terminal của người vận hành. Bấm/copy link để mở ứng dụng trong trình duyệt trên cùng máy; sau xác thực, vào lịch sử hoặc tạo cuộc họp mới.
2. Kéo thả hoặc chọn file audio/video có sẵn. Tên cuộc họp được điền sẵn từ tên file (bỏ phần mở rộng) cho tới khi người dùng tự gõ.
3. Sửa tên cuộc họp nếu cần, chọn ngôn ngữ chính (mặc định là lựa chọn lần trước) và tùy chọn nhập ngày họp, rồi bấm nút chính **Tạo cuộc họp**: ứng dụng tự upload lên backend, kiểm tra file và tạo cuộc họp. Ô **Chép lời ngay sau khi tạo** được bật sẵn và nhớ lựa chọn cuối trong trình duyệt; khi tắt, người dùng tự bấm **Bắt đầu chép lời** ở trang cuộc họp.
4. Chọn provider ChatGPT hoặc Google Antigravity qua OMP và model cho bước notes; UI chọn sẵn lựa chọn gần nhất để xem/đổi. Dùng phiên đã lưu hoặc kết nối bên được chọn. Nếu provider chưa dùng được, vẫn cho bắt đầu chép lời local và tạo notes sau. Xem tiến độ hoặc hủy tác vụ.
5. Xem transcript, nhãn người nói và notes nếu đã tạo; tùy chọn đặt tên người nói cho cuộc họp. Khi mới có transcript, hiển thị rõ chưa có notes.
6. Bấm mốc thời gian để nghe lại đoạn nguồn; sửa transcript hoặc notes.
7. Khi cần, chọn provider/model và chủ động tạo lại notes từ transcript đã sửa; không bắt buộc sử dụng cùng lựa chọn với bản notes trước.
8. Copy notes hoặc xuất Markdown; mở lại cuộc họp từ lịch sử khi cần.

### 2.1. Nhập bản ghi và metadata

- Hỗ trợ file audio và video trong tập định dạng được ứng dụng công bố. Tập định dạng cụ thể phải bao phủ các file mẫu thực tế được chọn để nghiệm thu.
- Hiển thị tiến độ upload riêng với tiến độ xử lý. Nếu upload bị ngắt, báo chưa hoàn tất, giữ nguyên dữ liệu form và cho **Tải lại từ đầu** với file đã chọn hoặc chọn file khác; dọn phần file tải dở và không xử lý file chưa nhận đủ. V1 không yêu cầu tiếp tục upload từ vị trí bị ngắt.
- Lựa chọn ngôn ngữ chính gồm **English** và **Vietnamese**.
- Ngôn ngữ chính là gợi ý nhận dạng. Chọn Vietnamese vẫn phải giữ thuật ngữ Anh; chọn English vẫn phải xử lý được đoạn tiếng Việt trong cùng bản ghi.
- Lựa chọn ngôn ngữ chính không thay đổi ngôn ngữ đầu ra của notes, vốn do người dùng chọn riêng (English hoặc Tiếng Việt) khi tạo notes, và không yêu cầu dịch transcript.
- Ngày họp có thể để trống. Không coi ngày xử lý hoặc thời gian sửa file là ngày họp đã được xác nhận.
- Sau import, cho sửa tên và ngày họp với kiểm tra phiên bản để tránh ghi đè từ tab cũ. Thay đổi ngày, kể cả thêm/xóa ngày, làm notes cần cập nhật và bị khóa trong lúc tạo notes; ngày trong bản chụp nguồn cũ giữ nguyên. Đổi riêng tên cuộc họp không thay nguồn tạo notes.
- Khi chạy lại âm thanh, có thể chọn ngôn ngữ gợi ý cho lần xử lý mới; lựa chọn được lưu cùng thế hệ transcript mới, không thay lịch sử ngôn ngữ của thế hệ cũ.
- Ứng dụng lưu bản sao âm thanh riêng cho cuộc họp. Lịch sử vẫn phải phát lại được sau khi file nguồn bên ngoài bị đổi tên, di chuyển hoặc xóa.

### 2.2. Xử lý và tiến độ

- Chỉ nhận một tác vụ xử lý tại một thời điểm trong v1, gồm cả xử lý bản ghi và tạo lại notes. Khi đang bận, báo rõ và từ chối yêu cầu xử lý mới; chưa có hàng đợi nhiều tác vụ. Vẫn xem và sửa được các cuộc họp đã hoàn tất.
- Khi ô **Chép lời ngay sau khi tạo** đang bật, trang cuộc họp gửi yêu cầu chép lời đúng một lần ngay sau khi tạo; tải lại trang hoặc Back không gửi lại. Nếu máy đang bận hoặc worker/model chưa sẵn sàng, cuộc họp vẫn được lưu; UI nêu lý do, hiện nút **Bắt đầu chép lời** và không tự thử lại.
- GUI hiển thị giai đoạn đang chạy (thanh tiến độ khi chép lời, danh sách bước khi tạo notes), trạng thái hoàn tất hoặc lỗi và cung cấp nút hủy nói rõ đang hủy gì (**Hủy chép lời**, **Hủy tạo notes**).
- Sau khi upload hoàn tất và backend nhận tác vụ, đóng tab hoặc mất kết nối trình duyệt không hủy xử lý trên backend. Mở lại UI phải xem được trạng thái, tiến độ hoặc kết quả đã lưu.
- Khi tiến trình xử lý hoặc máy chủ bị ngắt, sau khi hệ thống hoạt động lại phải đánh dấu tác vụ đang dở là bị gián đoạn và cho người dùng chủ động chạy lại từ đầu bằng bản ghi đã lưu. Không yêu cầu tự chạy lại hoặc phục hồi từ checkpoint; giữ các kết quả hoàn tất và bản notes đã sửa.
- Tác vụ bị lỗi hoặc bị hủy không được hiển thị là đã hoàn thành đầy đủ.
- Nếu transcript đã hoàn tất và lưu đầy đủ nhưng bước tạo notes thất bại hoặc bị hủy, giữ transcript đó. Sau khi tác vụ kết thúc, người dùng được xem/sửa và chủ động yêu cầu tạo notes từ transcript; đây là tác vụ riêng, không thay đổi yêu cầu chạy lại toàn bộ từ đầu khi chọn chạy lại bản ghi.
- Nếu provider chưa kết nối hoặc đang không dùng được, vẫn cho chạy ASR/phân biệt người nói, lưu transcript đầy đủ rồi kết thúc tác vụ. Không giữ tác vụ chờ đăng nhập/quota và không tự tạo notes khi provider trở lại. Người dùng chủ động tạo notes sau từ transcript đã lưu.
- Khi không thể tiếp tục, thông báo phải chỉ rõ vấn đề để người dùng có thể thử lại hoặc thay đầu vào. Không âm thầm bỏ phần còn lại của bản ghi.
- Không tự gửi âm thanh tới dịch vụ cloud khi ASR/phân biệt người nói local thất bại. Khi tạo notes lỗi/hết quota, báo lỗi, giữ transcript và notes cũ; người dùng chủ động chọn provider khác rồi chạy lại bước notes. Không tự đổi provider/tài khoản hoặc bật API billing, kể cả đã kết nối cả hai bên.
- Khi lỗi mạng ở lượt gọi tạo notes, không tự gửi lại inference, kể cả chưa nhận được kết quả. Giữ transcript/notes cũ, kết thúc lỗi và cho người dùng chủ động thử lại; SDK/harness cũng phải tuân theo chính sách này. Lượt sửa đầu ra đã nhận nhưng không hợp lệ ở Q18 là trường hợp riêng được phép.

Phần phục vụ UI/API, chạy model và gọi provider được tách thành các service theo [ADR 0004](adr/0004-app-worker-sqlite.md) và [ADR 0013](adr/0013-notes-service-boundary.md). Giao tiếp, nhận việc, hủy và phục hồi được mô tả trong [backend contract](backend-contract.md), vẫn nằm trong giới hạn một tác vụ xử lý tại một thời điểm.

## 3. Transcript, người nói và bằng chứng

### 3.1. Transcript

- Lưu transcript đầy đủ để đọc, chỉnh sửa và đối chiếu với bản ghi.
- Khi chạy lại từ bản ghi, lưu kết quả thành một thế hệ transcript mới; giữ transcript cũ cùng chỉnh sửa và liên kết nguồn của các bản notes cũ. Không trộn segment/nhãn người nói của hai lần xử lý. Xem [ADR 0006](adr/0006-transcript-generations.md).
- Khi thế hệ mới đã hoàn tất và lưu đầy đủ, mặc định mở thế hệ đó để xem/sửa; các thế hệ trước chỉ đọc, còn notes cũ vẫn sửa/copy/xuất được. Nếu chạy lại thất bại trước khi có transcript hoàn tất, giữ thế hệ hiện tại làm mặc định. Notes thất bại sau khi transcript mới đã hoàn tất không làm mất hoặc đảo ngược thế hệ mới.
- Bảo toàn nội dung và ngôn ngữ đã nói; giữ các thuật ngữ tiếng Anh trong câu Việt.
- Mỗi đoạn phát biểu có mốc thời gian trên bản ghi và nhãn người nói khi xác định được.
- Độ phân giải thời gian theo đoạn là đủ cho v1. Không cam kết mọi từ đều có timestamp chính xác.
- V1 cho sửa văn bản và người nói của từng đoạn; giữ nguyên timestamp/ranh giới đã nhận dạng, chưa có chỉnh thời gian hoặc tách/gộp đoạn.
- Mốc thời gian trên GUI phải mở được đúng đoạn âm thanh liên quan. Trong bản Markdown xuất ra, mốc thời gian vẫn phải đọc và đối chiếu được, dù không có trình phát của ứng dụng.

### 3.2. Người nói

- Phân biệt người nói trong từng cuộc họp bằng các nhãn như `Speaker 1`, `Speaker 2`, `Speaker 3`.
- Nhãn chỉ có ý nghĩa trong thế hệ transcript đó của cuộc họp; không biểu thị cùng một người ở hai lần chạy nhận dạng hoặc các cuộc họp khác nhau.
- Cho phép người dùng đặt, sửa hoặc xóa tên hiển thị của từng nhãn ngay trong màn hình duyệt. Đây là thao tác tùy chọn; khi chưa đặt tên hoặc xóa tên, hiển thị lại `Speaker 1`, `Speaker 2`, …
- Tên do người dùng đặt chỉ áp dụng trong thế hệ transcript đang sửa của cuộc họp hiện tại. Giữ nhãn người nói gốc để đối chiếu; thao tác đổi tên không đổi các đoạn phát biểu thuộc nhãn đó. Khi chạy lại có thế hệ mới, bắt đầu bằng nhãn Speaker để người dùng đặt tên lại, không tự ghép tên giữa hai lần xử lý hoặc sang cuộc họp khác.
- Đổi tên cập nhật phần hiển thị transcript và đánh dấu notes hiện có là cần cập nhật. Không tự thay tên trong các bản notes cũ hoặc ghi đè phần đã sửa thủ công. Khi người dùng tạo lại notes, bản mới sử dụng tên đã đặt cho các tham chiếu tới người nói; bản trước giữ nguyên để xem/copy/xuất.
- Vì notes dùng tên người nói tại thời điểm tạo, khi transcript hiện tại còn người nói mang tên mặc định do hệ thống đặt, khu vực tạo notes nhắc rõ điều đó, liệt kê các tên này và dẫn tới nơi đặt tên. Lời nhắc không chặn việc tạo notes.
- Cho gán lại người nói của riêng từng đoạn và thêm nhãn khi thiếu người nói trong thế hệ hiện tại. Giữ nhãn model gán ban đầu của từng đoạn để đối chiếu; gán lại không đổi display name của cả nhóm, không tự sửa các đoạn khác. Thay đổi chịu khóa nguồn/kiểm tra phiên bản, làm notes cần cập nhật và được dùng khi tạo notes mới; bản chụp/notes cũ giữ nguyên.
- **Phase sau — advanced:** học từ dữ liệu training để nhận diện và tự điền tên người nói trong các cuộc họp sau. V1 không có huấn luyện, hồ sơ giọng nói hoặc tự nhận diện danh tính; dữ liệu và tiêu chí cho tính năng này sẽ được thiết kế riêng.
- Tên được nhắc trong nội dung vẫn được giữ khi phù hợp. Ví dụ, “John will send the report” có thể tạo action item với người phụ trách John mà không cần biết John là nhãn người nói nào.

### 3.3. Nội dung chưa rõ

- Đoạn mà hệ thống phát hiện không chắc chắn phải được đánh dấu để người dùng nghe lại, kèm mốc thời gian.
- Bước tạo notes không được tự bổ sung thông tin sự kiện vào phần nội dung còn thiếu hoặc chưa rõ.
- Quyết định hoặc action item dựa trên đoạn chưa rõ phải được đưa vào phần cần xác nhận.
- Nói chồng, tiếng ồn và chuyển ngôn ngữ là các trường hợp phải có trong đánh giá khi xuất hiện trong dữ liệu thực tế.

Đánh dấu không chắc chắn là hỗ trợ cho bước duyệt, không phải bảo đảm rằng hệ thống phát hiện được mọi lỗi nhận dạng. WhisperX công bố các hạn chế với nói chồng và phân biệt người nói; vì vậy chất lượng phải được đo trên dữ liệu của nhóm. [Nguồn WhisperX](https://github.com/m-bain/whisperX#limitations-)

## 4. Meeting notes

Notes là bản nháp để người dùng duyệt. Cấu trúc nội dung gồm:

| Phần | Nội dung |
| --- | --- |
| Overview | Tổng quan ngắn về cuộc họp |
| Key Topics and Rationale | Vấn đề quan trọng, lập luận và các phương án đã cân nhắc |
| Decisions | Các quyết định đã thống nhất, kèm mốc thời gian nguồn |
| Open Questions | Vấn đề chưa chốt và các điểm còn bất đồng |
| Confirmed Action Items | Các công việc đã được thống nhất |
| Items Requiring Confirmation | Đề xuất chưa thống nhất, nội dung mơ hồ hoặc cần người dùng kiểm tra |

Notes phải giữ được ý nghĩa của phát biểu tiếng Anh lẫn tiếng Việt. Việc diễn đạt lại không được làm tăng mức chắc chắn của phát biểu hoặc biến một ý tưởng thành quyết định.

Model sinh dữ liệu có cấu trúc trung gian với ID đoạn nguồn; backend kiểm tra cấu trúc và tham chiếu nguồn trước khi chuyển thành Markdown cho người dùng duyệt/sửa. Kiểm tra ID và cấu trúc không thay thế việc kiểm chứng ý nghĩa; schema cụ thể tiếp tục được thiết kế.

Nếu đầu ra sai cấu trúc hoặc tham chiếu nguồn, cho tối đa **một lượt yêu cầu model sửa tự động trong cả tác vụ tạo notes**, cùng provider/model. Giới hạn áp dụng chung cho mọi đoạn và bước hợp nhất, không đặt lại theo từng lượt gọi. Nếu vẫn không hợp lệ hoặc phát sinh lỗi cần sửa khác sau khi hết lượt này, báo lỗi và giữ transcript/notes cũ; không công bố notes dở hay sai schema. Đây không phải giới hạn retry lỗi mạng.

Transcript vừa ngân sách thì xử lý một lượt để tạo bản nháp. Với mọi cấu hình owner model đã biết, nếu bản nháp đơn hoặc final phân cấp và prompt đối chiếu toàn bộ nguồn đã làm sạch vẫn vừa ngân sách, thực hiện đúng một lượt audit nguồn có giới hạn rồi trả về bản đã kiểm tra. Audit là phép chọn mã neo trong catalog: `dN` giữ nguyên toàn bộ mục bản nháp, `dNpEND` giữ một tiền tố từ an toàn, còn `sN` khôi phục đúng một đoạn nguồn nguyên văn; runtime giải mã và kiểm tra các mã này trước khi công bố. Mã `sN` chỉ được dùng trong `key_topics` hoặc `confirmation_items`, giữ ngôn ngữ gốc và thêm nhãn ngôn ngữ đầu ra. Audit phải kiểm tra thì/tình thái rõ ràng: thảo luận hoặc khám dự kiến về sau không phải việc đã khám; phải giữ phủ định, điều kiện, không chắc chắn và ủng hộ/phản đối. Mã neo ngăn audit sinh văn xuôi mới nhưng không chứng minh mọi khẳng định bản nháp được giữ lại; nội dung bản nháp chỉ được giữ khi toàn bộ mục có căn cứ nguồn. Nếu prompt audit toàn nguồn không vừa ngân sách, trả bản nháp đã kiểm tra và ghi `source_audit.status=skipped`, `reason=prompt_budget`. Audit lỗi sau lượt repair/recovery chung là lỗi kết thúc, không công bố bản nháp chưa được kiểm tra hay một phần. Khi span thời lượng tương đối của nguồn (`max(end_seconds) - min(start_seconds)`) vượt 3.600 giây, chia theo đoạn phát biểu với phần chồng tại ranh giới giờ, trích thông tin kèm ID nguồn rồi hợp nhất bằng cùng provider/model đã chọn. Kiểm soát ngân sách đầu vào/đầu ra ở mọi bước, kể cả audit và hợp nhất; không bỏ phần cuối hoặc để harness tự rút gọn mất bằng chứng. Cần đo riêng chất lượng và quota/thời gian của nhiều lượt gọi.

### 4.1. Action items

Mỗi action item có nội dung công việc, người phụ trách, thời hạn nếu có, trạng thái đã thống nhất/cần xác nhận và mốc thời gian nguồn.

| Tình huống | Cách ghi nhận |
| --- | --- |
| Công việc đã thống nhất, người phụ trách và thời hạn rõ | Ghi thông tin được phát biểu và dẫn về đoạn nguồn |
| Thiếu người phụ trách | Ghi chưa xác định; không tự giao cho người dùng hoặc boss |
| Thiếu thời hạn | Ghi chưa xác định; không tự đặt deadline |
| `Speaker 2` nói “I'll send the report tomorrow” | Người phụ trách là `Speaker 2`; xử lý “tomorrow” theo quy tắc ngày họp |
| Người nói giao việc rõ ràng cho John | Người phụ trách là John, dù người phát biểu có nhãn khác |
| “We should investigate this next week” nhưng chưa có cam kết rõ | Đưa vào phần cần xác nhận, giữ những thông tin thực sự có trong phát biểu |
| Đề xuất do hệ thống sinh, nếu có | Ghi rõ là đề xuất của hệ thống và tách khỏi công việc đã thống nhất trong cuộc họp |

Người nói và người phụ trách là hai thông tin riêng. Người dùng có thể sửa người phụ trách khi duyệt mà không cần đặt tên cho các nhãn người nói.

Nếu đã đặt tên cho `Speaker 2`, bản notes được tạo sau đó dùng tên này khi người phụ trách thực sự là người nói đó. Đặt tên người nói không được đổi người phụ trách được nhắc đích danh trong phát biểu hoặc tự gán các công việc chưa có người nhận.

### 4.2. Ngày họp và deadline

- Chỉ quy đổi mốc thời gian tương đối khi có ngày họp và đủ cơ sở xác định.
- Nếu không có ngày họp, giữ nguyên cách nói như “tomorrow”; không lấy ngày xử lý làm mốc.
- Các cách nói chưa xác định được một ngày cụ thể như “next week” được giữ nguyên. Không tự chọn một ngày trong tuần.
- Kết quả phải cho phép đối chiếu deadline với phát biểu gốc tại mốc thời gian nguồn.

## 5. Duyệt, sửa và tạo lại notes

- GUI dùng bố cục A đã chọn: lịch sử cuộc họp bên trái; trang cuộc họp chia hai tab **Meeting notes** và **Transcript & bản ghi** (tab mở sẵn theo trạng thái cuộc họp, `#notes`/`#transcript` trên địa chỉ, Back quay lại tab trước), trình phát dính ở mép trên tab Transcript & bản ghi khi cuộn; dark theme cho màn hình chính và các hộp thoại.
- Khi audio đang phát và còn thấy một phần danh sách đoạn bên dưới thanh trình phát, đoạn transcript đang phát tự cuộn vào tầm nhìn dưới thanh trình phát. Không tự cuộn khi audio tạm dừng, ngay sau khi người dùng cuộn tay, khi focus đang ở một ô sửa của transcript, hay khi cả danh sách đoạn nằm ngoài tầm nhìn (đang đọc notes ở tab Meeting notes).
- Người dùng sửa trực tiếp được cả transcript và notes.
- Khi ghi transcript, gán/đặt tên người nói, metadata cuộc họp hoặc một bản notes, backend kiểm tra phiên bản đã đọc. Nếu tab khác đã thay đổi cùng dữ liệu, từ chối ghi đè, giữ phần chưa lưu ở UI để người dùng đối chiếu với bản mới; không tự trộn nội dung.
- Sau khi transcript, gán/tên người nói hoặc ngày họp thay đổi, ứng dụng báo rằng notes hiện tại chưa phản ánh thay đổi đó.
- Người dùng chủ động kích hoạt tạo lại notes; việc sửa transcript không tự ghi đè notes.
- Trong lúc tạo notes, khóa chỉnh sửa nguồn của cuộc họp đó ở backend cho đến khi tác vụ đã kết thúc; nguồn gồm transcript, tên người nói và ngày họp đã nhập nếu có. Vẫn được xem nguồn, sửa các bản notes đã có và xem/sửa cuộc họp khác. Yêu cầu hủy chưa giải phóng khóa khi worker chưa dừng.
- Mỗi lần tạo lại notes tạo một bản nháp mới. Giữ bản trước để người dùng quay lại, kể cả khi bản đó đã được sửa thủ công.
- Nội dung chính của mỗi bản notes là Markdown; sửa, hiển thị, copy và xuất dùng cùng nội dung của bản được chọn. Người dùng được sửa tự do; sau chỉnh sửa, hệ thống không bảo đảm mọi action item vẫn có đủ trường có cấu trúc. Xem [ADR 0005](adr/0005-markdown-notes.md).
- Markdown do người dùng sửa có liên kết nguồn không hợp lệ vẫn được lưu nguyên văn. Cảnh báo và vô hiệu thao tác mở nguồn ở liên kết hỏng, không tự đổi ID hoặc dẫn sang nguồn khác; copy/xuất lấy đúng nội dung đã lưu. Quyền sửa tự do này không áp dụng cho đầu ra model chưa qua kiểm tra trước công bố.
- Phân biệt được bản notes đang xem, bản được tạo từ transcript trước đó và bản mới nhất. Không trình bày một bản cũ như thể đã phản ánh transcript vừa sửa.
- Mỗi bản notes gắn với bản chụp bất biến của nguồn đã dùng: nội dung transcript, ID/mốc nguồn, tên người nói và ngày họp nếu có. Mở bằng chứng của notes cũ phải dẫn về bản chụp đó, kể cả transcript trong cùng thế hệ đã được sửa. Hiển thị rõ nguồn cũ so với nguồn hiện tại; không yêu cầu lịch sử từng phím gõ. Sửa nội dung notes không viết lại bản chụp nguồn. Xem [ADR 0008](adr/0008-notes-source-snapshots.md).
- Bước duyệt tập trung vào quyết định, người phụ trách, thời hạn, số liệu, thuật ngữ quan trọng và các phần cần xác nhận.

Mục tiêu là khoảng **5–10 phút duyệt và sửa cho mỗi giờ họp**. Đây là mục tiêu cần đo với người dùng thực tế, không phải một giới hạn buộc người dùng phải dừng kiểm tra.

## 6. Lưu trữ, xuất kết quả và kết nối

### 6.1. Lịch sử trên máy chạy backend

- Hiển thị danh sách cuộc họp đã nhập/xử lý để mở lại.
- Trong sidebar, giữ danh sách cuộc họp gần đây ngắn gọn và đặt lịch tháng nhỏ ngay bên dưới. Đánh dấu các ngày có cuộc họp trong dữ liệu đã lưu; số cuộc họp và dấu ngày cập nhật khi nhập hoặc xóa cuộc họp.
- Cho phép chuyển tháng trước/sau, chọn trực tiếp tháng/năm trong quá khứ và quay về hôm nay. Bấm một ngày hoặc mục **Chưa đặt ngày** trong lịch nhỏ sẽ chuyển vùng làm việc chính sang lịch tháng lớn, giữ sidebar để điều hướng; ở màn hình hẹp (từ 680 px trở xuống) lịch nhỏ ẩn đi và nút **Lịch** ở đầu vùng làm việc là lối vào.
- Lịch lớn hiển thị các cuộc họp theo ngày và có danh sách đầy đủ cho ngày được chọn, kể cả khi nhiều cuộc họp cùng ngày. Chọn một cuộc họp để mở transcript/notes; có đường quay lại màn hình duyệt. Lịch nhỏ và lịch lớn dùng chung tháng/ngày đang chọn.
- Dùng ngày họp đã nhập để xếp lịch. Cuộc họp thiếu ngày nằm trong mục **Chưa đặt ngày**, không tự xếp vào ngày upload/xử lý. Không hiển thị giờ bắt đầu giả khi chỉ có ngày họp và thời lượng; chuyển tháng/ngày trong lịch không sửa metadata cuộc họp.
- Lưu trên máy chạy backend bản sao âm thanh, metadata, transcript, tên người nói được đặt riêng cho cuộc họp, notes và các bản notes trước đó cần để quay lại.
- Dữ liệu lịch sử phải tồn tại qua việc restart hoặc tạo lại container trong cùng một bản cài khi vùng lưu dữ liệu được giữ nguyên. Yêu cầu này độc lập với migration sang máy khác, vốn ngoài phạm vi v1.
- Giữ dữ liệu đến khi người dùng chủ động xóa; không có lịch tự xóa trong phạm vi đã chốt.
- Khi xóa cuộc họp, xóa bản sao âm thanh và kết quả do ứng dụng quản lý. Giữ nguyên file nguồn bên ngoài.
- Từ chối xóa cuộc họp có tác vụ đang hoạt động; người dùng hủy, chờ backend xác nhận đã dừng rồi mới xóa. Cuộc họp khác vẫn xóa được.

### 6.2. Copy và xuất

- Có nút **Copy notes** và chức năng xuất **Markdown**.
- Copy/xuất sử dụng bản notes người dùng đang chọn, bao gồm các chỉnh sửa của bản đó.
- Kết quả giữ cấu trúc nội dung, action items, trạng thái cần xác nhận và mốc thời gian nguồn.
- Ứng dụng không tự gửi kết quả tới boss, các thành viên hoặc một dịch vụ bên ngoài.

### 6.3. Thiết lập và runtime

- Cho phép tải model và các tài nguyên cần thiết từ Internet trong lần thiết lập đầu.
- Sau khi thiết lập đủ tài nguyên, ASR/phân biệt người nói không cần Internet và được phép chạy khi provider notes chưa sẵn sàng. Nghe lại, đọc/sửa transcript và notes đã lưu, mở lịch sử, copy và xuất phải hoạt động khi mở UI trên máy chạy backend và ngắt Internet. Tạo notes mới cần kết nối provider; nếu chưa dùng được, vẫn lưu transcript và cho tạo notes bằng tác vụ riêng sau đó.
- “Local” áp dụng cho âm thanh, ASR/phân biệt người nói, logic và lưu trữ do máy tự quản đảm nhiệm. Cho phép trao đổi dữ liệu với trình duyệt được cấp quyền. Bước notes được gửi transcript, ID/mốc nguồn, nhãn/tên người nói và ngày họp cần thiết tới provider đã chọn; không gửi bản ghi âm thanh.
- Chuẩn bị sẵn model ASR/phân biệt người nói, tài nguyên và các thành phần frontend để không tải động từ Internet lúc chạy. Tắt telemetry của ứng dụng và các runtime local; kết nối xác thực/inference tới provider notes là ngoại lệ mạng đã chọn. Không suy ra nhà cung cấp cloud không lưu hoặc sử dụng dữ liệu từ việc ứng dụng tắt telemetry local.
- V1 chỉ mở truy cập qua localhost trên máy chạy backend và bắt buộc có token để sử dụng; không mở cổng truy cập ứng dụng ra LAN hoặc Internet.
- Model và thư viện được chọn theo khả năng hoạt động trên máy đã chốt; phần xử lý được đóng gói trong Docker Compose.
- Nếu chọn model có yêu cầu tài khoản, token hoặc chấp thuận điều khoản, bước thiết lập phải thể hiện rõ và để người dùng thực hiện. Việc cho phép tải model không đồng nghĩa với cho phép ứng dụng tự chấp thuận điều khoản thay người dùng.

### 6.3.1. Chọn provider và kết nối tài khoản tạo notes

- UI cung cấp hai lựa chọn **ChatGPT** và **Google Antigravity qua OMP** khi chạy lần đầu có tạo notes hoặc tạo lại notes. Người dùng chọn bên nào thì kết nối tài khoản bên đó; không yêu cầu đăng nhập cả hai.
- Cho chọn model của provider trước mỗi lần chạy; nhớ provider gần nhất và model gần nhất riêng mỗi bên, hiển thị rõ để người dùng đổi. Model/provider của tác vụ giữ nguyên qua các bước chia/hợp nhất/sửa đầu ra. Nếu model đã chọn hết quota hoặc không còn dùng được, báo rõ, không tự chọn model khác. Danh sách dựa trên kết nối; catalog không bảo đảm inference thành công.
- Hoạt động này cho phép gửi nguồn tạo notes tới provider đã chọn, không thay đổi nguồn transcript hay các bản notes cũ. Hoãn phương án Qwen tạo notes local trong v1.
- Đăng nhập provider là kết nối dịch vụ tạo notes, không phải cơ chế đăng nhập ứng dụng hoặc hệ thống nhiều người dùng. OAuth có vòng đời/quota do provider quản lý; không áp quy tắc token localhost không hết hạn cho nó.
- Lưu phiên riêng của một tài khoản mỗi provider trên bản cài để dùng lại, có nút ngắt kết nối. Chỉ yêu cầu đăng nhập khi chưa kết nối hoặc phiên mất hiệu lực; không bắt đăng nhập sau mỗi lần tạo notes. Không nhúng credential vào source/image hoặc đưa chúng vào phản hồi UI/log vận hành.
- Khi hết quota/lỗi tạo notes, giữ transcript và báo rõ để người dùng chọn provider khác rồi chủ động tạo lại. Không tự đổi bên, xoay tài khoản hoặc bật API billing. Notes đã có vẫn giữ nguyên.
- Nhánh ChatGPT chọn thử Sign in with ChatGPT trực tiếp → Responses API cho ứng dụng cá nhân local; nhánh Antigravity gọi OMP qua RPC. Quyền tài khoản, callback Docker, refresh/revoke và đầu ra vẫn cần kiểm chứng. Không coi subscription là API credit không giới hạn hoặc catalog model là bằng chứng gọi được mọi model.
- Khi một tác vụ đang hoạt động đã chọn tài khoản/provider cho bước notes, từ chối ngắt hoặc đổi tài khoản đó, kể cả chưa tới bước gọi model. Người dùng hủy và chờ backend xác nhận đã dừng rồi mới ngắt; provider không thuộc tác vụ vẫn ngắt được. Ngắt kết nối không xóa dữ liệu cuộc họp hay notes.
- Đường OMP + Antigravity được giữ theo quyết định của người dùng sau khi đã thông báo ràng buộc mục 6 điều khoản Google về phần mềm bên thứ ba và khả năng đình chỉ Antigravity/Gemini CLI. Xem [ADR 0007](adr/0007-cloud-notes-providers.md), [điều khoản hiện hành](https://www.antigravity.google/terms) và [khảo sát harness](harness-notes.md).

### 6.4. Cài đặt và đóng gói

- Cung cấp một bộ triển khai Docker Compose. Sau thiết lập, khởi động hệ thống bằng một lệnh.
- Hỗ trợ hai cách lấy image: tự build từ source của dự án hoặc pull image công khai đã được build từ registry mà không cần tài khoản để tải image đó. Cả hai cách đều phải dẫn tới cùng luồng thiết lập và sử dụng của cùng phiên bản. GHCR là registry được đề xuất theo repo GitHub hiện tại; namespace/image cụ thể sẽ được xác định khi triển khai phát hành.
- Cài đặt qua terminal, có hướng dẫn điều kiện máy, bước kiểm tra môi trường và setup tải tài nguyên trước khi khởi động. Không yêu cầu trình cài đặt đồ họa; người dùng tự thực hiện các điều kiện truy cập model nếu có.
- Bộ triển khai gồm `app`, worker transcription, notes service và model-installer; SQLite lưu dữ liệu nghiệp vụ/trạng thái tác vụ; âm thanh là file riêng trên ổ đĩa local. Worker transcription chỉ ở mạng nội bộ, notes service giữ provider egress; giới hạn shared data volume được ghi tại [ADR 0013](adr/0013-notes-service-boundary.md).
- Máy đích phải đáp ứng cấu hình phần cứng, NVIDIA driver và container runtime tương thích được công bố. Các yêu cầu và bước thiết lập phải đủ rõ để cài mới mà không phụ thuộc môi trường phát triển của tác giả.
- Dễ cài trên máy khác là yêu cầu cài mới. Không yêu cầu mang theo lịch sử, cấu hình hoặc model cache từ một bản cài cũ; bước thiết lập có thể tải model cho bản cài mới.
- Dữ liệu cuộc họp và các tài nguyên cần giữ được lưu ngoài lớp ghi tạm của container, để thay container trong cùng bản cài không xóa lịch sử đã lưu.
- Hiệu năng được nghiệm thu trên máy hiện tại. Chạy được trên máy khác không mặc nhiên bảo đảm cùng thời gian xử lý.
- Mỗi bản cài phải có token truy cập riêng; không nhúng token dùng chung, credential hoặc dữ liệu cuộc họp vào source/image công khai.

### 6.5. Token và QR truy cập

- Người chưa được xác thực bằng token hợp lệ không được sử dụng chức năng hoặc truy cập dữ liệu cuộc họp. Kiểm soát phải áp dụng ở backend, gồm API, nghe âm thanh, tải kết quả và thay đổi dữ liệu; chỉ chặn màn hình UI là chưa đủ.
- Sinh token ngẫu nhiên riêng ở lần khởi chạy đầu của mỗi bản cài và lưu trong vùng dữ liệu của bản cài. Token giữ nguyên qua restart hoặc tạo lại container khi vùng dữ liệu đó được giữ nguyên.
- Khi khởi chạy, hệ thống tạo QR chứa link có token hiện tại để người được cấp quyền truy cập ứng dụng. Token truy cập ứng dụng khác với token dùng tải model, đăng nhập registry hoặc kết nối provider notes.
- Cung cấp thêm link có thể bấm/copy để dùng ngay trên cùng máy. Không yêu cầu quét QR bằng điện thoại hoặc truy cập từ thiết bị khác trong v1.
- Token truy cập dùng lại nhiều lần và không tự hết hạn theo thời gian.
- Trình duyệt giữ trạng thái xác thực bằng cookie và xóa token khỏi thanh địa chỉ sau đăng nhập. Ứng dụng không áp thời hạn tự hết hiệu lực cho trạng thái xác thực; nếu trình duyệt mất cookie, người dùng mở lại cùng link để đăng nhập.
- Có lệnh local để người vận hành chủ động thay token. Sau khi thay, token cũ và mọi trạng thái xác thực dựa trên token cũ phải mất hiệu lực; cung cấp QR/link mới để đăng nhập lại. Thay token không xóa dữ liệu cuộc họp hoặc hủy tác vụ xử lý đã được nhận.
- QR/link được hiển thị trong terminal cho người có quyền khởi chạy/cấp lại; không xuất hiện trên trang chưa xác thực hoặc trong log vận hành thông thường.
- Trang trước xác thực chỉ được cung cấp thông tin cần thiết để đăng nhập; không trả dữ liệu cuộc họp hoặc tự cấp token cho khách chưa xác thực.
- Máy cá nhân là môi trường tin cậy: v1 bảo vệ truy cập ứng dụng qua web, không đặt yêu cầu chống người có quyền dùng cùng tài khoản hệ điều hành, quản trị Docker hoặc đọc trực tiếp dữ liệu máy chủ.
- Không bổ sung tài khoản/mật khẩu hoặc phân quyền nhiều người dùng trong v1.

### 6.6. Source công khai và license

- Mục tiêu phát hành là source và image công khai, cho phép người khác tự build hoặc tải về sử dụng trong phạm vi chính sách non-commercial đã thống nhất.
- Chính sách cho phép sử dụng, sửa đổi và chia sẻ lại cho mục đích phi thương mại, giữ license và thông báo bản quyền của phần mã gốc. Việc sử dụng phục vụ kinh doanh, kể cả tổng kết cuộc họp nội bộ của doanh nghiệp khác, cần được chủ sở hữu cho phép riêng.
- “Công khai” mô tả khả năng lấy source/image; quyền sử dụng, sửa đổi và phân phối phải được thể hiện bằng văn bản license. Chính sách đã chốt không tự thay thế cho văn bản license của bản phát hành.
- PolyForm Noncommercial 1.0.0 là ứng viên để đối chiếu khi chuẩn bị phát hành. Cần chọn và rà soát văn bản thể hiện đúng chính sách trước khi áp dụng; không coi việc chọn phạm vi sản phẩm là đã hoàn thành rà soát pháp lý. [Văn bản PolyForm chính thức](https://polyformproject.org/licenses/noncommercial/1.0.0).
- License dự án chỉ áp dụng cho phần mã mà chủ sở hữu dự án có quyền cấp phép. Thư viện, base image và model vẫn có điều khoản riêng; cần đối chiếu quyền phân phối của những thành phần thực tế được chọn khi chuẩn bị image công khai.
- Chưa áp dụng một license cụ thể vào repo hoặc công bố source/image trong phiên làm rõ specification này.

## 7. Giới hạn phần cứng và lựa chọn kỹ thuật

Ngân sách phần cứng là máy Ryzen 9 và RTX 5080 hiện có. RTX 5080 có **16 GB VRAM** theo thông số NVIDIA. Không đưa việc mua thêm phần cứng thành điều kiện để đạt phạm vi v1. [Nguồn NVIDIA](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/)

Khảo sát môi trường hiện tại ghi nhận Ubuntu 24.04.4, Ryzen 9 9950X và khoảng 60,4 GiB RAM tổng mà hệ điều hành nhận. Lần khảo sát chưa đọc được GPU qua `nvidia-smi`; cần xác minh runtime GPU trên máy đích trước khi đo hiệu năng.

[PDF nghiên cứu ban đầu](ASR-initial-deep-research-vi.pdf) là tài liệu tham khảo về giải pháp. Các tên PhoWhisper, WhisperX, pyannote, Qwen2.5 32B và vLLM là ứng viên, không phải các thành phần đã được chốt bắt buộc.

Trong phiên thiết kế backend, người dùng chọn Whisper large-v3 qua faster-whisper và pyannote Community-1 làm bộ thử nghiệm local. Sau phép đo của #168, default phần cứng được đổi thành Qwen3-ASR-1.7B trên GPU và Whisper small trên CPU; Whisper large-v3 vẫn là lựa chọn explicit. Qwen3-14B từng được chọn thử nhưng đã hoãn theo Q12 để tập trung VRAM cho ASR/phân biệt người nói. Tạo notes dùng ChatGPT hoặc model qua OMP + Antigravity do người dùng chọn, ưu tiên khảo sát Gemini ở nhánh Antigravity; xem [khảo sát model](model-candidates.md).

Các quyết định kỹ thuật cần giải quyết bằng thử nghiệm:

- Chọn ASR cho toàn tiếng Anh, tiếng Việt xen thuật ngữ và chuyển ngôn ngữ trong cùng bản ghi. Kết quả benchmark tiếng Việt của PhoWhisper chưa đủ để suy ra chất lượng ở cả ba trường hợp. [Nguồn VinAI](https://github.com/VinAIResearch/PhoWhisper)
- Chọn cách phân biệt người nói và căn chỉnh thời gian đủ tốt để mở đúng đoạn nguồn, kể cả khi chuyển ngôn ngữ.
- Chọn model tạo notes theo quyền tài khoản và cách xử lý transcript dài phù hợp context, quota và chất lượng của từng provider. Không âm thầm cắt nguồn hoặc để harness tự rút gọn làm mất bằng chứng; việc chia đoạn/hợp nhất cần được thiết kế và kiểm chứng.
- Kiểm tra khả năng tải và chạy offline toàn bộ tài nguyên ASR/phân biệt người nói. Ví dụ, Community-1 có điều kiện truy cập trên Hugging Face và hỗ trợ chạy từ bản tải local. [Model card pyannote](https://huggingface.co/pyannote/speaker-diarization-community-1)

UI trên trình duyệt, Python/FastAPI, Docker Compose với `app`, worker transcription, notes-service và model-installer đã được chọn; processing roles giao việc qua SQLite và file âm thanh local. Schema/API và điều phối đã được cụ thể hóa trong backend contract và ADR 0013, cần kiểm chứng runtime. Framework frontend được chọn khi triển khai; cấu hình model phát hành dựa trên kết quả thử nghiệm. Các lựa chọn kỹ thuật phải phục vụ hành vi và tiêu chí trong đặc tả này.

## 8. Nghiệm thu

### 8.1. Bộ bản ghi thực tế

Khởi đầu bằng ít nhất ba bản ghi đại diện, với âm thanh/ASR/phân biệt người nói local và notes qua provider được người vận hành chọn:

1. Cuộc họp hoàn toàn bằng tiếng Anh.
2. Cuộc họp tiếng Việt xen thuật ngữ Anh.
3. Cuộc họp có cả tiếng Anh và tiếng Việt trong cùng bản ghi.

Người dùng đánh dấu trước các ý quan trọng, quyết định và công việc cần có trong kết quả. Bộ mẫu phản ánh cách thu thực tế, độ dài 1–2 giờ và 2–3 người nói. Cần có mẫu dài 2 giờ để kiểm chứng trực tiếp mục tiêu thời gian cho bản ghi 2 giờ.

### 8.2. Tiêu chí kiểm tra

| Mã | Tiêu chí | Cách kiểm tra |
| --- | --- | --- |
| AC-01 | Nhận đúng nội dung quan trọng ở cả ba trường hợp ngôn ngữ | Đối chiếu từng bản ghi với phần người dùng đã đánh dấu; không dùng kết quả tốt ở một ngôn ngữ để che lỗi ở ngôn ngữ khác |
| AC-02 | Notes ghi đủ các ý quan trọng đã đánh dấu | So sánh notes với nội dung nguồn và các mục đã xác định trước |
| AC-03 | Không bịa quyết định, người phụ trách hoặc deadline trong bộ mẫu | Đối chiếu từng quyết định/action item; kiểm tra riêng trường hợp thiếu thông tin và phát biểu mơ hồ |
| AC-04 | Dùng đúng nhãn người nói tại các đoạn quan trọng | Kiểm tra đoạn đổi người nói và action item dùng đại từ “tôi”; không yêu cầu tự nhận diện tên |
| AC-05 | Mốc thời gian tìm được đúng bằng chứng | Bấm chip mốc thời gian của mục notes (chế độ Xem) và nghe đoạn nguồn; timestamp theo đoạn là đủ. Chip chỉ gắn vào mục còn nguyên nội dung như lúc tạo; Markdown, copy và tải về không có link |
| AC-06 | Xử lý deadline tương đối đúng điều kiện | Kiểm tra có/không có ngày họp, “tomorrow” và “next week” |
| AC-07 | Giữ chỉnh sửa và các bản notes | Sửa transcript, kiểm tra cảnh báo notes cũ; tạo lại, rồi mở lại bản notes đã sửa trước đó |
| AC-08 | Lịch sử không phụ thuộc vị trí file nguồn | Di chuyển file nguồn sau khi nhập; mở lại cuộc họp và phát âm thanh từ bản sao |
| AC-09 | Xóa đúng phạm vi | Xóa cuộc họp; kiểm tra dữ liệu ứng dụng được xóa và file nguồn bên ngoài vẫn còn |
| AC-10 | ASR/phân biệt người nói và duyệt dữ liệu local; notes mới qua provider đã chọn | Ngắt Internet, nhập bản ghi qua UI, chạy ASR/phân biệt người nói rồi xem/sửa transcript; nghe lại/đọc/sửa/copy/xuất dữ liệu đã lưu. Notes mới báo thiếu kết nối. Khi có mạng, kiểm tra kết nối xác thực/inference chỉ theo provider đã chọn, không gửi audio, telemetry local hoặc tải UI/model local lúc runtime |
| AC-11 | Copy và Markdown phản ánh bản notes được chọn | Đối chiếu kết quả xuất với bản đã sửa, gồm action items; Markdown và bản xuất không chứa link timestamp |
| AC-12 | Tiến độ, hủy và lỗi được thể hiện đúng | Hủy tác vụ, thử đầu vào không hỗ trợ và lỗi xử lý; không báo hoàn thành đầy đủ cho tác vụ thất bại |
| AC-13 | Mục tiêu xử lý bản ghi 2 giờ trong tối đa 30 phút | Đo trên máy đích sau thiết lập và kết nối provider; tính từ bắt đầu xử lý đến khi cả transcript và notes sẵn để duyệt, gồm nạp model, tiền xử lý và thời gian gọi provider; ghi provider/model, điều kiện mạng/quota và các lượt retry |
| AC-14 | Mục tiêu duyệt/sửa khoảng 5–10 phút mỗi giờ họp | Người dùng đo thời gian thực tế để đưa bản nháp về mức dùng được trên từng bản ghi |
| AC-15 | Toàn bộ luồng sử dụng hoạt động qua UI trình duyệt | Trên Chrome và Firefox bản ổn định tại thời điểm kiểm thử ở laptop/desktop, mở URL và thực hiện nhập, xử lý, nghe lại, sửa, mở lịch sử, copy và xuất |
| AC-16 | Tác vụ đã được backend nhận tiếp tục khi trình duyệt mất kết nối | Upload đủ file, bắt đầu xử lý, đóng tab hoặc ngắt kết nối trình duyệt trong khi backend vẫn chạy; mở lại để xem tiến độ hoặc kết quả |
| AC-17 | Cài mới được từ source hoặc image công khai bằng Docker Compose | Kiểm tra riêng hai đường build từ source và pull image cùng phiên bản không cần đăng nhập registry trên môi trường đủ phần cứng/runtime; làm theo hướng dẫn terminal, thiết lập tài nguyên, khởi động bằng một lệnh, kết nối provider từ UI và xử lý mẫu; điều kiện truy cập model nếu có được xử lý riêng |
| AC-18 | Giữ lịch sử khi container được thay thế trong cùng bản cài | Lưu cuộc họp và chỉnh sửa, tạo lại container mà giữ nguyên vùng lưu dữ liệu, rồi mở lịch sử và nghe lại âm thanh; không đòi tác vụ đang chạy tự tiếp tục |
| AC-19 | Tác vụ bị ngắt được nhận diện và chạy lại có chủ đích | Ngắt tiến trình xử lý giữa tác vụ rồi khởi động lại; kiểm tra trạng thái bị gián đoạn, không tự xử lý lại, cho bấm chạy lại từ bản ghi đã lưu và giữ các kết quả hoàn tất/notes đã sửa |
| AC-20 | Một tác vụ xử lý; không nhận hàng đợi ẩn | Khi đang xử lý, gửi thêm yêu cầu xử lý hoặc tạo lại notes; kiểm tra phản hồi bận và không chạy thêm tác vụ, trong khi vẫn xem/sửa được cuộc họp đã hoàn tất |
| AC-21 | Upload bị ngắt không tạo kết quả giả hoặc giữ file dở vô hạn | Ngắt upload, kiểm tra thông báo chưa hoàn tất, phần file dở được dọn và không bị đưa vào xử lý; tải lại từ đầu thành công |
| AC-22 | Không có token hợp lệ thì không truy cập được chức năng và dữ liệu | Thử trình duyệt chưa xác thực, token sai và gọi trực tiếp API/URL âm thanh/xuất kết quả/thay đổi dữ liệu; phải bị từ chối, không trả nội dung cuộc họp hoặc token truy cập |
| AC-23 | QR và link khi khởi chạy truy cập được trên cùng máy | Giải mã QR, mở link trên máy chạy backend và xác thực thành công; kiểm tra cookie giữ trạng thái xác thực, token được xóa khỏi thanh địa chỉ và không xuất hiện trong log vận hành thông thường; QR/link được cung cấp trong terminal và tạo được khi ngắt Internet |
| AC-24 | Token dùng lại được; ứng dụng không tự đặt hạn sử dụng | Đăng nhập lại bằng cùng token nhiều lần và kiểm tra với thời gian mô phỏng tăng; token và trạng thái xác thực không bị vô hiệu chỉ do đã dùng hoặc đã qua một thời hạn cố định; khi xóa cookie, mở lại link cũ để đăng nhập |
| AC-25 | Chỉ truy cập ứng dụng qua localhost trong v1 | Kiểm tra cấu hình publish cổng và truy cập thành công từ cùng máy; thử truy cập qua địa chỉ LAN từ máy khác phải không kết nối được tới dịch vụ ứng dụng, kể cả khi biết token |
| AC-26 | Token giữ nguyên trong cùng bản cài và riêng biệt giữa các bản cài | Restart/tạo lại container với vùng dữ liệu cũ rồi đăng nhập bằng link cũ; khởi tạo bản cài khác với vùng dữ liệu mới và kiểm tra có token khác, không dùng chung token có sẵn trong source/image |
| AC-27 | Thay token vô hiệu toàn bộ quyền truy cập cũ | Đăng nhập ở hai phiên trình duyệt độc lập, chạy lệnh local thay token, kiểm tra cả token cũ và cookie cũ bị từ chối ở backend; QR/link mới dùng được, dữ liệu giữ nguyên và tác vụ đã nhận tiếp tục |
| AC-28 | Đặt tên người nói thủ công, riêng cho từng cuộc họp | Đặt/sửa/xóa tên và mở lại sau restart; kiểm tra nhãn gốc vẫn giữ, để trống thì dùng nhãn mặc định, cuộc họp khác không đổi tên; notes cũ được đánh dấu cần cập nhật nhưng giữ nguyên, bản tạo mới dùng tên đã đặt và copy/xuất đúng bản được chọn |
| AC-29 | Giao diện A dùng dark theme | Duyệt trên laptop/desktop với lịch sử bên trái và trang cuộc họp hai tab (Meeting notes, Transcript & bản ghi); kiểm tra chữ, trạng thái, focus bàn phím, trình phát và các hộp thoại đều đọc/thao tác được trên nền tối |
| AC-30 | Duyệt lịch sử bằng lịch tháng nhỏ và lớn | Kiểm tra ngày có/không có cuộc họp, nhiều cuộc họp cùng ngày, tháng trống, tháng 12 sang tháng 1, năm nhuận và chọn năm cũ; mở đúng cuộc họp từ lịch, tìm được cuộc họp chưa đặt ngày, dấu ngày cập nhật sau nhập/xóa và metadata/người nói/notes không bị đổi khi điều hướng |
| AC-31 | Khóa nguồn của cuộc họp trong lúc tạo notes | Tạo notes rồi thử sửa transcript/tên người nói qua UI, tab khác và API trực tiếp; phải bị từ chối. Vẫn đọc được nguồn, sửa bản notes cũ và cuộc họp khác; sau khi tác vụ hoàn tất hoặc thực sự dừng vì lỗi/hủy/gián đoạn thì sửa nguồn được trở lại |
| AC-32 | Giữ transcript hoàn tất khi tạo notes lỗi hoặc bị hủy | Gây lỗi/hủy ở bước notes sau khi transcript đã lưu đầy đủ; kiểm tra tác vụ không báo hoàn tất, transcript còn nguyên và sau khi tác vụ kết thúc có thể sửa rồi yêu cầu tạo notes riêng. Chạy lại toàn bộ bản ghi vẫn bắt đầu từ đầu |
| AC-33 | Không xóa cuộc họp khi worker còn xử lý nó | Thử xóa qua UI/API khi tác vụ đang chạy hoặc đang dừng; phải bị từ chối. Hủy, chờ xác nhận đã dừng rồi xóa thành công; cuộc họp khác vẫn xóa được |
| AC-34 | Chạy lại bản ghi giữ nguồn cũ và chỉnh sửa | Sửa transcript, tạo/sửa notes, chạy lại từ bản ghi; kiểm tra thế hệ transcript mới được lưu riêng, nguồn cũ còn nguyên và các bản notes cũ vẫn đối chiếu đúng nguồn, không bị gắn segment/speaker của lần xử lý mới |
| AC-35 | Chọn và kết nối độc lập hai provider tạo notes | Với tài khoản/quota hợp lệ, kiểm tra riêng ChatGPT và OMP + Antigravity: chọn một bên, kết nối qua UI và tạo notes mà không cần đăng nhập bên kia; tạo lại với provider khác vẫn sinh bản mới, giữ bản cũ và không đổi transcript |
| AC-36 | Dùng lại phiên provider và có ngắt kết nối | Kết nối từng provider, tạo notes nhiều lần và restart với cùng vùng dữ liệu; phiên còn hợp lệ được dùng lại. Thử phiên mất hiệu lực, UI yêu cầu kết nối lại. Ngắt kết nối khi rảnh rồi kiểm tra ứng dụng không tiếp tục dùng phiên đã ngắt, bên còn lại và dữ liệu cuộc họp giữ nguyên |
| AC-37 | Tạo transcript trước khi provider dùng được | Thử chưa đăng nhập, provider hết quota hoặc mất Internet; chạy ASR/diarization, lưu transcript và kết thúc tác vụ, hiển thị chưa có notes. Kiểm tra có thể sửa nguồn/nhận tác vụ mới, không tự sinh notes khi kết nối trở lại; người dùng chủ động tạo notes mà không chạy lại âm thanh |
| AC-38 | Không tự chuyển provider hoặc bật API billing khi tạo notes lỗi | Kết nối cả hai provider, gây lỗi/quota ở bên được chọn; kiểm tra transcript và notes cũ còn nguyên, tác vụ báo lỗi và không gọi provider còn lại hoặc API trả phí. Người dùng chọn bên khác rồi tạo notes thành bản mới thành công |
| AC-39 | Transcript dài được xử lý đầy đủ qua chia/hợp nhất | Dùng nguồn vượt ngân sách một lượt gọi, có quyết định đổi ở cuối và action item qua ranh giới đoạn; kiểm tra mọi phần nguồn đều được đưa vào xử lý, hợp nhất không mất ID và không vượt ngân sách mỗi bước; đối chiếu chất lượng như AC-02/03/05 |
| AC-40 | Tối đa một lượt sửa đầu ra không hợp lệ cho cả tác vụ notes | Mô phỏng sai schema/ID ở bước trích hoặc hợp nhất; kiểm tra cùng provider/model chỉ được gọi sửa thêm một lượt tổng cộng. Lượt sửa thất bại hoặc lỗi khác sau đó phải kết thúc lỗi, không công bố notes chưa hợp lệ; transcript/notes cũ giữ nguyên |
| AC-41 | Notes cũ giữ đúng nguồn trước chỉnh sửa | Tạo notes rồi sửa transcript/tên người nói trong cùng thế hệ; mở bằng chứng của notes cũ và kiểm tra đúng văn bản, tên, ngày họp, ID/mốc nguồn ban đầu, khác với nguồn hiện tại. Sửa notes cũ không đổi bản chụp; tạo lại notes gắn với nguồn mới |
| AC-42 | Tab cũ không ghi đè chỉnh sửa mới | Mở cùng dữ liệu ở hai tab, lưu tab A rồi lưu bản cũ ở tab B; kiểm tra từ chối ghi đè và giữ bản nháp chưa lưu của B. Thực hiện với transcript/tên người nói và cùng một bản notes; tạo notes mới trong lúc sửa bản cũ không chuyển đích ghi |
| AC-43 | Mặc định đúng thế hệ transcript và không tự ghép tên người nói | Chạy lại thành công, kiểm tra bản mới là mặc định, bản cũ chỉ đọc, notes cũ vẫn sửa/xuất được; bản mới dùng nhãn Speaker, không nhận tên từ bản cũ. Thất bại trước khi lưu transcript mới giữ mặc định cũ; thất bại ở notes giữ transcript mới đã hoàn tất |
| AC-44 | Không ngắt/đổi tài khoản provider của tác vụ đang hoạt động | Thử từ UI/API lúc tác vụ đã chọn provider, gồm cả trước bước notes và đang hủy; phải bị từ chối. Hủy, chờ xác nhận dừng rồi ngắt thành công; provider không thuộc tác vụ vẫn ngắt được và dữ liệu giữ nguyên |
| AC-45 | Sửa tên/ngày họp giữ đúng nguồn và lịch sử | Sửa tên, thêm/đổi/xóa ngày sau import; kiểm tra lịch cập nhật, ngày thay đổi làm notes cần cập nhật nhưng snapshot/notes cũ giữ nguyên. Sửa ngày lúc tạo notes hoặc ghi từ version cũ phải bị từ chối. Chạy lại với ngôn ngữ khác không đổi thông tin thế hệ cũ |
| AC-46 | Sửa người nói từng đoạn và thêm nhãn đúng phạm vi | Gán lại một segment, thêm nhãn còn thiếu rồi gán vào nhãn đó; kiểm tra original assignment giữ nguyên, đoạn khác/timestamp không đổi, notes cần cập nhật và notes mới dùng nguồn đã sửa. Thử sai generation, thế hệ cũ, tab cũ và lúc khóa nguồn phải bị từ chối |
| AC-47 | Lỗi mạng không tự gửi lại inference | Mô phỏng mất mạng sau khi gửi request hoặc đang nhận kết quả; kiểm tra không có lượt gửi lại từ app/SDK/harness, giữ transcript/notes cũ và báo lỗi. Người dùng thử lại tạo tác vụ mới; retry HTTP tạo tác vụ với cùng request ID chỉ trả lại tác vụ cũ, không chạy thêm model |
| AC-48 | Lưu Markdown chỉnh tay có link hỏng mà không mở sai nguồn | Sửa ID/link thành nguồn không tồn tại hoặc không thuộc snapshot của notes; lưu thành công, giữ nguyên văn bản và cảnh báo, không mở nguồn thay thế. Copy/xuất đúng nội dung; đầu ra model lỗi tương tự vẫn phải qua AC-40 |
| AC-49 | Chọn và nhớ model, giữ lựa chọn suốt tác vụ | Chọn hai model ở hai provider, mở lại UI và kiểm tra lựa chọn mỗi bên được nhớ. Chạy tác vụ nhiều bước rồi đổi preference cho lần sau; tác vụ hiện tại vẫn dùng model cũ. Model mất quyền/quota phải báo lỗi, không tự đổi model |

AC-13 và AC-14 là **mục tiêu đã thống nhất nhưng chưa được kiểm chứng**. Báo cáo nghiệm thu phải ghi rõ kết quả đạt/chưa đạt, phần cứng, cấu hình model và bản ghi dùng để đo. Không suy ra đạt mục tiêu chỉ từ tốc độ quảng cáo của một thành phần hoặc một đoạn âm thanh ngắn.

Các kiểm tra chất lượng áp dụng trên bộ mẫu đã duyệt; kết quả đạt trên bộ mẫu không phải cam kết chính xác tuyệt đối cho mọi cuộc họp. Khi kết quả chưa đạt, cần ghi rõ sai lệch và thử điều chỉnh trong giới hạn phần cứng hiện có.

## 9. Công việc thiết kế và kiểm chứng trước phát hành

Các quyết định phạm vi sản phẩm đã được thống nhất và đưa vào các mục trên. Các việc dưới đây tiếp tục được giải quyết qua [thiết kế backend](backend-design.md), thử nghiệm, triển khai hoặc chuẩn bị phát hành:

- Triển khai schema, HTTP API và điều phối qua SQLite theo [backend contract](backend-contract.md); bảo đảm tác vụ độc lập với kết nối trình duyệt và việc thay token. Chọn framework frontend khi triển khai giao diện theo thiết kế đã duyệt.
- Thiết kế/kiểm chứng adapter ChatGPT và OMP + Antigravity, đăng nhập từ UI trong Docker, quản lý credential, model access, lỗi/quota, hủy và kiểm tra notes có nguồn. Hoãn Qwen local; không thêm training danh tính vào v1.
- Xác minh GPU runtime, chọn model/cấu hình, xác định định dạng và giới hạn đầu vào từ các file mẫu, rồi đo chất lượng và hiệu năng trên máy hiện có. Các mục tiêu AC-13 và AC-14 vẫn cần kết quả thực nghiệm.
- Triển khai token/cookie và kiểm soát truy cập backend phù hợp với localhost; kiểm tra việc xóa token khỏi URL và tránh ghi credential vào log. Thiết kế giữ token không tự hết hạn là quyết định sản phẩm cho máy cá nhân, không suy rộng thành cấu hình mặc định cho truy cập Internet sau này. [Tham khảo cơ chế phiên của OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
- Chuẩn bị build/pull image công khai, chọn namespace/tag/digest phát hành và kiểm tra hai đường cài đặt cho cùng phiên bản. Công khai image là mục tiêu phát hành, chưa phải trạng thái hiện tại của repo.
- Chọn văn bản license thể hiện chính sách ở mục 6.6, đối chiếu quyền phân phối của các dependency/model được chọn và hoàn tất tài liệu cấp phép trước khi công bố.
