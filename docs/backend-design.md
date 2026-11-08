# Backend design v1 — ASR Studio

Trạng thái: **đã thống nhất các quyết định thiết kế Q1–Q27**, ngày 2026-10-16. Không còn câu hỏi sản phẩm đang chờ trong phiên thiết kế này; các việc còn lại ở mục 5 là triển khai và kiểm chứng. Tài liệu này cụ thể hóa [specification v1](specification.md) và [frontend design v1](frontend-design.md), chưa xác nhận backend hoặc model đã được triển khai, chạy thử hay nghiệm thu.

[Backend contract v1](backend-contract.md) cụ thể hóa ranh giới module, schema, API, ownership/hủy/phục hồi và các tình huống cạnh tranh theo Q1–Q27. Các tên bảng/endpoint và thông số vận hành là thiết kế kỹ thuật trước triển khai; kết quả kiểm chứng có thể yêu cầu điều chỉnh mà vẫn giữ các hành vi đã thống nhất.

## 1. Tổ chức backend và lưu trữ

- Bộ Docker Compose tách `app` phục vụ UI/API, `worker` chạy ASR/diarization và `notes-service` gọi provider notes. Tách vòng đời tiến trình để lỗi model hoặc provider không trực tiếp làm chết tiến trình phục vụ UI; worker không cần provider mount hay egress. Việc hết tài nguyên toàn máy vẫn cần kiểm soát và kiểm thử riêng. Xem [ADR 0013](adr/0013-notes-service-boundary.md).
- Python cho phần ứng dụng và điều phối; FastAPI phục vụ HTTP API. Worker là tiến trình riêng gọi các runtime model.
- SQLite lưu metadata cuộc họp, transcript, tên người nói, các bản notes và trạng thái tác vụ. Âm thanh được lưu thành file riêng trên ổ đĩa local của máy chạy backend.
- Dữ liệu nằm trong vùng lưu bền, được giữ khi thay container. Không sử dụng một service PostgreSQL trong thiết kế đã chọn.
- Giao dịch ghi phải ngắn; không giữ giao dịch cơ sở dữ liệu trong lúc suy luận model. `app` ghi nhận tác vụ vào SQLite khi hệ thống rảnh; `worker` hoặc `notes-service` nhận việc bằng giao dịch nguyên tử và cập nhật tiến độ/kết quả. Trạng thái, role và quyền nhận tác vụ được lưu tại SQLite; các service xử lý cùng phụ thuộc schema và `processing_slot` này.
- Giữ giới hạn một tác vụ xử lý toàn hệ thống, gồm xử lý bản ghi và tạo notes. Không nhận thêm tác vụ vào hàng đợi khi đang bận.

Lý do lựa chọn được ghi tại [ADR 0004](adr/0004-app-worker-sqlite.md).

## 2. Nguồn tạo notes và chỉnh sửa

**Quyết định Q3: khóa chỉnh sửa nguồn của cuộc họp trong lúc tạo notes.** Không áp dụng đề xuất cho phép tiếp tục sửa nguồn trong khi model đang tạo notes.

