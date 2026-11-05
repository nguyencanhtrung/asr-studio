# Quyền riêng tư — hồ sơ thay đổi v0.1.0

Các mục lịch sử được chuyển từ tài liệu thay đổi chung khi chuẩn bị #17.
Số đo giữ revision và điều kiện gốc; không phải phép đo lại artifact 0.1.0.

### #74 / PR #91 — Tắt telemetry ONNX Runtime trong tiến trình con của worker

#### Vấn đề

Worker chạy ASR, diarization và kiểm tra runtime (preflight) trong các tiến trình con. AC-10 yêu cầu khi có mạng chỉ có kết nối của provider đã chọn, nhưng runner diarization mở kết nối tới `*.events.data.microsoft.com`.

#### Nguyên nhân đo được

- onnxruntime 1.31.0 (phụ thuộc của faster-whisper, được pyannote import) nhúng client telemetry với cờ tắt `ORT_DISABLE_TELEMETRY`. Môi trường worker đã đặt các cờ tắt telemetry khác (Hugging Face, pyannote) nhưng không đặt cờ này.
- Runner diarization mở 1–2 kết nối TCP/443 ngắn (khoảng 1 s) khoảng 8 s sau khi diarizing bắt đầu. Thấy ở cả 4 lần chạy hiệu năng của #16 và ở lần xác nhận trên clip 14 phút (2 kết nối, 4.698 byte gửi đi trong 197 s, toàn bộ tới Microsoft); không thấy trong ASR, notes, clip 3 phút (diarization xong sau 8 s) hay khi ngoại tuyến.
- Giới hạn: không kiểm tra nội dung TLS; quy kết dựa trên đích đến, tiến trình sở hữu và mã thư viện.

Nguồn: issue #74 (mục Quan sát).

#### Thay đổi thiết kế

Đặt `ORT_DISABLE_TELEMETRY=1` ở hai chỗ dựng môi trường tiến trình con, cạnh các cờ tắt telemetry sẵn có:

- `worker._safe_runner_environment()`: môi trường của mọi tiến trình con worker (runner ASR, runner diarization, notes, probe CUDA của torch).
- `diarization_runner._set_offline_environment()`: chạy đầu `diarize_file` và preflight `--check-runtime`, trước khi pyannote và onnxruntime được import; gọi trực tiếp `python -m asr_en_vi.diarization_runner` vẫn có biến.

Không vá thư viện; Dockerfile và compose không đổi. Test đổ nếu biến bị bỏ khỏi một trong các đường: runner ASR và diarization do worker khởi chạy, probe CUDA, runner diarization gọi trực tiếp, preflight.

#### Số đo trước và sau

Clip 14 phút (840 s audio), CUDA float16, mạng bật; theo dõi kết nối của container worker suốt tác vụ (lấy mẫu `/proc/net/tcp*` mỗi 0,2 s kèm tiến trình sở hữu socket, cộng bộ ghi SYN/DNS/SNI không giữ payload).

| | Trước sửa (đối chứng, `151f3e4`) | Sau sửa (`b153a25`) |
| --- | --- | --- |
| Diarizing | 115,2 s | 109,0 s |
| Người nói / số đoạn | 5 / 205 | 5 / 187 |
| Kết nối TCP ra ngoài từ worker | TCP/443 từ runner diarization, 7,4 s sau khi diarizing bắt đầu | không có (chỉ một kết nối tự kiểm tra tới app) |
| DNS / SNI | DNS `mobile.events.data.microsoft.com` ×4; SNI cùng tên ×2 | không có |

Nguồn: PR #91 (tiêu chí 2 và 3). Baseline #16 của clip này: 5 người nói, 181 đoạn; số đoạn lệch giữa các lần chạy ASR trên cùng image cũ, không do thay đổi này. Kết nối tự kiểm tra hiện ra ở cả hai bộ theo dõi, nên "không có" là kết quả đo được, không phải bộ theo dõi bị mù.

#### Đánh đổi và phương án đã loại

