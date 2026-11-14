# Nghiên cứu contract ingest resumable cho Chrome extension

Ngày: 2026-11-15 · [Thiết kế phiên ghi và upload phục hồi được](https://github.com/nguyencanhtrung/asr-studio/issues/203) · Đề xuất contract, chưa triển khai API

## Kết luận thiết kế

MVP nên có namespace riêng api/extension/v1 với một recording session biết backend, device và endpoint ngay lúc tạo. Session nhận các phần byte có thứ tự, nhưng chỉ coi phần đó là transport chunk; không probe, remux hay chạy ASR từng Blob. Per-chunk durable ACK không bao giờ cho phép xóa bytes local: client giữ toàn bộ bytes và metadata qua mọi ACK, retry và Cancel. Chỉ full-session durable receipt mới cho phép tự động dọn client buffer; Cancel một mình không loại bỏ bytes local, trừ khi có quyết định discard/delete riêng được user hoặc operator xác nhận. Finalize là một job bất đồng bộ có manifest bất biến; server chỉ công bố archive khi đủ chuỗi phần, đủ byte và đủ digest, sau đó mới validation/normalization có giới hạn.

Audio server đã hoàn tất phải giữ tới khi operator hoặc user gọi explicit delete. Không kế thừa TTL dọn stale-upload hiện tại cho recording session mới, không tự chọn chính sách 7 hoặc 30 ngày. Incomplete staging phải được hiển thị là chưa hoàn tất, có bounded admission/backpressure, và chờ quyết định cancel/delete rõ ràng hoặc retention policy tương lai được phê duyệt. Live consumer là đường nâng cấp tách biệt, không tạo SLA latency, không chọn engine và không làm thay đổi tính toàn vẹn archive.

## Bằng chứng hiện trạng và điều MediaRecorder thực sự hứa

Đường upload hiện tại là known-size: POST /api/uploads bắt buộc size_bytes dương; begin_upload khóa writer và kiểm tra offset; route fsync file rồi mới cập nhật offset SQLite. complete_upload chuyển validating, normalize trong worker thread của app, không phải worker inference, fsync/rename/fsync directory rồi mới ready; raw archive bị unlink sau đó. Xem [recordings.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/recordings.py#L162-L304), [routes.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/routes.py#L706-L907), và [commit media](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/recordings.py#L390-L480). Defaults hiện tại là 4 GiB, 4 giờ và chunk 64 MiB trong [config.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/config.py#L10-L18); đây là cấu hình, không phải phép đo khả năng.

Startup hiện tại đánh dấu receiving/uploaded/validating/ready là interrupted rồi dọn raw/temp không consumed ([recordings.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/recordings.py#L840-L882)). Ingest mới không được bê nguyên hành vi này: crash phải để lại session và receipt để resume hoặc explicit cancel/delete, không âm thầm biến bytes có thể khôi phục thành mất dữ liệu. SQLite hiện bật WAL, synchronous=FULL, foreign keys và busy timeout 5 giây ([storage.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/storage.py#L569-L699)).

W3C quy định timeslice/requestData tạo nhiều Blob nhưng Blob riêng lẻ không cần playable; chỉ tổ hợp completed recording mới bắt buộc playable. stop() queue dataavailable rồi mới stop, nên listener stop không phải persistence barrier. Code phải await Blob cuối và local write trước finalize. Xem [W3C MediaStream Recording](https://www.w3.org/TR/mediastream-recording/#mediarecorder-methods).

Bằng chứng capture: Chromium 156.0.8078.4, MIME audio/webm;codecs=opus, 11 Blob, 52.485 byte, nguồn oscillator synthetic chứ không phải tabCapture/microphone. Bản full thiếu duration bị probe invalid_media nhưng remux/normalize được 3,24 giây; bỏ Blob cuối vẫn được 2,94 giây; Blob đầu được 0,242 giây; Blob thứ hai riêng ffprobe lỗi. Remux decode prefix không chứng minh đủ session. Đây không phải nghiệm thu Chrome Stable, tab/mic, recording dài, 4 giờ/4 GiB hay production; short remux không phải hardening untrusted media.

Nguồn số liệu ở đoạn trên là output JSON của harness MediaRecorder/FFmpeg, tóm lược nguyên giá trị dưới đây. Cách tái tạo: profile Chromium cô lập; AudioContext sampleRate 48000, oscillator 440 Hz qua gain 0.15 vào MediaStreamDestination; MediaRecorder yêu cầu 128000 bit/s và timeslice 250 ms, Stop sau timer 3250 ms, đợi stop rồi ghép mọi Blob theo thứ tự. So sánh bản đầy đủ, bỏ Blob cuối, Blob đầu/Blob thứ hai; chạy ffprobe, `probe_media`, rồi remux copy audio (`-map 0:a:0 -vn -c:a copy -protocol_whitelist file,pipe`) với timeout 15 giây cho mẫu ngắn trước `normalize_media`. Các timer/bitrate là tham số fixture, không phải duration đo hoặc tốc độ cam kết.

<details>
<summary>Kết quả synthetic đã ghi nhận</summary>

```json
{
  "browser": "156.0.8078.4",
  "mime": "audio/webm;codecs=opus",
  "blob_count": 11,
  "bytes": 52485,
  "sha256": "d1ff1eb8aac4e1483c3b7671e9ab83acdd3aa508b4728fb100f76894b2b45978",
  "ffmpeg": "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers",
  "cases": [
    {
      "file": "finalized.webm",
      "probe": {
        "status": "rejected",
        "code": "invalid_media"
      },
      "normalized_after_remux": {
        "status": "pass",
        "duration": 3.24
      }
    },
    {
      "file": "without-last-blob.webm",
      "probe": {
        "status": "rejected",
        "code": "invalid_media"
      },
      "normalized_after_remux": {
        "status": "pass",
        "duration": 2.94
      }
    },
    {
      "file": "first-blob.webm",
      "probe": {
        "status": "rejected",
        "code": "invalid_media"
      },
      "normalized_after_remux": {
        "status": "pass",
        "duration": 0.242
      }
    },
    {
      "file": "second-blob.webm",
      "probe": {
        "status": "rejected",
        "code": "invalid_media"
      },
      "normalized_after_remux": null
    }
  ],
  "short_limit_check": {
    "status": "rejected",
    "code": "duration_limit",
    "limit_seconds": 3
  }
}
```

</details>

## API và schema đề xuất

Tất cả endpoint protected dùng session token. GET có thể không có Origin nhưng vẫn bắt buộc token và scope; unsafe method yêu cầu exact extension Origin. Không dùng cookie UI, không nhét token vào URL và không redirect bearer/audio.

1. POST /api/extension/v1/recording-sessions tạo session với protocol, client_recording_id bất biến đã lưu local, MIME/codec từ recorder.mimeType, sample rate, channels, source tab/mic, mixing single-track và metadata. Server lấy owner từ token, không tin device_id body. Response 201 trả recording_id, backend_identity (backend_id, normalized origin/base path, api_version, fingerprint), device, receiving, next sequence/offset, limits, writer lease và digest algorithm.
2. PUT /api/extension/v1/recording-sessions/{id}/chunks/{sequence} nhận application/octet-stream với offset, length và SHA-256. Fetch nhận Blob có kích thước cố định để browser đặt Content-Length; JavaScript không tự đặt header bị cấm này. Chỉ nhận sequence kế tiếp/offset đúng. ACK sau staged file fsync và DB row/offset commit; replay cùng digest trả ACK cũ, khác digest/offset là conflict, không overwrite. Một session có một writer, nhận epoch/If-Match từ status để chặn request cũ sau hủy hoặc phục hồi.
3. GET /api/extension/v1/recording-sessions/{id} trả identity, state, next sequence/offset, durable bytes, manifest rows phân trang, writer lease, finalize intent, archive receipt và lỗi; đây là nguồn sự thật sau timeout.
4. POST /api/extension/v1/recording-sessions/{id}/finalize nhận finalize_id, capture_epoch, `capture_complete: true`, final sequence, total bytes/count và ordered manifest. Normal finalize từ chối nếu chưa có marker Stop; không tự coi prefix do crash là full recording. Server kiểm sequence, offset, tổng byte và digest, ghi manifest immutable, trả 202 với state `archiving`. Server phải recompute từng stored chunk digest và so với digest đã khai báo, recompute/canonicalize ordered manifest rồi so `manifest_hash` với giá trị đã khai báo, đồng thời stream toàn bộ archive để tính `archive_sha256` riêng. Chỉ khi cả hai kiểm tra hoàn tất mới phát `archive_durable`, kèm `manifest_hash` và `archive_sha256` như hai giá trị khác nhau, để client dọn buffer; không bao giờ so sánh hai hash này như bằng nhau. State `finalizing` tiếp tục tới ready. Cùng intent retry idempotent, manifest khác 409.
5. POST /api/extension/v1/recording-sessions/{id}/cancel ghi cancel_requested và tăng epoch bền vững trước khi trả 202; sau khi writer dừng thì GET trả canceled. Lặp lại cancel trả cùng kết quả. Cancel không xóa audio server; DELETE explicit mới cleanup. Cancel ACK cũng không tự cho phép xóa client buffer: buffer chỉ được dọn sau full-session durable receipt, hoặc sau một thao tác local discard riêng do user xác nhận.

Create có UNIQUE(owner_device, client_recording_id), so fingerprint canonical của version/codec/metadata/endpoint: cùng request trả cùng session, nội dung khác trả 409. Fingerprint và manifest đều có version. Manifest canonical đề xuất là UTF-8 `v1\n`, tiếp theo từng dòng `sequence:offset:length:sha256\n`, số nguyên thập phân không leading zero và digest hex lowercase. Bounded metadata đủ nhỏ để hash trong browser; không ghép toàn bộ audio trong RAM. Finalize id và manifest được lưu trong cùng transaction, server không tạo intent thứ hai khi retry. Thiếu/sai/hết hạn token là 401; scope sai 403; tài nguyên ngoài owner là 404; sequence/epoch/idempotency sai là 409; vượt byte/count cap là 413; media lỗi 422; thiếu disk reservation 507; 429 có Retry-After. Client không retry 4xx terminal mù.

Scope `recording:cancel` được bổ sung cho session chính thiết bị tạo; không cấp quyền xóa cả thư viện. Xóa artifact server thuộc thao tác operator trong UI với cookie/same-origin guard riêng, không dùng scope token extension để gọi admin. Session đã gắn meeting phải đi qua luồng xóa meeting để không để lại transcript/notes mồ côi. Discard local là quyết định riêng, không được hiểu là xóa server.

Server tách recording_sessions, recording_chunks và recording_finalize_intents. Session lưu owner/device, backend identity, version, state/epoch, source, limits, writer lease, next sequence/offset, artifact keys, retention disposition và error. Chunk có khóa (session_id, sequence), offset, length, digest, staged key, durable time. Finalize intent lưu `manifest_hash`, `total bytes/count`, idempotency key, epoch và `archive_sha256` riêng; không dùng một hash thay cho hash kia. IndexedDB có sessions/chunks/outbox; token không nằm trong chunk store.

## State machine, ownership và epoch fence

State hợp lệ là receiving → archiving → archived → finalizing → ready. Archived nghĩa là raw archive đầy đủ đã durable, từng chunk digest và ordered manifest đã đối chiếu, full archive hash đã tính xong, và full-session durable receipt đã cấp; đây là mốc được phép dọn client buffer, không phải mốc normalized M4A sẵn sàng. Receiving/archiving/archived/finalizing có thể đi tới cancel_requested rồi canceled; failed giữ rõ archive_available hoặc staging_available. Ready và archived audio vẫn retained tới explicit DELETE. Incomplete staging không có TTL tự động trong contract này.

Mọi append/finalize worker ghi epoch hiện tại vào request. Cancel thực hiện transaction có khóa: kiểm tra state, tăng epoch, ghi cancel_requested và ngăn writer mới. Khi worker chuẩn bị commit raw archive, normalized output hoặc ready, nó phải conditional-update với state phù hợp và epoch cũ. Nếu epoch đã tăng, worker được phép dọn temp của chính nó nhưng không được publish; session giữ trạng thái canceled/failed và receipt hiện có. Nếu cancel commit trước, finalize late bị từ chối. Nếu archive receipt đã commit trước, receipt đó vẫn hợp lệ và bất biến; Cancel ở archived/finalizing chỉ ngăn phần xử lý media còn lại, giữ raw archive và receipt. Receipt giữ epoch tại thời điểm archive commit, không bị sửa theo epoch hủy mới. Nếu ready đã commit, cancel session trả 409 too_late; hủy transcript/notes dùng contract xử lý phía sau. Xóa audio luôn là thao tác DELETE riêng. Như vậy cancel/finalize race không thể công bố muộn dữ liệu đã hủy.

[Contract token](chrome-extension-device-token-research.md) đề xuất pairing một lần, device token 30 ngày và session token 15 phút; đây là design defaults, không phải số đo. Scope là recording:create/upload/resume/finalize/status:read và cancel cho session của chính thiết bị; transcript:read/notes:read chỉ cho recording của device khi 204 chốt. Không có provider/admin/filesystem/notes mutation. Session hết hạn dùng device token cấp lại session cùng endpoint/identity rồi GET status; device token expired/revoked thì giữ buffer, dừng retry và pair lại.

backend_id + normalized URL gồm base path + api_version là identity. Đổi Server URL phải pause và capabilities-check/pair hoặc re-bind có xác nhận. Localhost và tunnel không được coi là alias tự động, kể cả người dùng nghĩ cùng instance; không auto-failover, không nhận ACK của server khác. CORS/Origin không thay authentication: host permission chỉ cấp khả năng fetch. Điều này bám [contract token](chrome-extension-device-token-research.md) và [Chrome cross-origin requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests).

Đừng bắt browser gom 4 GiB để gọi crypto.subtle.digest. Web Crypto chỉ định digest nhận một BufferSource và trả ArrayBuffer, không có update/finalize incremental trong API [Web Cryptography Level 2](https://www.w3.org/TR/webcrypto/#SubtleCrypto-method-digest). Client digest từng chunk và có thể tính ordered manifest hash trên metadata; server phải recompute/compare từng stored chunk digest, recompute/canonicalize ordered manifest rồi so `manifest_hash` với giá trị đã khai báo, sau đó stream bytes theo thứ tự để tính `archive_sha256` của full archive. `manifest_hash` và `archive_sha256` là hai hash khác mục đích, không bao giờ được so sánh như bằng nhau. `archive_durable` receipt gồm byte/count, `manifest_hash`, `archive_sha256`, epoch và source manifest; ready thêm normalized digest, decoded duration, codec, validation version.

## Crash, durability, retention và lưu trữ

Client copy mỗi dataavailable vào chunk giới hạn, digest, transaction IndexedDB ghi bytes/metadata rồi mới enqueue upload. Stop gọi recorder.stop, await Blob cuối và write queue, ghi final sequence/bytes local, rồi POST finalize. Local durable và server durable là hai mốc khác nhau. Handler xử lý từng Blob bằng slice có giới hạn và đợi transaction complete; không `arrayBuffer()` cả archive. Stop phải ghi marker `capture_complete` cùng final sequence/bytes sau barrier. Nếu hết chỗ khi ghi tail, báo incomplete và giữ phần đã lưu; không giả lập marker hoàn chỉnh.

Resume upload chỉ gửi lại bytes đã persist. Nếu Chrome crash trước marker Stop, client báo capture interrupted/needs_attention; không tự finalize như cuộc họp đầy đủ. Recorder mới tạo capture_epoch/session mới, không ghép một WebM stream mới vào archive cũ. Phục hồi/export prefix phải hiển thị rõ là bản gián đoạn và được người dùng chọn; không tự chạy transcript/notes như phiên hoàn chỉnh. Đây là biên prototype cần kiểm chứng, không cam kết ghi xuyên thời gian Chrome không chạy.

Writer lease chỉ điều phối tác vụ, không phải thời hạn xóa dữ liệu. Thu hồi lease tăng epoch; tên temp riêng cho từng writer và conditional commit ngăn writer cũ ghi đè. Không giữ SQLite transaction trong lúc đọc network, assemble hoặc chạy FFmpeg.

Server kiểm token/identity/state/sequence, ghi staged chunk, fsync file/directory, commit row/offset với synchronous FULL rồi mới ACK. Crash sau fsync trước DB commit đưa file vào quarantine đúng session để đối chiếu khi retry/operator kiểm tra; nếu DB ghi đã durable mà file thiếu/corrupt, đánh dấu storage_corrupt, không replay ACK thành công; crash sau DB commit trước response được replay bằng digest. Raw đã fsync nhưng chưa ready thì giữ raw và finalize intent.

Finalize stream staged chunks theo thứ tự vào raw temp; trước khi publish phải recompute/compare digest của từng stored chunk, recompute/canonicalize ordered manifest và so `manifest_hash` với giá trị đã khai báo, rồi tính `archive_sha256` riêng trên toàn bộ bytes. Sau raw fsync/rename/directory-fsync, một transaction conditional theo epoch phải commit archive key, hashes, immutable receipt và pending media-finalization job. Chỉ sau commit này mới trả `archive_durable` và dọn staged chunks; không so sánh `manifest_hash` với `archive_sha256`. Validation/remux/normalize dùng temp riêng, byte/duration/CPU/wall-clock/stdout/stderr/process limits; output fsync/rename/directory-fsync rồi conditional-update `finalizing` → `ready`. Raw, normalized và temp có thể cùng tồn tại nên phải reserve disk; durable không phải encrypted backup. Mã hóa storage/backup chỉ nằm trong kế hoạch theo [quyết định vận hành](https://github.com/nguyencanhtrung/asr-studio/issues/211#issuecomment-6881123886).

Completed server audio retention là tới explicit delete, không áp stale-upload TTL hiện tại và không invent approved 7/30-day cleanup. Incomplete staging cũng không bị dọn theo tuổi trong contract mới. Operator UI phải hiển thị session id, owner/device, endpoint identity, state, durable bytes, staging/archive availability, last seen và lý do lỗi; cho phép Cancel và Delete riêng. Cancel bảo toàn artifact để phục hồi hoặc quyết định tiếp; Delete là hành động phá hủy có xác nhận, chạy theo epoch/state fence và báo kết quả. Nếu sau này cần retention tự động, đó là quyết định vận hành riêng với số ngày, owner, grace period và kiểm chứng; không dùng trong MVP hiện tại.

Bounded admission ngăn retention làm đầy đĩa: create reserve logical budget và disk headroom, thiếu thì trả 507/policy error, không tạo session mồ côi. Append kiểm max chunk/session/free reservation. Khi local pending gần cap, extension pause capture hoặc Stop có kiểm soát và báo chưa hoàn tất; không hứa ghi vô hạn offline. Không overwrite chunk ACK hoặc chuyển endpoint để né quota; backpressure phải giữ bytes đã capture và cho retry/cancel.

Thiết kế defaults cần công bố, không phải capability đo được: append tối đa 8 MiB/request; một writer/session; toàn bộ client buffer retained tối đa 512 MiB, kể cả phần server đã ACK; session tối đa 4 GiB và decoded duration tối đa 4 giờ; normalized output tối đa 512 MiB; append idle timeout 30 giây. Hai giới hạn 4 GiB/4 giờ khởi đầu theo config hiện tại nhưng không hứa mọi recording đúng 4 giờ đều qua. Reservation dùng byte cap đã thương lượng cho từng session (không luôn reserve mức tối đa toàn server). Công thức peak là 2 × raw session cap + 2 × normalized output cap + 64 MiB metadata; với các defaults trên là khoảng 9,0625 GiB, chỉ là phép tính thiết kế tại mức server tối đa, không phải dung lượng đo hoặc bắt buộc cho mọi phiên nhỏ. Hết quota trả lỗi rõ ràng, giữ session và không silently drop; quá byte/duration trả 413 trước hoặc trong finalize. Probe/normalize phải có timeout và disk guard riêng; các hằng số hiện tại PROBE_TIMEOUT_SECONDS=60, COMMAND_TIMEOUT_SECONDS=6*60*60, stderr 8 KiB và probe JSON 1 MiB trong [media.py](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/src/asr_studio/media.py#L15-L34) chỉ là baseline source, chưa là untrusted-ingest acceptance.

Vì giữ toàn bộ bytes đến receipt toàn phiên, giới hạn recording hiệu dụng là min(server session cap, client retained cap), cả khi mạng tốt; với default này client chạm 512 MiB trước 4 GiB. UI phải hiển thị giới hạn hiệu dụng và warning/Stop sớm có phần dự phòng cho Blob cuối. Không hứa 4 giờ chỉ từ bitrate yêu cầu: timeslice có thể trễ, codec có padding và decoded duration vẫn bị kiểm tra. Margin Stop phải được đo trong bài thử dài; không nới/bỏ validation để file vừa biên vượt qua.

Bổ sung các giới hạn thiết kế để đo ở prototype: tối đa 65.536 chunk/session, manifest request 16 MiB, tối đa hai writer request đang chạy toàn server, một job media-finalization toàn server và headroom cho OS. Cap nhỏ hơn theo chính sách/proxy phải được capabilities trả trước khi ghi. Với công thức peak reserve ở trên, cleanup staged chunks chỉ bắt đầu sau receipt transaction; hoàn tất cleanup trước khi tạo remux copy có thể lớn bằng raw. Nếu không bảo đảm thứ tự này phải reserve thêm một raw copy. Giữ raw archive đến explicit delete; M4A là derivative, không lặng lẽ thay raw bằng file đã re-encode.

Giới hạn media đề xuất cho prototype, chưa chứng minh đủ cho máy thực: ffprobe 60 giây wall; remux 300 giây wall/CPU; normalization 1.800 giây wall/CPU; một subprocess tại một thời điểm, process group có thể terminate/kill và output file-size cap. Chỉ nhận WebM/Opus single-audio theo negotiated MIME cho MVP; validate header/codec, ép demuxer phù hợp và protocol allowlist chỉ file/pipe, không cho remote media references. Giữ stdout/stderr caps từ baseline. Decode tới `max_duration + 1` rồi kiểm duration thực; vượt cap phải reject, không cắt xuống max rồi tuyên bố đủ. Timeout/limit/corruption giữ raw receipt và error để người dùng retry hoặc xóa; không tự chạy lại vô hạn. Các thông số này là đề xuất đo, không phải hardening đã triển khai.

IndexedDB phù hợp cho bytes lớn hơn chrome.storage; Chrome ghi rõ IndexedDB dùng được trong service worker nhưng quota/eviction vẫn có, còn storage.local hiện có giới hạn 10 MB nếu không xin unlimitedStorage. unlimitedStorage hoặc persist không tạo thêm dung lượng đĩa và không mã hóa. Token dài hạn dùng storage.local với access level trusted contexts theo token contract; session token chỉ memory; không dùng storage.sync. Xem [Chrome Storage API](https://developer.chrome.com/docs/extensions/reference/api/storage/) và [Storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies).

## HTTP, loopback và tunnel

Unknown total không cần request HTTP mở vô hạn. Mỗi append nhỏ, có Content-Length và idempotency sequence/digest; HTTP chunked chỉ là framing, không phải MediaRecorder chunk contract. RFC 9112 cho phép body chưa biết size nhưng gateway có thể yêu cầu Content-Length; RFC 9110 coi PUT idempotent và cho retry khi response mất. Xem [RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html#section-6) và [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2).

Local loopback đi thẳng app nhưng vẫn chịu app idle timeout hiện tại 60 giây và disk/CPU local. Remote HTTPS qua proxy/tunnel có thêm giới hạn. Official Cloudflare connection limits hiện liệt kê Proxy Read Timeout 125 giây trả 524 và Proxy Write Timeout 30 giây trả 524; URL tối đa 16 KB và tổng request headers 128 KB. Cloudflare Cache docs hiện liệt kê maximum upload size 100 MB cho Free/Pro, 200 MB Business và tới 5 GB Enterprise, đồng thời khuyên chia request lớn thành chunk nhỏ. Đây là giá trị tài liệu Cloudflare hiện tại, không phải config deployment ASR đã audit: [connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/), [cache upload limits](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/).

Tunnel có layer riêng giữa cloudflared và origin; docs nói hai layer cùng áp dụng thì timeout ngắn hơn thắng, còn connect/TLS/Host Header/disableChunkedEncoding phải kiểm tra config thực tế. Chưa audit deployment, plan, reverse proxy, buffering hay throughput nên không hứa 4 GiB một connection. [Origin parameters](https://developers.cloudflare.com/tunnel/reference/origin-parameters/) là nguồn chính. 8 MiB chỉ là design default; capabilities phải trả max chunk/deadline và finalize luôn 202 async.

Phép thử mạng riêng dùng Chromium 156.0.8078.4, MV3 profile tạm và server loopback AF_INET6: install-time host permission `http://[::1]/*` cho phép GET/POST từ extension page và service worker; GET thiếu Origin, POST có extension Origin, redirect:error không gọi đích redirect. Đây là bổ sung cho phép thử IPv4 trong [báo cáo token](chrome-extension-device-token-research.md), không chứng minh runtime permission prompt hoặc ASR API IPv6 hiện tại. Backend hiện chỉ nhận HTTP localhost/127.0.0.1; hỗ trợ IPv6 vẫn phải bổ sung.

## Retry, cancel và media finalize

Retry network/408/5xx/429 hoặc mất response: GET status trước; nếu next sequence vẫn là chunk đó thì gửi lại cùng bytes/digest. Design retry schedule có thể kế thừa chính sách hiện tại sáu lần với backoff 1, 2, 4, 8, 16, 32 giây trong [backend contract](https://github.com/nguyencanhtrung/asr-studio/blob/a903214758e90c31c8b61e4d94d46419cb43b31d/docs/backend-contract.md#L120-L146), nhưng đây là policy retry, không đo độ tin cậy mạng. Không retry mù finalize bằng intent mới. Nếu cancel response mất, GET; canceled chỉ dừng server pipeline, không xóa local buffer tự động; audio server vẫn chờ DELETE. Crash server không có cancel giữ intent/artifact để recovery receiving/archiving/finalizing, không đồng nghĩa encoder của browser còn chạy.

Metadata cần lưu: MIME/codec thực tế, sample rate, channel count, source labels và mixing mode; timecode từng dataavailable chỉ là timeline chẩn đoán, không phải sample sequence. Sample sequence chưa đo được để null; nguồn tab/mic mô tả input, không phải danh tính người nói. Sample continuity phải được suy ra từ decoder/đầu ra, không tin duration client. Server kiểm tra MIME/codec allowlist, selected audio stream và decoded duration. ffprobe phải trả lỗi bounded; ffmpeg -t giới hạn duration output và protocol_whitelist giới hạn protocol theo [FFmpeg docs](https://ffmpeg.org/ffmpeg.html), [ffprobe](https://ffmpeg.org/ffprobe.html) và [protocols](https://ffmpeg.org/ffmpeg-protocols.html). Remux chỉ là bước cứu container có metadata thiếu; full-session manifest vẫn là điều kiện hoàn tất.

## Tách archive khỏi live

Sau ready, backend tạo meeting/recording duy nhất và ý định transcript + notes bền vững theo [nghiên cứu xử lý](https://github.com/nguyencanhtrung/asr-studio/issues/204). Intent phải được lưu hoặc khôi phục bằng reconciliation theo ready session, không dựa vào một callback trong RAM; không khởi động ASR từ từng chunk. Archive raw là nguồn audit/integrity; normalized M4A là derivative cho pipeline hiện tại, có digest và version. Live tương lai có thể thêm capability live_consumer và cursor (recording_id, sequence) đọc các chunk đã durable, nhưng không được xóa/chuyển ownership archive, không coi một Blob là playable, không chia sẻ token provider và không cam kết latency. Nếu live thất bại, archive vẫn finalize độc lập.

## Ma trận kiểm chứng

| Nhóm | Ca bắt buộc | Kết quả đạt | Phân loại hiện tại |
|---|---|---|---|
| API/identity | create retry, request conflict, GET thiếu Origin, token thiếu/sai/scope, đổi URL/redirect | idempotent; GET vẫn token; sai owner 404; không gửi sang alias mới | Thiết kế; API extension chưa triển khai |
| Append | seq/offset đúng, gap, duplicate cùng digest, duplicate khác digest, concurrent writer | chỉ một writer; ACK sau fsync+DB; replay không nhân byte | Cơ sở tương tự đã có trong test_recordings.py; extension unrun |
| Crash | disconnect giữa write, crash sau fsync trước DB, sau DB trước ACK, restart service worker/Chrome | giữ chunk/manifest hoặc quarantine; resume đúng; không ready prefix | Chưa chạy crash harness; upload resume khác encoder resume |
| Stop | dataavailable cuối trước stop nhưng persistence còn pending, Stop lặp | await Blob cuối và queue; finalize chỉ sau barrier | Spec + issue evidence; chưa test product |
| Finalize/media | full exact bytes, missing duration, thiếu last Blob, Blob thứ hai riêng, corrupt, remux timeout | full manifest bắt buộc; prefix không ready; bounded reject | Artifact synthetic đã chứng minh prefix pitfall; hardening unrun |
| Limit | 4h/4GiB dưới/trên biên, chunk > cap, disk thiếu, quota/eviction | 413/507 rõ; giữ recoverable bytes; không hứa mọi 4h | Chưa có product/long-duration evidence |
| Network | loopback, HTTPS, Cloudflare plan/tunnel config, request timeout/buffering | append nhỏ, Content-Length, async finalize, actual config recorded | Đã thử IPv4/IPv6 loopback Chromium fixture; HTTPS deployment chưa audit |
| Auth/storage | session expiry, device revoke, local buffer restart, uninstall/reinstall | renew cùng endpoint; revoke dừng; buffer không gửi sang server khác | Token contract; Chrome quota/browser matrix unrun |
| Retention/race | cancel trong append/finalize, cancel sau archived, late worker, explicit DELETE | epoch fence chặn late publish; cancel không xóa audio; DELETE riêng | Thiết kế; chưa chạy race harness |
| Archive/live | ready chạy downstream một lần; live consumer lỗi | archive độc lập, không ASR từng Blob | Boundary design; 204/205 handoff |

## Giới hạn và bàn giao

Chưa có API/database implementation, actual reverse-proxy/cloudflared config, Chrome Stable/OS matrix, 1–4 giờ/4 GiB recording, tabCapture/microphone bytes, quota/eviction/crash test, untrusted remux hardening hay production acceptance. Không suy từ browser 156 synthetic oscillator sang mic/tab capture. 204 cần chốt downstream idempotency sau ready; 205/206 cần kiểm chứng prototype và độ bền. Nghiên cứu này không đổi auth architecture và không triển khai encryption/backup.

## Nguồn chính

- [W3C MediaStream Recording](https://www.w3.org/TR/mediastream-recording/)
- [W3C Web Cryptography Level 2](https://www.w3.org/TR/webcrypto/)
- [Chrome Storage API](https://developer.chrome.com/docs/extensions/reference/api/storage/) và [Storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)
- [Chrome cross-origin network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)
- [RFC 9110 HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) và [RFC 9112 HTTP/1.1](https://www.rfc-editor.org/rfc/rfc9112.html)
- [Cloudflare connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/), [Cache upload limits](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/), [Tunnel origin parameters](https://developers.cloudflare.com/tunnel/reference/origin-parameters/)
- [FFmpeg](https://ffmpeg.org/ffmpeg.html), [ffprobe](https://ffmpeg.org/ffprobe.html), [FFmpeg protocols](https://ffmpeg.org/ffmpeg-protocols.html)