- Nguồn tạo notes gồm transcript, tên người nói và ngày họp đã nhập nếu có. Nguồn phải giữ nguyên trong suốt lần tạo notes.
- Khóa phải được thực thi ở backend, kể cả khi thao tác đến từ tab khác hoặc gọi API trực tiếp; trạng thái vô hiệu hóa trên UI không đủ.
- Khóa có hiệu lực từ khi backend nhận tác vụ tạo notes đến khi tác vụ hoàn tất hoặc đã kết thúc vì lỗi, hủy hay gián đoạn. Yêu cầu hủy chưa đồng nghĩa worker đã dừng; không giải phóng khóa khi worker còn có thể đọc hoặc ghi kết quả.
- Người dùng vẫn được đọc nguồn, xem và sửa các bản notes đã có. Việc chỉnh sửa notes phải nhắm đúng bản notes được chọn. Các cuộc họp khác vẫn xem/sửa bình thường.
- Sau khi mở khóa, sửa transcript, gán lại speaker, tên người nói hoặc ngày họp làm notes cần cập nhật như spec. Không tự thay nội dung notes hoặc tự chạy lại model.
- **Q23:** cho sửa tên và ngày họp sau import; ngày thuộc nguồn tạo notes nên chịu khóa trong lúc tạo và kiểm tra phiên bản khi ghi. Đổi ngày làm notes cần cập nhật, giữ ngày cũ trong bản chụp; chỉ đổi tên cuộc họp không thay nguồn notes. Ngôn ngữ gợi ý được chọn cho lần xử lý mới, không sửa lịch sử thế hệ cũ.
- **Q19:** mỗi bản notes gắn với bản chụp nguồn bất biến đã dùng để tạo nó, gồm nội dung transcript, ID/mốc nguồn, tên người nói và ngày họp. Sửa nguồn sau đó không đổi bằng chứng của notes cũ; mở nguồn của notes cũ phải chỉ rõ đang xem bản chụp, không phải transcript hiện tại. Bản chụp phục vụ đối chiếu về sau, không thay quyết định khóa nguồn trong lúc tạo. Xem [ADR 0008](adr/0008-notes-source-snapshots.md).
- **Q20:** khi lưu transcript/tên người nói hoặc một bản notes, kiểm tra phiên bản đã đọc. Nếu dữ liệu đích đã thay đổi, từ chối ghi đè và giữ phần chưa lưu ở UI để người dùng đối chiếu với bản mới; không tự trộn nội dung. Tạo bản notes mới không được khiến thao tác đang sửa bản cũ ghi nhầm sang bản mới.
- **Q24:** cho gán lại speaker của từng segment và thêm nhãn khi thiếu; giữ nhãn model gán ban đầu để đối chiếu. Các thay đổi chỉ áp dụng ở thế hệ hiện tại, chịu khóa nguồn/kiểm tra phiên bản, tăng phiên bản nguồn và làm notes cần cập nhật. Timestamp/ranh giới đoạn giữ nguyên; không có sửa thời gian hoặc tách/gộp đoạn trong v1.

## 3. Kết quả hoàn tất, lỗi, chạy lại và xóa

### 3.1. Transcript hoàn tất nhưng notes thất bại

- Giữ transcript đã hoàn tất và lưu thành công khi bước tạo notes lỗi hoặc bị hủy. Sau khi tác vụ kết thúc và khóa được giải phóng, cho người dùng xem, sửa và chủ động yêu cầu tạo notes từ transcript này.
- Trạng thái tác vụ trước vẫn là lỗi, đã hủy hoặc bị gián đoạn theo nguyên nhân thực tế; không coi việc có transcript là đã hoàn tất cả pipeline.
- Chỉ coi transcript là kết quả dùng được khi phần transcript đã hoàn tất và được lưu đầy đủ. Chính sách với dữ liệu nội bộ còn dở không được suy thành quyền xuất một transcript đầy đủ.
- **Quyết định Q14:** khi provider chưa kết nối hoặc chưa dùng được, vẫn cho chạy ASR/diarization local. Lưu transcript đầy đủ rồi kết thúc tác vụ, hiển thị đã có transcript/chưa có notes; không giữ một tác vụ chờ đăng nhập hoặc quota. Sau đó người dùng chủ động tạo notes, không phải chạy lại âm thanh. Không báo toàn bộ pipeline đã có đủ kết quả khi notes chưa được tạo.
- **Quyết định Q15/Q25:** lỗi/hết quota ở bước notes không tự chuyển sang provider hoặc tài khoản khác, kể cả bên kia đã đăng nhập, và không tự bật API billing. Không tự gửi lại inference khi lỗi mạng; giữ transcript/notes cũ, để người dùng chủ động thử lại hoặc chọn bên khác bằng tác vụ mới. SDK/harness phải tuân theo chính sách này; một lượt sửa đầu ra Q18 vẫn được phép.

### 3.2. Hai thao tác khác nhau

| Thao tác | Nguồn và hành vi |
| --- | --- |
| Chạy lại toàn bộ từ bản ghi | Một lần xử lý từ đầu bằng bản ghi đã lưu, theo yêu cầu phục hồi tác vụ gián đoạn trong spec; không tự tiếp tục từ checkpoint |
| Tạo notes từ transcript đã hoàn tất | Một tác vụ tạo notes do người dùng chủ động yêu cầu; dùng transcript hiện có và tạo bản notes mới |

