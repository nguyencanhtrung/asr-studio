# Backend contract v1 — bản thiết kế kỹ thuật

Tài liệu cụ thể hóa Q1–Q27 đã thống nhất trong [backend design](backend-design.md), hoàn tất tổng hợp ngày 2026-10-16. Đây là bản thiết kế trước triển khai, không phải mô tả API/database đang tồn tại. Tên bảng, endpoint và các giá trị cấu hình bên dưới là lựa chọn kỹ thuật ban đầu; các hành vi sản phẩm đã chốt là ràng buộc khi triển khai. [Spec](specification.md) là nguồn yêu cầu nghiệm thu.

## 1. Ranh giới và luồng dữ liệu

```mermaid
flowchart LR
    UI["UI localhost"] --> App["FastAPI app"]
    App <--> DB[(SQLite)]
    App <--> Media["Âm thanh local"]
    Worker["Transcription worker"] <--> DB
    Worker --> Audio["ASR và diarization local"]
    Media --> Audio
    NotesService["Notes service"] <--> DB
    NotesService --> Notes["Pipeline notes"]
    Notes --> GPT["ChatGPT: SIWC và Responses API"]
    Notes --> OMP["OMP RPC: Antigravity"]
```

| Module | Trách nhiệm và dữ liệu sở hữu |
| --- | --- |
| Meetings | Metadata, các thế hệ transcript, speaker, phiên bản chỉnh sửa, bản chụp nguồn, các bản notes; kiểm tra đích sửa và tính mới của notes |
| Recording storage | Upload tạm, xác minh đầu vào, file âm thanh bền, đường dẫn nội bộ và nghe lại; dọn file dở theo trạng thái bền |
| Processing | Nhận/claim tác vụ, slot xử lý toàn hệ thống, ownership, tiến độ, hủy, công bố kết quả và phục hồi sau restart |
| Notes | Ngân sách context, chia/trích/hợp nhất, kiểm tra schema/ID nguồn, một lượt sửa tối đa và render Markdown; gọi adapter provider theo cấu hình cố định của tác vụ |
| Access and connections | Token/cookie localhost; các kết nối provider độc lập, OAuth/refresh/logout, trạng thái tài khoản và credential riêng của bản cài |

`app` tiếp nhận thao tác UI; transcription worker và notes service thực hiện đúng role của
mình, ghi tiến độ/kết quả qua cùng lớp nghiệp vụ/lưu trữ. Không sao chép các quy tắc khóa,
phiên bản và công bố kết quả thành hai triển khai khác nhau. Giao dịch DB chỉ bao quanh thay
đổi dữ liệu ngắn, không bao quanh upload, decode, inference hoặc gọi mạng. Hai role vẫn dùng
một `processing_slot`; owner role không thay thế lifetime lock.

## 2. Mô hình lưu trữ

| Dữ liệu | Các trường/quan hệ chính và bất biến |
| --- | --- |
| `meetings` | ID, title, meeting date có thể trống, metadata version, current generation ID, trạng thái xóa; ngày họp là ngày lịch, không phải timestamp upload |
| `recordings` / `uploads` | Meeting ID, đường dẫn do backend tạo, trạng thái nhận/kiểm tra/ready, metadata âm thanh; upload chưa ready không được xử lý |
| `transcript_generations` | ID, meeting ID, ngôn ngữ gợi ý và cấu hình model của lần chạy, source version, thời điểm hoàn tất; chỉ current generation hoàn tất được sửa |
| `segments` / `speakers` | Thuộc generation, ID ổn định, thời gian đoạn, văn bản, `original_speaker_id` do model gán và `speaker_id` đang dùng, display name và dấu chưa chắc chắn. Cho thêm nhãn thủ công trong generation; không sửa original assignment hoặc ghép speaker giữa hai generation |
| `notes_sources` | ID, meeting/generation ID, phiên bản nguồn, payload bất biến chứa đầy đủ text/ID/timestamp, `speaker_id` đã dùng cho từng segment, tên người nói và ngày họp tại thời điểm tạo notes; giữ cả `original_speaker_id` để đối chiếu. Không đọc lại nội dung hoặc mapping speaker mutable để dựng bằng chứng của notes cũ. Bất biến do cơ sở dữ liệu giữ, không chỉ do quy ước: trigger `notes_sources_update_guard` (migration schema SQLite v11) từ chối mọi `UPDATE` bảng này (`notes_sources_are_immutable`), chỉ `INSERT` khi nhận tác vụ notes mới tạo ra bản chụp; khóa ngoại `RESTRICT` đã chặn xóa dòng đang được tham chiếu |
| `notes_revisions` | ID, source snapshot ID, Markdown, edit version, provider/model thực dùng, prompt/schema version, task ID và `coverage_json` (manifest độ phủ nguồn của lần tạo, xem mục 5; cột JSON hợp lệ do migration schema SQLite v9 thêm, NULL với bản notes tạo trước đó, không nằm trong payload API), `citations_json` (trích dẫn mốc thời gian của từng mục, xem mục 5; cột JSON hợp lệ do migration schema SQLite v10 thêm, chỉ thêm cột nullable nên bản v9 đang chạy được nâng cấp nguyên vẹn; NULL với bản notes tạo trước đó; có trong payload API dưới tên `citations`, danh sách rỗng khi NULL); sửa Markdown tăng edit version nhưng không đổi `citations_json`, tạo lại thêm row mới |
| `tasks` | Kind `recording` hoặc `notes`, meeting, provider (`chatgpt` hoặc `antigravity`)/model/effort/account version đã chọn khi có bước notes, source snapshot ID, trạng thái/giai đoạn/tiến độ, owner, heartbeat, yêu cầu hủy, kết quả và lỗi |
| `processing_slot` | Một row giữ active task ID hoặc trống; nhận tác vụ và chiếm slot trong cùng giao dịch để không nhận hai tác vụ từ hai tab |
| `provider_connections` | Provider ID, định danh tài khoản hiển thị, connection version và trạng thái không chứa secret; credential nằm trong vùng lưu riêng, không trả cho UI |
| `notes_preferences` | Một row giữ provider dùng gần nhất (`chatgpt`, `antigravity` hoặc trống); model gần nhất của từng provider (và effort của ChatGPT) nằm trong store riêng của provider đó. Chỉ dùng để chọn sẵn trên UI, không cập nhật cấu hình tác vụ đã nhận |

Một nguồn có thể đã được sửa nhiều lần trước khi tạo notes; không cần lưu mỗi lần gõ thành snapshot. Bản chụp được tạo khi nhận bước notes, trong cùng giao dịch xác nhận nguồn và khóa sửa. Khi công bố transcript của một lần xử lý có bước notes tiếp theo, công bố generation, chuyển current generation và chuẩn bị nguồn/khóa notes phải nhất quán, không để tab khác sửa chen vào giữa.

Phiên bản ghi gắn với đối tượng đang sửa: source version cho text, gán speaker, nhãn/tên người nói của generation; metadata version cho tên/ngày họp; edit version riêng cho từng bản notes. Sửa notes cũ vẫn nhắm notes ID cũ khi một bản mới vừa tạo xong. Notes thuộc generation cũ hiển thị là nguồn lịch sử; notes cùng generation nhưng source version hoặc ngày họp khác snapshot hiển thị cần cập nhật. Đổi riêng title không làm nguồn notes khác; không dùng mọi thay đổi metadata version làm điều kiện stale.

Đổi ngày và nhận tác vụ notes kiểm tra khóa trong cùng cơ chế giao dịch. Nếu một PATCH chứa cả title và ngày trong lúc ngày bị khóa, từ chối cả PATCH, không lưu một phần rồi báo lỗi. Gán speaker chỉ nhận nhãn cùng generation, kiểm tra source version và trạng thái current/unlocked; nhãn model gán gốc có thể trống khi model không xác định được, vẫn giữ nguyên sau sửa. Timestamp/ranh giới đoạn không phải trường được sửa trong v1. Ngôn ngữ gợi ý nằm trong cấu hình của lần xử lý mới, không cập nhật ngược generation đã tạo.

## 3. Tác vụ, hủy và phục hồi

