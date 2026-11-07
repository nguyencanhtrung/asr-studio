---
status: accepted
---

# Tách notes thành service có mạng và giữ một slot xử lý chung

Issue [#166](https://github.com/nguyencanhtrung/asr-studio/issues/166) tách việc gọi
provider notes khỏi worker chạy ASR/diarization. Mục tiêu là worker xử lý model local
không cần mount provider và chỉ ở mạng nội bộ, trong khi notes service vẫn có thể gọi
provider. Quyết định này bổ sung cho [ADR 0004](0004-app-worker-sqlite.md) và
[ADR 0012](0012-model-installer-network-boundary.md); nó không biến SQLite thành hàng
đợi nhiều tác vụ.

## Quyết định

Compose chạy app, worker transcription, notes service và model-installer từ cùng image
([`compose.yaml`](../../compose.yaml)). Worker dùng mạng `internal`, không mount volume
provider. Notes service mount provider và data, không mount model, không cấp GPU và dùng
mạng mặc định để gọi provider. Lệnh chuẩn bị model dùng override có mạng mặc định để
model hub egress; đó là một process one-shot, không phải quyền egress của worker đang
chạy ([`compose.model-prepare.yaml`](../../compose.model-prepare.yaml)).

Worker transcription và notes service có heartbeat/lifetime lock riêng trong
[`service_runtime.py`](../../src/asr_studio/service_runtime.py). Worker legacy giữ
exclusive `.worker.lock` và shared probe trên `.notes-service.lock`; notes service giữ
exclusive notes lock. Vì vậy legacy combined worker không khởi động khi split role đang
sống, và split transcription worker không khởi động khi legacy worker đang sống. Một
legacy process trước khi có probe vẫn được bảo vệ: notes service kiểm tra worker lock
và coi heartbeat cũ/không gắn role là legacy trước khi recovery. Heartbeat dùng cho
readiness; lock là bằng chứng overlap/liveness trong các guard này
([`worker.py`](../../src/asr_studio/worker.py), [`notes_service.py`](../../src/asr_studio/notes_service.py)).

Không tạo slot riêng cho từng role. `processing_slot` hiện có tiếp tục là nguồn sự thật
cho task active; claim dùng một giao dịch `BEGIN IMMEDIATE`, kiểm tra kind và
`owner_role`, rồi chỉ clear slot nếu ID đang clear đúng là active ID
([`tasks.py`](../../src/asr_studio/tasks.py)). Executor process-group, heartbeat khi
runner chạy và reaper dùng chung của worker cũng được tái sử dụng cho notes task; tách
service không tạo đường hủy thứ hai.

## Schema, task và rollback

Schema hiện hành là v16 và migration v16 thêm `tasks.owner_role`, với giá trị mặc định
`legacy` để đọc được các row trước khi tách ([`storage.py`](../../src/asr_studio/storage.py)).
Các task mới ghi role cụ thể; split role được phép claim task cùng kind còn mang
`legacy`, nhưng chỉ sau khi lock cho thấy legacy không còn sống. Giá trị DB này là
compatibility marker, không thay thế lifetime lock.

Pending semantics được giữ rõ ràng:

- `accepted` là task đang chờ claim. Split service không biến nó thành `interrupted`
  khi startup; nó giữ `processing_slot.active_task_id`, rồi claim atomically nếu kind
  và role phù hợp. Điều này cho phép notes task cũ mặc định `legacy` tiếp tục chạy.
- `running` của role đang khởi động lại được đánh dấu `interrupted`; `stopping` được
  đánh dấu `canceled`. Recovery chỉ xét kind/owner role của service và chỉ nhường slot
  khi ID trong slot thuộc tập task đã xử lý. Task của role đang sống không bị đụng tới.
- Nếu notes service đã thấy một legacy worker cũ không có notes probe, nó chờ worker lock
  nhả rồi mới recovery ở vòng lặp tiếp theo; không dùng heartbeat hết hạn một mình để
  kết luận process đã dừng.
- Legacy helper vẫn giữ semantics cũ cho caller cũ, nhưng production legacy worker chỉ
  gọi recovery sau khi đã giữ cả worker lock và notes reservation. Split startup giữ
  worker lock (transcription) hoặc kiểm tra worker lock legacy (notes) trước recovery;
  unit test [`test_service_isolation.py`](../../tests/test_service_isolation.py) khóa
  các overlap này.

Upgrade được thực hiện bởi `initialize()` trong transaction và ghi migration v16 sau
khi thêm cột. Không sửa tay `owner_role` hoặc xóa migration record để rollback. Bản
 application cũ thấy schema mới hơn và dừng với lỗi `Database schema is newer than this
 application` ([`storage.py`](../../src/asr_studio/storage.py)). Rollback an toàn là
 dừng Compose, giữ bản backup của database/data trước upgrade và khôi phục bản backup
 rồi chạy bản cũ; nếu không có backup thì phải chạy forward-compatible application để
 export/migrate dữ liệu trước khi chọn phương án khác. Audio có thể vẫn nằm trong data
 volume, nhưng database và task slot phải được khôi phục cùng phiên bản.

## Giới hạn riêng tư và phương án loại

Data volume hiện chứa SQLite, recording files, transcript và publication layout, nên
notes service có thể đọc recording files ở cấp filesystem vì nó mount cùng volume
([`compose.yaml`](../../compose.yaml), [`recordings.py`](../../src/asr_studio/recordings.py)).
Đây không phải filesystem isolation cho audio và không được mô tả như vậy. Luồng notes
chỉ đưa transcript/source cần thiết cho provider theo adapter hiện có; muốn chặn khả
năng đọc audio thật sự phải tách layout recording/database hoặc thêm volume/service
boundary khác, chưa chọn trong issue này.

Giữ notes trong worker bị loại vì worker khi đó vẫn cần provider mount và egress. Tạo
database hoặc processing slot riêng cho notes bị loại vì phá invariant một task xử lý
toàn hệ thống và làm recovery hai nguồn sự thật. Runtime disposable-stack, provider và
egress checks là phần root sở hữu; các unit/contract test ở đây không chứng minh chúng.