**Quyết định Q10/Q21:** kết quả chạy lại được lưu thành một thế hệ transcript mới, giữ transcript cũ cùng các chỉnh sửa và liên kết nguồn của các bản notes cũ. Khi transcript mới đã lưu đầy đủ, nó trở thành thế hệ mặc định để xem/sửa, kể cả bước notes sau đó thất bại. Các thế hệ trước chỉ đọc; các bản notes cũ vẫn sửa/copy/xuất được. Không trộn segment/nhãn hoặc tự ghép tên người nói giữa hai lần xử lý; thế hệ mới dùng nhãn Speaker để người dùng đặt tên lại. Nếu lần chạy mới chưa có transcript hoàn tất, giữ thế hệ đang có làm mặc định. Xem [ADR 0006](adr/0006-transcript-generations.md).

### 3.3. Xóa cuộc họp

- Từ chối xóa cuộc họp có tác vụ đang hoạt động. Người dùng hủy, chờ backend xác nhận đã dừng rồi mới xóa.
- Cuộc họp khác vẫn có thể xóa theo spec.
- Kiểm tra điều kiện xóa phải được thực thi ở backend và phối hợp với việc nhận tác vụ để tránh một tác vụ mới bắt đầu giữa lúc kiểm tra và xóa.
- Xóa dữ liệu do ứng dụng quản lý, giữ nguyên file nguồn bên ngoài. Dùng trạng thái xóa lưu bền để tiếp tục dọn file sau restart theo [backend contract](backend-contract.md); cần kiểm chứng lỗi giữa chừng.

## 4. Nội dung notes

- Markdown là nội dung chính được lưu và chỉnh sửa của mỗi bản notes. Hiển thị, copy và xuất lấy từ cùng nội dung của bản được chọn.
- Tạo lại notes luôn sinh bản mới, giữ nguyên các bản trước và phần đã sửa thủ công.
- **Quyết định Q11:** model sinh dữ liệu có cấu trúc trung gian với ID đoạn nguồn; backend kiểm tra cấu trúc và tham chiếu nguồn trước khi chuyển thành Markdown. Cấu trúc dữ liệu và điều kiện kiểm tra ở backend contract; schema thực thi được hoàn thiện khi triển khai. Kiểm tra tham chiếu không bảo đảm nội dung đúng về ý nghĩa.
- **Q17:** nguồn vừa ngân sách thì xử lý một lượt; nguồn dài được chia theo đoạn phát biểu, có phần chồng tại ranh giới, trích thông tin kèm ID nguồn rồi hợp nhất bằng cùng provider/model đã chọn. Mọi bước phải kiểm soát ngân sách đầu vào/đầu ra, kể cả hợp nhất; không cắt bỏ phần cuối hoặc dùng auto-compaction làm mất nguồn. Đổi lại cần nhiều lượt gọi và quota/thời gian hơn.
- **Q18:** khi sai cấu trúc hoặc tham chiếu nguồn, cho tối đa một lượt yêu cầu sửa tự động trong **toàn bộ tác vụ notes**, cùng provider/model. Giới hạn không được đặt lại cho mỗi chunk hoặc bước hợp nhất. Nếu vẫn lỗi hoặc cần sửa thêm sau khi đã dùng lượt này, kết thúc lỗi, giữ transcript/notes cũ, không công bố notes dở hay sai schema. Q25 không cho tự gửi lại inference vì lỗi mạng; không đánh đồng việc này với lượt sửa đầu ra đã nhận đầy đủ.
- Sau khi người dùng sửa tự do, backend không bảo đảm mọi action item vẫn có đủ trường có cấu trúc. Không tự diễn giải lại hoặc ghi đè nội dung người dùng để ép về schema.
- **Q26:** Markdown do người dùng sửa có liên kết nguồn không hợp lệ vẫn được lưu nguyên văn, có cảnh báo và vô hiệu thao tác mở nguồn ở liên kết đó; không tự đổi ID hoặc mở nguồn khác. Chính sách này không cho phép công bố đầu ra model chưa qua kiểm tra Q18. Cách dựng liên kết từ snapshot/segment ID được ghi trong backend contract.

Lý do lựa chọn được ghi tại [ADR 0005](adr/0005-markdown-notes.md).

## 5. Các nhánh đã thống nhất và việc còn phải kiểm chứng

