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

Worker legacy giữ exclusive `.worker.lock`. Worker transcription giữ exclusive
`.transcription.lock`; notes service giữ exclusive `.notes-service.lock`. Cả hai split
service giữ shared `.worker.lock` trong toàn bộ lifetime. Vì vậy legacy combined worker
không khởi động khi split role đang sống, và split role không khởi động khi legacy worker
đang sống. Các descriptor của role và shared gate được truyền cho child executor
([`service_runtime.py`](../../src/asr_studio/service_runtime.py),
[`worker.py`](../../src/asr_studio/worker.py),
[`notes_service.py`](../../src/asr_studio/notes_service.py)).

Startup chỉ recovery sau khi giữ đủ lock. Heartbeat chỉ phục vụ readiness. Role marker và
heartbeat không thay thế lifetime lock khi quyết định process còn sống.

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
- Legacy helper vẫn giữ semantics cũ cho caller cũ. Production legacy worker chỉ gọi
  recovery sau khi giữ worker lock. Split startup giữ role lock và shared worker gate
  trước recovery; unit test [`test_service_isolation.py`](../../tests/test_service_isolation.py)
  khóa các overlap này.

Role migration không đổi hợp đồng prompt đã lưu trên task. Khi nâng cấp đồng thời sang
`notes-hierarchical-v18`, task Notes đã nhận bằng prompt cũ có thể kết thúc với
`notes_contract_changed`; không được mô tả kiểm tra role-only như bằng chứng tự chạy lại
hợp đồng prompt cũ. Hoàn tất hàng đợi trước upgrade, hoặc dùng GUI tạo lại Notes từ transcript
đã giữ sau upgrade. Guard này tránh ghi phiên bản prompt cũ cho kết quả dùng prompt mới
([`notes_runner.py`](../../src/asr_studio/notes_runner.py)).

Notes audit ở prompt v18 chỉ giữ các đường đã có bằng chứng cho nguồn đơn: ChatGPT hợp nhất
lại schema 1, còn Antigravity chọn tham chiếu từ catalog. Reference guard của Antigravity chặn mã
lạ, đổi nhóm, lặp origin và metadata mới; runtime giải mã quote và thêm nhãn. ChatGPT có thể viết
lại prose và phải qua validator/source review. Cả hai dùng cùng provider/model đã chọn và tối đa
một logical audit với repair/recovery chung. Không thêm audit sau final phân cấp vì các thử nghiệm
chưa đạt lợi ích chất lượng; coverage ghi `quality_not_qualified`. Không dùng dữ liệu/gold để chọn
đường chạy. Những phép thử model cũ được giữ cùng các lần không đạt; replay không được mô tả là
inference hoặc latency mới. Độ đầy đủ nội dung vẫn còn follow-up #79; #88 giữ nhu cầu audio/người
duyệt. Thay đổi này không sửa primary prompt/schema/model/caps hay adapter ngoài boundary.

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
egress checks là các acceptance check riêng; các unit/contract test ở đây không chứng
minh chúng.
