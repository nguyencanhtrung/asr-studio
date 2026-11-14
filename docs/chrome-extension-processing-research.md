# Tự chạy transcript và meeting notes sau khi ghi bằng extension

Ngày 2026-11-15 · [Thiết kế tự tạo transcript và meeting notes sau finalize](https://github.com/nguyencanhtrung/asr-studio/issues/204) · Nghiên cứu thiết kế, chưa triển khai production

## Kết luận ngắn

Đề xuất coi normalized recording đạt ready sau finalize là một sự kiện bền vững, rồi trong một transaction tạo hoặc lấy lại cả meeting và một processing intent. Intent này tách hai bước: transcription và notes. Worker chỉ lấy task đã được chấp nhận khi có slot; lúc recording còn đang capture hoặc upload thì không giữ inference slot.

Contract cần bảo đảm:

- một finalized recording chỉ có một meeting và một workflow tự động;
- một request logic chỉ có một task acceptance và một publication logic;
- retry sau crash có thể gọi ASR/provider lại, nhưng không tạo generation hoặc notes revision thứ hai;
- transcript thành công là artifact độc lập. Notes provider chưa kết nối hoặc notes thất bại chỉ làm notes chờ/thất bại, không làm mất transcript và không chạy ASR lại;
- hủy và xóa đặt tombstone/epoch trước khi worker được phép publish muộn;
- Giữ nguyên contract task/slot của GUI; pipeline intent có waiting reason riêng và cần một thay đổi render nhỏ để web UI giải thích trạng thái chờ, không giả định UI tự hiện field lạ.

Các giá trị ghi là “đề xuất” là mặc định thiết kế để prototype đo, không phải latency, capacity hay capability đã được chứng minh.

Phụ thuộc upstream: [contract ingest resumable của extension](chrome-extension-resumable-ingest-research.md) đã hợp nhất; đây là ranh giới đầu vào của nghiên cứu này. Nghiên cứu này dùng mốc upstream là archive/recording đã durable và ready; không tự chạy ASR từ từng Blob hoặc từng transport chunk.

## R1 — Hiện trạng và ranh giới cần giữ

Đây là flow hiện tại trong repository tại một commit bất biến:

| Mốc | Bằng chứng mã nguồn | Hệ quả thiết kế |
|---|---|---|
| Tạo upload, ghi byte theo offset, complete/normalize | [routes.py upload routes](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/routes.py#L706-L907), [recordings.py upload state](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/recordings.py#L162-L304), [complete_upload](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/recordings.py#L390-L480) | Đây là đường upload known-size hiện có. Extension ingest mới không được coi một Blob là recording hoàn chỉnh. |
| Upload ready → meeting + recording | [create_meeting](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/recordings.py#L554-L631) và [POST /api/meetings](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/routes.py#L923-L940) | Existing create idempotent theo upload_id; cùng payload trả meeting cũ, payload khác trả 409, và consume upload cùng transaction. Extension cần thêm khóa receipt/recording tương ứng, không dùng title làm khóa. |
| Auto transcription hiện tại | UI lưu handoff một lần rồi meeting page gọi task: [shell-core.js](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/web/shell-core.js#L1411-L1427), [history.js](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/web/history.js#L344-L353), [task-core.js](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/web/task-core.js#L244-L309) | Đây là client handoff, không phải durable server intent. Sau này server phải tự khôi phục được nếu browser đóng trước request hoặc response bị mất. |
| Transcription acceptance | [accept_transcription_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L191-L342) | request_id và request_hash chống duplicate/conflict, nhưng acceptance hiện kiểm slot, worker, model trước rồi mới insert task; busy/offline chưa được xếp hàng bền vững. |
| Một inference slot | [processing_slot schema](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/storage.py#L67-L151) và [claim_next_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L398-L441) | Giữ singleton slot và giữ contract hiện tại: task đã accepted sở hữu slot. Auto intent chờ trước acceptance khi bận/offline/provider; không giữ slot qua capture/upload hoặc qua network/provider call ngoài worker. |
| ASR/diarization → generation | [_run_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/worker.py#L740-L1027), [publish_complete_generation](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L708-L900) | ASR là child process; publication full generation là transaction có owner/slot fence. Nếu crash trước transaction có thể chạy lại ASR; nếu commit rồi mất response thì GET/reconcile phải thấy generation đã có. |
| Notes hiện tại | [POST /notes-runs](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes_routes.py#L86-L120), [accept_notes_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L337-L570), [publish_notes_revision](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L982-L1059) | Notes hiện là thao tác manual sau transcript. Acceptance pin provider/model/source snapshot và publication insert revision + succeed task + release slot trong một transaction; đây là seam phù hợp cho notes-only retry. |
| Restart và lifetime | [run_worker](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/worker.py#L1059-L1121), [recover_abandoned_tasks](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L902-L957), [service_runtime.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/service_runtime.py#L12-L205), [notes_service.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes_service.py#L80-L148) | Split transcription/notes worker và lock/heartbeat phải giữ. Accepted task của split role sống qua restart; running thành interrupted, stopping thành canceled. Intent/reconciler mới phải bám contract này, không làm worker cũ tự nhận task của role kia. |
| Xóa | [meeting_deletion.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/meeting_deletion.py#L57-L83), [_finalize_database_delete](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/meeting_deletion.py#L218-L312), startup recovery [app.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/app.py#L140-L155) | Existing delete dùng marker và chặn active task. Processing intent mới phải được tính là active, có tombstone để reconciler không tái tạo sau khi rows/artifacts đã xóa. |

Một khác biệt quan trọng: startup cleanup của legacy upload hiện đánh dấu receiving/uploaded/validating/ready là interrupted và dọn toàn bộ raw/temp, kể cả raw của upload consumed; normalized audio chỉ được giữ nếu thuộc upload consumed ([recover_recordings](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/recordings.py#L840-L882)). Đó không phải hành vi được phép bê sang recording session resumable của extension; contract ingest sibling là nguồn quyết định cho mốc archive và resume.

## R2 — Intent bền vững và handoff nguyên tử

### Flow đề xuất

1. Khi upstream báo recording ready, server kiểm receipt, recording identity và deletion tombstone.
2. Một transaction ready_handoff tạo hoặc lấy lại meeting, recording link và pipeline intent. Cùng transaction ghi outbox event recording_ready; không gọi ASR/provider và không giữ inference slot.
3. Reconciler/scheduler chỉ giữ transcription step_intent ở waiting khi slot bận, worker/model offline hoặc queue capacity đạt. Chưa có task row và chưa claim slot.
4. Khi worker/model readiness, slot NULL và epoch/tombstone fence cùng pass trong một transaction, connection-aware acceptance primitive atomically insert/link task accepted và claim processing_slot như contract hiện tại. Sau commit, split worker dùng claim_next_task hiện có để chuyển accepted → running.
5. Transcription worker chạy ASR/diarization, rồi transaction publication tạo complete transcript generation và outbox transcript_ready.
6. Reconciler consume transcript_ready, tạo immutable notes source snapshot và notes intent. Nếu provider chưa sẵn sàng hoặc slot bận, notes intent vẫn waiting và chưa có notes task; khi provider + slot + fence pass, primitive mới tạo/link notes task accepted và claim slot.
7. Notes worker chạy provider ngoài transaction, rồi transaction publication tạo notes revision và đóng notes intent. Mọi bước có thể chạy lại từ DB: thiếu response không làm mất intent; duplicate outbox không tạo row thứ hai vì unique key.

Trạng thái logic của pipeline:

recording_ready → meeting_persisted → transcription_intent_waiting → transcription_accepted → transcribing → transcript_ready → notes_intent_waiting / notes_waiting_provider → notes_accepted → notes_running → complete

Nhánh kết thúc là failed_retryable, failed_terminal, canceled hoặc deleted. transcript_ready là mốc độc lập: notes không được coi là điều kiện để giữ hay công bố transcript.

### Thành phần logic cần có

Đây là contract dữ liệu, không phải schema production bắt buộc:

- pipeline_intent: recording_id, meeting_id, workflow_version, capture/finalize identity, state, epoch, priority class, retry counters, next eligible time, cancel/deletion fence, last error.
- step_intent: một row cho transcription và một row cho notes; stage, dependency, attempt number, pinned recipe/source, logical publication key.
- processing_outbox: event type, source key, payload version, created/processed time; unique theo event và source generation.
- task execution lease: owner_role, owner_id, lease/epoch, heartbeat, execution number. Đây là thông tin của một lần worker chạy, không phải một logical task mới.

Reconciler phải làm được ít nhất bốn việc: dựng lại meeting/intent còn thiếu từ ready receipt; giữ step intent chờ đến khi acceptance preconditions pass rồi gọi primitive tạo task + claim slot; biến running task của worker đã chết thành đúng trạng thái restart hiện có sau khi chứng minh lifetime ownership và child drain/reap; và phát hiện generation complete nhưng notes handoff còn thiếu. Không có callback trong RAM nào là nguồn sự thật duy nhất.

### Ranh giới giao dịch có thể triển khai

Hiện trạng cần giữ cho đúng với source: create_meeting tự mở BEGIN IMMEDIATE, kiểm tra upload ready, insert meeting/recording và consume upload trong cùng transaction ([create_meeting](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/recordings.py#L554-L631)). accept_transcription_task tự mở transaction và hiện vừa nhận task vừa claim processing_slot ([accept_transcription_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L191-L342)); accept_notes_task cũng có provider-operation lock rồi tự mở transaction để pin snapshot, insert task và claim slot ([accept_notes_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L337-L570)). Vì vậy scheduler không được mở transaction rồi gọi lồng các helper này, cũng không được chỉ kiểm cancellation trước khi gọi chúng.

| Seam | Hiện tại | Thay đổi đề xuất |
|---|---|---|
| ready handoff | create_meeting là transaction riêng theo upload_id; chưa có pipeline intent/outbox | Tách lõi create_meeting_tx(connection, ...) và ready_handoff_tx(connection, ...). Public helper hiện tại vẫn là wrapper tự mở transaction; reconciler dùng cùng connection để ghi meeting/recording link, intent, step và outbox rồi mới commit. |
| task admission/linkage | accept_* có thể từ chối vì slot busy/worker offline; khi thành công helper tự insert task và claim slot trong cùng transaction | Giữ accepted task owns processing_slot. Auto step_intent chỉ chờ khi chưa đủ điều kiện; khi readiness + slot + epoch/tombstone pass, connection-aware acceptance primitive cùng transaction insert task, link step và claim slot. |
| cancel/delete race | cancel_task và deletion marker có transaction riêng; current task có thể chuyển stopping hoặc bị từ chối khi active | Transaction admission dùng fence ghi có điều kiện; transaction cancel/delete tăng epoch hoặc đặt tombstone. Hai giao dịch được serialize bởi BEGIN IMMEDIATE, nên một bên thắng rõ ràng thay vì dựa vào pre-check trong application code. |

Contract của admission primitive nên có các bước trong một BEGIN IMMEDIATE trên đúng connection:

1. Kiểm ownership/step linkage và tombstone trong transaction; tra request_id/attempt đã tồn tại trước điều kiện slot/readiness. Cùng hash và linkage trả task cũ kể cả task đó đang giữ slot hoặc worker đang offline; khác payload trả conflict. Không ghi lại attempt terminal hay resurrect pipeline đã canceled/deleted.
2. Chỉ với attempt chưa được admit, kiểm recording ready, expected intent_epoch/capture_epoch và slot NULL. Worker/model readiness là snapshot/preflight bên ngoài transaction, không thực hiện filesystem/network I/O trong SQLite lock; provider pin lấy dưới operation lock theo thứ tự hiện có rồi kiểm version trong transaction. Readiness thay đổi sau commit được worker xử lý thành trạng thái có tên, không bảo đảm process sống mãi bằng một phép kiểm. Nếu chưa đủ điều kiện, không tạo task/claim slot; commit waiting_reason trên intent hoặc ghi lại bằng transaction ngắn sau rollback.
3. Insert task accepted và UPDATE step linkage có điều kiện theo epoch/tombstone; đồng thời claim processing_slot bằng UPDATE ... WHERE active_task_id IS NULL. Nếu bất kỳ conditional write nào không khớp, rollback cả task/linkage/slot claim; không có task mồ côi. Idempotent replay ở bước đầu không claim slot lần nữa.
4. Commit rồi để đúng split worker chuyển accepted → running theo contract hiện có. Không giữ transaction khi đọc audio, gọi ASR/provider hay assemble output.

Đây là admission fence thật sự, không phải một lần kiểm cancellation trước helper. Có hai cách triển khai tương đương: các helper public gọi lõi connection-aware bên trong wrapper transaction của chính chúng, hoặc scheduler/reconciler gọi trực tiếp lõi trên connection đang mở. Cách đầu là mặc định đề xuất; nếu chưa refactor được thì không được ghép helper hiện tại vào transaction ngoài. Với notes, provider pin có thể được lấy dưới provider-operation lock như hiện tại, nhưng DB admission vẫn phải nhận expected intent_epoch và commit task/snapshot/linkage trên connection-aware primitive; không gọi provider trong DB transaction và không đổi task đã pin nếu provider thay đổi sau commit.

### Điểm commit, crash và chỉnh sửa transcript

- Ready receipt/archive phải durable trước ready_handoff. Commit của ready_handoff là meeting/recording link + pipeline/step intent + outbox recording_ready. Crash trước commit để lại ready receipt để reconciler replay; crash sau commit nhưng trước response được GET/reconcile trả lại theo ready key và unique key, không tạo workflow thứ hai.
- ASR chạy ngoài transaction trên artifact đã pin. Kết quả tạm chỉ được đưa vào publication khi đã hoàn tất kiểm tra. transcript_publish_tx(connection, task_id, owner_id, expected intent_epoch, execution_lease_id, output) phải kiểm owner/lease/epoch/tombstone trong cùng BEGIN IMMEDIATE rồi insert generation, speakers, segments, current_generation, task terminal, release slot và outbox transcript_ready trong cùng commit. Crash trước commit có thể gọi ASR lại; crash sau commit chỉ làm mất response, không mất generation/outbox và không tạo generation thứ hai khi reconcile.
- Reconciler xử lý transcript_ready bằng một transaction riêng: đọc đúng generation_id do outbox của task ASR này ghi cùng source_version, dựng đầy đủ notes_source bất biến và link notes step/intent bằng unique generation/publication key. SQLite serialize transaction này với manual transcript edit. Nếu cùng generation được edit trước snapshot, snapshot pin source_version mới; nếu current_generation_id đã chuyển sang generation khác trước handoff, intent chuyển needs_attention/source_changed và không tự lấy generation khác. Không mô tả snapshot này là đã đóng băng tại thời điểm ASR publish. Nếu handoff commit trước, snapshot giữ source_version cũ và edit sau đó không được mutate snapshot. Khi notes task đã accepted/running, giữ notes_source_locked hiện có; nếu chỉ đang waiting_provider, snapshot vẫn immutable, UI phải báo notes đang pin bản trước edit và một run do user khởi động sau edit sẽ tạo snapshot mới.
- Manual transcript edit chỉ tăng source_version của current generation qua transaction có optimistic fence ([update_current_generation_segment_text](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L1077-L1190)). Vì notes source đã pin generation + source_version, retry notes không quay lại ASR và không đọc âm thầm nội dung mới; publication muộn chỉ được nhận nếu còn đúng task/lease/epoch và không overwrite revision manual.

### Nối session mới với thư viện hiện tại

Schema hiện có `recordings.upload_id NOT NULL UNIQUE REFERENCES uploads(id)` ([storage.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/storage.py#L27-L50)); không thể truyền session unknown-size vào create_meeting như một upload_id giả. Đề xuất migration thêm `extension_session_id` unique, cho upload_id nullable và constraint chính xác một trong hai nguồn. Public helper legacy vẫn kiểm upload ready/consume; lõi insertion dùng connection được truyền vào, còn ready_handoff của extension chỉ nhận artifact descriptor đã được finalizer xác minh và cùng transaction link session → recording → meeting → intent. Giữ đường known-size của UI hiện tại.

Raw/normalized/temp của extension nằm trong namespace riêng do server sinh, ngoài các thư mục mà legacy recovery quét. Recovery mới đối chiếu session/receipt trước cleanup; không chuyển archive sang `uploads/` rồi mong legacy startup giữ nó. `recover_recordings` hiện unlink mọi raw và dọn temp, kể cả upload consumed. Migration phải cập nhật source-aware audio lookup, recovery và meeting deletion: đường legacy vẫn dùng canonical upload paths, đường extension dùng session manifest, directory-safe cleanup, epoch/tombstone và cả raw lẫn derivative. Không nới kiểm tra path để chấp nhận arbitrary filesystem input. Đây là thay đổi cần kiểm chứng khi hiện thực, không phải schema đã có.

## R3 — Khóa, uniqueness và semantics exactly-once

| Đối tượng | Khóa bền vững đề xuất | Nếu replay |
|---|---|---|
| Finalize | (recording_session_id, capture_epoch, finalize_id); manifest/recipe fingerprint bất biến | Cùng fingerprint trả receipt/state cũ; khác fingerprint trả conflict, không overwrite archive. Chi tiết ingest nằm ở sibling document. |
| Meeting/recording | ready_receipt_id hoặc external recording_id UNIQUE; giữ unique upload_id hiện có cho legacy | Retry create sau mất response trả cùng meeting/recording. Title/date/language khác là conflict, không tạo meeting thứ hai. |
| Pipeline | recording_id UNIQUE; workflow_version/capture_epoch là thuộc tính đã pin | Chỉ một workflow auto cho một finalized recording, kể cả sau nâng workflow_version. Hủy dùng tombstone, không cho reconcile mở workflow mới; retry explicit tạo attempt trong workflow đó. |
| Task acceptance | (step_intent_id, attempt_no) UNIQUE và request_id UNIQUE; request hash gồm stage, recipe, source key | Cùng request sau khi được admitted trả cùng task row. Busy/offline/provider chưa sẵn sàng không tạo task row; step intent giữ waiting reason. Cùng request_id nhưng khác payload/recipe trả 409. |
| Transcript generation | task_id/logical publication key UNIQUE; generation number vẫn unique theo meeting | Publication retry thấy generation cũ hoặc insert conflict; không có hai generation cho cùng task attempt. Generation mới chỉ xuất hiện khi có transcription retry mới được người dùng hoặc policy cho phép. |
| Notes source | snapshot id bất biến, fingerprint gồm generation_id, source_version, notes_source_version và metadata_version | Retry notes từ đúng snapshot, không đọc current transcript mới một cách âm thầm. Cơ sở hiện có: notes_sources immutable và task pin source_id ([storage.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/storage.py#L242-L352), [notes.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L866-L894)). |
| Notes publication | notes_revisions.task_id UNIQUE và publication key UNIQUE | Mất response sau commit trả revision đã có; không insert notes thứ hai cho cùng task. |

Exactly-once ở đây chỉ là exactly-once logical acceptance và exactly-once durable publication trong phạm vi DB. Nó không cam kết exactly-once ASR/provider invocation: crash sau khi gọi engine/provider nhưng trước publication có thể làm lần sau gọi lại. Vì vậy cần execution_no/lease để audit và publication fence để kết quả cũ không thắng owner mới; không nên nói với người dùng rằng provider chỉ nhận đúng một request.

Các transaction boundary:

- ready_handoff_tx(connection): meeting + recording link + pipeline intent + outbox event; không network, không FFmpeg, không inference. Không gọi lồng create_meeting; wrapper và lõi phải dùng cùng connection nếu cần một boundary.
- auto_accept_and_claim_tx(connection): readiness + slot NULL + epoch/tombstone fence + task idempotency + step linkage + processing_slot claim; không network, không inference. Cancel/delete có thể thắng trước hoặc sau transaction này, nhưng không có acceptance sau tombstone thắng và không có accepted task không giữ slot.
- claim_next_task: giữ hành vi hiện tại cho task accepted đã sở hữu slot; đúng split role chuyển accepted → running trong transaction ngắn rồi worker mới chạy child/provider.
- transcript_publish_tx(connection): kiểm task owner/epoch/stage và intent tombstone, insert generation/segments, current_generation, task terminal, release slot và outbox transcript_ready trong cùng commit.
- notes_accept_tx(connection): snapshot immutable + notes task + recipe pin + step linkage + epoch fence + slot claim; không gọi provider và không được lồng accept_notes_task.
- notes_publish: kiểm owner/epoch/deletion, insert revision, task succeeded, release slot, close step; không update đè revision manual.
- cancel/delete: ghi epoch/tombstone trước; cleanup chỉ xóa rows/artifacts sau khi không còn lease active.

## R4 — Scheduling, busy/offline/readiness và trạng thái UI

### Scheduler

Giữ processing_slot là một slot inference duy nhất, giữ hai service role và giữ contract hiện tại rằng task accepted sở hữu slot. MVP chỉ xếp durable pipeline/step intent trước acceptance. Scheduler không tạo task accepted khi slot bận/offline/provider chưa sẵn sàng; nó ghi waiting reason trên intent rồi gọi auto_accept_and_claim_tx chỉ khi readiness, slot NULL và epoch/tombstone fence cùng pass. Không giữ SQLite transaction trong lúc đọc audio, gọi ASR, chờ provider hoặc assemble output.

GUI submission hiện tại không phải durable queue ticket. POST /api/tasks tiếp tục gọi acceptance hiện tại: nếu worker offline hoặc slot busy thì trả lỗi hiện có, còn khi accepted thì task claim slot như hiện tại. Vì chưa đổi contract GUI thành durable admission, scheduler không thể chọn “GUI lâu nhất” hay hứa thời gian chờ cứng.

Mặc định đề xuất cho prototype 205:

| Chính sách | Default đề xuất | Ý nghĩa |
|---|---:|---|
| Pending automatic workflows | tối đa 32 trên một backend | Chặn queue vô hạn; khi đầy, recording/meeting vẫn được giữ và UI nhận queue_capacity, không báo đã xử lý. |
| Pending automatic step intents | tối đa 64, tối đa một intent pending cho mỗi step của một recording | Chỉ intent chờ được tính vào cap; không tạo task row hoặc claim slot do retry/duplicate finalize. |
| Auto order | FIFO theo ready_handoff sequence, tie-break bằng intent id | Đây là FIFO có thể chứng minh vì chỉ automatic intents là durable queue ticket trong MVP. |
| GUI opportunity | sau tối đa 2 automatic admissions liên tiếp, mở một cửa sổ 1 giây không admit auto; GUI POST nào đến và pass acceptance hiện tại có thể thắng | Best effort cụ thể, có thể để slot rảnh; không phải oldest-GUI guarantee và không hứa GUI luôn thắng race. |
| Retry | tối đa 6 execution attempts tự động, backoff tăng dần và có ceiling; sau đó retryable chờ người dùng | Đây là policy khởi đầu để đo, không phải reliability claim. |
| Heartbeat/lifetime | giữ interval health 5 giây trong [worker.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/worker.py#L68) và [notes_service.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes_service.py#L33); không có stale window tự reclaim slot hoặc chạy replacement | Heartbeat chỉ báo health. Recovery chỉ đổi ownership/task sau khi đã có exclusive lifetime ownership và drain/reap child; 205 phải inject crash/clock skew trước khi chốt. |

Nếu queue đạt cap, không rollback ready audio. Hệ thống lưu một capacity_blocked outcome trên pipeline/meeting, hiện “Đã lưu bản ghi; hàng đợi xử lý đang đầy” và cho retry admission; đây là blocker có tên, không phải lỗi mơ hồ.

### Alternative bị loại khỏi MVP

Một unified queue bền vững cho cả GUI task và automatic step sẽ cho phép chọn oldest trên toàn hệ thống, nhưng phải đổi semantics POST /api/tasks, durable ticket/cancel/retry, status/recovery và UI. Đó là hướng lớn hơn bị reject/defer cho issue này; MVP chỉ có FIFO tự động và GUI opportunity best-effort như trên.

### Readiness và trạng thái trả cho web UI

Giữ các status accepted, running, stopping, succeeded, failed, canceled, interrupted mà [_task_summary](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L145-L188) và UI hiện dùng. Waiting của auto pipeline là trạng thái của pipeline/step intent, không giả mạo thành task accepted:

- pipeline step_state: waiting, admitted, running, succeeded, failed, canceled;
- waiting_reason: worker_offline, slot_busy, model_unavailable, provider_unconfigured, provider_unavailable, capacity;
- next_eligible_at, admission_attempt_no, pinned source/generation id và last error;
- task payload chỉ giữ status/fields hiện có; khi primitive đã admit thì task accepted vẫn có nghĩa accepted + slot owner.

Ý nghĩa:

- worker offline: automatic intent waiting(worker_offline), chưa có task; GUI POST giữ lỗi worker_unavailable hiện tại;
- slot busy: automatic intent waiting(slot_busy), chưa có task; GUI POST giữ lỗi system_busy hiện tại;
- model chưa chuẩn bị/GPU không phù hợp: automatic intent waiting(model_unavailable), không gọi ASR; recipe đã pin, đổi model là một transcription retry mới;
- notes provider chưa kết nối: transcript vẫn chạy/succeed; notes intent waiting_provider, chưa có notes task, không claim slot và không gọi provider;
- provider tạm lỗi/time-out: notes task failed + retryable, giữ source snapshot; không tạo transcription task mới;
- lỗi dữ liệu/nguồn vượt giới hạn/response không hợp lệ lâu dài: failed_terminal, giữ artifact và hướng dẫn rõ.

Các field waiting cần được thêm additive vào pipeline summary/meeting response và cần một nhánh render nhỏ để UI nói rõ “đã lưu, đang chờ slot/worker/provider”. UI hiện chỉ biết render task state, poll task, source_locked, failure code và notes revision ([task-core.js](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/web/task-core.js#L74-L87), [notes-core.js](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/web/notes-core.js#L17-L98), [notes_state](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L739-L769)). Không được coi payload additive tự làm UI hiện field unknown; không cần đổi web UI thành task queue mới hoặc tách thêm worker role.

### API và quyền của người gọi

MVP thêm `GET /api/extension/v1/recording-sessions/{id}/processing` với session token và `recording:status:read`. Server lấy owner từ credential, resource ngoài owner trả 404; payload chỉ gồm pipeline/step state, waiting_reason, task/result ids thuộc phiên và relative meeting_url của cùng backend. Client không dùng response để đổi endpoint, không nhét token vào URL. Nội dung transcript/notes mở trong web UI đã xác thực; MVP chưa cấp thêm transcript:read/notes:read hay notes:run chỉ để hiển thị tiến độ.

Retry notes và cancel processing dùng UI operator với cookie/same-origin guard hiện có. UI cần route điều khiển pipeline, đề xuất `POST /api/meetings/{id}/processing/retry-notes` (request_id, expected_pipeline_epoch, source_snapshot_id, confirmed_recipe_version) và `/processing/cancel` (request_id, expected_pipeline_epoch). Request lặp cùng payload trả cùng attempt/cancel outcome; payload khác 409. Retry chỉ tạo notes attempt, không tạo ASR task; khi chưa có snapshot vì ASR chưa complete thì 409 transcript_required. ASR retry hoặc đổi recipe cũng cần thao tác explicit riêng; canceled workflow không tự retry. Server DELETE tiếp tục qua luồng xóa meeting của operator. Các route này là đề xuất chưa triển khai; token extension không được dùng gọi chúng.

## R5 — Recipe, pinned transcript, notes-only retry và chỉnh sửa thủ công

### Pin ở đúng mốc

- Transcription task pin ASR choice/model path, device, compute type, language, recording/normalized artifact digest và workflow version. Không đổi recipe của task đã accepted; user chọn model khác tạo attempt mới.
- Khi transcript publication thành công, notes handoff tạo source snapshot của generation, source_version, notes_source_version, metadata_version và meeting date. Snapshot phải immutable.
- Notes task pin provider, connection version, model, reasoning effort, prompt/schema version, output language và source_snapshot_id. Các field này tương ứng những gì acceptance hiện có đã lưu ([accept_notes_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L397-L570)).

### Nguồn snapshot và recipe phải có interface riêng

`accept_notes_task` hiện kiểm current generation/version và tự dựng snapshot mới; nó chưa nhận source_snapshot_id lịch sử. Notes-only retry từ snapshot đã pin cần primitive nội bộ mới nhận snapshot id, kiểm snapshot thuộc đúng meeting/pipeline, recipe và epoch, rồi atomically tạo/link task + claim slot. Không bỏ guard của public notes API để gọi lại helper cũ với dữ liệu khác nghĩa.

ASR choice/language và notes provider/output-language/model/effort được lấy từ cấu hình operator đã chọn, pin trong intent; không nhận filesystem path/provider credential từ extension. Nếu chưa có provider được chọn/kết nối thì notes chờ cấu hình và xác nhận trong UI. Với ASR Automatic, lưu choice ngay và pin resolved runtime model khi admit lần đầu; retry cùng attempt dùng recipe đó. Đổi account/connection version hoặc model khi chờ yêu cầu xác nhận recipe mới, không âm thầm gửi transcript sang tài khoản khác. Snapshot chỉ được tạo một lần cho step; retry transport/worker không re-snapshot current transcript.

### Retry notes không nhận dạng lại

1. Transcript generation đã complete nhưng notes chưa có hoặc task notes retryable.
2. Reconciler/user retry lấy cùng source_snapshot_id và generation id của lần đó.
3. Tạo notes attempt mới với request_id mới theo attempt number và provider/model mới nếu người dùng xác nhận; không tạo transcription task.
4. Provider call có thể lặp lại sau crash, nhưng publication key của notes attempt chỉ có một revision.

Nếu người dùng sửa transcript sau snapshot, code hiện tại tăng source_version và chặn edit trong lúc notes active ([current_generation_for_write](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L1077-L1143)). Default đề xuất: retry của notes failed tiếp tục dùng snapshot đã pin để giữ semantics “notes-only”; notes run mới do người dùng bấm sau khi sửa sẽ tạo snapshot mới từ current generation. Notes cũ được gắn stale/historical, không bị xóa hoặc âm thầm viết lại.

Manual notes edit dùng optimistic edit_version và cập nhật đúng revision mà người dùng chọn ([update_note](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/notes.py#L790-L831)). Automatic publication luôn insert revision mới, không update markdown của revision manual. Nếu publication đến sau khi người dùng sửa, UI hiển thị revision mới như một lựa chọn và giữ bản manual; không coi provider output là quyền ghi đè.

Provider chưa configured là trạng thái chờ có chủ ý, không phải lý do fail transcript. Khi user kết nối provider, hành động “thử notes” chỉ resolve notes step từ pinned transcript. Nếu provider/model đã đổi trong lúc task chờ, không sửa task đã pin; tạo attempt notes mới với request hash mới và ghi rõ provider/model trong UI.

## R6 — Hủy, xóa, ownership và lifetime

### Lease và epoch

Mỗi step/attempt có intent_epoch và execution_lease_id. Worker heartbeat chỉ là health signal; lock file/role lifetime lock mới nói ownership process, còn lease/epoch là fence để publication không nhận kết quả cũ. Một transaction hủy hoặc xóa tăng epoch và đặt tombstone. Mọi admission, claim, progress, ASR publication, notes publication đều conditional theo epoch + lease + meeting chưa bị xóa.

Giữ nguyên lifetime split worker: process lock bảo vệ service, heartbeat báo health, task owner xác định worker đang chạy. Worker cũ mất ownership hoặc thấy stopping phải terminate/reap child trước khi release slot; logic hiện tại đã làm điều đó ([run child](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/worker.py#L309-L523), [finish_canceled_task](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L540-L602)).

Admission và publication phải truyền expected epoch/lease vào câu lệnh ghi hoặc primitive nhận connection; kiểm tra trước rồi gọi helper transaction riêng là không đủ. Nếu cancel/delete commit trước, rowcount fence từ chối admission/publication. Nếu admission/publication commit trước, cancel/delete thấy task/artifact đã bền và chuyển trạng thái hoặc cleanup theo transaction của nó. Đây là thứ tự commit được quan sát, không phải suy đoán theo thời điểm request đến.

### Lifetime trước recovery

Stale heartbeat hoặc lease expiry một mình không được clear processing_slot, chuyển accepted/running thành retry, hoặc chạy worker thay thế khi service/child lifetime lock còn có thể được giữ. Recovery phải trước hết acquire exclusive lifetime ownership của đúng role, chứng minh owner cũ đã thoát, drain và reap child/process group; chỉ sau bằng chứng đó mới mở transaction recovery để áp dụng trạng thái role-specific hiện có. Nếu chưa chứng minh được exclusivity, giữ nguyên slot/task và báo blocked/recovery_pending, không chạy song song.

Current behavior cần bảo toàn: recover_abandoned_tasks phân biệt role; split accepted task không bị worker role khác tự lấy, running có thể thành interrupted và stopping thành canceled theo recovery hiện tại ([recover_abandoned_tasks](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/tasks.py#L902-L957)). Proposed intent scheduler chỉ thêm bước chờ trước acceptance; một task auto đã accepted thì đi qua đúng contract này, không biến heartbeat thành cơ chế reclaim mới.

### Các race bắt buộc

- Hủy trước auto admission: intent chuyển canceled/tombstone, không tạo task và không gọi engine/provider.
- Hủy sau acceptance nhưng trước running: giữ current behavior accepted → canceled và release slot trong transaction cancel.
- Hủy khi transcription running: running → stopping; sau khi child chết là canceled; không có generation.
- Hủy sau transcript thành công, trước notes admission: transcript giữ nguyên; notes intent canceled có thể được user retry explicitly.
- Hủy khi notes running: provider có thể đã nhận request, nhưng không có local revision nếu publication fence thấy epoch đã đổi. Transcript vẫn giữ.
- Cancel response mất: GET intent/task theo stable id; không tạo cancel request mới có semantics khác.
- Xóa meeting khi automatic intent waiting: đặt deletion tombstone, đóng intent rồi cleanup; reconciler bỏ qua recording/intent có tombstone. Nếu task đã accepted thì giữ kiểm active task và cancellation hiện có trước cleanup.
- Xóa meeting khi running: giữ hành vi UI an toàn hiện có là báo active task và yêu cầu stop/đợi task dừng; cleanup chỉ chạy khi không còn active lease. Nếu triển khai async delete, marker phải được commit trước và publication phải bị epoch fence.
- Worker publish đua với delete: transaction nào commit tombstone trước thì publication bị từ chối; nếu publication commit trước thì artifact là kết quả hợp lệ và delete transaction xóa nó theo luồng cleanup, không có publication muộn sau khi meeting đã bị xóa.

Không dùng heartbeat hoặc lease expiry làm TTL xóa audio, transcript hay notes, cũng không dùng chúng một mình để reclaim slot/run replacement. Heartbeat chỉ báo health; retention/delete là quyết định riêng, còn recovery cần exclusive lifetime ownership và child drain/reap. Existing deletion transaction đã xóa cả notes sources, revisions, transcript rows, tasks, recording và upload sau khi kiểm active task ([finalize database delete](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/meeting_deletion.py#L218-L312)); intent tombstone phải được thêm vào cùng fence đó.

### Revoke thiết bị: quyền truy cập và work đã nhận

Hiện tại HEAD chưa có credential theo từng thiết bị. AuthStore giữ một token và cookie-signing generation cho cả installation ([auth.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/auth.py#L22-L174)); middleware kiểm session ở mỗi API private ([app.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/app.py#L191-L215)), còn rotate làm session/link cũ mất hiệu lực ([cli.py](https://github.com/nguyencanhtrung/asr-studio/blob/273aaddbae54b06a0f14a602dd7d6ee71123b76f/src/asr_studio/cli.py#L85-L87)). Task row và worker ownership không gắn với cookie/device, nên rotation hiện tại không hủy task queued/running; API cũ chỉ không gọi được nữa. Đây là current behavior, không phải device-revocation contract hoàn chỉnh.

Default đề xuất cho device revoke trong contract extension:

- Credential bị revoke chặn mọi API extension được bảo vệ: session, upload, finalize, status và cancel trong scope đã cấp. Discovery public vẫn không chứa secret; token extension vốn không có server DELETE hay quyền admin. Token extension còn hợp lệ chỉ được dùng các scope đã cấp cho phiên chính thiết bị đó tạo; quyền xóa server và quản trị provider không được suy ra từ token. Hủy xử lý/xóa meeting qua UI operator có xác thực riêng; revoke không tự động thu hồi quyền nội bộ của worker.
- Pipeline intent, task đã accepted, ready archive và publication đã được authorize trước thời điểm revoke tiếp tục chạy/reconcile như work server-owned. Revoke không xóa audio/transcript/notes, không release slot giữa chừng và không biến response publication thành unauthorized chỉ vì cookie cũ hết hạn.
- Nếu mục đích revoke là dừng xử lý do nghi ngờ lộ thiết bị, đó là thao tác thứ hai có xác thực: cancel/delete explicit đặt epoch/tombstone và dùng đúng fence R6. Không gộp “revoke credential” với “cancel mọi work” một cách ngầm định. Session ingest chưa có ready receipt không tạo pipeline intent; chunk/finalize tiếp theo từ thiết bị đã revoke bị từ chối và phần staging tuân contract ingest sibling.
- Revoke device cũng không ngắt provider connection của installation. Provider credential/account bị ngắt là một boundary khác; task đã pin provider vẫn giữ recipe và sẽ báo lỗi/retry theo R7, không tự chọn account khác.

## R7 — Ma trận lỗi và boundary kiểm chứng

| Ca kiểm chứng | Kết quả contract đề xuất | Phân loại |
|---|---|---|
| Finalize lặp cùng id/manifest | Cùng receipt/state; không tạo meeting/task thứ hai | Đề xuất dựa trên khóa ingest sibling; API extension chưa triển khai |
| Finalize lặp khác manifest | 409 conflict; không overwrite archive | Đề xuất |
| Crash trước ready_handoff commit | Receipt ready còn; reconciler tạo meeting + intent; không chạy ASR từ prefix | Đề xuất; cần crash harness |
| Crash sau meeting/intent commit, trước response | Retry theo recording key trả meeting và intent cũ | Đề xuất; cần API test |
| Cancel/delete commit cạnh task admission | Transaction nào commit trước quyết định: admission sau tombstone rollback; cancel sau admission thấy task và chuyển trạng thái | Đề xuất; cần race harness |
| Duplicate task request cùng body | Trả task cũ, không claim slot lần hai | Cơ sở hiện có request_id UNIQUE/hash; auto intent admission mới chưa chạy |
| Duplicate request_id khác recipe/source | 409 idempotency conflict | Cơ sở hiện có hash conflict; recipe pipeline là phần mở rộng |
| Worker bận | Automatic step intent giữ waiting(slot_busy), chưa có task và chưa claim slot; GUI POST vẫn nhận system_busy hiện tại | Đề xuất cho auto; GUI behavior hiện có |
| Worker offline trước admission | Automatic step intent giữ waiting(worker_offline), chưa có task; GUI POST vẫn nhận worker_unavailable hiện tại | Đề xuất cho auto; GUI behavior hiện có |
| Restart trước worker claim | Accepted split-role task tiếp tục theo recovery hiện tại; automatic intent vẫn chờ hoặc được admit lại khi readiness pass | Cơ sở recovery hiện có; auto orchestration mới chưa chạy |
| Crash khi ASR/provider đã gọi nhưng trước publication | Có thể gọi lại; không có generation/revision duplicate nếu chưa commit | Suy luận từ transaction boundary; physical invocation chưa đo |
| Crash sau publication commit nhưng trước response | GET/reconcile thấy generation/revision đã có; unique key ngăn publication thứ hai | Cơ sở hiện có ở publish_complete_generation/publish_notes_revision; orchestration mới chưa chạy |
| Notes provider chưa configured | Transcript vẫn succeed; notes waiting_provider, retry notes từ snapshot | Đề xuất; không gọi provider trong nghiên cứu này |
| Provider timeout/5xx/rate limit | Notes task retryable/backoff; transcript giữ; không retranscribe | Đề xuất; phân loại provider cần prototype |
| Provider quota/content filter/source quá lớn | Terminal hoặc user-action-required tùy code; giữ transcript + source snapshot | Đề xuất; không suy ra từ latency |
| Manual transcript/date edit cạnh notes handoff | SQLite commit order quyết định snapshot pin trước hoặc sau edit; snapshot không bị sửa, run mới sau edit dùng snapshot mới | Cơ sở source_version/metadata_version hiện có; auto handoff chưa triển khai |
| Manual notes edit trước/sau auto publish | Edit version bảo vệ bản manual; auto tạo revision riêng, không overwrite | Cơ sở update_note và notes_revisions.task_id UNIQUE; UI policy cần kiểm chứng |
| Cancel đua publish | Epoch/lease conditional update: không có late publication sau cancel thắng | Đề xuất; race harness chưa chạy |
| Delete đua claim/publish | Tombstone chặn claim; cleanup chờ lease; reconciler không tái tạo | Cơ sở deletion marker hiện có, tombstone processing là mở rộng |
| SQLite busy/lock ngắn | Không chạy inference trong transaction; intent giữ waiting hoặc current accepted task giữ contract, retry sau lỗi giao dịch | Đề xuất; cần đo contention |
| Queue đầy | Meeting/audio vẫn được lưu; status capacity_blocked và thao tác retry rõ ràng | Đề xuất policy, không phải giới hạn máy đã đo |
| Device bị revoke sau khi task accepted | Work server-owned tiếp tục; API mới từ device đó bị 401/403; cancel/delete qua UI operator có xác thực | Current HEAD chỉ có rotation toàn installation; device policy là đề xuất |
| Heartbeat stale/lease hết hạn khi lifetime lock chưa mất | Không clear slot, không requeue và không chạy replacement; chờ exclusive ownership + child drain/reap | Bắt buộc theo lifetime safety; cần fault harness |

Các test hiện có chứng minh các transaction/state của app hiện tại, không chứng minh API extension, crash injection, long recording, quota/eviction, provider response loss hay production reverse proxy. Không dùng các con số config hiện có làm cam kết hiệu năng.

## R8 — Khuyến nghị và bàn giao

### Khuyến nghị cho MVP

1. Thêm durable pipeline_intent, step/outbox và reconciler; bắt đầu từ ready receipt, không từ UI callback.
2. Tách các public helper tự mở transaction khỏi connection-aware primitives; dùng admission fence trong cùng transaction để khi đủ readiness + slot + epoch thì atomically tạo/link task accepted và claim processing_slot như hiện tại.
3. Giữ split transcription/notes service, owner_role, heartbeat và trạng thái restart hiện có; chỉ thêm queue/lease metadata và scheduler fairness.
4. Dùng stable keys/unique constraints ở từng logical boundary; ghi rõ exactly-once publication không phải exactly-once engine/provider call.
5. Tạo transcript trước, rồi notes từ immutable pinned snapshot; notes provider failure/configuration chỉ chặn notes.
6. Giữ web UI/routes/status shape; thêm pipeline waiting reason và một nhánh render nhỏ, không tuyên bố UI hiện tại tự hiểu field mới hoặc biến GUI thành durable queue.
7. Dùng epoch/tombstone cho cancel/delete; heartbeat/lease expiry không đủ để reclaim, chỉ recovery sau exclusive lifetime ownership + child drain/reap. Mã hóa storage và backup vẫn chỉ ghi nhận kế hoạch, chưa triển khai, theo [quyết định vận hành](https://github.com/nguyencanhtrung/asr-studio/issues/211#issuecomment-6881123886).

### Handoff prototype 205

- SQLite fake scheduler với một slot, hai role, auto intent FIFO, auto_accept_and_claim_tx, direct GUI acceptance hiện tại, outbox/reconciler và các unique key ở R3.
- Fake ASR/provider có fault injection tại từng điểm: trước claim, sau claim, sau invocation, trước/sau publication commit, mất response.
- Chứng minh duplicate finalize/create/task/revision, intent waiting busy/offline/model/provider, best-effort GUI opportunity không có hard wait guarantee, notes-only retry, manual edit stale state, cancel/delete race và restart recovery chỉ sau lifetime ownership/child drain.
- Kiểm tra payload additive với web UI hiện tại; không gọi provider thật, không dùng credential/audio thật.

### Handoff long-test 206

- Chrome/OS matrix với tab/mic thật trong môi trường test, stop tail và service-worker/app restart.
- Recording dài và biên quota/eviction/disk reservation; kiểm tra backpressure và không tự xóa bytes chưa có durable receipt.
- Chạy race/crash lặp lại với transcript và notes publication, provider response loss, queue fairness lâu ngày và recovery sau process kill.
- Đo thời gian/CPU/disk thực tế trước khi chốt retry ceiling, queue cap, health/retry timing hoặc bất kỳ SLA nào.

### Bất định còn lại

- API/database orchestration chưa có trong HEAD; mọi queue/intent/epoch ở trên là design proposal.
- Provider không cung cấp guarantee exactly-once mà contract này có thể ép; cần chấp nhận repeated physical calls hoặc thêm provider idempotency key nếu provider hỗ trợ.
- Contract ingest đã merge; identity của ready receipt, normalized artifact và deletion contract phải khớp với doc đó.
- Chưa có bằng chứng production cho 1–4 giờ, 4 GiB, Chrome Stable, quota/eviction, tunnel/proxy hay crash harness. Không suy từ synthetic capture ngắn sang các điều kiện đó.

## Nguồn ngoài repository

[SQLite Transaction](https://www.sqlite.org/lang_transaction.html) xác nhận chỉ có một write transaction tại một thời điểm và BEGIN không lồng; đó là căn cứ cần connection-aware primitive. [SQLite WAL](https://www.sqlite.org/wal.html) giải thích commit/recovery, không thay cho receipt/file fsync của ingest.

Các claim về MediaRecorder chỉ dùng tài liệu chính thức: [W3C MediaStream Recording](https://www.w3.org/TR/mediastream-recording/) quy định khi nhiều Blob do timeslice/requestData thì Blob riêng lẻ không nhất thiết playable, còn tổ hợp của recording hoàn tất phải playable; stop() xếp dataavailable trước stop. [Chrome Storage API](https://developer.chrome.com/docs/extensions/reference/api/storage/) và [Chrome cross-origin network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests/) là nguồn chính thức cho giới hạn/persistence và host permissions của extension. [HTTP idempotency](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2) không tự cung cấp exactly-once processing; tính đúng đắn của flow này nằm ở unique key và transaction local, không ở response HTTP.

## Tách bạch current behavior và đề xuất MVP

| Boundary | HEAD hiện tại | Đề xuất MVP |
|---|---|---|
| Ready → processing | Browser giữ handoff; server chưa có auto pipeline intent durable | Ready receipt/archive tạo meeting + processing/step intents + outbox; reconciler khôi phục từ DB |
| GUI task | POST acceptance kiểm readiness/slot; bận/offline trả lỗi; accepted task claim singleton slot | Giữ nguyên contract này; GUI chưa là durable queue ticket |
| Automatic step | Chưa có server intent sau finalize để tự tiếp tục | Intent chờ trước task acceptance khi busy/offline/provider/capacity; chỉ khi readiness + slot + epoch pass mới accept |
| Acceptance boundary | create_meeting/accept_* tự mở transaction riêng | Thêm connection-aware primitive; một BEGIN IMMEDIATE atomically fence epoch/tombstone, insert/link task accepted và claim slot; không nested transaction |
| ASR/notes publication | Publication hiện có transaction owner/slot fence; response mất có thể cần GET | Thêm intent epoch + durable outbox handoff; repeated physical invocation được phép, publication logic vẫn unique |
| Fairness | Không có durable GUI queue ticket để xếp oldest | Auto intent FIFO bounded; sau 2 auto admissions nhường cửa sổ GUI 1 giây; best effort, không hard GUI wait guarantee |
| Lifetime | Split-role recovery hiện có; heartbeat/lock semantics nằm ở service runtime | Heartbeat chỉ health; không stale-reclaim. Chỉ recovery sau exclusive lifetime ownership và child drain/reap; accepted split-role semantics giữ nguyên |
| UI | UI biết task status/notes state đã định nghĩa, không tự render field mới | Additive pipeline waiting_reason + một nhánh render nhỏ; không biến thành unified task queue |
| Device revoke | HEAD có token/generation toàn installation; rotation làm session/link cũ mất hiệu lực nhưng không hủy task | Device revoke chặn API mới của device; work đã accepted tiếp tục server-owned; cancel/delete explicit qua UI operator có xác thực mới đặt epoch/tombstone |