Trạng thái điều phối: `accepted` → `running` → `succeeded`, `failed`, `canceled` hoặc `interrupted`. Khi hủy tác vụ đang chạy, chuyển qua `stopping`; chỉ chuyển terminal và nhường slot sau khi các tiến trình/request do lần chạy đó quản lý đã kết thúc. `accepted` là khoảng chuyển giao duy nhất giữa app và processing role, không phải hàng đợi nhận nhiều việc.

- Khi nhận: kiểm tra meeting chưa xóa, nguồn/version hợp lệ, slot trống và worker sẵn sàng; lưu cấu hình tác vụ, chiếm slot và khóa nguồn nếu đây là tác vụ notes. Hai yêu cầu tranh cùng slot chỉ một yêu cầu được nhận.
- Khi claim: role phù hợp nhận bằng điều kiện nguyên tử trên ID, kind, `owner_role` và trạng thái rồi gắn owner của lần khởi động. Mỗi lần ghi tiến độ/kết quả phải khớp task ID, owner và trạng thái đang cho phép ghi; kết quả đến muộn từ lần chạy cũ bị từ chối.
- Pipeline recording: kiểm tra/chuẩn bị audio → ASR/diarization → công bố transcript đầy đủ → notes nếu đã chọn chạy bước này. Các bước GPU chạy lần lượt; dữ liệu transcript còn dở không thay current generation.
- Nếu từ đầu provider chưa dùng được và chạy chép lời trước, tác vụ kết thúc với phạm vi kết quả `transcript_only`; UI ghi rõ chưa có notes. Đây không phải tác vụ notes đang chờ. Nếu đã bắt đầu bước notes nhưng nó thất bại, tác vụ báo lỗi dù transcript đã hoàn tất vẫn dùng được.
- Pipeline notes: tạo snapshot/khóa → chia/trích/hợp nhất khi cần → kiểm tra/sửa một lượt tối đa → render → lưu notes mới. Chỉ công bố notes hoàn chỉnh qua giao dịch sau khi kiểm tra lại ownership và trạng thái hủy.
- Tiến độ notes là dữ liệu đo được riêng với thời lượng audio: payload task có `progress` dạng `{current, total, unit}` khi pipeline đã có số đếm. `chunking` chỉ tăng `current` sau khi chunk được provider trả về và validation thành công; `merging` có số lượt đã hoàn tất nhưng `total` để trống vì số lượt phụ thuộc vào kích thước summary; sửa có tổng đã biết ghi số lượt đã hoàn tất; recovery có tổng chưa biết ghi số lượt bắt đầu và UI không gọi chúng là hoàn tất. `on_coverage` vẫn là manifest cuối cùng, không dùng làm đồng hồ tiến độ.
- Hoàn tất và hủy cạnh tranh qua trạng thái DB: tác vụ đã commit hoàn tất không bị đổi thành canceled; tác vụ đã nhận hủy không được công bố một kết quả đến muộn thành notes mới.
- Hủy subprocess phải thu hồi cả nhóm tiến trình con do tác vụ sở hữu. Hủy cloud chấm dứt request/client local và bỏ kết quả đến muộn; không giả định nhà cung cấp đã hoàn lại quota hay dừng toàn bộ công việc phía họ.
- Mất heartbeat không chứng minh runner/service đã dừng. Trong khi chưa xác nhận, giữ slot và báo đang kiểm tra; không chỉ hết hạn heartbeat là chạy một tác vụ GPU mới. Lifetime lock là guard overlap; sau restart/recovery đã loại bỏ runner cũ, role tương ứng đánh dấu task dở `interrupted`, mở khóa và cho người dùng chủ động chạy lại. `accepted` pre-split vẫn pending để split role phù hợp claim được.
- Khi đọc task active, `worker_state: "checking-worker"` được tính lại từ heartbeat và UI notes phải ưu tiên thông báo đang kiểm tra worker thay vì nói inference vẫn tiếp tục. Nhận notes kiểm tra idempotency trước; nếu processing slot đã có chủ, đọc lại idempotency để xử lý request giống hệt vừa được chấp nhận rồi mới từ chối nhanh `system_busy`; giao dịch sau provider lock vẫn kiểm tra lại để xử lý race.
- Đóng tab, đổi token truy cập ứng dụng hoặc polling lỗi không làm thay đổi ownership/tác vụ. Không tự xử lý lại khi worker trở lại; tác vụ notes mới từ transcript đã lưu khác với chạy lại từ đầu bản ghi.