| Nhánh | Thiết kế đã thống nhất | Công việc triển khai/kiểm chứng còn lại |
| --- | --- | --- |
| Stack ứng dụng | Python/FastAPI; `app` và `worker` riêng; module/API/polling trong backend contract | Đối chiếu contract khi triển khai; transport tiến độ không thay vòng đời tác vụ |
| Điều phối tác vụ | Một slot SQLite, claim nguyên tử, ownership khi công bố kết quả; thiết kế hủy/phục hồi trong contract | Kiểm chứng restart, tiến trình con, kết quả đến muộn và deadline; không giải phóng chỉ vì heartbeat hết hạn |
| Dữ liệu và chỉnh sửa | Nguồn notes bất biến; kiểm tra phiên bản khi ghi; sửa tên/ngày họp và speaker từng đoạn ở thế hệ hiện tại | Hiện thực schema/API và kiểm tra xung đột, khóa ngày/gán speaker, bảo toàn dữ liệu lịch sử |
| Bằng chứng trong notes | Cấu trúc trung gian có ID nguồn; một lượt sửa tối đa; snapshot; cho lưu Markdown chỉnh tay với cảnh báo link hỏng | Hoàn thiện schema/renderer, kiểm tra liên kết đúng snapshot và kiểm chứng ý nghĩa trên mẫu |
| Pipeline model | ASR/diarization local; Qwen3-ASR-1.7B là mặc định GPU, Whisper small là mặc định CPU; notes cloud chia/hợp nhất khi cần | Đo chất lượng, bộ nhớ, thời gian, token budget và tiếp tục đối chiếu cấu hình phát hành |
| Kết nối tạo notes | Direct SIWC/OMP RPC; lưu phiên; chọn provider/model, nhớ lựa chọn; không tự gửi lại inference lỗi mạng hoặc đổi provider/model | Callback Docker, hủy auth, timeout, quyền tài khoản và khả năng tắt retry của SDK/harness cần kiểm chứng |
| Nhập và xóa dữ liệu | Upload không resume; giữ âm thanh; không xóa cuộc họp đang chạy | Định dạng/giới hạn dựa trên mẫu, chuẩn âm thanh lưu, file tạm và dọn dữ liệu sau lỗi |
| Truy cập | Token/cookie theo ADR 0002; API và ranh giới quyền trong contract | Kiểm chứng bootstrap/cookie, đổi token, CSRF và quyền đọc audio/snapshot/export |
| Nghiệm thu | Các AC trong spec vẫn áp dụng | Bộ mẫu, kiểm thử failure/restart và báo cáo hiệu năng/chất lượng |

Người dùng đã chốt bộ thử nghiệm tại Q7 rồi điều chỉnh phần tạo notes tại Q12 trong [khảo sát model](model-candidates.md). Theo [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), default phần cứng hiện là Qwen3-ASR-1.7B trên GPU và Whisper small trên CPU; cấu hình này không tự khẳng định các mục tiêu nghiệm thu chất lượng/hiệu năng. Không suy ra mục tiêu xử lý bản ghi hai giờ trong 30 phút đã đạt từ tài liệu model hoặc từ thiết kế này.

## 6. Vòng 2 — các quyết định đã chốt

Người dùng đồng ý các đề xuất Q7–Q11. Với Q7, phạm vi đồng ý là bộ model thử nghiệm, không phải kết luận chất lượng/hiệu năng đã đạt:

| Câu hỏi | Quyết định | Đánh đổi hoặc nhánh phụ thuộc tiếp theo |
| --- | --- | --- |
| Q7 — Bộ model thử nghiệm | Whisper large-v3 qua faster-whisper; pyannote Community-1. Qwen3-14B Q4_K_M từng được chọn thử nhưng **đã hoãn theo Q12**; tạo notes dùng cloud | Đây là quyết định lịch sử của bộ thử nghiệm; default GPU sau đó chuyển sang Qwen3-ASR-1.7B theo #168. ASR/diarization chạy lần lượt và giải phóng GPU giữa các model |
| Q8 — Stack backend | Python cho phần ứng dụng/điều phối; FastAPI phục vụ HTTP API; worker là tiến trình riêng gọi runtime model | Dùng chung ngôn ngữ và kiểu dữ liệu; tiếp tục thiết kế API và module. [FastAPI](https://fastapi.tiangolo.com/features/) hỗ trợ OpenAPI và validation qua Pydantic |
| Q9 — Giao việc giữa các service | Trạng thái, role và quyền nhận tác vụ nằm trong SQLite; app ghi nhận một tác vụ khi rảnh, worker hoặc notes-service nhận bằng giao dịch nguyên tử và cập nhật tiến độ | Các service cùng phụ thuộc schema DB và `processing_slot`; phải thiết kế ownership, lock/heartbeat theo role, phát hiện service ngắt và giải phóng tài nguyên trước khi nhận việc mới. Không nhận tác vụ thứ hai để chờ |
| Q10 — Chạy lại khi đã có transcript được sửa | Lưu kết quả chạy lại thành một thế hệ transcript mới, giữ nguồn cũ và liên kết của các bản notes cũ | Tốn thêm dung lượng văn bản; Q19/Q21 sau đó chốt snapshot và cách xem thế hệ cũ. Không trộn segment hoặc speaker của hai lần xử lý |
| Q11 — Kiểm tra notes do model sinh | Model sinh cấu trúc trung gian có ID nguồn; backend kiểm tra cấu trúc và tham chiếu trước khi render Markdown | Chỉ kiểm tra được các điều kiện xác định, không tự bảo đảm ý nghĩa đúng; Q18 sau đó chốt một lượt sửa tự động tối đa/tác vụ. Notes do người dùng sửa vẫn là Markdown tự do |

## 7. Q12 — hai lựa chọn cloud cho bước tạo notes

**Đã chốt:** hỗ trợ cả **ChatGPT** và **OMP + Google Antigravity**. Trước mỗi lần có tạo notes, UI cho chọn provider và model, dùng phiên đã lưu hoặc kết nối bên được chọn. Q27 chốt nhớ lựa chọn gần nhất, gồm model riêng mỗi provider, nhưng hiển thị rõ để người dùng đổi; không bắt buộc đăng nhập cả hai hoặc cố định Google làm mặc định. Provider/model giữ nguyên trong tác vụ; lựa chọn cũ không còn dùng được phải được báo rõ, không tự chuyển sang model khác.

- Âm thanh, ASR, phân biệt người nói và lưu trữ cuộc họp vẫn local. Bước notes gửi transcript, ID/mốc nguồn, nhãn/tên người nói và ngày họp cần thiết tới provider đã chọn, nhận kết quả về để kiểm tra và lưu local. Không gửi bản ghi âm thanh qua đường tạo notes.
- Hoãn Qwen local và việc dựng runtime LLM local trong v1 để dành VRAM cho ASR/diarization. Training nhận diện danh tính người nói vẫn thuộc phase sau.
- Không còn yêu cầu tạo notes mới khi mất Internet. ASR/diarization, nghe lại, đọc/sửa dữ liệu đã lưu và copy/xuất vẫn hoạt động local. Q14 cho bắt đầu chép lời khi chưa có kết nối provider, kết thúc tác vụ sau khi lưu transcript và tạo notes bằng tác vụ riêng sau đó.
- Đăng nhập provider khác token/cookie truy cập ứng dụng localhost. Không thay yêu cầu token ở ADR 0002, cũng không áp quy tắc token local không hết hạn cho OAuth của provider.
- Quyết định giữ OMP + Antigravity được đưa ra sau khi đã thông báo ràng buộc của Google: điều khoản hiện hành cấm truy cập bằng phần mềm bên thứ ba và nêu khả năng đình chỉ Antigravity/Gemini CLI. Đây là lựa chọn tích hợp của dự án, không phải xác nhận được Google hỗ trợ. [Điều khoản, mục 6](https://www.antigravity.google/terms)

Lý do và hệ quả được ghi tại [ADR 0007](adr/0007-cloud-notes-providers.md). [Khảo sát subscription](subscription-notes-options.md) phân biệt quyền dùng gói với API tính phí riêng; [khảo sát harness](harness-notes.md) ghi bằng chứng từ OMP/Pi đã cài. Chưa thực hiện đăng nhập, truyền dữ liệu cuộc họp hoặc chạy thử model.

### 7.1. Hướng adapter — đã chọn để thử, chưa kiểm chứng

Worker giữ cùng luồng tạo notes và kiểm tra đầu ra, gọi adapter theo lựa chọn của tác vụ. OMP chỉ phụ trách kết nối/provider inference; SQLite, khóa nguồn, bản notes và trạng thái tác vụ vẫn do ứng dụng quản lý.

| Lựa chọn trên UI | Hướng triển khai đã chọn | Điều phải xác minh |
| --- | --- | --- |
| ChatGPT | Q16 chọn luồng Sign in with ChatGPT trực tiếp cho bản cài cá nhân local; adapter gọi Responses API bằng quyền người dùng cấp | Eligibility, model access, OAuth/callback trong Docker, refresh/revoke, structured output và quota của tài khoản |
| Google Antigravity | Python worker gọi OMP qua subprocess RPC; nối các event đăng nhập vào UI | OMP 18.5.0 có `get_login_providers`, `login`, `open_url` và phản hồi nhập code. Cần thử callback trong Docker, hủy auth và cấu hình cô lập |

Nguồn: [Sign in with ChatGPT](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt), [OMP RPC](https://omp.sh/docs/rpc). Pi cài hiện tại không được tài liệu hóa là thay thế nguyên trạng cho adapter Antigravity; không đưa cả hai harness vào v1 chỉ để gọi cùng một provider.

Đề xuất runner có cấu hình và nơi lưu credential riêng của ứng dụng, tắt tools/extensions/skills/MCP và việc tự nạp ngữ cảnh dự án. Transcript là dữ liệu đầu vào, không phải chỉ dẫn cho agent chạy lệnh. JSON event của harness chưa phải JSON notes đã hợp lệ: backend vẫn kiểm tra schema và ID nguồn theo Q11. Chọn model Gemini cụ thể sau kiểm chứng; catalog trong ảnh không chứng minh quyền gọi hoặc chất lượng từng model.

### 7.2. Vòng 3 — Q13–Q15 đã chốt

| Câu hỏi | Quyết định | Hệ quả |
| --- | --- | --- |
| Q13 — Dùng lại đăng nhập | Lưu một tài khoản mỗi provider trên bản cài, có nút ngắt kết nối; mỗi lần chạy chọn provider, chỉ đăng nhập khi chưa kết nối hoặc phiên mất hiệu lực | Không yêu cầu đăng nhập sau mỗi lần tạo notes; credential của hai provider độc lập. Q22 chặn ngắt/đổi tài khoản đã gắn với tác vụ đang hoạt động |
| Q14 — Provider chưa dùng được lúc bắt đầu | Vẫn cho ASR/diarization local, lưu transcript và kết thúc tác vụ; người dùng tạo notes sau | Không giữ slot xử lý hoặc khóa nguồn để chờ đăng nhập/quota. Báo rõ transcript hoàn tất, chưa có notes; không tự tạo notes khi provider dùng được trở lại |
| Q15 — Lỗi/quota khi tạo notes | Giữ transcript và báo lỗi; người dùng chọn provider khác rồi chủ động chạy lại | Không tự chuyển provider/tài khoản hay bật API billing. Notes cũ giữ nguyên; hủy/lỗi vẫn phải kết thúc worker trước khi mở khóa nguồn và nhận việc mới |

Phiên provider được giữ trong vùng lưu bền riêng của bản cài, không nhúng vào source/image và không trả credential cho UI. Trạng thái đã kết nối không bảo đảm quota hoặc mọi model còn dùng được; phải xử lý lỗi thật từ provider. Luồng auth chưa có transcript không được tạo một tác vụ notes chờ vô hạn; thời hạn auth và chi tiết refresh/revoke tiếp tục được thiết kế.

## 8. Vòng 4 — Q16–Q22 đã chốt

Người dùng đồng ý toàn bộ Q16–Q22. Các hành vi đã được đưa vào spec và tiêu chí nghiệm thu; schema, giao thức tác vụ và cú pháp liên kết nguồn tiếp tục được cụ thể hóa. Model ID/cấu hình phát hành và đường đăng nhập/inference vẫn cần phép thử.

| Câu hỏi | Tình huống | Quyết định |
| --- | --- | --- |
| Q16 — Đường tích hợp ChatGPT | Dùng một harness cho cả hai provider hay adapter ChatGPT riêng? | Thử direct Sign in with ChatGPT chính thức → Responses API cho nhánh ChatGPT; giữ OMP RPC cho Antigravity. Phù hợp phạm vi browser cùng máy/localhost; ứng dụng chịu trách nhiệm OAuth/refresh. Đây là chọn hướng thử, chưa khẳng định entitlement hoặc callback đã hoạt động |
| Q17 — Transcript vượt ngân sách một lượt gọi | Cuộc họp dài hơn context dành cho đầu vào hoặc đầu ra, kể cả đã trừ prompt và phần dự phòng | Chia theo các đoạn phát biểu, có phần chồng tại ranh giới; trích thông tin kèm ID nguồn rồi hợp nhất trong cùng provider/model được chọn. Kiểm soát ngân sách ở mọi bước, kể cả hợp nhất; không cắt bỏ phần cuối, không dùng auto-compaction của harness. Đổi lại có nhiều lượt gọi và tốn quota/thời gian hơn |
| Q18 — Đầu ra không hợp lệ | Model trả sai cấu trúc hoặc ID bằng chứng không tồn tại, dù lượt gọi đã kết thúc | Cho tối đa **một lượt yêu cầu sửa tự động trong cả tác vụ notes**, cùng provider/model, kèm lỗi kiểm tra và nguồn cần thiết. Nếu vẫn không hợp lệ hoặc phát sinh lỗi cần sửa khác sau khi hết ngân sách này, kết thúc lỗi; giữ transcript/notes cũ, không công bố notes dở hoặc sai schema. Giới hạn này dành cho sửa đầu ra, không phải chính sách retry mạng |
| Q19 — Nguồn của notes cũ sau khi sửa transcript | Notes đã tạo xong; người dùng sửa cùng transcript hoặc đổi tên người nói rồi mở lại bằng chứng của notes cũ | Mỗi bản notes gắn với một bản chụp bất biến của đúng transcript, ID/mốc nguồn, tên người nói và ngày họp đã dùng. Khi đối chiếu notes cũ, mở đúng nguồn đó và cho biết khác nguồn hiện tại; không tạo lịch sử cho từng phím gõ. Vẫn giữ khóa nguồn trong lúc tạo theo Q3 |
| Q20 — Hai tab cùng sửa | Tab A lưu trước, tab B đang giữ nội dung cũ rồi bấm lưu transcript/tên người nói hoặc cùng bản notes | Kiểm tra phiên bản khi ghi; từ chối ghi đè từ bản cũ, giữ nội dung chưa lưu ở UI và cho người dùng tải bản mới để đối chiếu. Không tự trộn hoặc chọn bản lưu sau đè bản trước |
| Q21 — Chọn thế hệ transcript và tên người nói sau chạy lại | Chạy lại có thế hệ transcript mới nhưng speaker ID không bảo đảm cùng nghĩa với lần trước | Khi transcript mới đã lưu đầy đủ, mặc định mở thế hệ mới; các thế hệ cũ xem lại ở chế độ chỉ đọc, notes cũ vẫn sửa/copy/xuất được. Thế hệ mới bắt đầu bằng nhãn Speaker, người dùng đặt tên lại; không tự ghép tên giữa hai lần xử lý. Nếu chạy lại chưa có transcript hoàn tất, giữ thế hệ đang có làm mặc định |
| Q22 — Ngắt kết nối provider trong lúc xử lý | Có tác vụ đang hoạt động đã chọn tài khoản/provider đó cho bước notes, kể cả chưa tới bước gọi model | Từ chối ngắt/đổi tài khoản của provider đó; hướng dẫn hủy tác vụ và chờ backend xác nhận dừng rồi ngắt. Provider không thuộc tác vụ vẫn có thể ngắt; dữ liệu cuộc họp và các bản notes giữ nguyên |

Bằng chứng cho Q16 ở [khảo sát harness](harness-notes.md): direct SIWC hỗ trợ đăng ký động cho ứng dụng local và callback `127.0.0.1`; Codex app-server là phương án khác có giao thức auth dành cho UI. Với v1 chỉ localhost, chưa có nhu cầu đưa đăng nhập từ browser trên máy LAN khác vào quyết định này.

Q17–Q18 chỉ kiểm soát cấu trúc, nguồn tham chiếu và việc bao phủ đầu vào, không bảo đảm model giữ đủ ý hoặc diễn giải đúng. Cần đánh giá riêng các quyết định bị thay đổi ở cuối cuộc họp, action item qua ranh giới chunk và lỗi ghép người nói trên bộ mẫu.

## 9. Vòng 5 — Q23–Q27 đã chốt

Rà soát repo ngày 2026-10-16 không tìm thấy bản ghi thực hoặc manifest mẫu: prototype chỉ có text, thời lượng và tên file minh họa. Prototype hiện chỉ sửa text segment và tên hiển thị speaker; title/date/language nhập lúc tạo meeting. Vì vậy các mục Q23/Q24 là mở rộng có chủ đích, chưa triển khai trong prototype. Format/codec, giới hạn file, model ID và hiệu năng là việc cần dữ liệu/thử nghiệm, không yêu cầu người dùng đoán cấu hình.

Người dùng đồng ý toàn bộ đề xuất Q23–Q27. Các quyết định đã được đưa vào spec, backend contract và tiêu chí nghiệm thu.

| Câu hỏi | Tình huống | Quyết định |
| --- | --- | --- |
| Q23 — Sửa metadata sau import | Quên hoặc nhập sai tên/ngày họp; ngày ảnh hưởng deadline tương đối | Cho sửa tên và ngày họp. Đổi ngày làm notes cần cập nhật, giữ ngày cũ trong snapshot; ngày chịu khóa nguồn khi tạo notes và kiểm tra phiên bản khi ghi. Ngôn ngữ gợi ý được chọn cho lần xử lý mới, không viết lại lịch sử của thế hệ cũ |
| Q24 — Sửa người nói của riêng một đoạn | Đoạn “I'll send it” bị gán sai speaker; đổi tên cả nhãn không sửa được lỗi riêng đoạn | Ngoài sửa text/tên hiển thị, cho gán lại speaker của từng segment và thêm nhãn khi thiếu người nói; giữ nhãn do model gán ban đầu để đối chiếu. Chỉ sửa ở generation hiện tại, tăng phiên bản nguồn và làm notes cần cập nhật. V1 giữ nguyên timestamp/ranh giới đoạn, chưa thêm tách/gộp |
| Q25 — Lỗi mạng khi gọi model | Mất kết nối sau khi đã gửi request, chưa rõ provider đã xử lý hoặc tính quota chưa | Không tự gửi lại inference khi lỗi mạng; giữ transcript/notes cũ, báo lỗi để người dùng chủ động thử lại. SDK/harness phải tuân theo chính sách này. Một lượt sửa đầu ra theo Q18 vẫn được phép; retry HTTP từ UI bằng cùng request ID và OAuth refresh không đồng nghĩa chạy thêm tác vụ notes |
| Q26 — Liên kết nguồn do người dùng sửa bị hỏng | Markdown chỉnh tay chứa ID không tồn tại hoặc không thuộc nguồn của bản notes | Vẫn cho lưu và giữ nguyên văn bản; cảnh báo, vô hiệu thao tác mở nguồn ở liên kết không hợp lệ. Không tự sửa ID hoặc dẫn sang nguồn khác; không áp quyền sửa tự do này cho đầu ra model chưa qua Q18 |
| Q27 — Chọn model trên UI | Một provider có nhiều model; người dùng muốn cân đối quota và chất lượng | Cho chọn provider + model trước mỗi lần chạy, nhớ lựa chọn gần nhất của từng provider và hiển thị rõ để đổi. Không đổi model giữa tác vụ hoặc khi model hết quota/không còn truy cập được. Danh sách lấy theo kết nối, model gợi ý và cấu hình phát hành chốt sau kiểm chứng; catalog không bảo đảm inference thành công |

Phiên thiết kế đã thống nhất Q1–Q27. [Backend contract](backend-contract.md) là bản tổng hợp kỹ thuật để triển khai; [spec](specification.md) là chuẩn nghiệm thu. Không còn câu hỏi sản phẩm đang chờ trong phạm vi backend này. Việc chọn cấu hình phát hành, các ngưỡng vận hành và định dạng đầu vào phụ thuộc kiểm chứng bằng tài khoản/mẫu thật, không phải kết quả đã đạt. Chưa triển khai backend, đăng nhập, gửi dữ liệu họp hoặc benchmark trong phiên thiết kế.

## Hồ sơ thay đổi cho v0.1.0

Xem [bằng chứng và đánh đổi theo tag](release/runtime-v0.1.0.md),
[các thay đổi notes](release/notes-v0.1.0.md) và
[chỉ mục thay đổi](architecture-changes.md). Số đo lịch sử giữ nguyên điều kiện
và revision gốc; không phải phép đo lại artifact 0.1.0.