- Dùng biến môi trường thay vì `disable_telemetry_events()` của onnxruntime: biến đủ chặn theo phép đo trên.
- Sửa ở môi trường tiến trình con thay vì vá thư viện (yêu cầu của issue).
- Runner ASR gọi trực tiếp không tự đặt biến: hiện onnxruntime không được nạp ở đó vì `vad_filter=False`; bật VAD thì cần đặt (#93). Tiến trình ASR do worker khởi chạy vẫn nhận biến.
- Chỉ có một lần đo tay trên một clip; chưa có kiểm tra tự động khi mạng bật, nên telemetry kiểu này có thể tái phát khi đổi phiên bản onnxruntime, pyannote hoặc faster-whisper (#92).

#### Follow-up

- #92: kiểm tra egress khi mạng bật trong `scripts/offline-smoke.py`, fail khi có đích ngoài danh sách cho phép.
- #93: test khóa thứ tự đặt biến trước khi nạp onnxruntime; đặt biến trong runner ASR nếu bật VAD.
- #75: kết nối ngoài provider đã chọn của OMP (catalog, discovery) và danh mục ChatGPT, phần còn lại của AC-10.

#### Liên kết issue/PR

Issue #74, PR #91; liên quan #16 (AC-10), #75, #92, #93.

#### Tag

Quyền riêng tư

#### Tài liệu đích

- `docs/backend-contract.md` mục 3 (đoạn runtime chạy bằng tiến trình riêng: thêm quy tắc môi trường tắt telemetry).
- `docs/acceptance-report.md` mục 5.1, dòng AC-10 (đo lại khi #75 xong).
- Release notes (#17): giới hạn đã biết của AC-10.


### #17 / PR #112 — Nhật ký runtime và startup mặc định an toàn

#### Vấn đề

Log mặc định của worker và startup có thể chứa traceback, stderr nguyên dạng từ tiến trình con,
đường dẫn model/recording và mã task/meeting. Startup không tương tác còn gọi `access show`, nên
token localhost và QR có thể đi vào pipe hoặc log CI.

#### Nguyên nhân đo được

- Runner, diarization runner và worker từng in traceback trực tiếp ra stderr.
- Worker kế thừa stderr của tiến trình con; thư viện có thể làm đầy pipe hoặc ghi nội dung riêng tư
  trực tiếp vào log container.
- CLI và shell startup từng ghép path hoặc exception vào thông báo chuẩn bị model/runtime.

#### Thay đổi thiết kế

- Thêm `safe_logging.emit_safe_log`, chỉ cho phép bộ cố định `event`/`stage`/`code`; exception,
  path, task ID, meeting ID và child output không được nội suy vào log mặc định.
- Worker đọc đồng thời stdout và stderr của child qua pipe, tiếp tục drain stderr để child không bị
  block, rồi chỉ ghi một event `child_stderr_suppressed` an toàn.
- Startup chỉ chạy `access show` trên terminal tương tác. Pipe/redirect/CI không nhận token hoặc QR,
  và nhận lệnh explicit để operator tự gọi khi cần. Access show và task diagnose vẫn là lệnh chủ ý
  in dữ liệu cục bộ.

#### Số đo trước và sau

Trước: 8 điểm traceback trong ba tiến trình và một điểm kế thừa child stderr trong phạm vi log;
startup luôn gọi access show kể cả khi stdout không phải TTY. Sau: 134/134 focused regression tests
đạt; sentinel exception/child-output không xuất hiện ở stderr mặc định, và test startup non-TTY xác
nhận access show không được gọi.

#### Đánh đổi và phương án đã loại

- Log mặc định mất chi tiết vendor để bảo vệ dữ liệu; operator dùng mã/stage cố định, `runtime check
  --verbose`, `access show` hoặc `task diagnose` theo chủ ý.
- Chọn pipe + drain thay vì `DEVNULL` để giữ tín hiệu có child stderr mà không làm nghẽn lifecycle;
  không lưu nội dung stderr.
- Không xây subsystem debug mới và không thay đổi schema lỗi public hoặc semantics hủy task.

#### Follow-up

- Khi phát hành, kiểm tra lại log mặc định và lệnh chẩn đoán explicit.
- Khi phát hành, giữ hướng dẫn access-show explicit trong tài liệu vận hành.

#### Liên kết issue/PR

Issue #17; PR #112.

#### Tag

Quyền riêng tư; Kiến trúc

#### Tài liệu đích

- `docs/user-guide.md` mục Run locally.
- Release notes (#17) cho giới hạn log mặc định và lệnh chẩn đoán explicit.