Worker quản lý ASR/diarization runtime bằng tiến trình group riêng có deadline và đường hủy; notes service dùng cùng reaper/process-group contract cho notes runner. Notes ChatGPT dùng HTTPX read timeout 60 giây cho khoảng im lặng giữa các chunk, trong total budget mỗi lượt gọi 900 giây và deadline toàn tác vụ 3600 giây; [HTTPX mô tả read timeout là thời gian chờ một chunk dữ liệu](https://www.python-httpx.org/advanced/timeouts/), không phải giới hạn tổng thời lượng. Theo Q25, không tự gửi lại inference khi lỗi mạng, kể cả chưa nhận được kết quả; tắt các cơ chế resend tương ứng trong SDK/harness. Giữ kết quả hoàn tất và kết thúc lỗi để người dùng chủ động tạo tác vụ mới. Lượt sửa Q18 chỉ áp dụng cho đầu ra đã nhận đầy đủ nhưng không hợp lệ, không dùng để gửi lại request mất phản hồi.

## 4. API và cập nhật UI

Chọn HTTP JSON cho thao tác và polling trạng thái khi có tác vụ; chu kỳ ban đầu khoảng một giây, có điều chỉnh khi tab ẩn/mất mạng. Polling chỉ đọc DB, không gọi model để hỏi tiến độ. UI hiển thị giai đoạn, lượng audio/đoạn đã xử lý khi có số đo và trạng thái hủy. Thanh tiến độ chép lời chỉ lấy `transcript_end_seconds` chia cho `audio_duration_seconds` từ lần poll tác vụ hiện có, giới hạn ở 100 % và không xác định khi thiếu thời lượng; trong diarization/alignment/publication không tạo phần trăm hay ETA giả. Bước kiểm tra upload sau khi truyền đủ byte cũng là indeterminate và chỉ hiển thị byte đã truyền cùng thời gian kiểm tra đã đo. Danh sách bước tạo notes ánh xạ `accepted`, `starting`/`starting_notes`, `preparing_source`, `generating`/`generating_notes`, `chunking`, `merging`, `repairing`, `rendering` và `publishing`; vì backend ghi đè `stage` khi tác vụ lỗi, bị hủy hoặc gián đoạn, trình duyệt nhớ bước cuối đã thấy trong phiên để đánh dấu bước đó. Sau reconnect lấy trạng thái hiện tại, không phát lại lệnh tạo tác vụ. Có thể thay transport tiến độ sau mà không đổi vòng đời tác vụ.

| Endpoint dự kiến | Hành vi |
| --- | --- |
| `POST /api/access/session` | Đổi token local hợp lệ lấy cookie; loại token khỏi URL theo ADR 0002 |
| `GET /api/meetings` và `GET /api/meetings/{id}` | Lịch sử, lịch ngày/chưa đặt ngày, metadata và các kết quả hiện có |
| `POST /api/uploads`, `GET /api/uploads/{id}`, `PUT /api/uploads/{id}/content`, hoàn tất upload | Tạo vùng tạm bằng ID backend cấp, nhận file theo từng phần byte rồi kiểm tra; chưa hoàn tất không tạo recording ready |
| `GET /api/asr-models` | Danh mục bốn lựa chọn ASR cho một lượt chạy, kèm readiness theo worker và phần cứng hiện tại, và trạng thái cài đặt từng model |
| `POST /api/asr-models/{id}/install`, `DELETE /api/asr-models/{id}/install` | Xếp một job cài model vào service `model-installer`, hoặc xin dừng job đang chạy ([ADR 0012](adr/0012-model-installer-network-boundary.md)) |
| `POST /api/meetings` và `PATCH /api/meetings/{id}` | Tạo meeting từ upload hợp lệ; sửa title/ngày với expected metadata version. Đổi ngày phải kiểm tra khóa nguồn và làm notes cần cập nhật |
| `GET /api/meetings/{id}/audio` | Phát/tua audio được cấp quyền, không lộ đường dẫn filesystem |
| `GET /api/meetings/{id}/generations` và `GET /api/generations/{id}` | Chọn current generation hoặc đọc lịch sử; trạng thái readonly do backend trả và kiểm tra khi ghi |
| `PATCH /api/generations/{id}/segments/{segment_id}` | Sửa text hoặc speaker đang gán với expected source version; chỉ nhận nhãn cùng generation, không sửa timestamp/ranh giới hoặc nhãn model gán gốc |
| `POST /api/generations/{id}/speakers` và `PATCH /api/generations/{id}/speakers/{speaker_id}` | Thêm nhãn bị thiếu hoặc sửa/xóa display name; cùng kiểm tra source version, current generation và khóa nguồn |
| `POST /api/meetings/{id}/notes-runs` | Body: `request_id`, `expected_generation_id`, `expected_source_version`, `expected_metadata_version`, tùy chọn `output_language` (`en`/`vi`), `provider` (`chatgpt` mặc định, hoặc `antigravity`), `expected_model` (chuỗi 1–200 ký tự) và `expected_reasoning_effort` (chuỗi; ChatGPT theo model, Antigravity nhận `high`/`medium`/`default`, `default` được coi là `high` trước khi so sánh) là lựa chọn UI đã hiển thị. Chỉ ghim trạng thái của provider được chọn: connection version, model đã lưu và effort (Antigravity: `high` hoặc `medium` đã lưu; giá trị cũ `default` đọc là `high`); không đọc store của provider kia. Khi có `expected_model`/`expected_reasoning_effort` khác giá trị đã lưu được ghim thì từ chối `notes_preference_changed` 409 (model đã lưu đã đổi; tải lại và xác nhận lại), không tạo tác vụ, snapshot hay chiếm slot; vắng field thì giữ hành vi cũ. Lỗi: `invalid_provider`, `invalid_expected_model`, `invalid_reasoning_effort` 400; `provider_disconnected`, `reconnect_required`, `notes_model_unselected`, `notes_preference_changed`, `login_in_progress` 409 (không bao giờ tự chọn model, provider hoặc tài khoản khác). `login_in_progress`: đang có phiên đăng nhập Antigravity chờ hoàn tất; hoàn tất hoặc hủy trước khi tạo notes. `request_hash` chỉ thêm provider khi khác `chatgpt`, và chỉ thêm `expected_model`/`expected_reasoning_effort` khi có, nên request cũ vẫn idempotent. Tóm tắt tác vụ notes có thêm `provider`, `model`, `reasoning_effort` đã ghim |
| `GET /api/notes/{id}`, `PATCH /api/notes/{id}`, `GET /api/notes/{id}/export` | Đọc/sửa/xuất đúng bản notes; PATCH có expected edit version; export/copy lấy cùng Markdown. Payload bản notes (cả danh sách trong `GET /api/meetings/{id}/notes`) có thêm `citations`: danh sách `{section, text, sources: [{id, start_seconds}]}`, rỗng với bản tạo trước khi có trích dẫn; PATCH không đổi nó. Payload còn có `warnings`: danh sách `{code: "source_link_invalid", reason, href}`, mỗi phần tử là một link Markdown có `notes_source=` trong địa chỉ mà không mở được bản chụp của chính bản notes; `href` đúng như đã gõ, `reason` là `unknown_segment` (đúng cuộc họp và bản chụp nhưng ID đoạn không có trong đó) hoặc `other_source` (mọi địa chỉ khác: cuộc họp hoặc bản chụp khác, thiếu đoạn, sai dạng). Link hợp lệ đúng là `/meetings/{meeting}?notes_source={bản chụp của bản notes}&segment={ID đoạn của bản chụp}`, thêm `#transcript` tùy chọn. Cảnh báo tính lại ở mỗi lần đọc từ Markdown đang lưu và bản chụp, không có cột lưu, và là `[]` khi không có link hỏng. Bản đã sửa tay (`edit_version` > 1) được `GET`, `PATCH` và export trả lại đúng nguyên văn đã lưu; chỉ bản chưa từng sửa mới bị bỏ link timestamp do phiên bản cũ sinh |
| `GET /api/notes-sources/{id}` | Lấy bản chụp nguồn chỉ đọc để đối chiếu timestamp; mọi dữ liệu vẫn cần xác thực ứng dụng |
| `POST /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel` | Nhận một tác vụ (có thể kèm `asr_model` cho riêng lần chạy), đọc trạng thái hoặc yêu cầu hủy; không trả thành công trước khi ghi bền |
| `GET /api/providers`, `GET /api/providers/{id}/models`, tạo/đọc/hủy login session, `DELETE /api/providers/{id}/connection` | UI kết nối và chọn model đúng provider; không trả OAuth credential; ngắt/đổi account phải kiểm tra Q22 trong backend |
| `GET /api/notes-preferences` và `PUT /api/notes-preferences` | `GET` trả `{provider: dùng gần nhất hoặc null, models: {chatgpt?, antigravity?}, reasoning_effort: effort ChatGPT, reasoning_efforts: {chatgpt, antigravity}}` (`reasoning_effort` giữ nguyên cho client cũ). `PUT {provider, model, reasoning_effort?}` chỉ kiểm tra tĩnh (không mạng, không inference, không chặn bởi tác vụ đang chạy): ChatGPT kiểm tra độ dài model và effort theo model; Antigravity kiểm tra ID an toàn và chỉ nhận effort `high`/`medium` (`default` = `high`), giá trị khác trả `invalid_reasoning_effort`. Lỗi: `invalid_provider`, `invalid_preference`, `invalid_model`, `invalid_reasoning_effort` (400). `PUT /api/providers/antigravity/preference {model, reasoning_effort?}` lưu cùng model (vắng field thì giữ mức đã lưu) và trả `reasoning_effort`; `GET /api/providers/antigravity/models` và `GET /api/providers` (mục Antigravity) trả mức đã lưu. Mỗi lần gọi OMP của tác vụ gửi `set_thinking_level` sau `set_model` rồi đọc lại bằng `get_state`; không khớp thì dừng trước khi gửi prompt với `thinking_level_unavailable` 422. Lỗi OMP có `httpStatus` 404 hoặc "Requested entity was not found" (kiểm tra sau auth/quota) được báo là `model_unavailable` 422, không phải `provider_unavailable`. Model về sau không còn dùng được được báo khi chạy (`model_unavailable`), không bị thay thế. Không đổi tác vụ đã nhận |
| `DELETE /api/meetings/{id}` | Từ chối meeting có tác vụ; đánh dấu xóa và dọn dữ liệu đúng phạm vi một cách có thể tiếp tục sau restart |

### Giao thức upload theo phần byte

`POST /api/uploads` nhận `{filename, size_bytes}`. Response `201` trả `id`, các URL của upload,
`size_bytes`, `received_bytes: 0` và `status: "receiving"`.

`GET /api/uploads/{id}` trả cùng tóm tắt upload và luôn trả `received_bytes`. Đây là số byte
đã ghi và `fsync` thành công trước khi server cập nhật DB. Client dùng số này để tiếp tục.

`PUT /api/uploads/{id}/content?offset=N` nhận một phần liên tiếp của file với content type
`application/octet-stream`. Nếu thiếu `offset`, server dùng `0`. Client phải gửi tuần tự với
`N = received_bytes` hiện tại. Mỗi yêu cầu chỉ có một writer. Server cắt file tạm về offset đã
commit trước khi ghi phần mới. Server chỉ cập nhật `received_bytes` sau khi ghi xong và gọi
`fsync`.

| Tình huống | Response và bất biến |
| --- | --- |
| Phần ghi thành công nhưng chưa đủ file | `200`, `status: "receiving"`, `received_bytes` tăng theo số byte đã commit |
| Phần cuối ghi đủ `size_bytes` | `200`, `status: "uploaded"`, `received_bytes == size_bytes` |
| `offset` khác `received_bytes` | `409`, `detail.code: "offset_mismatch"`, detail có `received_bytes`; upload vẫn `receiving` |
| Một yêu cầu bị ngắt hoặc hết thời gian chờ | Server drain xong thao tác ghi/fsync/commit đang sở hữu, giữ `receiving`, bỏ byte chưa commit, rồi nhả writer; client đọc lại `GET` rồi tiếp tục |
| Hai yêu cầu content cùng lúc | `409`, `detail.code: "upload_busy"`, detail có `received_bytes`; chỉ writer đầu tiên được ghi |

Client không gọi endpoint hoàn tất trước khi `received_bytes == size_bytes`. Sau đó,
`POST /api/uploads/{id}/complete` chuyển upload sang `validating` trước khi kiểm tra media; khi
thành công nó chuyển sang `ready`. Nếu phản hồi POST bị mất, timeout hoặc là lỗi 5xx/408/429,
đó là kết quả chưa biết: client không được DELETE upload mà phải đọc `GET /api/uploads/{id}`.
Trạng thái `ready` tiếp tục tạo meeting, `validating` được poll mỗi giây không có deadline tổng
ngắn, `uploaded` chỉ được POST complete lại khi client biết chắc trạng thái đó, còn
`failed`/`receiving` hoặc trạng thái khác phải đi qua cleanup có chủ đích. GET trạng thái tạm thời
được retry tối đa sáu lần với backoff 1, 2, 4, 8, 16 và 32 giây; nếu vẫn không xác nhận được,
client giữ `upload_id`, cho phép kiểm tra lại hoặc hủy, và không tạo meeting.

`GET /api/uploads/{id}` trả `failure_code` khi trạng thái là `failed`. Lỗi media có mã riêng:
`duration_limit` (413, detail có `max_duration_seconds`) khác với giới hạn byte, còn
`duration_mismatch` (422) báo duration khai báo của audio không khớp đáng kể với audio giải mã
được. Với A/V, duration của container chỉ là provisional khi stream audio được chọn không có
duration riêng; server vẫn giải mã đúng audio stream với giới hạn `max + 1` rồi kiểm tra duration
đầu ra. Chỉ duration của selected audio có tính authoritative mới được so sánh với duration giải
mã; audio-only có thể dùng duration container làm fallback authoritative. Riêng định dạng
`mp3`, `aac`/ADTS được nhận diện bằng `format_name` của ffprobe, duration có thể là ước lượng
bitrate nên luôn là provisional, kể cả khi stream có trường duration. Không dùng ước lượng này
để từ chối sớm hoặc kết luận file bị cắt. Server vẫn decode tối đa `max + 1` và áp giới hạn lên
audio đầu ra thực; đuôi tên file không quyết định độ tin cậy của duration.

`GET /api/uploads/policy` trả thêm `chunk_bytes`. Giá trị mặc định là `67108864` byte (64 MiB).
Client dùng giá trị này làm kích thước phần, nhưng mỗi request vẫn phải dùng offset server đã xác
nhận. Sau lỗi mạng hoặc phản hồi `upload_busy`/`upload_interrupted`, client đọc lại trạng thái và
thử lại phần hiện tại tối đa sáu lần với cùng backoff; lỗi tạm thời của GET trạng thái dùng cùng
lịch và giới hạn.

`GET /api/asr-models` yêu cầu session đã xác thực và trả `{asr_models: [...]}`. Mỗi phần tử có
`id`, `model_name`, `ready`, `reason` và `prepare_command`; `id` là `auto`, `whisper-large-v3`,
`pho-whisper-large` hoặc `qwen3-asr-1.7b`. `auto` vẫn có trong danh sách khi worker chưa sẵn
sàng, nhưng readiness khi đó là `false`; client vẫn cho chọn để giữ hành vi tự chọn hiện tại.
Backend chỉ đọc runtime profile khi worker khỏe, cùng quy tắc với dữ liệu runtime trong
`GET /api/meetings/{id}`. Lỗi model catalog không làm hỏng việc tạo meeting.

Khi không có model mặc định do người vận hành chọn, `auto` dùng `qwen3-asr-1.7b` với `bfloat16`
trên GPU và `whisper-small` với `int8` trên CPU. Một lựa chọn explicit vẫn dùng đúng engine của
model đó; đường dẫn Whisper large-v3 cũ không bị diễn giải thành Qwen. Task đã lưu `asr_model: auto`
vẫn được chạy theo đường dẫn model đã lưu.

Phản hồi của endpoint Cài đặt thêm, ngoài các trường trên: ở mỗi model khác `auto`,
`install` (`{state}` với `state` là `idle`, `queued`, `running`, `cancelling` hoặc `failed`; khi
`running` có `phase` là `downloading`, `converting` hoặc `verifying` cùng `bytes_done`/`bytes_total`
đã đo ở thư mục tạm; khi `failed` có `error` và, với `insufficient_disk`, `required_bytes` và
`free_bytes`), `required_bytes` (dung lượng trống cần có) và `free_bytes` (còn trống ở thư mục
model, hoặc `null`); ở cấp ngoài, `installer`: `{available, models_writable}`. `available` là
`false` khi nhịp tim của `model-installer` cũ quá 10 giây hoặc chưa có; giao diện khi đó giữ lệnh
`prepare_command`. `GET /api/meetings/{id}` không có các trường này.

`POST /api/asr-models/{id}/install` (cùng yêu cầu session và same-origin với các route ghi khác)
trả 202 `{install: {...}}`; gọi lại khi job đang chờ hoặc chạy trả cùng một job, không tạo thêm.
`DELETE` cùng đường dẫn trả 202 và đặt `cancelling`; job dừng và dọn thư mục tạm. Từ chối, mã
trong `detail.code`: `unknown_model` 404 (gồm `auto`), `model_ready` 409 (không bao giờ ghi đè
model đã chuẩn bị), `gpu_required` 409 (từ chối trước khi tải bất cứ gì), `worker_unavailable`
409, `models_not_writable` 409, `installer_unavailable` 503, và với `DELETE` `install_not_active`
409. Mã lỗi của job: `insufficient_disk`, `network`, `disk_full`, `integrity`, `target_not_empty`,
`models_not_writable`, `interrupted`; thử lại là gọi `POST` lần nữa.

Client disconnect trong lúc ghi là một kết quả dự kiến: server drain thao tác ghi/fsync/commit
đang sở hữu, trả 204 khi xử lý trực tiếp và giữ offset bền vững; cancellation thật sự vẫn được
propagate để task owner không bị nuốt.

Cleanup/startup chạy backfill hữu hạn (100 dòng mỗi lượt) để đổi filename của các upload `failed`
cũ sang `removed-upload`, nhưng giữ nguyên `failure_code`. Filename của upload `ready` hoặc
`consumed` không bị đổi; upload `ready` chưa gắn meeting tiếp tục nằm trong retention 24 giờ theo
thiết kế. `meeting_date` chỉ nhận năm từ 1000 đến 9999.

Mỗi yêu cầu tạo tác vụ mang request ID để retry HTTP do mất phản hồi trả lại cùng tác vụ đã nhận, không tạo thêm lần gọi model. Cùng request ID nhưng payload khác phải bị từ chối; retry không được trở thành tác vụ mới chỉ vì tác vụ cũ đã kết thúc. Đây khác tự gửi lại inference bị Q25 cấm. Ghi từ phiên bản cũ hoặc trạng thái đang khóa trả lỗi có mã phân biệt (`stale_version`, `notes_source_locked`, `system_busy`, `provider_in_use`); UI giữ bản nháp khi xung đột. Lỗi provider cần đăng nhập không được diễn giải thành mất cookie đăng nhập ứng dụng.

Cookie/token tiếp tục theo [ADR 0002](adr/0002-localhost-reusable-token.md). Các thao tác thay đổi trạng thái cần kiểm soát nguồn request và phiên hợp lệ; callback OAuth xác minh state/PKCE theo provider. Các endpoint nghe audio, snapshot và export không phải đường vòng bỏ xác thực.

## 5. Notes và tham chiếu nguồn

Schema trung gian có phiên bản, giữ sáu nhóm nội dung trong spec. Quyết định và action item phải có ít nhất một ID nguồn hợp lệ trong snapshot; người phụ trách/thời hạn cho phép chưa xác định. Thời hạn giữ cả cách nói gốc và ngày quy đổi khi đủ điều kiện. Model không tự sinh URL, đường dẫn audio hay timestamp dùng để seek: renderer dựng chúng từ ID đã kiểm tra.

Ngân sách mỗi lượt gọi gồm prompt, nguồn, phần hợp nhất khi có, phần đầu ra và khoảng dự phòng. Mỗi phần (chunk) sở hữu các segment liên tiếp của riêng nó theo thứ tự snapshot; mọi phần sau còn mở đầu bằng phần chồng: các segment nguyên vẹn ở cuối phần trước, đánh dấu `overlap`, vẫn nằm trong tập ID được trích dẫn và tổng cộng tối đa `NOTES_OVERLAP_BYTES` (2.000 byte của các mục gửi đi). Phần chồng lấy lùi từ cuối phần trước và dừng ở mảnh của segment bị cắt hoặc khi vượt mức nên có thể rỗng; segment đầu tiên của phần mới được ưu tiên chỗ và phần chồng cũ nhất nhường nó. Phần chồng chiếm ngân sách của phần sau nên cùng giới hạn 24 phần chứa được ít nguồn hơn so với không chồng. Các ID nguồn gốc tồn tại xuyên các bước.

Kế hoạch chia nguồn dựng manifest độ phủ và tự kiểm tra (`verify_coverage`); pipeline kiểm tra lại trước lượt gọi provider đầu tiên, nên kế hoạch không đạt dừng khi chưa có lượt gọi nào. Việc kiểm tra yêu cầu manifest khớp bản dựng lại từ các phần; các phần sở hữu nối tiếp nhau (hai phần liền nhau chỉ chung vị trí của một segment bị cắt) và phủ mọi segment của snapshot theo thứ tự; segment nguyên vẹn khớp bản chụp; mỗi mảnh giữ ID, thời gian và người nói của segment gốc, văn bản khớp khoảng byte đã ghi, hai mảnh liền nhau nối tiếp hoặc chồng nhau không hở, mảnh cuối kết thúc ở byte cuối; phần chồng đúng bằng các segment cuối của phần trước. Manifest là JSON (`version` 1) gồm `segment_count`, `provider_calls` và `output_recoveries` (số lượt gọi provider và số lượt phục hồi đầu ra vượt trần của cả tác vụ) và, mỗi phần, `index`, `owned` và `overlap` (khoảng nửa mở `[a, b)` vị trí segment trong snapshot; `overlap` là null khi không có), `parts` (mỗi mảnh có `position`, `part`, `count` và `bytes`, khoảng byte UTF-8 trong văn bản segment) và `provider_call_index` (thứ tự lượt gọi provider có phản hồi được chấp nhận cho phần đó, tức lượt sửa hoặc lượt phục hồi nếu phần đó đã cần). Nguồn vừa một lượt cũng có manifest với một phần `[0, N)`. Manifest chỉ được báo cho runner sau khi tài liệu cuối hợp lệ nên lần chạy lỗi không để lại manifest, và chỉ chứng minh độ phủ của bước trích xuất; các bước hợp nhất và rút gọn không được ghi lại.

Tóm tắt trung gian có trần đầu ra suy từ ngân sách prompt để hai bản ở mức trần vẫn vừa một prompt hợp nhất (kiểm thử với mỗi bản trích 100 ID nguồn): mỗi bản tối đa một phần ba `MAX_PROMPT_BYTES` làm tròn xuống trăm byte (`MAX_REDUCTION_OUTPUT_BYTES` = 10.700 byte, prompt nhắm tới `MAX_REDUCTION_TARGET_BYTES` = 8.000 byte), phần ba còn lại dành cho chỉ dẫn, bảng ánh xạ ID và schema. Hợp nhất đi theo tầng: ngay khi mọi tóm tắt cùng vừa một prompt thì chạy lượt cuối; nếu chưa, ghép các cặp liền kề theo thứ tự nguồn. Cặp vừa prompt thì hợp nhất; cặp không vừa thì tóm tắt lớn hơn được rút gọn riêng bằng một lượt gọi (kết quả phải nhỏ hơn bản gốc) còn bản kia sang tầng sau, và bản lẻ cuối cùng cũng sang tầng sau nguyên vẹn. Không tóm tắt nào bị bỏ hoặc cắt. Tác vụ lỗi `notes_synthesis_limit_exceeded` khi hết `MAX_REDUCTION_CALLS` (24, tính cả lượt rút gọn và lượt cuối), `MAX_REDUCTION_LEVELS` (8) hoặc `MAX_PROVIDER_CALLS` (53, gồm tối đa 4 lượt phục hồi đầu ra), khi một lượt rút gọn không làm bản tóm tắt nhỏ đi, hoặc khi một tóm tắt không vừa prompt kể cả đứng một mình (ví dụ trích dẫn quá 999 ID nguồn khác nhau). Bộ kiểm thử cần trường hợp quyết định bị đổi ở cuối cuộc họp và action item qua ranh giới đoạn.

Nếu riêng một segment không vừa một lượt gọi, văn bản của nó được cắt thành các mảnh có thứ tự để gửi model: lấy đoạn dài nhất còn vừa ngân sách thật của một phần (tìm nhị phân, tính cả dấu mảnh rộng nhất), lùi về khoảng trắng cuối cùng ở nửa sau của đoạn, hoặc cắt tại ranh giới code point (không bao giờ giữa một chuỗi byte UTF-8) khi không có khoảng trắng. Mảnh sau bắt đầu sớm tối đa 300 ký tự so với chỗ mảnh trước kết thúc (`_PART_OVERLAP_CHARS`, dịch tới đầu từ khi có thể) để câu bị cắt nằm trọn trong một mảnh. Mỗi mảnh được gửi với ID (đã ánh xạ), `start_seconds`, `end_seconds` và người nói của segment gốc cùng dấu `"part": [i, n]`; khoảng byte chỉ nằm trong manifest, không nằm trong prompt. Bản chụp nguồn và transcript đã lưu không đổi, không có timestamp theo từ, và một mảnh không bao giờ làm phần chồng. Nguồn vượt `MAX_TOTAL_SOURCE_BYTES` (1.000.000 byte) hoặc cần hơn `MAX_NOTES_CHUNKS` (24) phần, kể cả khi một segment cần hơn 24 mảnh, vẫn bị từ chối khi nhận tác vụ (`notes_source_too_large` 413) mà không tạo tác vụ, snapshot hay chiếm slot.

Dữ liệu nằm trong phần chồng có thể được đọc nhiều lần, nên hợp nhất phải xử lý mục trùng và không biến phát biểu lặp thành nhiều action item độc lập. Prompt trích xuất cho trích dẫn segment `overlap` nhưng không cho tạo ý chỉ dựa trên chúng. Trước khi hợp nhất, một mục của phần k bị bỏ khi bản thô của phần k−1 có mục cùng nhóm nội dung với đúng cùng tập `source_ids` không rỗng và mọi ID đó thuộc phần chồng giữa hai phần; so khớp theo tập ID, không theo lời lẽ, và phần k−1 đã đọc các segment ấy đầy đủ nên mục của nó được giữ. Mục không có `source_ids` không bao giờ bị bỏ; mục diễn đạt lại với tập ID khác được giữ cho bộ hợp nhất, mà prompt (cả hai giai đoạn, hai ngôn ngữ) yêu cầu gộp mục lặp và không ghi một phát biểu thành hai quyết định hoặc action item.

Bộ đếm sửa đầu ra nằm ở tác vụ notes và dùng chung cho toàn bộ các lượt trích/hợp nhất/rút gọn. Không nhờ SDK/harness tự thử lại thêm ngoài chính sách đã chọn. Mọi chunk thành công chưa tạo ra một bản notes người dùng có thể nhầm là kết quả đầy đủ.

Một lượt gọi có đầu ra vượt trần không dừng tác vụ ngay: ChatGPT báo `provider_output_too_large`, còn Antigravity (OMP không áp trần phản hồi) trả tài liệu hợp lệ nhưng quá lớn nên bị kiểm tra kích thước (`size`) từ chối. Pipeline gửi lại đúng prompt gốc, không kèm đầu ra hỏng, cộng một câu nêu mục tiêu độ dài thấp hơn, bằng tiếng Anh hoặc tiếng Việt theo ngôn ngữ đầu ra, tối đa `MAX_OUTPUT_RECOVERY_ATTEMPTS` (2) lần mỗi yêu cầu và `MAX_OUTPUT_RECOVERY_CALLS` (4) lần mỗi tác vụ, gọi là lượt `recovery`. Mục tiêu đầu là `MAX_REDUCTION_TARGET_BYTES` cho bước rút gọn trung gian, còn lại là trần của bước đó (`MAX_CHUNK_OUTPUT_BYTES` hoặc `MAX_OUTPUT_BYTES`); mỗi lượt sau nhắm 3/4 lượt trước, làm tròn xuống trăm byte. Trần phản hồi không đổi và prompt phục hồi vẫn qua kiểm tra `MAX_PROMPT_BYTES`; prompt không vừa thì không được gửi. Prompt gốc giữ nguyên từng byte nên `PROMPT_VERSION` không đổi. Hết lượt phục hồi mà vẫn vượt, tác vụ lỗi như trước và không công bố gì: ChatGPT trả `provider_output_too_large` với chẩn đoán của lượt cuối (`stage`, `provider_call_index`, `output_bytes`, `response_limit`, `attempt` = `recovery`), Antigravity trả `notes_invalid_output`. Đầu ra của lượt phục hồi hỏng vì lý do khác vẫn dùng lượt sửa duy nhất của tác vụ; lỗi `provider_unavailable` và các lỗi khác dừng ngay.

Notes service/runner trả kết quả bằng event `result` gồm Markdown và manifest. Worker/notes service coi kết quả thiếu hoặc rỗng manifest là lỗi giao thức (`notes_runner_protocol_error`) và không công bố gì. Khi công bố, `publish_notes_revision` kiểm tra manifest (`chunks` không rỗng, mỗi `provider_call_index` là số nguyên ≥ 1, JSON ASCII tối đa 65.536 byte; không đạt thì `invalid_coverage` 400 hoặc `coverage_too_large` 413 và tác vụ lỗi `notes_publication_failed`) rồi lưu vào `notes_revisions.coverage_json` trong cùng giao dịch với bản notes. `PROMPT_VERSION` là `notes-hierarchical-v41` (nguồn ChatGPT/Gemini đã đăng ký ngân sách > 1 giờ: map theo phần 900 giây và lượt đối chiếu có cấu trúc thay final tự do, xem `docs/architecture-changes.md` mục #79/#181 và #183; reconcile là overlay thưa `ledger_version` 2: ref không nêu được mã giữ nguyên; lỗi theo từng ref được gom và sửa bằng một lượt repair thưa; sau call cuối, mã thêm trích dẫn ngữ cảnh tiền đề cục bộ, manifest `source_context`) vì prompt mọi bước yêu cầu văn phong telegraph, mỗi thông tin chỉ ở một nhóm, overview tối đa 3 mục, key_topics không lặp nhóm riêng, và kế hoạch/lịch/ngày dự kiến đã thống nhất vào decisions. Với Gemini đã đăng ký, prompt nguồn yêu cầu số/tên có trong segment được trích và quyết định đổi kế hoạch/ngày/hạn nêu cũ → mới, tách khỏi công việc phát sinh; adapter đếm đúng schema đối chiếu trong giới hạn request. Riêng Gemini đã đăng ký, reconcile kiểm tên nghiêm ngặt (tên một từ hoặc không có trong transcript phải khớp đúng chính tả segment được trích, nếu không ref lỗi và đi vào repair dùng chung) và liên kết ngữ cảnh thêm số hồ sơ ngắn có nhãn cùng câu kết quả ngay sau kiến nghị được trích, tiêu đề hồ sơ và khối biểu quyết kế tiếp có đúng tên người (cửa sổ có cấu trúc 32 segment/360 giây, dừng khi đổi mục nghị sự; vẫn tối đa 12 ID thêm/mục, không thay trích dẫn cũ; xem mục #183); nhánh này cho phép quote lỗi định dạng gắn lại vào toàn bộ segment đã khai báo/đã trích trong giới hạn độ dài, chỉ khi bắt buộc bước kiểm ngữ nghĩa; từ v35, reconcile của nhánh này chia sổ thành các view liên tục cân bằng chạy song song (từ v40 tối đa bốn, `min(4, N)`; v35-v39 tối đa hai): mỗi view có toàn nguồn, chỉ sở hữu ref của mình, mục của mọi view khác là ngữ cảnh chỉ đọc không có ref theo thứ tự sổ, chỉ view đầu được thêm mục; mọi view phải hợp lệ rồi mới gộp một lần; từ v41, lỗi đầu ra primary của các view được gom vào một request repair dùng chung, giữ riêng ledger, ref, nguồn hiển thị và schema từng view; trần phản hồi áp dụng cho toàn gói, không nhân theo số view; đã dùng repair hoặc còn lỗi sau sửa thì toàn tác vụ lỗi; mọi view tính vào trần reduction và trần call toàn cục; manifest `source_audit` giữ `mode` `reconcile` và chỉ số cuối `provider_call_index`, thêm `part_count`, `provider_call_indices` theo thứ tự view; từ v41, chỉ số repair có thể lặp trong danh sách này khi một request sửa nhiều view, còn `provider_calls` vẫn đếm request thực tế; sau reconcile có một lượt `semantic_audit` dạng bản vá tham chiếu bất biến (`audit_version` 2), chia tối đa tám tập mục liên tục cân bằng không chồng lặp (`min(8, N)`, từ v38; v36-v37 là `min(4, N)`) và chạy song song; mỗi tập chỉ thấy nguồn hiển thị (segment mục của tập trích dẫn, ứng viên liên kết strict và một segment kề mỗi phía trong cùng mục nghị sự, ≤ 120 giây; `source_scope` nêu vị trí gốc và khoảng bị lược) và chỉ ref của mình; ID không hiển thị bị từ chối và đi vào repair dùng chung, kiểm tra tất định vẫn dùng toàn nguồn; phản hồi bắt buộc có `verified` gồm đủ mọi ref sở hữu với giá trị boolean (`true` là giữ nguyên đã kiểm, `false` khi ref có đúng một thay đổi hoặc nằm trong `unresolved_refs`), thiếu/thừa/sai kiểu/mâu thuẫn làm tập lỗi; mọi tập phải hợp lệ trước khi hợp và áp bản vá một lần, bản gộp vẫn trong 64.000 byte; biến thể do mã sinh có thể thay một cụm vai trò phụ ở cuối câu bằng nhãn `[responsibility unclear]`/`[vai trò chưa rõ]`, giữ nguyên lõi định lượng, số, ngày, phủ định và điều kiện, manifest đếm `responsibility_unclear`; từ v39, quyết định có lõi hồ sơ-kết quả ứng viên (đúng chữ gốc: một hồ sơ chính, kết quả rõ, biểu quyết nêu trực tiếp; mã chỉ đề xuất, không chứng minh) có thêm ref `.c` chỉ do model chọn bằng một thay đổi hợp lệ; ref unresolved luôn thành trích đoạn cần kiểm tra, mã không thay bằng lõi; manifest đếm `core_selected`, `core_preserved` luôn 0 (đã loại bỏ), và mục thành tích ghép công việc người nói tự nhận ngôi thứ nhất được chuyển sang trích đoạn cần kiểm tra; từ v39, mỗi view reconcile của nhánh này còn có gợi ý tư vấn `attention.unstated_clauses` lấy từ chính segment mục trích (≤ 2/mục, ≤ 48 và ≤ 16.384 byte/view, trích ≤ 200 ký tự); từ v40 (chưa đo thực tế) phản hồi bắt buộc có `cue_checked` gồm đúng mọi ref sở hữu có gợi ý với giá trị boolean (`true` khi update của ref bổ sung ít nhất một gợi ý quan trọng, bắt buộc có update; `false` cho phép mọi disposition), thiếu/thừa/sai kiểu/`true` không có update làm ref lỗi và đi vào repair dùng chung; repair thưa chỉ quyết định ref lỗi có gợi ý (thấy gợi ý và nguồn của chính chúng), giữ quyết định đã chấp nhận của ref khác, rồi kiểm lại toàn bộ map; manifest `source_audit.counts.cue_checked` đếm `checked`/`accepted`/`declined`, chỉ là độ phủ quyết định của model, không chứng minh đúng; từ v40, map fact inventory Gemini đã đăng ký chạy tối đa 5 worker song song (trước 3), ChatGPT giữ 3 và map khác vẫn 2; prompt map fact inventory của nhánh này còn yêu cầu giữ bước tiếp theo cùng điều kiện/phê duyệt sau và gắn vai trò từng bên với đúng đối tượng; giữ một repair dùng chung và các giới hạn hiện hành (chọn văn bản gốc hoặc biến thể ẩn danh do mã sinh, chỉ nối thêm trích dẫn, xóa owner/deadline; biến thể chi tiết đánh dấu riêng mã điều khoản phụ không chắc chắn bằng nhãn theo ngôn ngữ, giữ số hồ sơ/kết quả/biểu quyết/ngày; số lượng có dấu hiệu ASR ghép danh sách kích cỡ được đánh dấu chưa rõ nếu chính các segment trích dẫn không có tổng số độc lập, không cho chọn lại số cũ; manifest đếm `reference_unclear` và `quantity_unclear`; ref chưa giải quyết chuyển sang confirmation bằng các segment nguồn nguyên văn có nhãn cần kiểm tra, bỏ câu khẳng định và metadata chưa xác minh; ref sai/lặp/xung đột hoặc vượt giới hạn vẫn làm tác vụ lỗi; manifest `semantic_audit` có `part_count`, `provider_call_indices` và chỉ số cuối `provider_call_index`, xem mục #183); Sau bản vá, mã giữ mức xấp xỉ của số lượng theo đúng đơn vị và giá trị trong nguồn của mục; số chính xác khác không làm căn cứ thay thế. Ý định bỏ phiếu gắn tên nhưng thiếu lời giao lượt/xác nhận cục bộ đã trích hoặc câu báo cáo trực tiếp về đúng người được chuyển sang trích đoạn cần kiểm tra. Người nói được quy tên (mọi thì, hoặc trước danh từ phát biểu; từ v38 gồm cả compare/affirm) có chức danh, được nguồn coi là người, hoặc (từ v38) không xuất hiện đúng nguyên tên ở bất kỳ đâu trong toàn nguồn (so khớp cả từ, không phân biệt hoa thường, không nhận biến thể ASR) cũng cần tự giới thiệu, câu báo cáo trực tiếp, lời giao lượt hay lời cảm ơn trực tiếp cục bộ trong nguồn của mục; nếu không, mã bỏ phần quy tên đầu câu bằng hậu tố nguyên văn, không có hậu tố thì chuyển sang trích đoạn cần kiểm tra. Liên kết strict bổ sung câu tiếp nối bị cắt và lời giao lượt lân cận trong cửa sổ hiện hành (tối đa 4 segment/120 giây phía sau), giữ trần 12 ID thêm/mục; liên kết không chứng minh danh tính. Strict còn nối tên viết tắt với tên đầy đủ cục bộ, khoảng thời gian cùng số/đơn vị trong cửa sổ 16 segment/180 giây, và từ nội dung còn thiếu trong cùng mục nghị sự có số hồ sơ/tiêu đề và kết quả đã trích (32 segment/360 giây, dừng tiêu đề khác kể cả khi có từ chỉ kết quả). Mục overview chỉ nhận liên kết viết tắt khi strict. Từ v37, nhánh fact inventory của Gemini đã đăng ký còn bật `complete_bindings` (mặc định tắt): số hồ sơ có nhãn thiếu trong nguồn của mục được nối với tiêu đề nghị sự gần nhất phía trước có đúng số đó (32 segment/360 giây, dừng khi đổi mục), và cụm số lượng nguyên văn nằm giữa hai đoạn đã trích cùng mục nghị sự được nối với segment chứa đúng cụm đó; overview chỉ nhận thêm liên kết hồ sơ có nhãn; liên kết mới xét sau liên kết cũ, thêm trọn nhóm trong trần cũ, không sửa câu chữ. Liên kết mới được xét sau các liên kết cũ, thêm trọn vẹn trong trần 12 ID và trần tham chiếu của mục; không sửa câu chữ/metadata hay thêm lượt gọi. Tên tổ chức đầy đủ trùng nguyên văn còn có thể được nối bằng một ID trong cửa sổ riêng 32 segment/600 giây, không qua tiêu đề nghị sự; cửa sổ cũ vẫn dùng cầu nối và cách xét trần cũ. Trong confirmation_items, vai trò chính thức không có dấu hiệu từ ngữ trong nguồn riêng của mục được chuyển sang trích đoạn cần kiểm tra; ở confirmation_items và action_items, chủ thể nhóm đứng đầu (hội đồng, ủy ban, ban, commission) không được nguồn của mục nêu, hoặc việc hoàn thành mà nguồn chỉ nêu tiến độ một phần, cũng vậy; số hiệu phụ sau động từ sửa/thay văn bản có các biến thể nguồn gần nhau cùng nhóm được đánh dấu chưa rõ, giữ số hiệu chính và không chọn một biến thể; bỏ qua danh sách nhiều văn bản và trích đoạn nguyên văn. ChatGPT không đổi. Runner từ chối tác vụ có phiên bản prompt khác (trừ v4 tiếng Anh, vẫn được chạy) hoặc phiên bản schema notes khác bản hiện hành bằng `notes_contract_changed`, nên tác vụ ghim phiên bản cũ đã nhận mà còn chờ khi nâng cấp kết thúc lỗi và người vận hành tạo lần chạy mới.

Renderer không ghi liên kết hay timestamp vào Markdown. Cùng đoạn mã dựng từng dòng Markdown (`_section_bullets`) cũng dựng trích dẫn (`build_citations`): mỗi mục một entry theo thứ tự render, gồm `section` (tiêu đề phần đã render theo ngôn ngữ notes), `text` (nội dung mục đúng như dòng Markdown, bỏ `- `, giữ ký tự đã escape) và `sources` (`id`, `start_seconds` của từng đoạn trong `source_ids`, theo thứ tự document); mục không có nguồn vẫn có entry với `sources` rỗng để các mục trùng nội dung giữ đúng vị trí. Runner thêm `citations` vào event `result`; worker chuyển tiếp nguyên danh sách, coi giá trị khác danh sách là lỗi giao thức và coi event không có `citations` là bản notes cũ (không có trích dẫn). `publish_notes_revision` kiểm tra trước khi ghi cùng giao dịch với bản notes: danh sách tối đa 6 phần × 200 mục, mỗi entry đúng ba field, mỗi nguồn đúng `id`/`start_seconds`, ID thuộc bản chụp nguồn của tác vụ, không lặp trong một mục và `start_seconds` bằng mốc bản chụp ghi cho đoạn đó, JSON tối đa 1.024.000 byte; không đạt thì `invalid_citations` 400 hoặc `citations_too_large` 413 và tác vụ lỗi `notes_publication_failed`. Cột `citations_json` giữ JSON này; `update_note` không bao giờ đổi nó. UI vẽ trích dẫn thành chip ở chế độ Xem, chỉ cho mục còn đúng tiêu đề phần và nội dung (xem frontend 6.3); chip dẫn tới `/meetings/{id}?notes_source={snapshot}&segment={segment}#transcript`, đường dẫn ứng dụng không chứa token, và UI mở đúng snapshot rồi seek cùng bản audio. Export không chứa timestamp. Với Markdown do người dùng sửa, backend cho lưu nguyên văn và trả cảnh báo `warnings` (xem bảng API) về tham chiếu không hợp lệ; đọc, PATCH và export đều trả lại đúng Markdown đã gõ, không bỏ hay sửa link. UI không mở nguồn ở link có cảnh báo (hiện thành chữ kèm dòng cảnh báo) và không đoán ID thay thế. Copy/export vẫn lấy cùng Markdown đã lưu. Không ép nội dung chỉnh tay ngược về schema model hoặc coi cảnh báo link là quyền công bố đầu ra model chưa hợp lệ: cảnh báo chỉ nằm ở đường đọc, `publish_notes_revision` và `validate_document` không dùng nó, nên đầu ra model có ID nguồn, link hoặc schema hỏng vẫn bị từ chối trước khi công bố.

## 6. Kết nối provider và file bền

ChatGPT dùng direct SIWC, OMP RPC dành cho Antigravity; model và provider cố định trong mỗi tác vụ. Dùng store riêng của bản cài, giữ định danh đăng ký/host và phiên qua restart. UI chỉ nhận trạng thái kết nối, thông tin tài khoản cần hiển thị và URL/code của luồng auth; không nhận credential inference/refresh. Không mount cấu hình coding cá nhân của người dùng vào runner.

**Hợp đồng trạng thái provider (#84).** `GET /api/providers` trả thêm `in_use` và `in_use_reason` cho từng provider. Backend tính hai trường từ cùng truy vấn active notes task dùng để bảo vệ mutation: provider đang được task `accepted`/`running`/`stopping` ghim có `in_use: true`, `in_use_reason: "notes"`; provider khác vẫn `false`/`null`. `GET /api/providers/{provider}/models` có thể trả `409 provider_in_use`; đây là từ chối tải catalog, không phải mất kết nối, nên client giữ status/account/model đã biết. Các endpoint mutation vẫn phải dùng server guard; metadata chỉ giúp UI khóa đúng provider.

Q27 cho chọn provider + model trước khi chạy, chọn sẵn lựa chọn gần nhất và hiển thị rõ. Danh sách model dựa trên kết nối và khả năng adapter; catalog không chứng minh quyền inference hoặc quota. Model đã nhớ không còn dùng được phải báo để người dùng chọn lại, không tự chọn model thay thế. Tác vụ lưu provider/model ID thực dùng, không đọc lại preference giữa các chunk/hợp nhất. Thay preference khi đang chạy chỉ áp dụng cho lần chạy sau.

OMP runner chỉ nhận prompt và các chức năng cần cho inference; tắt tools/extensions/skills/MCP, tự nạp ngữ cảnh dự án và auto-compaction. Phải kiểm chứng các cờ/cấu hình, logging và đường ghi của bản OMP được pin. Đăng nhập là luồng riêng có thể hủy/hết hạn; không chiếm slot model chỉ để chờ người dùng. Với Antigravity, đoạn đổi credential (từ `adapter.login` đến lúc ghi trạng thái) giữ khóa thao tác liên tiến trình, cùng khóa với lúc nhận tác vụ và lúc runner chạy, và kiểm tra lại guard tác vụ đang chạy sau khi lấy khóa (`provider_in_use`); nên đăng nhập không thể thay tài khoản giữa lúc chạy, và nhận tác vụ khi còn phiên đăng nhập chờ bị từ chối (`login_in_progress`). Bắt đầu đăng nhập mới sẽ hủy phiên chờ cũ; trong lúc phiên đăng nhập giữ khóa, liệt kê/kiểm tra model và ngắt kết nối trả `provider_busy`. Tác vụ đã gắn provider/account version chặn login, logout, liệt kê/kiểm tra model và đổi model Antigravity của chính provider đó (`provider_in_use`); provider còn lại vẫn dùng được bình thường trong lúc chạy. Runner chỉ kiểm tra provider còn kết nối đúng connection version đã ghim (`provider_disconnected`, `reconnect_required`, `connection_changed`), không so với preference hiện tại. `provider_disconnected` yêu cầu kết nối; chỉ `reconnect_required` mới nói cần kết nối lại. `provider_unavailable` không chứng minh nguyên nhân mất Internet; UI giữ copy cause-neutral và chỉ khuyên kiểm tra Internet/thử lại, còn lỗi transport local giữ mã mất kết nối riêng. Ngắt kết nối Antigravity trả trạng thái local-only; không diễn giải `remote_revocation_confirmed` như quyền Google bị thu hồi. Mỗi lần gọi Antigravity là một tiến trình OMP mới (không giữ hội thoại), cùng nhóm tiến trình với runner nên hủy nhóm runner dừng cả OMP; external revoke vẫn có thể làm tác vụ lỗi, không tự chọn tài khoản khác.

Audio bền được tách khỏi audio tạm đã chuẩn hóa cho model. Chuẩn codec lưu/phát, định dạng upload và giới hạn dung lượng/thời lượng phải được xác định với file thật; repo hiện chỉ có dữ liệu prototype. Chưa có kết quả chứng minh decode, playback hoặc model xử lý bản hai giờ.

Upload và kết quả file có trạng thái bền để nhận biết file chưa hoàn tất; không công bố file sẵn sàng trước khi lưu thành công. Xóa meeting kiểm tra active task và đánh dấu trạng thái xóa trong cùng giao dịch; từ đó từ chối tác vụ mới cho meeting. Dọn file ngoài giao dịch DB, có thể thử lại sau restart theo dấu xóa, chỉ nhắm file ứng dụng quản lý; báo đang xóa/lỗi khi chưa dọn xong, không báo thành công giả. Không dọn file tạm còn thuộc tác vụ đang hoạt động chỉ dựa trên tuổi file.

## 7. Kiểm chứng trước triển khai/phát hành

- Review các bất biến và tình huống cạnh tranh: nhận hai tác vụ, ghi từ tab cũ, hủy cạnh tranh hoàn tất, publish generation mới, ngắt tài khoản và xóa meeting.
- Thử nghiệm có chủ đích sau giai đoạn thiết kế: SIWC/OMP auth trong Docker, refresh/restart/disconnect, hủy, một request notes và ghi nhận model/quota; chọn mẫu được phép gửi provider. Chưa thực hiện bước này.
- Với mẫu thật: EN, VI xen thuật ngữ, code-switch, nhiều người nói và ít nhất một bản hai giờ; kiểm tra timestamp, decisions/owners/deadlines, VRAM, thời gian và quota theo AC.
- Q23–Q27 đã chốt; kiểm tra riêng sửa metadata/speaker theo phiên bản, retry HTTP không tạo thêm tác vụ, không tự gửi lại inference lỗi mạng, link chỉnh tay hỏng và giữ model trong suốt tác vụ. Những giới hạn kỹ thuật phụ thuộc phép đo được ghi thành kết quả đạt/chưa đạt, không yêu cầu người dùng tự đoán cấu hình.

## Hồ sơ thay đổi cho v0.1.0

Xem [bằng chứng và đánh đổi theo tag](release/privacy-v0.1.0.md),
[các thay đổi notes](release/notes-v0.1.0.md) và
[chỉ mục thay đổi](architecture-changes.md). Số đo lịch sử giữ nguyên điều kiện
và revision gốc; không phải phép đo lại artifact 0.1.0.


### Citation display groups (#153)

Notes revision responses additionally expose `display_citations`, a read-only projection of
`citations` using only that revision's immutable snapshot. Entry shape stays `{section, text,
sources}`; each display source includes `members: [{id, start_seconds}]` and, when validated,
`end_seconds`. Stored `citations` and snapshot JSON remain unchanged. Missing structural metadata
falls back to individual sources. Legacy snapshots have no acoustic quality flags; absence of
flags is not evidence that acoustic overlap is absent.

Only adjacent positions in the full snapshot with the same non-null speaker ID can join.
Uncited segments, invalid/overlapping intervals and snapshot quality barriers prevent joining.
The policy is gap <= 1 second and group span <= 60 seconds; long single segments stay single.
The API adds no snapshot fetch to rendering. Opening a source uses the existing snapshot cache
(one fetch per snapshot per page session). The projection adds member IDs/timestamps to revision
payloads, but does not send the whole snapshot. Group links carry an optional `group` query value
(the ordered member IDs); the source card resolves it only against the selected revision's
projection. Member links preserve that value through Back/Forward. Copy/export use saved Markdown.

Real EN/VI recording evaluation, listening, before/after screenshots and measured payload sizes
remain required before merge (AC-12); synthetic fixtures cannot establish acoustic accuracy.
