# Thay đổi kiến trúc và tối ưu v1 — ASR Studio

## 1. Mở đầu

**Mục đích.** Tổng kết các tối ưu và thay đổi kiến trúc/thiết kế kể từ đợt nghiệm thu #16 (image build từ revision `151f3e4`, xem [báo cáo nghiệm thu](acceptance-report.md)). Tài liệu sống theo giai đoạn phát triển; các mục đã được tách theo tag vào hồ sơ `docs/release/` cho #17. Số `#NN` là issue hoặc PR của repo trên GitHub.

## #97 — Attribute quadratic clustering memory and document the RAM budget

**Problem and measured cause.** The historical four-hour composite reached 4.93 GiB runner RSS
([historical source](release/archive/v0.1.0/performance-v0.1.0.md)). Current phase instrumentation
on a public repeated four-hour waveform reaches 4.561 GiB: kernel HWM rises
inside centroid `fast_linkage` after `pdist` creates the condensed distance vector. The
[measurement report](release/diarization-memory.md) links raw per-phase results, model/runtime
hashes and the exact public input recipe. The fixtures differ, so these are not before/after
optimization scores.

**Decision.** Take the issue's documentation alternative and keep speaker clustering unchanged.
Publish provisional budgets of 8 GiB installed RAM and 6 GiB available to the worker for the
four-hour cap; larger ASR footprints or other processes can need more. These are provisioning
decisions, not certified minima for every recording. The report distinguishes cumulative kernel
HWM from sampled RSS and contains no private recording or transcript.

**Trade-offs and follow-up.** Windowed clustering or subsampling would need a speaker identity/merge
design and speaker-quality acceptance. This change adds read-only reproduction tools rather than
altering assignments. Full app/ASR/notes acceptance and representative real-meeting data remain
separate from these memory measurements. Issue [#97](https://github.com/nguyencanhtrung/asr-studio/issues/97).
Tag: performance, operations. Target: `docs/release/diarization-memory.md`, README hardware guidance.

## Documentation policy

The [release process](release/process.md) defines checks and evidence reuse for new releases.
The former rule to copy entries by tag applied to the v0.1.0 dossier only.

1. Record each new design change in its PR.
2. Update the affected design or operating document.
3. Link detailed measurements from their original report.
4. Record the measured revision and configuration with each result.
5. Place the source link beside each measurement.
6. Keep private data outside public reports.

Release notes contain user changes, upgrade actions, and known limitations.
They link evidence instead of copying this history.
A release does not require every historical benchmark.
The [historical summaries](release/README.md#read-historical-evidence) retain links to the original v0.1.0 reports.

## 2. Chuỗi xử lý và chỗ mỗi thay đổi tác động

```text
upload
  │
chuẩn hoá — kiểm tra và chuyển audio phía server            (ngoài mốc AC-13)
  │
  ▼ POST /api/tasks: bắt đầu mốc AC-13
ASR — Whisper large-v3, CUDA
  │
diarization — pyannote Community-1, CUDA
  │   nạp model → giải mã đầu vào → diarizing
  │   ◄── [#71] giải mã một lần thành waveform 16 kHz mono; pipeline nhận waveform, không nhận đường dẫn
ghép người nói — căn đoạn ASR với lượt nói
  │
  ▼ transcript sẵn: mốc 1 của AC-13
bấm "Tạo notes" — notes không tự chạy sau transcript (#73)
  │
notes — chia chunk → trích xuất → hợp nhất → kiểm tra → render
  │   ◄── [#72] đầu ra vượt trần → gọi lại với mục tiêu độ dài nhỏ hơn
  ▼ notes sẵn: mốc 2 của AC-13

[#74] bao trùm ASR → notes: ORT_DISABLE_TELEMETRY=1 trong mọi tiến trình con của worker
[#99] đo cả đoạn từ POST /api/tasks tới notes sẵn (không đổi mã)
```

## 3. Các thay đổi đã chuyển vào hồ sơ phát hành

### #74 / PR #91 — Tắt telemetry ONNX Runtime trong tiến trình con của worker

Đã chuyển nguyên mục: [Quyền riêng tư](release/privacy-v0.1.0.md).

### #92 — Kiểm tra egress metadata trong offline smoke

#### Vấn đề

Issue #92 cần một bước smoke có mạng để phát hiện telemetry hoặc đích kết nối ngoài trong lúc
ASR và diarization chạy. Mục đích của bước này là bắt lại kiểu kết nối đã được nêu trong #74 mà
không ghi payload hay gọi provider.

#### Thay đổi thiết kế

- `scripts/offline-smoke.py --check-egress` theo dõi lần phiên âm thật đầu tiên trong network
  namespace của `app` và `worker`. WAV của bước này giữ trần 25 MiB và cho phép thời lượng tới
  1800 giây. Guard yêu cầu khoảng thời gian lớn hơn 8 giây từ quan sát `diarizing` đầu tiên tới
  quan sát `diarizing` cuối cùng. Poll chuyển stage muộn và thời gian dọn helper không được tính.
- `scripts/egress_observer.py` lấy mẫu `/proc/net/tcp*` và `/proc/net/udp*`, kèm PID/process
  metadata khi đọc được, và thu packet metadata bằng AF_PACKET. Nó giữ IP, port, DNS qname, TLS
  SNI, hướng và process metadata trong bộ nhớ hoặc evidence, không giữ payload.
- Policy chỉ tin exact IP của các service thuộc Compose project, exact resolver IP của từng
  container và loopback. Policy không coi mọi private, link-local, reserved, `.local` hoặc
  `.internal` là nội bộ. App listener response được phân biệt với initiated egress.
- DNS request được nhận diện khi đi tới resolver đã xác minh, kể cả khi Docker DNAT đổi port đích
  trước khi packet tới AF_PACKET. DNS response được giữ là inbound UDP. Mọi UDP khác, gồm UDP
  443, vẫn tạo endpoint metadata và đi qua deny policy.
- Mỗi observer chạy trong helper disposable với `--cap-drop ALL` và chỉ thêm `NET_RAW`,
  `SYS_PTRACE` và `DAC_READ_SEARCH`. Helper dùng network/PID namespace của đúng container đích,
  không dùng privileged mode, host PID hoặc host filesystem mount. Vì helper có proc access,
  thiết kế không tuyên bố rằng nó không thể đọc file đích qua `/proc`.
- Khi dừng, helper đọc hết packet đang chờ tới một lần đọc yên lặng, rồi gửi barrier health và
  chờ controller ACK trước khi thoát để chứng minh toàn bộ metadata trước barrier đã tới nơi.
  Capture dừng trước khi task kết thúc hoặc thiếu ACK đều fail.
  Mọi signal và xóa cưỡng bức kiểm tra nhãn ownership của helper trước khi thực hiện.
  Nguồn regression: `tests/test_offline_smoke.py`, `tests/test_egress_observer.py`.
- Bộ lấy mẫu bỏ `TIME_WAIT` đã đóng, không còn process sở hữu; packet capture vẫn giữ SYN.
  Các hàng đã đóng từng làm sample vượt giới hạn reader khi có nhiều HTTP poll. Lỗi stream giữ
  lý do cụ thể và không bị lỗi cleanup che mất. Regression có bảng nhiều hàng `TIME_WAIT`, một
  socket đang hoạt động và trường hợp task/cleanup cùng lỗi.

#### Số giới hạn và nguồn

Các con số dưới đây là guard hoặc giao thức quan sát, không phải benchmark hiệu năng.

| Hạng mục | Giá trị | Nguồn |
| --- | ---: | --- |
| WAV tối thiểu | 2 giây | [`MIN_AUDIO_SECONDS`](../scripts/offline-smoke.py) |
| WAV tối đa của egress path | 1800 giây | [`EGRESS_MAX_AUDIO_SECONDS`](../scripts/offline-smoke.py) |
| Trần file WAV | 25 MiB | [`MAX_AUDIO_BYTES`](../scripts/offline-smoke.py) |
| Diarization stage tối thiểu | lớn hơn 8 giây | [`EGRESS_MIN_DIARIZATION_SECONDS`](../scripts/offline-smoke.py) |
| Chu kỳ lấy mẫu mặc định | 0,2 giây | [`DEFAULT_SAMPLE_INTERVAL`](../scripts/egress_observer.py) |
| App listener của selftest | port 8765 | [`APP_LISTENER_PORT`](../scripts/egress_observer.py) |

Nguồn yêu cầu và giới hạn: [Issue #92](https://github.com/nguyencanhtrung/asr-studio/issues/92),
[`offline-smoke.py`](../scripts/offline-smoke.py), và
[`egress_observer.py`](../scripts/egress_observer.py). Không có số đo throughput, latency hoặc
độ chính xác được suy ra từ bước smoke này.

#### Đánh đổi và phương án đã loại

- Đọc socket table cho process ownership và AF_PACKET cho SYN/DNS/SNI: giữ được attribution và
  kiểm tra capture có nhìn thấy traffic, nhưng vẫn chỉ cung cấp metadata.
- Helper root với đúng ba capability được thêm: đáp ứng proc ownership trong shared PID namespace
  mà không cấp privileged, host PID hoặc host mount. Proc access là một giới hạn bảo mật cần ghi
  rõ, không được che bằng tuyên bố helper chỉ đọc network.
- Strict external deny với allowlist rỗng: phù hợp với smoke không gọi provider và không tạo
  quyết định allowlist động từ DNS hoặc private address. Provider traffic nằm ngoài bước này.
- First-to-last actual `diarizing` observation: số đo là lower bound. Không dùng poll terminal
  hoặc `finish_task` vì chúng có thể kéo dài số đo sau khi diarization đã kết thúc.

#### Giới hạn

TLS chỉ cho phép đọc ClientHello SNI khi handshake và segment cần thiết đều được capture. Observer
không kiểm tra payload mã hóa, certificate hoặc tên phát sinh sau ClientHello. Packet drop,
malformed packet, malformed evidence, process ownership không chứng minh được hoặc helper chết
đều làm smoke fail closed.

#### Liên kết issue/PR

[Issue #92](https://github.com/nguyencanhtrung/asr-studio/issues/92).

#### Tag

Kiến trúc | Quyền riêng tư

#### Tài liệu đích

- `docs/user-guide.md` — invocation, policy, capability và TLS limitation.
- `scripts/offline-smoke.py` — lifecycle và diarization lower-bound guard.
- `scripts/egress_observer.py` — capture, parsing và policy protocol.

### #72 / PR #94 — Phục hồi notes khi đầu ra của provider vượt trần

Đã chuyển nguyên mục: [Pipeline notes](release/notes-v0.1.0.md).

### #71 / PR #98 — Diarization giải mã bản ghi một lần thành waveform 16 kHz

Đã chuyển nguyên mục: [Hiệu năng](release/performance-v0.1.0.md).

### #99 / PR #103 — Đo lại AC-13 trên main sau #71, #72, #74

Đã chuyển nguyên mục: [Hiệu năng](release/performance-v0.1.0.md).

### #104 / PR #109 — Notes một lượt theo context thật của model

Đã chuyển nguyên mục: [Hiệu năng](release/performance-v0.1.0.md), [Pipeline notes](release/notes-v0.1.0.md).

### #17 / PR #112 — Nhật ký runtime và startup mặc định an toàn

Đã chuyển nguyên mục: [Quyền riêng tư](release/privacy-v0.1.0.md), [Kiến trúc runtime](release/runtime-v0.1.0.md).

### #100 / PR #111 — Notes văn phong telegraph, mỗi ý một mục

Đã chuyển nguyên mục: [Pipeline notes](release/notes-v0.1.0.md).

### #115 — Tắt PR Dependabot và xác minh config digest image công khai

#### Vấn đề

Metadata do BuildKit trả về sau khi push image có provenance và SBOM không bảo đảm chứa
`containerimage.config.digest`; gate cũ vì vậy dừng sau khi push thành công. Dependabot cũng
không còn là cơ chế cập nhật dependency được sử dụng trong repository.

#### Nguyên nhân đo được

Gate cũ đọc config digest từ `PUBLISHED_METADATA`, nhưng metadata published thực tế chỉ cung cấp
index/manifest digest và attestations. Việc tra cứu image công khai phải đi qua index rồi child
manifest đúng platform để lấy config descriptor.

Nguồn: `tests/test_published_image_contract.py`, `tests/test_image_config.py` và workflow phát hành.

#### Thay đổi thiết kế

Xóa cấu hình Dependabot để ngăn tạo PR cập nhật version. Helper `scripts/resolve-image-config.py`
dùng token GHCR anonymous, hash-check bytes của index, child manifest và config, bỏ qua descriptor
attestation, rồi yêu cầu đúng một child Linux cho platform. Workflow so sánh config digest công khai
với digest candidate trước các kiểm tra version, schema, revision và smoke; thứ tự publish source
trước image và kiểm tra hai kiến trúc trước khi gắn tag không đổi.

#### Số đo trước và sau

| | Trước | Sau |
| --- | --- | --- |
| Nguồn config digest của gate | metadata published không ổn định | registry index → child manifest → config |
| Số child native được chấp nhận | không xác minh | đúng 1 Linux child |

Nguồn: `tests/test_delivery_contract.py`, `tests/test_image_config.py` và `tests/test_published_image_contract.py`.

#### Đánh đổi và phương án đã loại

- Tra cứu registry anonymous: chịu thêm ba request artifact read-only ngoài token, đổi lại xác minh artifact thực sự đã public.
- Đọc lại metadata BuildKit sau push: loại vì metadata published có thể thiếu config digest khi có SBOM/provenance.
- Giữ Dependabot version updates: loại theo quyết định của issue #115; security alerts không thay đổi.

#### Follow-up

- #115: giữ các test negative cho platform thiếu, trùng, unknown và config sai.

#### Liên kết issue/PR

[Issue #115](https://github.com/nguyencanhtrung/asr-studio/issues/115).

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — release publication verification.

### #168 — Đổi mặc định GPU sang Qwen3-ASR và đánh dấu chuỗi lặp

#### Vấn đề

Mặc định GPU cũ là Whisper large-v3. Issue #168 yêu cầu chọn default theo kết quả đo; AC-01 của
#80 cũng cần một tín hiệu duyệt chung cho đoạn lặp, bất kể engine hay người nói.

#### Nguyên nhân đo được

AC-01 của issue #168: hai dạng lỗi của large-v3 được đo riêng và phản ứng khác nhau với cùng
thay đổi. Với câu ngắn,
large-v3 bịa dòng ở 8/27, 12/27 và 6/27 mẫu tương ứng với input as-is, thêm 0,5 giây cuối và thêm 0,5 giây đầu; `no_context` cho đúng các con số đó. Lỗi nhạy với biên input và không được
sửa bởi bất kỳ cấu hình decoder hoặc segmentation nào trong 12 cấu hình đã thử. Với clip dài,
WER của base là 0,789/0,896/0,970 và của `no_context` là 0,246/0,275/0,684 theo forward 0,5
giây, reverse 0,5 giây và forward 2 giây: conditioning là một yếu tố góp phần đo được, nhưng chưa phải toàn bộ. Cơ chế sâu hơn vẫn chưa biết.

| Engine | Dòng bịa, câu ngắn as-is/tail/head | Dòng bịa, clip dài forward/reverse/2 s | Dòng bịa, VIVOS/mixed/English | WER forward 0,5 s | VIVOS | mixed | English |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| large-v3 base | 8/12/6 | 0/7/9 | 0/0/0 | 0,7890 | 0,3572 | 0,6683 | 0,1988 |
| large-v3 `no_context` | 8/12/6 | 0/1/5 | 0/0/0 | 0,2457 | 0,1342 | 0,2670 | 0,0357 |
| Qwen product | 0/0/0 | 0/0/0 | 0/0/0 | 0,0592 | 0,0498 | 0,1456 | 0,0405 |
| PhoWhisper product | 0/0/0 | 0/0/0 | 0/0/0 | 0,0564 | 0,0281 | 0,3641 | 0,1071 |

PhoWhisper thắng ở forward 0,5 giây và VIVOS; `no_context` thắng ở English. Trên ba meeting,
số đoạn trùng liền kề và phần trăm cụm lặp lần đầu lần lượt là: A — base 159/3,0251%,
`no_context` 0/0%, Qwen 1/0,0188%; B — base 76/2,2880%, `no_context` 8/0,5830%, Qwen
2/0,0389%; C — base 67/2,7735%, `no_context` 0/0%, Qwen 0/0%. Phần trăm cụm lặp ở đây dùng một định nghĩa cho cả ba cấu hình, trong cùng phép đo. Khảo sát model không ghi định nghĩa của hai số 4,2 và 5,7, nên không so sánh được với hai số đó. Đoạn 10 phút có số output
khác nhau giữa các lần chạy là base 3, `no_context` 1, Qwen 1, PhoWhisper 3.

Số large-v3 dùng PCM WAV, còn product đưa audio qua AAC normalization; trên cùng clip, điều đó
đổi reverse từ 0,8960 thành 0,1185 và mixed từ 0,6683 thành 0,2945. Vì vậy số v0.2.1 đã công
bố chưa được tái lập đầy đủ. Phép so sánh ban đầu chưa có Whisper small; số đo riêng nằm trong ADR 0011.

Nguồn: [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), [số đo ứng viên model](model-candidates.md#đo-trên-bản-ghi-thật-11-2026).

#### Thay đổi thiết kế

`auto` dùng Qwen3-ASR-1.7B với Transformers và bfloat16 khi runtime profile là CUDA; trên CPU
vẫn dùng Whisper small với int8. `config.py`, Compose, `.env.example`, startup, model prepare,
offline smoke và capture dùng đường dẫn Qwen làm GPU hardware default. Engine được chọn theo
đường dẫn catalog đã resolve: một đường dẫn Whisper large-v3 explicit hoặc task cũ không bị đổi
sang Qwen. Whisper decoder tắt `condition_on_previous_text`; các engine khác không đổi.

Sau alignment, mọi engine được đánh dấu `repeated_segment` khi có run ít nhất ba segment liền
nhau có text giống nhau sau khi casefold, bỏ khoảng trắng và dấu câu. Tất cả thành viên run được
đánh dấu, không phụ thuộc speaker; Qwen không thêm `asr_uncertain` vì không có confidence tương ứng.

#### Số đo trước và sau

Revision/config: phép so sánh product dùng Qwen bfloat16; app dùng batch 4. Số trước là default
GPU và phép đo Whisper app được ghi trong cùng hồ sơ đo; không coi đây là phép đo nhân quả.

| | Trước | Sau |
| --- | --- | --- |
| GPU hardware default | Whisper large-v3 | Qwen3-ASR-1.7B |
| Đỉnh VRAM app | 5554 MiB, Whisper app | 8336 MiB, Qwen batch 4 |
| Model cần stage khi chuyển default | model cũ đã có | Qwen thêm 4.087.646.324 byte (~4,1 GB); `.env` cũ không tự đổi |
| Chuỗi lặp liền kề | chưa có reason chung | `repeated_segment` cho run từ 3 segment |

Nguồn: [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), [model candidates](model-candidates.md#đo-trên-bản-ghi-thật-11-2026).

Bản cài cũ giữ large-v3 nếu file `.env` hiện tại vẫn có `ASR_GPU_MODEL_PATH=/models/whisper-large-v3`;
`.env.example` không tự ghi đè file đó. Muốn chuyển sang Qwen, sửa dòng này thành
`/models/qwen3-asr-1.7b` rồi chuẩn bị model, hoặc chọn Qwen làm model mặc định trong Settings.

#### Đánh đổi và phương án đã loại

- Qwen làm default: tăng khoảng 4,1 GB tải xuống và đỉnh VRAM 8336 MiB, đổi lại kết quả so sánh tốt hơn trên mixed/English; Whisper large-v3 vẫn có thể chọn explicit.
- Giữ large-v3 làm default và chỉ đổi decoder: loại cho default theo kết quả so sánh; thay đổi `condition_on_previous_text` vẫn được giữ như một cải thiện decoder riêng.
- Chỉ đánh dấu segment có cùng speaker: loại vì chuỗi lặp cần được phát hiện độc lập với diarization.

#### Follow-up

- #80: kiểm chứng tín hiệu duyệt chuỗi lặp trên acceptance recordings và tiếp tục quyết định các thay đổi decoder khác.

#### Liên kết issue/PR

[Issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), liên quan #80.

#### Tag

Hiệu năng | Kiến trúc

#### Tài liệu đích

- `docs/adr/0011-asr-model-per-run.md` — default theo phần cứng và lựa chọn từng run.
- `docs/user-guide.md` — model mặc định, artifact, VRAM và confidence behavior.
- `docs/backend-contract.md` — semantics của `auto` và task legacy.

### #119 (pha 0 của #118) — ADR và khung reducer/store cho frontend

#### Vấn đề

Frontend (`src/asr_studio/web/`) là JS thuần với state sửa trực tiếp ở khoảng 250 chỗ, chống response cũ bằng các bộ đếm rải rác và không có unit test JS (CI chưa cài Node). Issue #118 chuyển dần sang core thuần; pha 0 dựng nền, chưa chuyển module UI nào.

#### Nguyên nhân đo được

Khoảng 250 lệnh gán state trực tiếp (`history.js` 145, `shell.js` 78, `notes.js` 29), các bộ đếm `loadSeq`/`searchSeq`/`saveSeq`, giao tiếp bằng `CustomEvent` và hàm global; test chỉ có Playwright.

Nguồn: issue #118.

#### Thay đổi thiết kế

`docs/adr/0010-frontend-reducer-core.md` chốt kiến trúc; `src/asr_studio/web/store.js` là ES module không chạm DOM gồm `createStore`, `combine`, `createRunner` (effect `fetch`, `schedule`, `cancel`, latest-wins theo `key`, kết quả mang `meta` để reducer bỏ response cũ). `tests/web/*.test.mjs` chạy bằng `node --test`, nằm trong `scripts/check.sh` và job quality của CI (Node 24, action ghim SHA). File UI hiện có, CSP và hành vi người dùng không đổi; không thêm dependency hay build step.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, thời gian wall của các test một lần chạy cục bộ; thời gian chạy trên CI nằm trong log job quality.

| | Trước | Sau |
| --- | --- | --- |
| Unit test JS | 0 | 19 test, dưới 0,1 s |
| Node trong CI | không | Node 24 |

Nguồn: `tests/web/store.test.mjs`, `.github/workflows/pipeline.yml`.

#### Đánh đổi và phương án đã loại

- Store tự viết dưới 150 dòng: không middleware hay devtools, đổi lại không phụ thuộc thư viện.
- Framework hoặc bundler: loại theo phạm vi #118.
- `node --test` nhận glob `tests/web/*.test.mjs`: Node 24 coi đối số thư mục là đường dẫn module nên không dùng `tests/web/`.

#### Follow-up

- Pha 1–5 của #118 chuyển từng module; #117 cần thống nhất thứ tự với pha 1.

#### Liên kết issue/PR

Issue #119; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #121 (pha 1 của #118) — dialog tạo cuộc họp chuyển sang reducer

#### Vấn đề

`shell.js` (dialog chọn file, upload, tạo cuộc họp) giữ một object `appState` sửa trực tiếp ở 75 chỗ, trộn logic với DOM, và chống response cũ bằng `runId`/`policyRunId` cùng cờ `startingUpload`. Logic upload, hủy, dọn upload dở và tạo cuộc họp không có unit test, chỉ có Playwright.

#### Nguyên nhân đo được

Đếm lệnh gán `appState.<trường>` (`=`, `+=`, `++`) trong `shell.js` trước pha 1 bằng `grep -cE`: 75 dòng. Số 78 ở #118 là ước lượng gộp thêm hai lệnh `.add`/`.delete` trên `appState.pendingCleanup` và khai báo `appState`; 75 là số dùng cho cột "Lệnh gán state" bên dưới. Các nhánh hủy giữa chừng, lỗi mạng, bấm lặp và thử lại chỉ kiểm được qua trình duyệt thật.

Nguồn: `git show 10162b3:src/asr_en_vi/web/shell.js`; issue #118.

#### Thay đổi thiết kế

- `shell-core.js`: core thuần (`initialState`, `reduce`, `selectView`, chính sách file, kiểm form, thông điệp lỗi). `shell.js` là ES module chỉ render và dispatch (`<script type="module">`, nạp trước các script `defer` chưa chuyển). Mọi id chống response cũ nằm trong state.
- `createRunner` thêm effect `upload` (XHR có tiến độ, hủy, cùng ngữ nghĩa `ok`/`error`/`meta`/latest-wins như `fetch`), `storage` và `navigate`. Upload là effect riêng để #117 chèn bước tách audio trước nó.
- `ApiError`, `requestJson`, `meetingIdFromPath`, `formatMeetingDate`, `sameOriginAudioSource` vẫn công bố trên `window` cho `history.js` tới pha 5.
- Route `/assets/store.js` và `/assets/shell-core.js` có bảo vệ truy cập như asset khác, kèm test.
- Test `tests/web/shell-view.test.mjs` chặn gán vào state, `let`/`var` cấp module và truy cập storage ngoài `readStored` trong view đã chuyển; chạy trong `check.sh` và CI.
- Gộp góp ý pha 0: quy tắc tiền tố `key`, ngữ nghĩa lỗi giữa lượt drain và trong callback `schedule` (ADR 0010, mục 11.2), test hàng đợi bị bỏ khi lỗi, contract test yêu cầu có file `tests/web/*.test.mjs`, Node 24 trong `docs/user-guide.md`.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, thời gian wall của các test một lần chạy cục bộ.

| | Trước | Sau |
| --- | --- | --- |
| Lệnh gán state trong `shell.js` | 75 | 0 |
| Unit test JS | 19 | 70, dưới 0,1 s |

Nguồn: `tests/web/shell-core.test.mjs`, `tests/web/store.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- Đóng dialog gửi DELETE upload dở rồi đóng ngay thay vì chờ DELETE trả lời; DELETE lỗi vẫn được nhớ và thử lại.
- Giá trị đã nhớ (ngôn ngữ, chép lời ngay) được đọc mỗi lần mở dialog, không chỉ lúc tải trang, để giữ hành vi cũ khi storage đổi giữa hai lần mở.
- Hủy rồi đóng khi DELETE treo: id nằm ở `pendingCleanup` từ lúc hủy nên không mất; dialog không bị khóa. Dialog bị trình duyệt tự đóng (Esc lần hai) được state theo kịp qua action `dialog-closed`.
- Render chỉ ghi DOM khi giá trị đổi, để vùng `aria-live` không bị đọc lại.
- Không dùng `combine`: dialog là tính năng duy nhất trên store này tới khi pha sau thêm tính năng thứ hai.

#### Follow-up

- #117 thêm effect tách audio trước `shell/upload`.
- Pha 2–5 của #118.

#### Liên kết issue/PR

Issue #121; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #123 (pha 2 của #118) — kết nối ChatGPT và Google dùng chung một core provider

#### Vấn đề

`connections.js` (ChatGPT, nút launcher, hộp thoại) và `antigravity.js` (Google Antigravity) mỗi file giữ một object state sửa trực tiếp, nhân đôi cùng một luồng (tải trạng thái, danh sách model, đăng nhập, poll, hủy, lưu, kiểm tra, ngắt kết nối) và chống response cũ bằng bộ đếm `pollGeneration`, `loadGeneration`, `pollTimer` và cờ `savingModel`, `verifying`, `disconnecting`, `busy`. Luồng đăng nhập, hết hạn, hủy giữa chừng và khôi phục sau tải lại chỉ kiểm được bằng Playwright.

#### Nguyên nhân đo được

Đếm lần gán vào `connectionState.<trường>` trong `connections.js` và `antigravityState.<trường>` trong `antigravity.js` tại 67b8f13 (bỏ chú thích; `=`, `+=`, `++`, `--` hậu tố và tiền tố, gán lồng): 32 + 3 tiền tố + 26 = 61. Cách đếm đầu tiên bằng `grep -c` trên dòng bỏ sót `++x` tiền tố và gán lồng nên ra 56; số đúng là 61. Hai file dài 912 + 461 dòng; chín cặp luồng lặp (tải, bắt đầu đăng nhập, poll, hủy, kết thúc đăng nhập, lưu, kiểm tra, ngắt kết nối, dựng danh sách model).

Nguồn: `git show 67b8f13:src/asr_en_vi/web/connections.js`, `git show 67b8f13:src/asr_en_vi/web/antigravity.js`; issue #118.

#### Thay đổi thiết kế

- `provider-core.js`: core thuần với một reducer cho mọi provider; mô tả provider là dữ liệu (endpoint, kiểu đăng nhập `popup`/`code`, độ trễ poll, hỗ trợ effort, khóa sessionStorage, bảng lỗi, văn bản). Hai provider dùng một store qua `combine`, tạo trong `connections.js` và `antigravity.js` import lại. Cả hai file là ES module chỉ render và dispatch (`<script type="module">`, giữ thứ tự cũ trong `shell.html`).
- `createRunner` thêm effect `open-window` (giữ chỗ cửa sổ rồi chuyển tới URL hoặc đóng), effect `event` (phát `asr:provider-choice-changed` cho `notes.js` đúng các thời điểm cũ) và tham số `now` đóng dấu đồng hồ lên action runner dispatch (hạn hết phiên đăng nhập đi vào reducer dưới dạng dữ liệu).
- Poll qua effect `schedule`; sessionStorage qua effect `storage`; khôi phục đăng nhập dở qua action `provider/boot-restored` mang chuỗi đã lưu.
- Route `/assets/provider-core.js` có bảo vệ truy cập và test asset; `connections.js`, `antigravity.js` thêm vào test asset.
- Gate `tests/web/shell-view.test.mjs` thêm hai view và core mới, mở rộng quy tắc theo góp ý pha 1 (bí danh `store.getState()`, lệnh đổi như `state.list.push`, object cấp module bị sửa), mỗi quy tắc có một ví dụ xấu trong test.
- Bảng trạng thái và chuyển trạng thái của core provider: `frontend-design.md` mục 11.6.

Khác biệt hành vi so với bản cũ (đều nhỏ, ghi để review): Google bỏ qua poll trả lời muộn sau hủy hoặc kết thúc (bản cũ có thể dựng lại phiên đã hủy) và tải trạng thái có id; bấm đăng nhập lặp trong lúc đang khởi động bị bỏ; khi hủy, Google ngừng poll trong lúc chờ DELETE và poll lại nếu hủy lỗi; nút Hủy của ChatGPT khóa tới khi DELETE trả lời; lỗi có câu riêng (cửa sổ đã đóng, URL không an toàn) hiện câu đó ở cả tiếng Anh; dòng chữ giống hệt không ghi lại vào vùng `aria-live`; cuộn tới mục Google không bị hủy bởi lượt tải mới hơn; Google với URL đăng nhập sai hoặc không an toàn giữ login và hiện bảng đăng nhập cùng nút Hủy (bản cũ giữ bảng ẩn, nút Hủy không với tới); ChatGPT bị chặn popup giữ focus ở nút đăng nhập (bản cũ về `body`); sau khi ngắt ChatGPT, ô effort ẩn chỉ còn `default`; nút Hủy ẩn bị `disabled` trước lần đọc đầu của hộp thoại; cuộn tới mục Google lệch khoảng 51 px khi đọc trạng thái lỗi hoặc cần đăng nhập lại (đích vẫn là mục Google).

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, một lần chạy cục bộ; dòng gán đếm như mục "Nguyên nhân đo được".

| | Trước | Sau |
| --- | --- | --- |
| Lệnh gán state trong `connections.js` + `antigravity.js` | 61 (bản đầu ghi 56) | 0 |
| Bộ đếm và cờ chống response cũ trong view | 8 (`pollGeneration`, `loadGeneration`, `pollTimer` x2, `savingModel`, `verifying`, `disconnecting`, `busy`) | 0 (id trong state của core) |
| Unit test JS | 70 | 133, dưới 0,2 s |
| `connections.js` + `antigravity.js` (dòng) | 1373 | 1358: view 279 + core 1079 (`wc -l`) |

Nguồn: `tests/web/provider-core.test.mjs`, `tests/web/store.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- Một core cho hai provider thay vì hai core: khác biệt là trường của mô tả (khoảng 20 trường) cộng vài nhánh nhỏ; đổi lại mỗi sửa luồng (badge, lưu, kiểm tra, ngắt kết nối) làm một lần. Phương án loại: một hàm reducer có `if (id === "chatgpt")` rải rác.
- Không thêm file store riêng: store chung nằm ở `connections.js` (module nạp trước) để tránh một route và test asset thừa.
- Đồng hồ vào reducer qua `now` do runner đóng dấu, thay vì `Date.now()` trong reducer hoặc view tự thêm vào từng action.
- Lỗi "trình duyệt tự đóng dialog lúc đang lưu" của pha 1 không sửa ở PR này (không thuộc luồng provider; vẫn mở ở #118).

#### Follow-up

- Pha 3–5 của #118.
- `tr`, `setProp` hiện lặp giữa `shell.js` và `connections.js`; gom khi bỏ global ở pha 5.

#### Liên kết issue/PR

Issue #123; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #126 (pha 3 của #118) — `notes.js` chuyển sang reducer

#### Vấn đề

`notes.js` (2093 dòng) giữ một object `state` sửa trực tiếp ở hàng trăm chỗ, trộn logic với DOM và chống response cũ bằng bộ đếm `loadSeq`, `readSeq`, `pollSeq`, `selectionSeq`, `sourceSeq`, bộ hẹn giờ `pollTimer` và cờ `requestInFlight`, `cancelInFlight`, `saveInFlight`, `transcriptionRequestInFlight`. Những luồng khó nhất của tab (response tới sai thứ tự khi đổi cuộc họp, poll cũ, hủy giữa chừng, tạo hoặc lưu lặp, xung đột revision, khôi phục bản nháp) chỉ kiểm được bằng Playwright. Bản nháp và bước task còn được tạo ngay trong lúc render.

#### Nguyên nhân đo được

Đếm lần gán vào `state`, `choice`, `data`, `draft`, `notes` tại 6f1a609 trong `git show 6f1a609:src/asr_en_vi/web/notes.js`, bỏ chú thích, đếm từng lần xuất hiện (không phải dòng): gán và gán kép `<tên>(.trường|[chỉ số])+ (=|+=|-=|*=|/=|++|--)` 121; `++`/`--` tiền tố 3 (`++choice.loadSeq`, `++state.readSeq`, `++state.sourceSeq`); đổi Map/Set `state.<x>.set|delete|add|clear(`, `data.saveInFlight.add|delete(` 12; tổng 136. Mười tên `*Seq`, `*InFlight`, `*Timer` (`cancelInFlight`, `loadSeq`, `pollSeq`, `pollTimer`, `readSeq`, `requestInFlight`, `saveInFlight`, `selectionSeq`, `sourceSeq`, `transcriptionRequestInFlight`). Bản nháp (`revisionDraft`) và bước task (`renderTaskSteps`) còn được tạo và ghi vào state ngay trong lúc render.

Nguồn: `git show 6f1a609:src/asr_en_vi/web/notes.js`; issue #118.

#### Thay đổi thiết kế

- `notes-core.js`: core thuần (`initialState`, `reduce`, `selectView`, `selectSummary`, `hasUnsavedDraft`) cùng các hàm thuần chuyển từ `notes.js` (nhãn, thông báo lỗi theo mã, chuẩn hóa revision, trích dẫn và request chờ, `markdownBlocks`, `sourceReference`, `sourceFromHref`). Thời gian, id ngẫu nhiên (`requestId`), giá trị đã lưu (`stored`), địa chỉ trang (`href`, `origin`) và trạng thái ẩn của trang vào qua action.
- `notes.js`: ES module khoảng 0,5 nghìn dòng, store riêng, render từ `selectView` bằng `setProp`, handler chỉ dispatch. Vị trí trong `shell.html` giữ nguyên (sau `history.js`, trước `connections.js`); `<script type="module">` thay `defer`.
- Effect mới `call` (`key`, `name`, `args`, `ok`, `error`, `meta`) và tham số `calls` của `createRunner`: một cổng cho lệnh tới `history.js` (`setNotesSourceLock`, `setNotesSummary`, `selectTab`, `markNotesSource`, `showNotesSourceTab`, `focusSpeakerCatalog`, `refreshMeeting`, `seekNotesSource`, `playRecording`) và cho clipboard, tải file, `history.replaceState`. Kết quả bất đồng bộ (tua, phát, chép) về thành action; hủy/thay thế theo `key` như `fetch`.
- Poll bằng `schedule`; `localStorage` (chế độ Xem/Viết) và request chờ, ngôn ngữ đầu ra (local + session) bằng `storage`; id request tạo trong view (`crypto.randomUUID`) và vào bằng action `notes/start-clicked`.
- Route `/assets/notes-core.js` có bảo vệ truy cập; `notes.js` và `notes-core.js` thêm vào hai test asset.
- Gate `tests/web/shell-view.test.mjs`: thêm `notes.js` (view) và `notes-core.js` (core); sửa hai điểm mù từ pha 2: `${…}` trong template literal được quét như mã, bí danh lát cắt (`const s = state.x; s.y = 1`) bị bắt; mỗi điểm có ví dụ xấu trong test.
- Bảng trạng thái và chuyển trạng thái: `frontend-design.md` mục 11.7. ADR 0010 thêm effect `call`.

Khác biệt hành vi so với bản cũ (đều nhỏ, ghi để review): request đọc/tải lựa chọn có key riêng nên không bị hủy giữa chừng, response cũ bị bỏ bằng id (như bản cũ); sau khi tạo notes mà máy chủ trả task đã kết thúc ngay, nút "Đang gửi yêu cầu…" tắt trước khi lần đọc lại xong (bản cũ giữ tới khi đọc xong); ô cảnh báo liên kết hỏng chỉ dựng lại khi nội dung đổi (bản cũ dựng lại mỗi lần render, làm vùng `aria-live` đọc lại); nội dung vẫn giống nhau thì không ghi lại vào DOM; request chờ và ngôn ngữ đã lưu được đọc ở mỗi sự kiện context cho cuộc họp chưa có trong state (bản cũ đọc ngôn ngữ một lần, còn request chờ thì đọc lại ở mỗi lần render tới khi thấy); bản nháp và bước task được đối chiếu sau mỗi action thay vì khi render; tóm tắt gửi cho `history.js` chỉ khi context tới hoặc tóm tắt đổi (bản cũ gửi mỗi lần render); lỗi mở nguồn để nút "Tua bản ghi" bị khóa (bản cũ giữ trạng thái trước); vùng `aria-live` trạng thái notes chỉ ghi giá trị cuối (bản cũ ghi hai lần khi đang chép lời hoặc sau lỗi chưa chắc chắn); `markNotesSource` và `seekNotesSource` chạy sau một tick (`schedule` delay 0, action `source-shown`) để view vẽ thẻ nguồn trước, như bản cũ; gõ vào bản nháp render lại đầy đủ nên hộp xung đột ẩn ngay khi phím gõ làm hết xung đột (bản cũ chờ lần render đầy đủ kế tiếp); lưu bị 409 thì cảnh báo xung đột chỉ hiện khi bản mới nhất đã đọc xong (bản cũ không render giữa 409 và lần đọc đó); ô xem trước chỉ dựng lại khi các khối Markdown đổi (khóa JSON), nên ẩn/hiện tab trình duyệt hay một lượt poll không làm mất focus bàn phím trên chip trích dẫn (bản cũ dựng lại mỗi lần render); tên người nói mặc định đi cùng action context (view hỏi `history.js` khi nhận `asr:meeting-context`) thay vì hỏi lúc render, nên người nói mới thêm ở tab Bản ghi (chưa có đoạn nào) chưa vào câu nhắc tới context kế tiếp; render chỉ chạy khi có meeting (bản cũ còn vẽ lại tab ẩn khi trả lời tải/lưu lựa chọn, chép/tải xuống tới sau lúc rời cuộc họp; không thấy được).

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test tests/web/*.test.mjs`, một lần chạy cục bộ; lần gán đếm như mục "Nguyên nhân đo được" (`=`, `+=`, `++`/`--` tiền tố và hậu tố, gán lồng) vào `state` và các bí danh của nó (`state`, `choice`, `data`, `draft`, `notes`; tức `state.choice`, `meetingNotes()`, `revisionDraft()`), cộng lệnh `.set/.delete/.add/.clear` trên Map/Set của state: 121 phép gán + 3 tiền tố + 12 lệnh Map/Set = 136 trên `git show 6f1a609:src/asr_en_vi/web/notes.js` bỏ comment. Dòng này đếm cả thay đổi Map/Set, mà số đo của #123 không đếm. Sau: 0 ở `notes.js`, kiểm bằng gate; `notes-core.js` tạo state mới nên không gán tại chỗ.

| | Trước | Sau |
| --- | --- | --- |
| Lần gán/đổi state trong `notes.js` (gồm thay đổi Map/Set) | 136 | 0 |
| Bộ đếm và cờ chống response cũ trong view | 10 | 0 (id trong state của core; `transcriptionRequestInFlight` chỉ còn là tên trường do `history.js` gửi) |
| Unit test JS | 133 | 237 (98 cho core notes, 4 cho runner `call`, 2 thêm cho gate), 0,1 s |
| `notes.js` (dòng) | 2093 | khoảng 2,5 nghìn: view khoảng 0,5 nghìn + core khoảng 1,9 nghìn (`wc -l`) |

Nguồn: `tests/web/notes-core.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- `history.js` chưa chuyển nên lệnh tới nó đi qua một effect `call` có tên, thay vì thêm một loại effect cho từng lệnh; câu hỏi chỉ đọc (`hasUnsavedTranscriptDrafts` lúc trang đóng, `currentContext` lúc boot, `defaultSpeakerNames` lúc nhận context) view gọi thẳng vì reducer không chờ giá trị trả về tức thời. Tên người nói vào state qua action context: `history.js` phát `asr:meeting-context` khi đổi tên, gán đoạn hay sửa đoạn; thêm người nói thì không, nên tên mới vào câu nhắc ở context kế tiếp.
- Request đọc không bị hủy khi có request mới hơn (key theo id) để giữ hành vi cũ; test Playwright của #12 chờ response thứ nhất đọc xong.
- Tổng số dòng tăng (khoảng 2,5 nghìn so với 2093) vì reducer tạo state mới thay vì gán tại chỗ: đổi lại mọi chuyển trạng thái nằm ở một nơi và test được ngoài trình duyệt.
- Câu "Revision notes mới nhất không khớp." giữ làm hằng `LATEST_MISMATCH`: bản cũ tạo nó rồi hiển thị câu của lỗi 409; bảng tiếng Anh vẫn cần nó.

#### Follow-up

- Lỗi pha 1 còn mở: dialog tự đóng trong lúc lưu (xem comment #118 ngày 2026-10-28); đọc notes của cuộc họp A trả lời muộn (start/cancel/409) làm rơi lần đọc đầu của cuộc họp B vì `readId` dùng chung (đã có ở bản cũ qua `readSeq`; bỏ qua đọc khi `meetingId` khác meeting hiện tại, hoặc stale theo từng meeting); `LATEST_MISMATCH` là chuỗi chết, xóa cùng bản tiếng Anh khi `i18n.js` mở ở pha 5.
- Pha 4–5 của #118; `tr`, `setProp` lặp giữa `shell.js`, `connections.js` và `notes.js`, gom khi bỏ global ở pha 5.
- Góp ý pha 2 còn mở: cuộn tới mục Google trước khi view Google render xong, `gen` trong meta của `POST …/input`, descriptor còn trường không ai đọc, cờ descriptor thực chất là switch theo provider.

#### Liên kết issue/PR

Issue #126; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #128 (pha 4a của #118) — điều hướng và thư viện cuộc họp trong `history.js` chuyển sang reducer

#### Vấn đề

Phần điều hướng và thư viện của `history.js` (danh sách, tìm kiếm, lịch, chọn cuộc họp, tab, sửa tên/ngày, xóa) giữ trong object `state` sửa tại chỗ, chống response cũ bằng `loadSeq`, `searchSeq`, `saveSeq` và hai bộ hẹn giờ `searchTimer`, `tabTimer`. Thứ tự response (đổi cuộc họp giữa chừng, tìm kiếm gõ nhanh, lưu rồi đổi cuộc họp, xóa lỗi rồi tải lại) chỉ kiểm được bằng Playwright.

#### Nguyên nhân đo được

Đếm lần gán vào trường điều hướng/thư viện của `state` trong `git show 2819779:src/asr_en_vi/web/history.js`, bỏ chú thích, đếm từng lần xuất hiện (không phải dòng), cùng cách đếm với #126 (tính cả bí danh của `state`): gán và gán kép `<tên>(.trường|[chỉ số])+ (=|+=|-=|*=|/=|++|--)` vào các trường `calendar`, `deletionRequestMeetingId`, `deletionRequestPending`, `draft`, `editorBusy`, `loadSeq`, `meeting` (gồm `state.meeting.transcription =`), `meetings`, `notesCount`, `notesKnown`, `notesSourceLockKnown`, `notesSourceLocked`, `notesTaskStatus`, `query`, `recent`, `results`, `saveSeq`, `searchFailed`, `tab`, `tabTimer`, `total`, `view` = 54, cộng 3 phép gán qua bí danh `draft` của `state.draft` trong `showConflict` (`draft.original.title`, `draft.original.meeting_date`, `draft.version`) = 57; `++` tiền tố 3 (`++state.searchSeq`, `++state.loadSeq`, `++state.saveSeq`); đổi Map `deletionFailures.set|delete(` 3; tổng 63. Các trường task và transcript của `state` (`selectedGenerationId`, `taskPollId`, `transcriptMutation*`, `editingSegment`, …) và các phép gán vào `draft` của bản nháp transcript không được đếm. Năm bộ đếm và bộ hẹn giờ thuộc phần 4a: `loadSeq`, `saveSeq`, `searchSeq`, `searchTimer`, `tabTimer`. Các tên `*InFlight`/`*Timer` còn lại (`taskPollTimer`, `taskRequestInFlight`, `transcriptMutationInFlight`, `transcriptReloadInFlight`, `transcriptionRequestInFlight`) thuộc 4b/4c, giữ nguyên.

Nguồn: `git show 2819779:src/asr_en_vi/web/history.js`; issue #118.

#### Thay đổi thiết kế

- `history-core.js`: core thuần (`initialState`, `reduce`, các selector `selectHeading/Sidebar/Calendar/LockStatus/Editor/Deletion`) cùng hàm thuần chuyển từ `history.js` (ngày và lưới lịch, `meetingIdFromPathname`, chọn thế hệ transcript, tab mặc định). Địa chỉ trang (`pathname`, `search`, `hash`, tab hiện tại) vào qua trường `at` mà view đóng dấu lên mọi action; ngày hôm nay vào qua `history/booted`.
- `history.js`: ES module, một store `combine({history: reduce})`; `render` so sánh với bản đã vẽ và chỉ vẽ phần đổi; handler chỉ dispatch. Phần task (4b) và transcript (4c) còn mệnh lệnh, giữ trong object `legacy`; chúng báo sửa tại chỗ bằng `history/meeting-updated` và tải lại bằng `history/meeting-fetched`.
- Effect mới ở `history.js`: `history/list`, `/search`, `/search-timer`, `/tab-timer`, `/meeting`, `/save`, `/conflict`, `/delete`, và lệnh `address.push` qua `calls`.
- Route `/assets/history-core.js` có bảo vệ truy cập; thêm vào hai test asset. `shell.html` đổi `history.js` sang `type="module"`.
- Gate `tests/web/shell-view.test.mjs`: `history-core.js` vào CORES; `history.js` vào `PARTIAL` (phải là module, miễn quy tắc view cho tới khi 4c xong).
- Bảng trạng thái và chuyển trạng thái: `frontend-design.md` mục 11.8. ADR 0010 thêm một ý: địa chỉ vào core như dữ liệu `at`.

Khác biệt hành vi so với bản cũ (đều nhỏ, ghi để review): danh sách cuộc họp tải lại nhiều lần thì kết quả mới nhất theo id thắng (bản cũ: cái tới sau cùng); xóa ô tìm kiếm hủy luôn hẹn giờ và tìm kiếm đang chạy; lịch vẽ lại ngay khi danh sách về, còn thanh bên chờ kết quả tìm kiếm; `aria-current` ở thanh bên theo view ngay, `repaintSidebar` của phần cũ chỉ dựng lại khi dữ liệu đổi; tên và ngày đang sửa nằm trong state (sự kiện input), giá trị DOM đi kèm khi gửi; promise của `loadMeeting` trả `false` ngay lúc bị thay thế (bản cũ: khi response cũ tới); hộp thoại xóa hiện lỗi khi tải lại kết thúc hoặc bị thay; cài cuộc họp tạo bản sao đã gộp (bản cũ sửa object nhận về); phản hồi lưu không phải object tính là lỗi; tab chỉ vẽ lại khi đổi; ngày tính bằng số học ngày dân sự (cùng kết quả cho năm 1000–9999); focus bàn phím trên link ở sidebar được giữ khi danh sách dựng lại (bản cũ rơi về `body` khi tải xong hoặc làm mới danh sách); sau khi xóa thất bại, nút xác nhận tắt tới khi tải lại xong kể cả khi gõ lại tên (bản cũ bật lại nếu gõ lại tên); lần lưu metadata cũ của cuộc họp đã rời bị hủy khi lưu cuộc họp khác (khóa `history/save`, bản mới nhất thắng). Giữ như cũ: nút "Xóa ngày" gán `value` trực tiếp để bỏ ngày nhập dở; làm mới cùng cuộc họp vẽ tab trước khi báo `asr:meeting-context` cho `notes.js`, để focus tiêu đề nguồn không rơi vào panel đang ẩn. Số lần `asr:meeting-context` mỗi lần tải giữ 4.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test tests/web/*.test.mjs`, một lần chạy cục bộ; lần gán đếm như mục "Nguyên nhân đo được": 57 phép gán (54 + 3 qua bí danh) + 3 tiền tố + 3 lệnh Map = 63 trên `git show 2819779:src/asr_en_vi/web/history.js` bỏ comment. Sau: 0 trong mã 4a của `history.js` (mọi thay đổi điều hướng/thư viện đi qua `reduce`); còn 1 phép gán ở cầu nối 4b/4c (`editTranscription` ghi `meeting.transcription` vào cuộc họp của store) và các sửa tại chỗ phần tử thế hệ transcript (`generation.source_version`, `item.text`, danh sách người nói), mà số đếm trước không tính; hết khi 4b/4c chuyển. `history-core.js` tạo state mới nên không gán tại chỗ.

| | Trước | Sau |
| --- | --- | --- |
| Lần gán/đổi state điều hướng/thư viện trong `history.js` (gồm Map) | 63 | 0 ở mã 4a; 1 ở cầu nối 4b/4c |
| Bộ đếm và bộ hẹn giờ chống response cũ ở phần 4a | 5 | 0 (id trong state của core) |
| Unit test JS | 237 | 314 (77 cho core history) |
| `history.js` (dòng) | 4295 | khoảng 4,8 nghìn: view và phần cũ 3857 + core 972 (`wc -l`) |

Nguồn: `tests/web/history-core.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- Phần 4b/4c đọc state qua `legacy` và `nav()` chỉ để đọc; chúng sửa meeting tại chỗ rồi báo `history/meeting-updated` (chỉ nhận nếu đúng object store đang giữ; `editTranscription` truyền chính object nó vừa sửa, nên bản cầm từ trước một `await` bị bỏ qua). Đây là cầu nối tạm tới khi 4b/4c chuyển.
- `history.js` chưa vào gate view (VIEWS) vì còn mã 4b/4c mệnh lệnh; vào ở 4c.
- Chuỗi "Không thể tải dữ liệu cuộc họp." giữ làm giá trị dự phòng: `i18n.js` chưa mở và `test_i18n` đòi mọi mục catalog xuất hiện dạng `t("…")`.
- Tổng số dòng tăng vì reducer tạo state mới thay vì gán tại chỗ: đổi lại mọi chuyển trạng thái nằm ở một nơi và test được ngoài trình duyệt.

#### Follow-up

- 4b (task) và 4c (transcript) của #118, rồi `history.js` vào VIEWS; bỏ `legacy` và `history/meeting-updated`.
- Chuỗi dự phòng chết ở trên xóa cùng bản tiếng Anh khi `i18n.js` mở ở pha 5.

#### Liên kết issue/PR

Issue #128; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #130 (pha 4b của #118) — task phiên âm trong `history.js` chuyển sang reducer

#### Vấn đề

Phần task phiên âm của `history.js` (bắt đầu, hủy, poll tiến độ, request id chờ ở sessionStorage, ngôn ngữ, lỗi task, chẩn đoán) chạy mệnh lệnh trên object `legacy`, với hẹn giờ `taskPollTimer`, cờ `taskRequestInFlight` và chống response cũ bằng so sánh `taskPollId` với `nav().meeting`. Thứ tự response (poll tới sau khi rời cuộc họp, bắt đầu hoặc hủy bị lỗi mạng, hủy giữa chừng, bấm lặp, khôi phục request chờ sau tải lại, đổi nhịp khi trang ẩn) chỉ kiểm được bằng Playwright.

#### Nguyên nhân đo được

Đếm trên `git show e9b3e50:src/asr_en_vi/web/history.js` bỏ chú thích, cùng cách đếm với #126 và #128 (từng lần xuất hiện, không phải dòng): 7 trường task của `legacy` (`taskPollTimer`, `taskPollId`, `taskRequestInFlight`, `pendingTaskRequestId`, `transcriptionLanguageMeetingId`, `transcriptionLanguageSelection`, `taskRequestError`) được gán 18 lần (`legacy.<trường> =`: `taskRequestError` 5, `taskPollTimer` 3, mỗi trường còn lại 2) và đọc/gán tổng cộng 42 lần; hai tên `*Timer`/`*InFlight` của phần task (`taskPollTimer` 9 lần xuất hiện, `taskRequestInFlight` 7). `transcriptionRequestInFlight` (2 lần) là tên trường hợp đồng gửi cho `notes.js`, giữ nguyên.

Nguồn: `git show e9b3e50:src/asr_en_vi/web/history.js`; issue #118, #130.

#### Thay đổi thiết kế

- `task-core.js`: core thuần (`initialState`, `reduce`, `selectTask`, `selectRuntime`, `selectRequestBusy`, `displayedLanguage`, `diagnosticCommand`). Id request mới, nhịp ẩn/hiện của trang, chuỗi sessionStorage và khóa tự chạy vào qua action; ghi sessionStorage và xóa khóa tự chạy bằng effect `storage`; poll bằng `schedule` + `fetch`; sao chép lệnh chẩn đoán bằng effect `call`.
- Lát `task` cùng store với `history` (`combine({history, task})`). Đối tượng task ở lại trong cuộc họp của `history-core`; bốn action `task/poll-loaded`, `task/start-succeeded`, `task/cancel-succeeded`, `task/refresh-loaded` do cả hai lát xử lý (`history-core` đặt task vào cuộc họp mới tạo, không sửa tại chỗ). View đóng dấu mọi action bằng `shown` (cuộc họp lát history đang giữ, kể cả ở trang chủ hoặc khi cuộc họp khác đang tải) như `at`, và đổi `legacy.rev`, `readyRev` của `history-core` thành `task/meeting-left`, `task/meeting-ready`.
- `history.js`: bỏ 7 trường task khỏi `legacy`, bỏ `editTranscription`, `startTaskPolling`, `pollTask` và các hàm request id; `renderTranscription` chỉ đọc `selectTask`. `history-core.js` thêm bốn case dùng chung và xuất `failureOf`; cuộc họp nhận task mang khóa nguồn notes như đã báo (`notes-core` chỉ đọc lại `notes_source_locked` khi nhận object cuộc họp mới, mà object nay đổi mỗi lượt poll). Thanh bên chỉ vẽ lại khi id cuộc họp mở đổi; "Tải lại bản mới nhất" bỏ kết quả theo `installRev` thay vì so object cuộc họp (object nay đổi mỗi lượt poll, nên so object sẽ bỏ nhầm khi đang chạy task).
- Route `/assets/task-core.js` có bảo vệ truy cập; thêm vào hai test asset. Gate `shell-view.test.mjs`: `task-core.js` vào CORES.
- Bảng trạng thái và chuyển trạng thái: `frontend-design.md` mục 11.9. ADR 0010 thêm một ý về `shown` và action dùng chung giữa các lát. Không thêm loại effect.

Khác biệt hành vi so với bản cũ (đều nhỏ, ghi để review; đã đối chiếu từng ý với `git show e9b3e50`): chỉ một lần đọc poll chạy cho mỗi task (bản cũ: `visibilitychange` hoặc cuộc họp tải lại giữa lúc đọc đặt thêm timer, hai lần đọc có thể chồng nhau); request poll bị hủy khi rời cuộc họp (bản cũ để chạy rồi bỏ kết quả); nút Hủy giữ tắt tới khi yêu cầu hủy xong (bản cũ: lượt poll kế tiếp vẽ lại và bật nút, bấm được lần hai); câu trả lời bắt đầu hoặc hủy tới sau khi một cuộc họp khác đã tải xong không đổi cuộc họp đang hiện (ở trang chủ hoặc khi cuộc họp khác đang tải thì như bản cũ: câu trả lời áp vào cuộc họp history còn giữ và poll chạy tới khi task kết thúc; bản cũ sửa object cuộc họp cũ, đặt `taskPollId` và timer cho cuộc họp không còn hiện, và với lỗi bắt đầu ghi một dòng lên trạng thái của cuộc họp khác), nhưng request chờ vẫn được rút; lỗi của yêu cầu gắn với cuộc họp của nó (bản cũ: một biến chung hiện ở mọi cuộc họp tới khi có bắt đầu hoặc hủy thành công); ô ngôn ngữ nhớ lựa chọn của người dùng, không nhớ giá trị vừa vẽ (bản cũ ghi lại giá trị đang hiện mỗi lần vẽ); request chờ đọc từ sessionStorage một lần khi cuộc họp tải xong (bản cũ: mỗi lần vẽ khi bộ nhớ chưa có của cuộc họp đó); dòng "mất kết nối", "không tải được transcript mới" và kết quả sao chép giữ tới sự kiện task kế tiếp, khi lần tải lại cuộc họp xong hoặc khi rời cuộc họp (khi mở lại cùng cuộc họp, bản cũ xóa ngay lúc bấm; bản cũ: tới lần vẽ transcript kế tiếp, do bất kỳ nguyên nhân); body 2xx không phải object là lỗi rõ ràng (bản cũ: khi bắt đầu, `TypeError` hiện chữ lỗi của JS ở khối lỗi; khi hủy, task bị gỡ khỏi cuộc họp, nút Bắt đầu hiện và poll dừng, không báo lỗi; khi poll, task rỗng được vẽ rồi dòng "mất kết nối"); khóa tự chạy mà `removeItem` bị chặn thì vẫn chạy đúng một lần (bản cũ: không chạy); nút Hủy vẽ lại khi bấm nên `asr:meeting-context` có thêm một lần (cùng nội dung) lúc bấm hủy và lúc sao chép lệnh; thanh bên không còn dựng lại khi cuộc họp cùng id được thay bằng object mới (người dùng không thấy). Chuỗi `asr:meeting-context` phát cho `notes.js` khác ở số lần, giá trị cuối giống hệt; mỗi lần thêm là một lần `notes.js` đọc lại notes (`GET …/notes`): lúc bắt đầu phát `{inflight true, task trước đó}` rồi `{false, task nhận}` (bản cũ: thêm `{true, task nhận}` ở giữa); poll tới `succeeded` (có đọc lại cuộc họp) phát 3 lần (bản cũ 4), tới `canceled` như cũ; tải cuộc họp khôi phục request chờ từ sessionStorage thêm 1 lần; mở lại cuộc họp đang có dòng "mất kết nối", "không tải được transcript mới" hoặc kết quả sao chép thêm 1 lần; hủy lỗi hoặc thành công thêm 1 đến 2 lần, trước câu trả lời vẫn mang task cũ; bấm sao chép lệnh chẩn đoán phát 1 lần; lần poll hoặc đọc lại thất bại cũng phát, nên khi máy chủ mất kết nối, lần đọc notes kèm theo cũng lỗi và tab Notes hiện lỗi đọc tới lượt poll tốt kế tiếp (bản cũ: không phát, tab Notes không đổi). Tải lại cuộc họp giữa lúc task chạy (không có request chờ) như cũ. Giữ như cũ: nhịp 1000/3000 ms, thử lại 3000 ms, `transcriptionRequestInFlight` giữ `true` tới khi đọc lại cuộc họp xong, `asr:meeting-context` cho `notes.js` giữ hình dạng và thời điểm sau mỗi lần vẽ task.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test tests/web/*.test.mjs`, một lần chạy cục bộ; lần gán đếm như mục "Nguyên nhân đo được". Sau: 0 trường task trong `legacy`, 0 tên `taskPollTimer`/`taskRequestInFlight` trong `history.js` (cùng biểu thức chính quy trên bản hiện tại); `selectRequestBusy` thay cho cờ.

| | Trước | Sau |
| --- | --- | --- |
| Lần gán trường task của `legacy` | 18 | 0 |
| Tên `*Timer`/`*InFlight` của phần task trong `history.js` | 2 (16 lần xuất hiện) | 0 |
| Unit test JS | 314 | 399 (85 cho core task), 0,1 s |
| `history.js` (dòng) | 3857 | 3588 |
| `history-core.js` (dòng) | 972 | 998 (`wc -l`) |
| `task-core.js` (dòng) | 0 | 501 (`wc -l`) |

Nguồn: `tests/web/task-core.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- `shown` đóng dấu lên mọi action thay vì để lát `task` giữ bản sao của cuộc họp: một bản sao sẽ lệch khỏi `meeting.transcription.task` mà `history-core` giữ và `notes.js` đọc. Đổi lại mỗi action mang thêm một object nhỏ; reducer test dựng nó bằng một hàm cùng hình dạng.
- `legacy.rev` và `readyRev` đổi thành action trong view vì `history-core` không phát được action; phản ứng trực tiếp trên `history/meeting-requested` buộc `task-core` lặp lại phép chọn cuộc họp (địa chỉ, `popped`, `hash-changed`).
- Yêu cầu bắt đầu, hủy và đọc lại cuộc họp không bị hủy khi rời cuộc họp (server đã nhận; hủy giữa đường làm mất câu trả lời, nên request chờ rút chậm); khóa `task/start` dùng chung vì cờ cũ cũng dùng chung.
- Chữ trạng thái theo từng status và bảng nhãn giai đoạn ở lại view (`TASK_STAGES` còn được test tương thích đọc từ `history.js`).

#### Follow-up

- 4c (transcript) của #118, rồi `history.js` vào VIEWS; bỏ `legacy`, `editTranscription` đã bỏ ở 4b, còn `history/meeting-updated` cho transcript.
- Chuỗi "Chưa xác nhận được việc nhận tác vụ…" không còn hiện (dòng trạng thái giữ "Bản ghi đã lưu…" như bản cũ); `history.js` giữ nó làm literal chết để `test_i18n` không báo mục dư: xóa literal và bản tiếng Anh khi `i18n.js` mở ở pha 5.

#### Liên kết issue/PR

Issue #130; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #132 (pha 4c của #118) — sửa transcript trong `history.js` chuyển sang reducer

#### Vấn đề

Phần sửa transcript của `history.js` (chọn generation, trình sửa đoạn và đổi tên người nói, bản nháp theo generation, bốn lần ghi, xung đột 409 và "Tải lại bản mới nhất", dòng trạng thái và focus sau khi ghi) chạy mệnh lệnh trên 12 trường của object `legacy`, với cờ `transcriptMutationInFlight`, `transcriptReloadInFlight`, và sửa trực tiếp object cuộc họp trong store (`item.text = …`, `generation.source_version = …`, rồi `history/meeting-updated`). Thứ tự response (ghi tới sau khi cuộc họp được làm mới, đổi cuộc họp giữa lúc ghi, 409, bấm lưu lặp, bản nháp sống qua lần tải lại) chỉ kiểm được bằng Playwright.

#### Nguyên nhân đo được

Đếm trên `git show ee95686:src/asr_en_vi/web/history.js` bỏ chú thích (cùng cách đếm với #130: từng lần xuất hiện, `\b<tên>\b`; "gán" là `legacy.<trường> =` không phải `==`): 12 trường transcript của `legacy` (`selectedGenerationId`, `currentGenerationId`, `transcriptMutationInFlight`, `transcriptMutationFocus`, `…FocusAfter`, `…FocusDescriptor`, `transcriptMutationStatusDescriptor`, `transcriptMutationError`, `transcriptReloadInFlight`, `transcriptDrafts`, `editingSegment`, `renamingSpeaker`) có 134 lần xuất hiện, trong đó 32 lần gán (`transcriptDrafts` là `Map` sửa bằng `.set`/`.delete`, không đếm là gán); tên `*InFlight` của phần transcript: `transcriptMutationInFlight` (20 lần), `transcriptReloadInFlight` (8), `setTranscriptMutationInFlight` (hàm). `transcriptionRequestInFlight` là tên trường hợp đồng gửi cho `notes.js`, giữ nguyên.

Nguồn: `git show ee95686:src/asr_en_vi/web/history.js`; issue #118, #132.

#### Thay đổi thiết kế

- `transcript-core.js`: core thuần (`initialState`, `reduce`, `navigation`, `displayed`, `draftOf`, `fieldValue`, `hasUnsavedDrafts`, `speakerAddReady`, `awaitsLatestReload`). Bản nháp là object thường, không sửa tại chỗ; id lần ghi là bộ đếm trong state (không `crypto`); chữ gõ, giá trị ô chọn và hai descriptor của điều khiển đang dùng vào qua action; vị trí focus sau khi ghi là `focus {rev, mode, target, from}`; các bước DOM bản cũ làm theo thứ tự là hàng đợi `ops` mà view chạy rồi xác nhận (`transcript/painted`).
- Lát `transcript` cùng store với `history` và `task` (`combine({history, task, transcript})`). Câu trả lời của lần ghi là **một** action, `transcript/mutation-succeeded`, do cả hai lát xử lý: `history-core` đặt giá trị vào cuộc họp mới tạo (`applyFieldAnswer`, `applySpeakerAdded`, xuất ra để lát transcript tính cùng kết quả), không sửa tại chỗ. `transcript/reload-loaded` cũng vậy. View đóng dấu `shown` thêm `meeting`, `view`, `installRev`, và đổi `installRev` thành `transcript/meeting-installed`, `legacy.rev` (đổi cuộc họp) thành `transcript/meeting-switched`.
- `history.js`: bỏ 12 trường khỏi `legacy` (còn `notesSourceSegmentId` của 4d), bỏ `reportMeeting`, `history/meeting-updated`, mọi hàm lưu/mở/đóng trình sửa/tải lại/đối chiếu bản nháp; còn lại hàm dựng DOM, `runTranscriptOp` (chạy `ops`), `moveTranscriptFocus`, `paintTranscript`, `mutationFacts` và các handler chỉ dispatch. Sao chép bản nháp là effect `call transcript.copy`. `history-core.js` thêm `answerOf`, `applyFieldAnswer`, `applySpeakerAdded`, `isRecord` (core transcript dùng chung) và hai case dùng chung; cuộc họp nhận câu trả lời mang khóa nguồn notes như đã báo (như `takeTask`).
- Route `/assets/transcript-core.js` có bảo vệ truy cập; thêm vào hai test asset. Gate `shell-view.test.mjs`: `transcript-core.js` vào CORES.
- Bảng trạng thái và chuyển trạng thái: `frontend-design.md` mục 11.10, kể cả lỗi, xung đột và response cũ. ADR 0010 thêm một ý về lệnh vẽ là dữ liệu. Không thêm loại effect.

Khác biệt hành vi so với bản cũ (đều nhỏ; đã đối chiếu từng ý với `git show ee95686`): cuộc họp nhận giá trị đã lưu là object mới, không phải object sửa tại chỗ (bản cũ: `item.text = serverValue` trong `applyTranscriptMutationResult`), nên `notes.js` nhận object cuộc họp mới trong `asr:meeting-context` sau lần lưu (khóa nguồn notes giữ như đã báo; thêm người nói vẫn không báo `notes.js`, như cũ); dòng trạng thái chung chỉ được ghi khi chữ đổi (bản cũ ghi lại cùng chữ "Đang lưu…" ở mỗi lần đồng bộ điều khiển); nút "Thêm vào transcript hiện tại" cho một bản nháp đã mất không làm gì (bản cũ: `TypeError` ở `draft.value`, không bắt); ô tên người nói mới được core theo dõi bằng sự kiện `input` (bản cũ so với DOM lúc có kết quả): khác chỉ khi giá trị ô đổi mà không có sự kiện `input`; chữ báo kết quả sao chép ghi vào bản duyệt khớp generation, trường và mục (bản cũ: bản duyệt của nút bấm), khác chỉ khi hai bản duyệt cùng một bản nháp hiện cùng lúc (không xảy ra: bản duyệt trong bảng khôi phục chỉ có cho generation khác generation đang xem); việc chuyển nút tải lại giữa khối lỗi và trình sửa chạy khi đổi trạng thái mở/đóng, kể cả khi hàng không có trong DOM (bản cũ: bỏ qua nếu hàng không có); sau khi cài cuộc họp, phần transcript vẽ ở lượt dispatch kế tiếp (cùng đồng bộ), sau khi bản nháp được đối chiếu (bản cũ: đối chiếu rồi vẽ trong cùng lượt); bấm Lưu liên tiếp khi đang bận, trả lời cũ sau khi đổi cuộc họp và đổi generation hoạt động như cũ (đã kiểm bằng test Playwright hiện có). Các khác biệt còn lại, đều đã đối chiếu với `git show ee95686`:

- Lỗi cũ được sửa: đổi tên người nói khi một câu trả lời poll của tác vụ hoặc một lần tải lại cùng cuộc họp thay object cuộc họp trong lúc lần đổi tên đang bay làm `applyTranscriptMutationResult` ném `ReferenceError: item is not defined` (`item` khai báo `const` trong khối `if (generation) {}` ở trên, dùng ngoài khối, `history.js:1252` của bản cũ): bản cũ hiện lỗi đó ở dòng báo và dòng trạng thái chung, giữ ô đổi tên mở, bản nháp đã bị bỏ, không hiện "Đã lưu tên người nói."; bản mới cập nhật nhãn và hiện "Đã lưu tên người nói.".
- Thân phản hồi 2xx không phải object (chuỗi, `null`, mảng, số) là lần ghi lỗi ở cả hai lát, với chữ mặc định của từng loại ("Không thể lưu đoạn transcript.", "Không thể lưu tên người nói.", "Không thể lưu người nói.", "Không thể thêm người nói."); bản nháp và trình sửa giữ, trừ lựa chọn người nói (bản nháp bỏ như mọi lần lựa chọn lỗi). Bản cũ: với `null` hoặc thân không phải JSON, báo lỗi bằng chữ của `TypeError` do trình duyệt sinh (`result.text` trên `null`); với chuỗi, mảng hoặc số JSON, chạy nhánh thành công: không áp giá trị (phiên bản 0), bỏ bản nháp, đóng trình sửa về chữ cũ và báo chữ thành công của loại đó ("Đã lưu.", "Đã lưu tên người nói.", "Đã thêm người nói.", "Đã lưu lựa chọn người nói." với ô chọn về người nói cũ). Bản mới không phát `asr:meeting-context` cho trường hợp này (bản cũ: 1 lần với chuỗi, mảng hoặc số, trừ thêm người nói).
- "Tải lại bản mới nhất" thành công gửi 2 sự kiện `asr:meeting-context` (bản cũ 3: `meetingInstalled` vẽ lại và báo, rồi `renderTranscription` tường minh báo thêm một lần); payload như cũ, mỗi sự kiện là một lượt đọc của `notes.js`.
- Sau 409 của lựa chọn người nói, dòng trạng thái chung kết thúc bằng chữ xung đột; bản cũ kết thúc bằng "Thay đổi transcript đã hoàn tất." vì lần dựng lại bắt buộc chạy `syncTranscriptMutationControls` khi còn đang ghi và viết "Đang lưu…" đè lên chữ xung đột. Giữ hành vi mới.
- Cuộc họp hiện tại bị xóa khi một lần lưu đang bay: bản nháp đã lưu bị bỏ như cũ, kể cả tên người nói thêm (`hasUnsavedTranscriptDrafts()` là false); dòng trạng thái chung đọc "Thay đổi transcript đã hoàn tất." thay vì chữ thành công của bản cũ ("Đã lưu." cho chữ đoạn, "Đã lưu tên người nói." cho tên, "Đã thêm người nói." cho thêm và thêm lại); lựa chọn người nói như cũ (bản cũ đã trả về sớm và đọc "Thay đổi transcript đã hoàn tất."). Dòng này được ghi ngay vào view cuộc họp đang ẩn, như cũ.
- Mỗi lần kết quả được báo, dòng trạng thái chung được ghi lại kể cả khi chữ giống lần trước ("Không có thay đổi." hai lần liền, từ chối phiên bản lặp lại), như bản cũ, nên vùng aria-live đọc lại; chỉ "Đang lưu…" và các lần đồng bộ điều khiển không ghi lại cùng chữ.
- Lựa chọn người nói chỉ ghi dòng trạng thái chung, không ghi dòng `p.segment-edit-status` của hàng (bản cũ: descriptor `speaker-assignment` không có dòng đích).
- Đồng bộ điều khiển (`syncTranscriptMutationControls`) chạy khi lát transcript đổi, không chạy ở lượt vẽ mà lát không đổi (mỗi giây poll của tác vụ); các lần dựng lại hàng/chip/generation vẫn tự đồng bộ. Bản cũ chỉ đồng bộ khi vẽ khóa nguồn, tải lại bản mới nhất, dựng lại hàng hoặc chip, và khi bắt đầu/kết thúc một lần ghi; bản mới đồng bộ nhiều lần hơn (cả mỗi phím gõ), không thấy được vì kết quả chỉ phụ thuộc state.
- Mỗi phím gõ trong trình sửa chạy 2 dispatch (`transcript/draft-typed`, rồi `transcript/painted` vì mỗi lần nhớ bản nháp có một lệnh `review`) và 2 lượt vẽ, không đổi DOM ngoài bản duyệt bản nháp; ô thêm người nói thêm 1 dispatch và 1 lượt vẽ mỗi phím (`transcript/add-name-typed`). Bản cũ không dispatch gì khi gõ.
- Sao chép bản nháp dùng một khóa effect `transcript/copy` (mới nhất thắng): sao chép bản nháp thứ hai trước khi lần đầu xong thì bản duyệt đầu không nhận "Đã sao chép bản nháp." (bản cũ ghi cả hai); không xảy ra trong thực tế vì clipboard trả lời trong vài ms.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, một lần chạy cục bộ; lần xuất hiện và lần gán đếm như mục "Nguyên nhân đo được".

| | Trước | Sau |
| --- | --- | --- |
| Lần xuất hiện của 12 trường transcript của `legacy` trong `history.js` | 134 | 0 |
| Lần gán các trường đó | 32 | 0 |
| Tên `*InFlight` của phần transcript trong `history.js` | 3 (40 lần xuất hiện) | 0 |
| Unit test JS | 399 | 466 (62 cho core transcript) |
| `history.js` (dòng) | 3588 | 2748 |
| `history-core.js` (dòng) | 998 | 1095 (`wc -l`) |
| `transcript-core.js` (dòng) | 0 | 817 (`wc -l`) |

Nguồn: `tests/web/transcript-core.test.mjs`, `tests/web/history-core.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- `ops` là hàng đợi trong state thay vì view so hai state rồi suy ra DOM: bản cũ sửa DOM từng bước (thay một hàng, giữ trình sửa đang mở và con trỏ, cập nhật nhãn mà không dựng lại) và so hai state không tái hiện được thứ tự đó mà không dựng lại hàng đang gõ. Đổi lại state mang cả việc vẽ chờ xác nhận.
- `shown.meeting` là cả object cuộc họp, không phải bản sao các generation: core cần so với cuộc họp lúc gửi (`mutation.meeting`) để biết trang đã được làm mới trong lúc ghi; một bản sao sẽ lệch khỏi cuộc họp `history-core` giữ.
- Lát chỉ vẽ khi đã đồng bộ với cuộc họp cài, nên mỗi lần cài có một lượt vẽ trễ (cùng một lượt dispatch) trước khi transcript hiện; đổi lại bản nháp luôn được đối chiếu trước khi vẽ như bản cũ.
- Lựa chọn generation tính lại từ cuộc họp ở mỗi action (`navigation`), nhớ `lastCurrent` thay vì `legacy.currentGenerationId` gán lúc vẽ.

#### Follow-up

- 4d (theo dõi phát, cuộn tay, đánh dấu và tua nguồn notes), rồi `history.js` vào VIEWS; bỏ `legacy`.

#### Liên kết issue/PR

Issue #132; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #134 (pha 4d của #118) — phát lại và nguồn notes trong `history.js` chuyển sang reducer

#### Vấn đề

Phần phát lại của `history.js` (hàng đang phát theo thời gian bản ghi, tự cuộn theo, tạm dừng khi người dùng cuộn tay, đánh dấu và tua tới nguồn notes, cổng `playRecording`) chạy mệnh lệnh trên 3 biến `let` cấp module (`lastManualScroll`, `ownScrollUntil`, `activeSegmentId`), trường `legacy.notesSourceSegmentId` và đọc hàng thẳng từ DOM; `memo` và `noticeWritten` cũng giữ trạng thái đã vẽ ở cấp module. Vì vậy `history.js` còn nằm ngoài gate view (`PARTIAL`). Thứ tự (tua khi bản ghi chưa có metadata, đổi cuộc họp giữa lúc tua, cuộn tay sát mốc 2 giây) chỉ kiểm được bằng Playwright.

#### Nguyên nhân đo được

Đếm trên `git show ded1ced:src/asr_en_vi/web/history.js` bỏ chú thích (cùng cách đếm với #132: từng lần xuất hiện, `\b<tên>\b`): `legacy` 7 lần, `memo` 8, `lastManualScroll` 4, `ownScrollUntil` 5, `activeSegmentId` 3, `noticeWritten` 3; `legacy.notesSourceSegmentId =` được gán 2 lần; 4 dòng `let` ở cột 0 (`lastManualScroll`, `ownScrollUntil`, `activeSegmentId`, `noticeWritten`). Tên `transcriptionRequestInFlight` (2 lần) là tên trường hợp đồng gửi cho `notes.js`.

Nguồn: `git show ded1ced:src/asr_en_vi/web/history.js`; issue #118, #134.

#### Thay đổi thiết kế

- `playback-core.js`: core thuần (`initialState`, `reduce`, `activeRows`, `sourceRow`, `validSource`, `canPlay`, `isScrollKey`). Đồng hồ, dữ kiện âm thanh (`currentTime`, `paused`, `ended`, đã có metadata, cuộc họp của `<audio>`), hai dữ kiện bố cục (danh sách còn dưới thanh trình phát, focus ở ô nhập), cuộn/wheel/touch/phím cuộn và `prefers-reduced-motion` vào qua action; các hàng lấy từ `shown.rows` (đoạn của generation đang vẽ). Việc cần làm là dữ liệu: lệnh vẽ `ops` (`scroll`, `reveal`) view chạy rồi xác nhận bằng `playback/painted`; đặt thời gian `<audio>` là effect `call playback.seek`; chờ metadata là `schedule` 5000 ms.
- Lát `playback` cùng store với `history`, `task`, `transcript` (`combine({history, task, transcript, playback})`). `shown` thêm `tab`, `generationId`, `rows`; view đóng dấu mọi action bằng `now`.
- `history-core.js`: trường `legacy {rev, kind}` đổi tên thành `left`; view vẫn đổi bộ đếm đó thành `task/meeting-left`, `transcript/meeting-switched` và thêm `playback/meeting-left`.
- `history.js`: bỏ `legacy`, `memo`, `noticeWritten`, 3 biến `let` của phần phát lại, `waitForAudioMetadata`, `applyNotesSourceMark`, `followsPlayback`. Trạng thái đã vẽ nằm trong closure của subscriber; số của dòng trạng thái chung nằm ở `data-notice-rev` của chính phần tử. `transcriptionRequestInFlight` giữ nguyên tên (`notes.js` đọc), viết thành khóa có nháy; quy tắc "bộ đếm hoặc cờ yêu cầu" của gate view nay chỉ quét mã ngoài chuỗi. `history.js` vào `VIEWS` của gate (bỏ `PARTIAL`), `playback-core.js` vào `CORES`.
- Route `/assets/playback-core.js` có bảo vệ truy cập; thêm vào hai test asset.
- Bảng trạng thái và chuyển trạng thái: `frontend-design.md` mục 11.11, kể cả lỗi và response cũ. Không thêm loại effect.

Khác biệt hành vi so với bản cũ (đã đối chiếu từng ý với `git show ded1ced`):

- Tua đang đợi metadata bị bỏ (trả `false`) khi người dùng đổi cuộc họp, về trang chủ, hoặc khi generation đang xem đổi (người dùng chọn, hoặc lần chạy lại xong tự chuyển sang bản mới); bản cũ vẫn đặt thời gian khi `loadedmetadata` của bất kỳ cuộc họp nào tới (có thể đặt vào bản ghi của cuộc họp khác). Lần tua cùng đích (`meetingId`, `startSeconds`) với lần đang đợi nhập vào nó như bản cũ: cả hai cùng nhận kết quả (mở nguồn rồi bấm "Tua bản ghi" lúc chưa có metadata: cả hai `true`, `playRecording` vẫn chạy); mỗi lần vẫn có hạn 5000 ms riêng tính từ lúc nó gọi (lần đầu quá hạn trả `false`, lần sau vẫn đợi), như bản cũ; bản cũ đặt `currentTime` một lần cho mỗi lần đợi, nay một lần chung, cùng giá trị nên không khác quan sát được. Lần tua khác đích thay lần đang đợi và lần cũ trả `false`.
- `markNotesSource` gọi trong lúc store đang xử lý (từ `asr:meeting-context`) tô dấu khi lượt xử lý kết thúc thay vì ngay lập tức (cùng một tác vụ, trước khi trình duyệt vẽ); giá trị trả về vẫn đồng bộ và cùng quy tắc.
- Hàng đang phát tính từ đoạn của generation đang vẽ thay vì đọc DOM: khác chỉ trong khoảng giữa lúc state chọn generation khác và lúc danh sách được vẽ lại (không hàng nào đang phát trong khoảng đó; bản cũ dùng hàng của danh sách cũ).
- Dữ kiện bố cục (hai lần `getBoundingClientRect` và `activeElement`) đọc ở mỗi mẫu khi đang phát, không chỉ khi hàng hiện tại đổi; chỉ đọc, không thấy được.
- Lỗi `error` của `<audio>` luôn được lắng nghe (bản cũ chỉ trong lúc đợi metadata); không có tác dụng khi không có lần tua đang đợi.
- Phần tử `#transcript-mutation-status` thêm thuộc tính `data-notice-rev`; không thấy được.
- Cuộn, wheel, chạm và phím cuộn nay là action (mỗi sự kiện một lần dispatch nhẹ, không có `at` và `shown`); các lượt vẽ chỉ-phát-lại bỏ qua phần còn lại của `render`.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, một lần chạy cục bộ; lần xuất hiện đếm như mục "Nguyên nhân đo được".

| | Trước | Sau |
| --- | --- | --- |
| Lần xuất hiện của `legacy`, `memo`, `lastManualScroll`, `ownScrollUntil`, `activeSegmentId`, `noticeWritten` trong `history.js` | 30 | 0 |
| `let` cấp module trong `history.js` | 4 | 0 |
| Unit test JS | 466 | 510 (44 cho core phát lại) |
| `history.js` (dòng) | 2748 | 2738 |
| `playback-core.js` (dòng) | 0 | 187 (`wc -l`) |

Nguồn: `tests/web/playback-core.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- Lát riêng thay vì mở rộng `transcript-core.js`: phát lại có đồng hồ, cuộn tay và lần tua riêng, không dùng bản nháp hay lần ghi của transcript; hai lát độc lập để mỗi core nhỏ và test riêng.
- Hàng cho core là mảng đoạn của generation (qua `shown.rows`) thay vì danh sách đo từ DOM: core quyết định hàng nào đang phát từ dữ liệu, view chỉ tô. Đổi lại view phải khai báo generation nào đang được vẽ (khóa vẽ của danh sách).
- `reveal` đo bố cục lúc vẽ (view báo lại `scroll-started`) thay vì view đưa bố cục của mọi hàng vào action: core chưa biết hàng nào được chọn khi nhận `mark-requested`.
- `playRecording` không có state: cổng tab là hàm thuần của core, còn `audio.play()` là việc của view.

#### Follow-up

- Phase 5 (#135): bỏ `window.ASRMeetingHistory` và `CustomEvent` còn sót, xóa code chết.

#### Liên kết issue/PR

Issue #134; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### #135 (pha 5 của #118) — bỏ global và `CustomEvent`, xóa code chết, gate quét mọi file

#### Vấn đề

Sau pha 4d mọi tính năng đã có reducer, nhưng bốn đường nối cũ vẫn còn: `window.ASRMeetingHistory` (lệnh từ `notes.js` tới `history.js`), `asr:meeting-context` (`CustomEvent` từ `history.js` tới `notes.js`), `asr:provider-choice-changed` (effect `event` từ `provider-core.js` tới `notes.js`) và `Object.assign(window, …)` trong `shell.js` (helper cho `history.js`, tới lúc đó ngầm là global). Gate view chỉ quét các file trong danh sách, nên một file mới hay `access.js`, `theme.js`, `i18n.js` nằm ngoài. Còn code chết để lại từ các pha trước và bốn bản sao helper (`tr`, `setProp`, `setAttr`).

#### Nguyên nhân đo được

Đếm trên `git show 4e29c59:src/asr_en_vi/web/*.js` (mọi file `.js`, từng lần xuất hiện): `CustomEvent` 3, `ASRMeetingHistory` 7, `asr:meeting-context` và `asr:provider-choice-changed` 6, `Object.assign(window` 1; `function tr(` 4 bản, `function setProp(` 4, `function setAttr(` 3. 32 `export` không file web nào khác và không test nào dùng. Hằng `LATEST_MISMATCH` (notes-core), literal `UNUSED_START_UNCONFIRMED` (history) và các mục EN của chúng không có nơi dùng; `ApiError` và `requestJson` (shell.js) không có nơi gọi nào sau pha 4 (`grep` trên toàn `web/`).

Nguồn: `git show 4e29c59`; issue #118, #135.

#### Thay đổi thiết kế

- `channel.js` (mới, 16 dòng): hai store dựng bằng `createStore`, `meetingContext` (action `channel/meeting-context {detail}`, state là `detail` cuối) và `providerChoice` (action `channel/provider-choice-changed`, state là bộ đếm). `history.js` dispatch lên `meetingContext` với một `detail` mới mỗi lần (cùng điểm báo, cùng trường, kể cả khóa có nháy `"transcriptionRequestInFlight"`); `notes.js` đăng ký, đổi thành `notes/context`, và vẫn đọc `currentContext()` đúng chỗ cũ (sau khi đăng ký). Subscriber chạy đồng bộ trong `dispatch`, theo thứ tự báo. Chi tiết: `frontend-design.md` 11.12, ADR 0010.
- Lệnh `notes.js` → `history.js`: `window.ASRMeetingHistory = Object.freeze({…})` thành `export const meetingHistory = Object.freeze({…})` (cùng hàm, cùng giá trị trả về); `notes.js` import nó, bỏ `meetingHistory()`, `toHistory` và mọi nhánh `Boolean(meetingHistory()) &&` (`history.js` luôn chạy trước).
- Bỏ loại effect `event` và tham số `emit` của runner (còn tám loại). `provider-core.js` phát `call provider.choice-changed` đúng chỗ cũ (cùng số lần); `connections.js` nối nó vào `providerChoice.dispatch`.
- `shell.js`: bỏ `Object.assign(window, …)`, `ApiError`, `requestJson`; export `tr`, `setProp`, `setAttr`, `formatMeetingDate`, `sameOriginAudioSource`, `meetingIdFromPath` và `history.js`, `notes.js`, `connections.js`, `antigravity.js` import chúng; còn 1 bản `tr`, `setProp`, `setAttr` (trước: 4, 4, 3). `tr` của shell xử lý thêm `fmt: "timestamp"` (bản của `connections.js`), gộp thành một; bản của `notes.js` và `history.js` là phần chung, nên không khác quan sát được. Không có global ngầm nào khác: mọi tên được dùng mà không khai báo trong file là `t`/`UI_LOCALE` (i18n) hoặc API trình duyệt (kiểm bằng phân tích tên chưa khai báo từng file).
- `access.js` là module (`type="module"` thay `defer`) và qua các quy tắc view của gate (không có core, xem mục dưới). `theme.js`, `i18n.js` giữ là script thường: ngoại lệ ghi trong ADR 0010 (chạy trước lần vẽ đầu; `t`/`UI_LOCALE` chỉ-đọc); `i18n.js` không có hàm cấp cao nhất nào khác ngoài binding `t`, `UI_LOCALE`.
- Gate (`tests/web/shell-view.test.mjs`): mọi `web/*.js` nằm đúng một trong `VIEWS`, `CORES`, `EXCEPTIONS`; quy tắc không-global/không-`CustomEvent` quét mọi file; trang không có script nội tuyến và chỉ ngoại lệ được nạp không-module; ngoại lệ chỉ khai báo tên trong danh sách cho phép; một tên từng là global mà không import thì fail. Quy tắc cũ giữ nguyên, không nới. Mỗi quy tắc mới có ví dụ xấu trong cùng test.
- Code chết đã xóa: `LATEST_MISMATCH` và mục EN, `UNUSED_START_UNCONFIRMED` và mục EN, chuỗi "Không thể tải dữ liệu cuộc họp." (fallback của `failureOf` trong `meetingLoadFailed`: lỗi từ runner luôn có `status` hoặc `message` khác rỗng nên nhánh fallback không bao giờ chạy trong trình duyệt; còn lại dùng câu mất kết nối mặc định) và mục EN, `BY_ID` (provider-core), `repaintSidebar()` của `history.js` và action `history/sidebar-repainted` (không ai gọi, cũng chết ở `4e29c59`; helper `repaintSidebar(state)` của core còn dùng), `effort.values` của Antigravity, `ApiError`, `requestJson`, 32 từ khóa `export` không ai import (hàm giữ lại vì nơi khác trong file dùng). `ANTIGRAVITY.login.firstPoll` được giữ: `pollWait(d, d.login.firstPoll, …)` đọc nó cho cả hai provider.
- Route `/assets/channel.js` có bảo vệ truy cập; thêm vào hai test asset.
- `access.js` không có core: trang trước đăng nhập chỉ có một form và một request, state duy nhất là nút đang bận; ADR 0010 ghi nó là ngoại lệ có lý do.
- Hook Playwright: `window.ASRMeetingHistory.*` (12 chỗ trong `tests/test_browser_*.py` và 1 trong `scripts/offline-smoke.py`) thành `import("/assets/history.js")` rồi `.meetingHistory.*`; `dispatchEvent(new CustomEvent('asr:provider-choice-changed'))` thành `import("/assets/channel.js")` rồi `providerChoice.dispatch(...)`. Cùng instance module với trang. Không sửa assertion về hành vi.
- Test flaky `test_incomplete_pending_request_waits_for_choice_loading_before_completion`: `localStorage` ghi bằng `page.add_init_script` trước `goto` thay vì sau `goto` (bản cũ ghi sau khi trang đã tải, có thể trước hoặc sau lúc notes đọc).

Khác biệt hành vi so với bản cũ (đối chiếu với `git show 4e29c59`):

- Tin báo lồng (báo trong lúc subscriber của kênh còn chạy) được xếp hàng, và nhiều tin lồng như vậy tới subscriber thành MỘT lần giao với state cuối (`providerChoice`: một lần giao, bộ đếm nhảy N); `dispatchEvent` cũ giao từng tin lồng ngay. Đã đo: toàn bộ suite trình duyệt (378 test, 2506 lần báo) và lần chạy đối chiếu 32 bước thấy 0 tin lồng, vì mọi chỗ báo chạy trong lần vẽ của history hoặc trong callback hoãn, và lệnh của subscriber notes tới history được store history xếp hàng. Thứ tự giữ nguyên. Không có kịch bản Playwright hiện có phụ thuộc vào điều này.
- Một lỗi tải cuộc họp có `error` không có `status` và `message` rỗng nay hiện câu mất kết nối thay cho "Không thể tải dữ liệu cuộc họp."; runner không tạo lỗi như vậy từ `fetch`.
- Một lỗi ném trong subscriber của kênh (lỗi lập trình trong reducer hoặc render của notes) nay đi ngược về nơi báo: lượt vẽ của history (hoặc lượt drain của store provider) dừng ở chỗ báo, như mọi lỗi subscriber của store (ADR 0010); `dispatchEvent` cũ ghi lỗi của listener rồi chạy tiếp. Tương tự, `notes.js` nay import `history.js` và `shell.js`, nên một lỗi khi nạp hai module đó làm `notes.js` không chạy. Không có đường chạy bình thường nào ném lỗi như vậy.
- Ngoài các mục trên, không còn khác biệt thấy được.

#### Số đo trước và sau

Điều kiện đo: Node 24.13.0, `node --test 'tests/web/*.test.mjs'`, một lần chạy cục bộ; đếm lần xuất hiện bằng `grep -o` trên mọi `src/asr_studio/web/*.js`, trước = `git show 4e29c59:<file>`.

| | Trước | Sau |
| --- | --- | --- |
| `CustomEvent`, `ASRMeetingHistory`, `asr:meeting-context`/`asr:provider-choice-changed`, `Object.assign(window` | 3, 7, 6, 1 | 0, 0, 0, 0 |
| Bản sao `tr`, `setProp`, `setAttr` | 4, 4, 3 | 1, 1, 1 |
| Loại effect | 9 | 8 |
| `export` cấp module trong `web/*.js` | 148 | 121 |
| Mục EN trong `i18n.js` | 715 | 712 |
| File `web/*.js` | 16 | 17 (`channel.js`) |
| Dòng `web/*.js` | 11291 | 11189 |
| Test trong `shell-view.test.mjs` | 8 | 13 |
| Unit test JS | 510 | 523 (10 cho kênh, 5 mới cho gate, bớt 1 test effect `event`, bớt 1 test của `history/sidebar-repainted`) |

Nguồn: `tests/web/channel.test.mjs`, `tests/web/shell-view.test.mjs`.

#### Đánh đổi và phương án đã loại

- Hai store nhỏ làm kênh thay vì một store cho cả trang: mỗi view giữ store, runner và phụ thuộc riêng; một store chung phải gộp mọi `calls` và `now` vào một runner.
- `call` vẫn là effect cho lệnh `notes` → `history` (có giá trị trả về hoặc lời hứa), không đổi thành action: hai store khác nhau không dùng chung hàng đợi action.
- Giữ `i18n.js`, `theme.js` là script thường thay vì module: chạy trước lần vẽ đầu và cung cấp `t` cho mọi module; ngoại lệ có lý do và gate (danh sách tên cho phép) chặn chúng lớn thêm.
- `store.js` ở `EXCEPTIONS` thay vì `CORES`: nó nhận `fetch`, timer, storage làm phụ thuộc nên tên tham số trùng quy tắc "core thuần".

#### Follow-up

- Quy tắc "tên từng là global" là danh sách cố định; phát hiện tên chưa khai báo tổng quát cần trình phân tích cú pháp (không thêm dependency).

#### Liên kết issue/PR

Issue #135; thuộc #118.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — kiến trúc frontend.

### Tổng kết refactor #118

Mục tiêu: đưa `src/asr_studio/web/` từ JS dùng global và state sửa tại chỗ sang core reducer thuần, effect là dữ liệu, store tự viết (ADR 0010), không đổi hành vi người dùng thấy, không framework, không build step, không dependency runtime, không nới CSP.

| Pha | Issue / PR | Chuyển gì |
| --- | --- | --- |
| 0 | #119 / PR #120 | ADR 0010, `store.js` (`createStore`, `combine`, `createRunner`), `node --test` và Node trong CI |
| 1 | #121 / PR #122 | `shell-core.js` + `shell.js`: dialog tạo cuộc họp và upload |
| 2 | #123 / PR #124 | `provider-core.js`: một core cho ChatGPT và Google Antigravity, `connections.js`, `antigravity.js` |
| 3 | #126 / PR #127 | `notes-core.js` + `notes.js`: tab Meeting notes |
| 4a | #128 / PR #129 | `history-core.js`: điều hướng và thư viện cuộc họp |
| 4b | #130 / PR #131 | `task-core.js`: task phiên âm |
| 4c | #132 / PR #133 | `transcript-core.js`: sửa transcript và người nói |
| 4d | #134 / PR #136 | `playback-core.js`: phát lại, tua nguồn notes |
| 5 | #135 | `channel.js`; bỏ global, `CustomEvent`, effect `event`; `access.js` thành module qua quy tắc view; gate quét mọi file; xóa code chết |

Bảng state và chuyển trạng thái của từng core nằm ở `frontend-design.md` 11.5 đến 11.12; chi tiết, số đo và khác biệt hành vi từng pha ở các mục #119 đến #135 phía trên.

Gate trong CI (`tests/web/shell-view.test.mjs`, chạy trong `scripts/check.sh`): mọi `web/*.js` thuộc đúng một trong view, core, ngoại lệ; view không gán state hay giữ `let`/`var`/object cấp module; core không chạm DOM, mạng, timer, storage, đồng hồ, ngẫu nhiên; không file nào gán `window`/`globalThis`, dùng `CustomEvent` hay sự kiện `ns:tên`; trang không có script nội tuyến; ngoại lệ (`i18n.js`, `theme.js`, `store.js`) chỉ khai báo tên trong danh sách cho phép.

Unit test JS: 0 (trước pha 0) → 19 (pha 0) → 510 (hết pha 4d) → 523 (pha 5). Playwright giữ nguyên, trừ hook kỹ thuật ở pha 5.

Mục tùy chọn trong comment của #118: pha 5 xử lý `LATEST_MISMATCH` và mục EN của nó, `ANTIGRAVITY.effort.values` (xóa), test flaky `test_incomplete_pending_request_waits_for_choice_loading_before_completion` (`add_init_script` trước `goto`); `ANTIGRAVITY.login.firstPoll` giữ vì `pollWait` đọc nó cho cả hai provider; `memo.rendered`, các `let` cấp module và `PARTIAL` đã xong ở 4d.

Còn mở sau #118 (không đổi hành vi nên không làm trong pha 5): trình duyệt tự đóng dialog tạo cuộc họp giữa lúc lưu rồi mở lại (pha 1); cuộn tới mục Google trước khi view Google vẽ xong, và trả lời `POST …/input` của Google chưa kiểm thế hệ đăng nhập (pha 2); `readId` dùng chung giữa các cuộc họp (pha 3); khóa `history/save` latest-wins theo cuộc họp, poll cũ hơn câu trả lời hủy, `shownOf` thuần cho harness task (4a đến 4c); khóa `transcript/copy` latest-wins và chi phí mỗi phím gõ trong trình sửa (4c); hơn mười cờ descriptor provider còn là switch theo provider; live region không ghi lại khi chữ không đổi (chỉ sửa nếu kiểm bằng trình đọc màn hình thấy cần).

### #145 / PR #146 — upload file theo phần byte qua tunnel giới hạn body

#### Vấn đề

Cloudflare Tunnel có thể từ chối một request body khoảng 100 MiB. Upload cũ gửi toàn bộ file trong
một `PUT`, dù app cho phép file tới 4 GiB. Upload audio lớn vì vậy không thể tạo meeting.

#### Nguyên nhân đo được

Proxy proof dùng giới hạn đúng 104.857.600 byte. File WAV hợp lệ có 184.320.078 byte. Probe bị từ
chối gửi 104.857.601 byte (giới hạn + 1); việc một request toàn bộ file vượt giới hạn là suy ra từ
hai kích thước này. Ba request phần 67.108.864 byte hoặc nhỏ hơn đi qua proxy và tạo meeting.

Nguồn: `timeout 600 .venv/bin/python scripts/prove_chunked_upload.py --duration 960`.

#### Thay đổi thiết kế

- Migration schema 13 thêm `uploads.received_bytes`, mặc định `0`, có CHECK không vượt
  `declared_size`. Database schema cũ v0.1.2 đọc được và backfill về `0`.
- `PUT /api/uploads/{id}/content?offset=N` ghi tuần tự từ offset bền. Server cắt file tạm về
  offset đã commit, ghi phần mới, `fsync`, rồi cập nhật DB. `offset_mismatch` trả 409 và byte đã
  nhận. Writer đang chạy trả `upload_busy` cùng byte đã nhận; disconnect, cancel hoặc idle timeout
  giữ upload ở `receiving` và nhả single-writer guard. Khi request bị cancel, thao tác ghi/fsync/
  commit đang sở hữu được drain tới khi kết thúc trước khi cleanup đọc offset DB, truncate, đóng
  file hoặc nhả guard; nhờ đó worker cũ không thể ghi đè hoặc nhả guard của request mới.
- `ASR_UPLOAD_CHUNK_BYTES` mặc định 64 MiB và được công bố trong policy `chunk_bytes`. Browser
  dùng `File.slice`, gửi offset tuần tự, hiển thị progress cộng dồn, đọc lại trạng thái và thử
  lại cùng phần tối đa sáu lần với backoff mũ 1/2/4/8/16/32 giây (tổng 63 giây, vượt idle
  timeout writer 60 giây); GET trạng thái tạm thời dùng cùng giới hạn.
- `shell-core.js` giữ `uploadOffset`, `uploadRetries` và retry hữu hạn cho GET trạng thái. `store.js` giữ ngữ nghĩa XHR hiện có.
  Không đổi CSP, không tách hoặc nối audio ở server, không thêm dependency.

#### Số đo trước và sau

Điều kiện: revision làm việc của issue #145, Chromium headless, proxy Python giới hạn 100 MiB,
file WAV 16 phút và các lệnh ghi ngay cạnh số đo.

| Chỉ số | Trước | Sau |
| --- | --- | --- |
| Kích thước request content | 104.857.601 byte trong probe bị proxy từ chối (file 184.320.078 byte; full-file rejection là suy ra) | Tối đa 67.108.864 byte mỗi `PUT` |
| Proof qua proxy | HTTP 413 | 3 phần, upload `ready`, tạo meeting thành công |
| Backend/config/migration tests | Không có offset/resume coverage | 38 tests passed |
| Browser chunk proof | Không có | 1 Playwright test passed, gồm một request bị abort và resume |
| Browser RSS khi extract video 1 giờ | Chưa đo | +759.345.152 byte; JS heap +2.318.849 byte |
| Upload audio đo được sau extract 1 giờ | Chưa đo | 67.177.975 byte; tuyến tính 4 giờ ≈268.711.900 byte |

Commands:

```sh
timeout 600 .venv/bin/python scripts/prove_chunked_upload.py --duration 960
timeout 900 .venv/bin/python -u scripts/measure_browser_extract_memory.py --duration 3600
timeout 180 env ASR_RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_browser_create_flow.py -k browser_uploads
timeout 180 .venv/bin/python -m pytest -q tests/test_recordings.py tests/test_storage.py tests/test_transcription_config.py
```

#### Đánh đổi và phương án đã loại

- Upload theo phần giữ file tạm và trạng thái resume trong SQLite. Đổi lại, server phải truncate và
  `fsync` mỗi phần, còn browser phải đọc trạng thái sau lỗi mạng.
- Retry tự động tối đa sáu lần cho phần hiện tại, với cửa sổ backoff 63 giây để có thể vượt qua
  một writer bị giữ bởi tunnel. Retry không gửi lại inference và không tạo meeting mới.
- BufferTarget của bước extract video vẫn giữ audio trong bộ nhớ. Số đo một giờ cho thấy RSS tăng
  khoảng 724 MiB. Tuyến tính bốn giờ có thể cần thêm khoảng 256 MiB (269 MB) chỉ cho output audio,
  chưa tính các bản copy. Không hứa hỗ trợ bốn giờ video trên browser thiếu bộ nhớ.
- Không dùng một request lớn hơn edge limit, không chia audio thành nhiều file, và không nới CSP.

#### Follow-up

- Cần quyết định giới hạn sản phẩm cho video import dài hoặc triển khai streaming extraction trước
  khi hứa hỗ trợ video bốn giờ trên browser hạn chế bộ nhớ.
- Có thể thay proxy proof bằng nginx/Cloudflare staging khi môi trường tunnel sẵn sàng.

#### Liên kết issue/PR

Issue #145, PR #146; liên quan #117.

#### Tag

Hiệu năng; Kiến trúc

#### Tài liệu đích

- `docs/backend-contract.md` — giao thức upload theo phần byte.
- `docs/frontend-design.md` — state và retry của shell upload.
- `docs/user-guide.md` — flow upload và giới hạn bộ nhớ khi extract video.

### #117 / PR #142 — tách audio khỏi video ngay trên trình duyệt trước khi upload

#### Vấn đề

Truy cập từ xa qua Cloudflare Tunnel bị chặn request body trên khoảng 100 MB ở edge, trong khi `uploadWithProgress` gửi cả file trong một PUT. Video họp khoảng 1 giờ thường lớn hơn 100 MB nên không tải lên được, dù giới hạn của app là 4 GiB. Server chỉ giữ audio (`recordings/{id}.m4a`, AAC 160k), nên phần hình chỉ tốn băng thông.

#### Nguyên nhân đo được

Mẫu thử: video 1 giờ 640x360, H.264 khoảng 1 Mbit/s, AAC 160 kbit/s, `moov` ở cuối file (kiểu ghi của nhiều công cụ họp). File gốc 517.496.558 byte; phần audio 67.177.975 byte (khoảng 13 %), dưới 100 MB.

Nguồn: lệnh tạo mẫu bên dưới; Cloudflare 413 sau khoảng 3 MB với PUT 150 MB, 5 MB qua được (quan sát trong issue #117).

#### Thay đổi thiết kế

- `audio-extract.js` (mới, nạp bằng `import()` khi gặp video đầu tiên) dùng Mediabunny 1.61.6 để đọc file theo luồng và copy gói AAC (`mp4`/`mov` → `.m4a`) hoặc Opus (`webm` → `.webm`), không giải mã hay mã hóa lại (`copy: {mode: "forced"}`, video bị bỏ). Codec khác, không có audio, thiếu API hoặc lỗi đọc: ném lỗi có `kind`.
- Thư viện vendor nguyên bản (MPL-2.0, mức file) ở `web/vendor/mediabunny/` cùng `LICENSE` và `PROVENANCE.md` (tarball npm, tích hợp sha512, sha256 của file); `release-inventory.py` ghi mục `web_vendor` như `fonts`, `collect-release-notices.py` đếm nó. CSP không đổi: không CDN, không WASM, không `wasm-unsafe-eval`; lý do loại ffmpeg.wasm nêu trong issue.
- `store.js` thêm effect `extract` (`deps.extract(file, {signal, onProgress})`, cùng quy tắc latest-wins/cancel như `upload`); thiếu `deps.extract` thì trả lỗi `unsupported`.
- `shell-core.js` thêm `phase` `extracting`, `sendFile`, `notice`. Sau khi dọn upload cũ, file video đi qua `extracting`; kết quả (audio hoặc file gốc khi lỗi) quyết định tên và `size_bytes` gửi ở `POST /api/uploads` và nội dung PUT. Hủy được cả ở bước tách (không có slot nên không DELETE). Khi sửa này `cancelUpload` giữ các effect `cancel` ngay cả khi chưa có `uploadId`.
- API server không đổi; server vẫn chuyển mọi đầu vào thành M4A/AAC. Bảng chuyển trạng thái ở `frontend-design.md` 11.5.

#### Số đo trước và sau

Điều kiện: Chromium headless (Playwright) trên máy 32 luồng, giao diện thật; thời gian từ lúc bấm **Tạo cuộc họp** tới lúc nhãn thành **Đang tải lên** (gồm nạp thư viện, đọc file 517 MB từ đĩa, ghi bộ nhớ); PUT bị giữ nên không tính thời gian truyền. Ba lần chạy liên tiếp. Tạo mẫu:

```sh
ffmpeg -f lavfi -i testsrc2=size=640x360:rate=10 -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -t 3600 -c:v libx264 -preset veryfast -b:v 1000k -maxrate 1200k -bufsize 2M -pix_fmt yuv420p \
  -c:a aac -b:a 160k hour.mp4
```

| | Trước | Sau |
| --- | --- | --- |
| Byte gửi lên (1 giờ) | 517.496.558 | 67.177.975 |
| Thời gian chuẩn bị trước khi gửi | 0 s | 6,43 s; 6,33 s; 6,35 s |

Nguồn: lần đo cục bộ cho #117 (script đo nằm ngoài repo). Máy chậm hơn, đĩa chậm hoặc file lớn hơn cho thời gian dài hơn; thời gian chủ yếu là đọc file.

#### Đánh đổi và phương án đã loại

- Mediabunny: thư viện JS thuần, không WASM, đọc Blob theo luồng; đổi lại thêm một file vendor lớn (bản `min.mjs` khoảng 0,7 MB) và nghĩa vụ MPL-2.0 mức file (giữ nguyên file, kèm license và nguồn). Issue ghi MIT là sai; giấy phép đúng là MPL-2.0.
- Audio tách ra giữ trong bộ nhớ (BufferTarget) trước khi gửi: khoảng 72 MB cho 1 giờ ở 160 kbit/s là kích thước đầu ra, không phải đỉnh bộ nhớ: đỉnh gồm BufferTarget, bản copy và `File` tạo ra nên có thể gấp vài lần; audio dài hơn nhiều giờ dùng nhiều bộ nhớ hơn tương ứng.
- Track audio bắt đầu muộn hơn hình: file tách ra và bản server chuyển mã từ video gốc đều bắt đầu ở 0, nên hai bên khớp nhau (kiểm bằng so sánh PCM trong `tests/test_browser_audio_extract.py`); lệch so với hình có sẵn từ trước #117.
- Chỉ AAC (mp4/mov) và Opus (webm) được copy; các codec khác gửi nguyên file (không mã hóa lại ở trình duyệt).
- ffmpeg.wasm bị loại: khoảng 30 MB WASM, cần nới CSP `wasm-unsafe-eval`, nạp cả file vào RAM, thêm LGPL vào inventory.

#### Follow-up

- Upload chia phần cho file audio vẫn trên khoảng 100 MB (audio dài hơn khoảng 1,4 giờ ở 160 kbit/s, hoặc file gửi nguyên): ngoài phạm vi, cần issue riêng.
- Tệp `docs/release/runtime-inventory-amd64.json` và `third_party/NOTICE-MANIFEST.json` là bằng chứng sinh từ image; phải sinh lại ở lần build release kế tiếp để có mục `web_vendor`.

#### Liên kết issue/PR

Issue #117.

#### Tag

Hiệu năng; Kiến trúc; Quyền riêng tư

#### Tài liệu đích

- `docs/release/performance-v0.1.0.md` — số đo.
- `docs/release/runtime-v0.1.0.md` — luồng upload và thư viện vendor.
- `docs/release/privacy-v0.1.0.md` — file video không rời trình duyệt khi tách được; không có kết nối mới.

## 4. Mốc AC-13 (#16 → #99 → #104)

Đã chuyển bảng mốc và điều kiện đo vào [báo cáo nghiệm thu](acceptance-report.md).

## 5. Đang làm / tiếp theo

Các kết quả chưa đạt và follow-up nằm trong [release notes](release/notes.md).

### #83 — Tiến độ và trạng thái task phản ánh đúng giai đoạn

#### Vấn đề

Issue #83 ghi nhận thanh ASR báo 99–100% trong diarization, upload không có số đo khi server kiểm tra, notes dài không cho biết chunk/merge, busy notes chờ khóa provider, heartbeat cũ vẫn được diễn giải là worker đang chạy, và read timeout 300 giây giữ nguồn bị khóa quá lâu. Đây là các biểu hiện của số đo không cùng mẫu số hoặc trạng thái chưa được phản ánh kịp thời.

#### Nguyên nhân đo được

`task-core.js` dùng `transcript_end_seconds / audio_duration_seconds` cho mọi stage; notes chỉ có `on_coverage` sau kết quả hợp lệ; nhận notes lấy provider operation lock trước khi kiểm tra processing slot; ChatGPT đặt read timeout notes ở 300 giây. `compose.yaml` giữ `restart: unless-stopped`, nhưng Docker coi container bị dừng thủ công là ngoại lệ của restart policy.

Nguồn: issue #83, `tests/test_browser_task_progress.py`, `tests/test_notes_provider.py`, `tests/test_notes_worker.py`.

#### Thay đổi thiết kế

- Thanh transcript chỉ determinate trong stage `transcribing`; diarization và các stage sau dùng thanh indeterminate.
- Upload chuyển sang thanh indeterminate trong lúc `POST /complete` kiểm tra server, kèm byte đã truyền và đồng hồ elapsed được stamp bởi reducer runner.
- Notes pipeline phát callback progress allowlist riêng với coverage final-only: `preparing_source`, `generating`, `chunking`, `merging`, `repairing`, `rendering`. Chunk chỉ tăng sau validation thành công; merge/recovery để `total` trống khi tổng số lượt chưa biết. Task lưu các số đo trong migration schema 14 và UI notes hiển thị stage/count.
- Nhận notes kiểm tra processing slot nhanh trước provider operation lock, nhưng vẫn kiểm tra lại trong transaction. Notes UI ưu tiên `checking-worker` khi heartbeat active task đã cũ.
- HTTPX notes read timeout là 60 giây, còn total timeout mỗi lượt gọi 900 giây và deadline toàn tác vụ 3600 giây; không retry inference khi mất mạng.
- Không đổi restart policy hoặc thêm watchdog. Compose vẫn dùng `unless-stopped`; quy trình kiểm chứng recovery phải làm process worker crash để container thoát bất thường, sau đó startup recovery đánh dấu task dở `interrupted`. Không dùng `docker kill -s KILL` như bằng chứng container tự restart, vì [Docker ghi rõ manual stop suppresses restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/).

#### Số đo trước và sau

| | Trước | Sau |
| --- | --- | --- |
| ASR bar trong diarization | 99–100% dù còn xử lý | indeterminate, không ETA giả |
| Server upload checking | 38–81 giây không có số | indeterminate + byte đã truyền + elapsed khi có clock |
| Notes chunk/merge | chỉ elapsed/provider/model | stage + chunk count hoàn tất; merge count hoàn tất; recovery count là số lượt bắt đầu, total indeterminate khi động |
| Busy notes admission | 5,0–5,1 giây trong bằng chứng issue | focused regression trả lời dưới 1 giây khi slot đã biết bận |
| Notes silent read | 300 giây read timeout | 60 giây read inactivity, 900 giây mỗi lượt gọi; 3600 giây toàn tác vụ |

Điều kiện đo sau: interpreter hiện có của workspace, `PYTHONPATH` trỏ source/test helpers, test offline/fake provider; browser chạy với `ASR_RUN_BROWSER_TESTS=1`. Không có live provider, model hoặc Docker socket trong phạm vi issue này.

Nguồn: issue #83; `tests/test_notes.py`, `tests/test_notes_provider.py`, `tests/test_notes_worker.py`, `tests/test_chatgpt_provider.py`, `tests/web/*.test.mjs`, `tests/test_browser_task_progress.py`. Read timeout semantics: [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/).

#### Đánh đổi và phương án đã loại

- Dùng stage/count thật và để `total` indeterminate cho reduction động: giữ thông tin hữu ích mà không dựng tổng giả.
- Giảm read inactivity xuống 60 giây nhưng giữ total mỗi lượt gọi 900 giây và toàn tác vụ 3600 giây: mất mạng im lặng được báo sớm, còn lượt reasoning dài vẫn có ngân sách tổng. Lượt reasoning hợp lệ cũng có thể bị dừng nếu provider không gửi chunk nào trong 60 giây; timeout không tự chứng minh mất mạng.
- Kiểm tra slot trước provider lock và lặp lại trong transaction: busy refusal nhanh nhưng vẫn giữ tính đúng khi hai request chạy đồng thời; nếu slot bận, đọc lại idempotency để nhận ra request giống hệt vừa được chấp nhận.
- Giữ `unless-stopped` và recovery trong worker: phân biệt crash process với manual stop theo semantics của Docker; loại watchdog host/container vì vượt phạm vi và dễ che khuất lỗi vận hành.

#### Follow-up

- Khi chạy nghiệm thu Docker thật, inject process crash của worker (không manual-stop container), xác nhận container restart và task chuyển `interrupted` sau startup recovery.
- Nếu cần hiển thị phần trăm merge, pipeline phải cung cấp một tổng đã chứng minh được; không suy ra từ `MAX_REDUCTION_CALLS`.

#### Liên kết issue/PR

Issue #83; liên quan #82 và #118.

#### Tag

Kiến trúc | Notes | Hiệu năng

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — worker heartbeat/recovery.
- `docs/release/notes-v0.1.0.md` — notes progress/timeout.

### #84 — Giữ provider đang dùng và làm rõ các chuyển trạng thái kết nối

#### Vấn đề

Trong lúc một notes task đang dùng một provider, UI có thể không biết provider nào bị ghim; catalog `409 provider_in_use` bị diễn giải như lỗi unavailable. Antigravity ngắt kết nối ngay không có xác nhận, notes yêu cầu kết nối lại khi model đã lưu chỉ bị thiếu, và copy lỗi busy/disconnected/network không phân biệt đủ.

#### Nguyên nhân đo được

`GET /api/providers` chưa trả metadata active-task; reducer provider chung chưa khóa mutation theo metadata hoặc giữ badge/account khi catalog bị từ chối. Antigravity dùng descriptor không xác nhận disconnect, notes gộp trạng thái connected-missing-model vào nhánh connect, và lỗi HTTP typed 503 bị coi là chưa biết kết quả như mọi lỗi 5xx.

#### Thay đổi thiết kế

- Route thêm `in_use`/`in_use_reason: "notes"` từ cùng active-task guard dùng cho server mutation; chỉ provider được task ghim bị khóa, provider còn lại vẫn list/save/disconnect được.
- Provider core khóa model, effort, save, verify và disconnect của provider in-use, hiển thị lý do qua status có `aria-describedby`; catalog `provider_in_use` giữ status, account, badge và model đã biết. Dedicated provider copy được ưu tiên trước fallback HTTP nhưng vẫn giữ copy recovery riêng của server khi có.
- Cả ChatGPT và Antigravity đều có xác nhận disconnect. Antigravity chỉ báo xóa credential local/OMP; phản hồi không được diễn giải thành remote Google revocation. `provider_disconnected` yêu cầu connect, `reconnect_required` mới yêu cầu reconnect; `provider_unavailable` không khẳng định Internet bị ngắt, còn transport loss giữ mã network riêng.
- Notes tách `configure` khỏi `connect`: provider connected nhưng model saved không có/không usable mở choice hiện có và focus model, không tự chọn model. Typed provider/worker admission refusals là definitive kể cả khi HTTP 503; lỗi transport/timeout mơ hồ vẫn giữ pending để verify.

#### Số đo trước và sau

| | Trước | Sau |
| --- | --- | --- |
| Metadata provider đang được notes task dùng | không có trong `/api/providers` | `in_use` + `in_use_reason` theo provider |
| Antigravity disconnect | click trực tiếp, không xác nhận | xác nhận, focus keep/confirm, local-only result |
| Connected + saved model không dùng được | action yêu cầu kết nối | action `configure`, mở/focus model choice |
| Provider unavailable copy | có thể khẳng định nguyên nhân | cause-neutral, khuyên kiểm tra Internet/thử lại |

Kiểm chứng: focused Node reducer tests, backend/provider guard và i18n tests, cùng browser regressions cho confirmation, catalog refusal, recovery copy và model focus; không dùng live provider/inference.

#### Đánh đổi và phương án đã loại

- Metadata là advisory cho UI; server guard vẫn là authority, nên race giữa status read và mutation không mở thêm quyền.
- Giữ model/account/badge cũ khi catalog bị `provider_in_use` có thể để catalog stale, nhưng bảo toàn trạng thái kết nối và model đã lưu cho tới khi task kết thúc.
- Dùng một confirmation flow chung với copy local-only cho Antigravity, không mô phỏng remote revocation semantics mà OMP không chứng minh được.

#### Follow-up

- Theo dõi trạng thái provider khi tác vụ kết thúc trong lúc hộp thoại vẫn mở; việc làm mới chỉ đọc metadata, không tự gửi inference.

#### Liên kết issue/PR

Issue #84.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/release/runtime-v0.1.0.md` — provider lifecycle và reducer transition.

### #85 — Làm rõ validation upload, giữ upload khi kết quả complete mơ hồ và ẩn danh row lỗi

#### Vấn đề

Một file media bị cắt vẫn được nhận khi duration khai báo lớn hơn nhiều duration giải mã được;
`duration_limit` còn bị giao diện diễn giải như lỗi dung lượng. Firefox cũng có thể mất phản hồi
`POST complete` trong lúc server vẫn kiểm tra, làm client xóa upload chưa biết kết quả. Các row
`failed` cũ giữ filename gốc, và ngày có năm ngoài miền lịch của UI vẫn được nhận.

#### Nguyên nhân đo được

Issue #85 ghi nhận sáu lỗi hành vi và một quyết định giữ nguyên: file M4A cắt còn 60% byte vẫn
trả HTTP 200; file im lặng dài 4 giờ 10 phút trả `413 duration_limit`; 4/5 lần đầu Firefox
quan sát thấy complete thất bại trong thời gian thử nghiệm; có 7 traceback `ClientDisconnect`;
32 row `failed` còn filename gốc; năm `0999` được nhận; và upload `ready` chưa gắn meeting giữ
148 MB theo retention 24 giờ hoặc tới restart (giữ nguyên theo thiết kế).

#### Thay đổi thiết kế

- `media.probe_media` coi duration của container là provisional cho A/V nếu selected audio
  không có duration riêng. `normalize_media` vẫn map đúng audio stream, giải mã với `max + 1`,
  rồi áp giới hạn lên audio đã chuẩn hóa. Chỉ selected-audio duration authoritative mới được
  so với duration giải mã (tolerance tối thiểu 2 giây hoặc 5%); audio-only được dùng container
  duration làm fallback authoritative. Thêm regression WebM thật có video dài hơn audio.
- API trả `duration_limit` với `max_duration_seconds` và tách `duration_mismatch`; UI có copy
  tiếng Việt/Anh riêng cho duration, trạng thái đang kiểm tra, phục hồi complete và upload được
  giữ lại.
- Sau complete bị mất response hoặc lỗi mơ hồ, reducer GET trạng thái upload. `validating` được
  poll mỗi giây không có deadline tổng ngắn; `ready` tạo meeting, `uploaded` complete lại,
  `failed` cleanup. GET tạm thời retry sáu lần với backoff 1/2/4/8/16/32 giây; hết retry thì
  giữ `uploadId`, cho kiểm tra lại hoặc hủy. Không DELETE khi trạng thái còn mơ hồ.
- Client disconnect được xử lý như kết quả dự kiến: drain/commit offset bền vững và trả 204 ở
  đường đi trực tiếp; cancellation thật sự vẫn propagate. Filename failed được đổi sang
  `removed-upload` khi fail/delete/expiry và qua một truy vấn ẩn danh toàn bộ hàng lỗi cũ khi cleanup,
  giữ nguyên `failure_code`; row `ready`/`consumed` giữ filename. Ngày giới hạn năm 1000–9999.
- Giữ status `ready` retention 24 giờ hoặc startup như quan sát #7; không đổi thành xóa sớm.

#### Số đo trước và sau

| Chỉ số/hành vi | Trước | Sau |
| --- | --- | --- |
| M4A bị cắt trong fixture thật | accepted và có thể tạo meeting | rejected với `duration_mismatch` sau normalize |
| A/V có container duration nhưng selected audio thiếu duration | dùng thời lượng container để giới hạn audio, có thể từ chối audio hợp lệ | container provisional; audio vẫn được decode/validate |
| `duration_limit` trên UI | copy lỗi dung lượng, không nêu trần | copy nêu đúng trần, detail có `max_duration_seconds` |
| Complete response mơ hồ | cleanup có thể DELETE upload | GET state; validating poll, uncertainty giữ upload |
| Filename của failed row cũ | 32 row gốc không tự đổi | ẩn danh toàn bộ hàng lỗi cũ trong giao dịch, giữ diagnostics |
| Năm cuộc họp | nhận `0999` | chỉ nhận 1000–9999 |
| Upload ready chưa gắn meeting | giữ theo retention | giữ theo cùng retention; status 7 không đổi |

Kiểm chứng: media/recording focused pytest, reducer/history Node tests, i18n checks và real-browser
checks chạy trên Chromium và Firefox với media tổng hợp.

#### Đánh đổi và phương án đã loại

- Giữ upload khi complete mơ hồ dùng thêm poll và retention, nhưng tránh mất dữ liệu khi server đã
  chuyển sang `ready`; đã loại DELETE ngay theo lỗi client.
- Không dùng video/container duration để cắt hoặc so sánh A/V vì nó làm hỏng WebM/Matroska có
  audio duration thiếu; chọn audio decode là authority sau normalize.
- Ẩn danh toàn bộ hàng lỗi cũ bằng một truy vấn thay cho chờ thêm lượt cleanup hoặc xóa hàng, để giữ
  diagnostics, audit và tên mà người dùng còn cần.

#### Follow-up

- Theo dõi kiểm tra media dài qua tunnel trên Firefox; nguyên nhân timeout ban đầu chưa được tái hiện xác định, còn luồng phục hồi đã có kiểm thử mất phản hồi.
- Theo dõi telemetry nếu có để điều chỉnh retry/backoff sau khi có dữ liệu production.

#### Liên kết issue/PR

Issue #85.

#### Tag

Upload, media validation, recovery, privacy

#### Tài liệu đích

- `docs/backend-contract.md` — upload state machine, duration contract, cleanup và date range.
- `docs/frontend-design.md` — validation recovery state/effects và UI transitions.
- `docs/architecture-changes.md` — mục này.

## 6. Mẫu mục mới

Mục mới sao chép mẫu dưới đây và giữ nguyên tên các heading, để việc tách theo tag không phải viết lại.

````markdown
### #<issue> / PR #<pr> — <tiêu đề ngắn: thay đổi gì>

#### Vấn đề

<Bối cảnh cần để hiểu mục này một mình (thuật ngữ, mốc AC) trong một hai dòng; điều gì sai hoặc chậm.>

#### Nguyên nhân đo được

<Nguyên nhân kèm số đo; bảng nếu nhiều số.>

Nguồn: <issue/PR/comment hoặc mục của tài liệu trong repo>.

#### Thay đổi thiết kế

<Thiết kế mới: module, hàm, hằng số, hành vi mới; điều gì không đổi.>

#### Số đo trước và sau

<Điều kiện đo (revision, mẫu, cấu hình), rồi bảng trước/sau.>

| | Trước | Sau |
| --- | --- | --- |
| <chỉ số> | <số> | <số> |

Nguồn: <issue/PR/tài liệu>. <Nếu nguồn lệch nhau: dùng số đo mới nhất, ghi một dòng chênh lệch.>

#### Đánh đổi và phương án đã loại

- <Phương án đã chọn>: <đánh đổi>.
- <Phương án đã loại>: <lý do>.

#### Follow-up

- #<n>: <một dòng>.

#### Liên kết issue/PR

Issue #<n>, PR #<n>; liên quan #<n>.

#### Tag

<một hoặc nhiều tag trong bảng ở mục 1: Hiệu năng | Kiến trúc | Quyền riêng tư | Notes>

#### Tài liệu đích

- <tệp và mục sẽ nhận nội dung khi release>
````

### #86 — Chi tiết giao diện nhỏ: thứ tự điều khiển, rail và focus

#### Vấn đề

Ở viewport 360 px, thứ tự vẽ của header khác thứ tự Tab; ba cuộc họp gần đây đẩy Version khỏi vùng nhìn thấy ở desktop ngắn; vòng focus của transcript có thể nằm dưới player dính; wordmark hiển thị dấu `/` dù tên sản phẩm là ASR EN VI.

#### Nguyên nhân đo được

Header dùng thứ tự DOM khác với hàng provider được xếp lại bằng CSS; rail không giữ footer trong vùng nhìn khi nội dung cần cuộn; `scroll-padding-top` bằng đúng 64 px của player, không chừa khoảng cho outline; wordmark chứa separator không thuộc tên hiển thị.

Nguồn: Issue #86 và kiểm thử layout browser của sản phẩm.

#### Thay đổi thiết kế

Header đặt nhóm hành động trước nhóm trạng thái provider trong DOM và giữ cùng thứ tự ở desktop/mobile. Footer rail sticky trên nền rail, vẫn cho phép cuộn nội bộ danh sách và lịch. Vùng transcript dùng `scroll-padding-top` bằng chiều cao player cộng 8 px; `#form-error` có `tabindex="-1"` để luồng lỗi có thể focus mà vẫn giữ nguyên ID/alert. Wordmark workspace hiển thị đúng `ASR EN VI`; accessible name của liên kết brand không đổi. Chiều rộng rail giữ 236 px desktop và 208 px ở breakpoint hẹp.

#### Số đo trước và sau

Kiểm thử trên Chromium và Firefox, light/dark, viewport 360×844, 1024×768 và 1280×800; transcript focus dùng 24 đoạn đã lưu.

| Chỉ số | Trước | Sau |
| --- | --- | --- |
| Thứ tự DOM/visual của header ở 360 px | lệch | trùng; EN → Lịch → tạo → OpenAI → Google |
| Version với 3 cuộc họp ở 1024×768 và 1280×800 | có thể dưới mép nhìn | nhìn thấy; rail vẫn cuộn nội bộ |
| Scroll padding của transcript | 64 px | 72 px (64 + 8 px clearance) |
| Wordmark nhìn thấy | `ASR / EN VI` | `ASR EN VI` |

Nguồn: `tests/test_browser_layout_details.py`, Issue #86.

#### Đánh đổi và phương án đã loại

- Sticky footer giữ Version trong vùng thao tác mà không ẩn recent/calendar; phần còn lại của rail vẫn cuộn nội bộ.
- Đổi thứ tự markup thay vì dùng tabindex dương hoặc chỉ xếp lại bằng CSS, để thứ tự keyboard và thứ tự vẽ có cùng nguồn.

#### Follow-up

- Không có thay đổi sản phẩm ngoài phạm vi layout của Issue #86.

#### Liên kết issue/PR

Issue #86.

#### Tag

Kiến trúc

#### Tài liệu đích

- `docs/architecture-changes.md` — mục này.

### #86 — Đồng bộ dữ liệu cuộc họp và phản hồi theo trạng thái

#### Vấn đề và thiết kế

Tab khác không biết revision hoặc khóa nguồn mới cho tới khi tải lại. Các mutation đã được server xác nhận nay phát thông báo gồm ID cuộc họp và loại thay đổi qua `BroadcastChannel`; store `meetingChanges` chuyển dữ liệu này thành effect đọc lại trạng thái chính thức. Receiver không phát lại thông báo. ID cuộc họp của mutation vẫn được dùng nếu tab phát đã điều hướng sang cuộc họp khác, vì kết quả lưu thuộc cuộc họp nguồn.

Reducer tiếp tục giữ bản nháp, selection và conflict khi cài dữ liệu mới. Notes chỉ phát khi revision hoặc khóa nguồn đổi, không phát theo mỗi tick tiến độ. Cách này tránh echo và giữ nhịp poll hiện có; thông báo giữa các tab chỉ là gợi ý đọc lại, server vẫn quyết định quyền ghi. Trình duyệt không có BroadcastChannel tiếp tục dùng cơ chế đọc và kiểm tra version hiện có.

HTTP 401 với payload middleware `{detail: "Authentication required"}` dừng poll và chuyển tới `/access`, thay vì báo mất mạng rồi thử lại vô hạn. Lỗi 401 có code của provider vẫn đi theo luồng khôi phục kết nối provider. Thông báo thành công/hủy dựa trên transcript thực đã lưu; mutation bị từ chối không báo thành công ở live region. Trường ngày bị khóa có lý do và `aria-describedby`; lỗi upload mới được đưa vào vùng nhìn thấy một lần; các liên kết lịch có tiêu đề đầy đủ.

#### Kiểm chứng và đánh đổi

Kiểm thử hai tab giữ bản nháp của tab nhận khi tab phát lưu rồi chuyển cuộc họp; một thông báo được phát, không echo. Các regression reducer kiểm tra workspace/provider 401, mutation bị từ chối và lý do khóa. `docs/frontend-design.md` ghi đúng rail 236 px/208 px. BroadcastChannel không thay thế kiểm tra version ở server và không cung cấp đồng bộ giữa thiết bị.

Nguồn: Issue #86; `tests/test_browser_history.py`, `tests/web/channel.test.mjs`, `tests/web/history-core.test.mjs`, `tests/web/notes-core.test.mjs`, `tests/web/task-core.test.mjs`, `tests/web/transcript-core.test.mjs`.

Đọc notes được gộp theo object cuộc họp mới từ server: các lần vẽ lại cùng context vẫn cập nhật trạng thái local nhưng không mở thêm request. Thông báo từ tab khác đi qua history rồi tới notes; thông báo local của mutation dùng đường đọc trực tiếp. Regression browser đếm đúng một lần đọc thay vì sáu, đồng thời giữ bản nháp. Cả poll transcription và notes đều xử lý payload 401 của workspace bằng effect chuyển tới `/access`, không xóa bản nháp, pending request hoặc trạng thái worker. 401 có cấu trúc của provider không kích hoạt điều hướng này.

### #85, #86 — Phân biệt thời lượng ước lượng và giữ vòng focus trên footer

#### Vấn đề và nguyên nhân đo được

MP3 VBR không có Xing/VBRI có thể được ffprobe ước lượng duration từ bitrate dù stream trả về trường duration. Khi coi số này là metadata khai báo, một file đầy đủ 120 giây có estimate lớn hơn 150 giây bị từ chối. Ở sidebar 1024×768 có ba cuộc họp gần đây, footer sticky cũng che một phần ngày 31/8/2026 đang focus: outline bottom 710,95 px vượt footer top 694,41 px.

#### Thay đổi thiết kế

Server dùng `format_name` thực từ ffprobe để coi MP3 và raw AAC/ADTS là provisional; không dùng đuôi file và không tăng dung sai mismatch. M4A vẫn giữ kiểm tra metadata so với audio đã decode. Bước decode vẫn giới hạn `max + 1`, và duration của audio đầu ra quyết định giới hạn thực.

Sidebar dùng token chung cho chiều cao footer và phần scroll padding, cộng padding đáy cùng khoảng hở focus; mobile đặt scroll padding này về 0. Không đổi kích thước rail, nội dung footer hoặc thứ tự Tab.

#### Trước và sau, với điều kiện kiểm tra

| Trường hợp | Trước | Sau |
| --- | --- | --- |
| MP3 VBR 120 giây, không Xing, estimate >150 giây, policy 150 giây | bị từ chối | decode 119–121 giây và chấp nhận |
| Cùng MP3, policy 90 giây | từ chối theo duration ước lượng | từ chối `duration_limit` sau decode có giới hạn |
| M4A 180 giây bị giữ lại 60% byte | từ chối theo metadata/audio lệch | tiếp tục từ chối |
| Focus ngày 31/8, 3 recent, 1024×768 | outline bottom vượt footer top khoảng 16,55 px | full outline bottom ≤ footer top +0,5 px trong kiểm tra Chromium/Firefox, light/dark |

Nguồn: `tests/test_media.py`, `tests/test_browser_layout_details.py`; fixture media được tạo trong thư mục tạm của test.

#### Đánh đổi, phương án đã loại và follow-up

MP3/raw AAC không dùng duration ước lượng để phát hiện phần thiếu; decode có giới hạn vẫn bảo vệ policy thời lượng. Loại phương án nới dung sai toàn bộ vì sẽ làm yếu kiểm tra M4A bị cắt. Scroll padding giữ nguyên cách cuộn/sidebar thay vì che bớt nội dung hoặc đổi thứ tự keyboard. Không có follow-up bắt buộc.

Issue #85, #86. Tag: kiến trúc, accessibility. Tài liệu đích: `docs/backend-contract.md`, `docs/frontend-design.md`.

### #166 — Tách notes service khỏi worker model và giữ invariant một slot

#### Vấn đề và quyết định

Notes provider không nên nằm trong worker transcription: worker cần chạy ASR/diarization
local, không cần provider mount và chỉ dùng mạng nội bộ. `notes-service` nhận notes task,
giữ provider mount và mạng egress; `compose.model-prepare.yaml` giữ mạng mặc định cho
process one-shot chuẩn bị model. Các service vẫn dùng cùng data volume hiện có, vì vậy
đây là boundary mạng/provider, không phải filesystem isolation của recording.

Schema v16 thêm `tasks.owner_role` với mặc định `legacy` cho row cũ
([`storage.py`](../src/asr_studio/storage.py)). `processing_slot` hiện có vẫn là nguồn
sự thật duy nhất; claim/recovery lọc kind và role trong transaction và không clear slot
của task khác ([`tasks.py`](../src/asr_studio/tasks.py)). `accepted` vẫn là pending để
split role claim task cũ; `running` được đánh dấu `interrupted` khi role tương ứng chết,
`stopping` thành `canceled`, còn task của role đang sống được bảo vệ.

Legacy combined worker giữ exclusive `.worker.lock`. Mỗi split role giữ exclusive lock
riêng và shared `.worker.lock` trong toàn bộ lifetime, trước runtime/provider init và
recovery. Vì thế owner_role chỉ tương thích dữ liệu, không phải bằng chứng service còn
sống. Heartbeat/readiness và lock/recovery được tách riêng trong
[`service_runtime.py`](../src/asr_studio/service_runtime.py), với regression ở
[`test_service_isolation.py`](../tests/test_service_isolation.py).

#### Đánh đổi, giới hạn và rollback

Tách database/recording volume sẽ cho isolation mạnh hơn nhưng cần thay layout snapshot,
recording và publication hiện tại; issue này ghi rõ giới hạn shared data volume thay vì
hứa cách ly audio. Bản application cũ không mở database schema mới; rollback phải dùng
backup database trước migration hoặc một application tương thích để export/migrate, không
xóa cột bằng tay. Runtime stack/provider/egress checks vẫn là các kiểm tra độc lập.

Issue #166. Tag: kiến trúc, vận hành, riêng tư. Tài liệu đích: [ADR 0013](adr/0013-notes-service-boundary.md),
[`docs/user-guide.md`](user-guide.md), [`docs/release/privacy-v0.1.0.md`](release/privacy-v0.1.0.md).

### #177 — callback ChatGPT từ GUI remote và tạo lại registration ASR Studio

#### Vấn đề và nguyên nhân

Khi ASR Studio chạy trên một máy nhưng trình duyệt ở máy khác, callback OAuth
`http://127.0.0.1/...` trỏ về máy trình duyệt và không thể hoàn tất phiên trên server. Các
registration ChatGPT cũ cũng tiếp tục mang metadata tên cũ dù sản phẩm đã dùng tên ASR Studio.

#### Thay đổi thiết kế

GUI có ô dán toàn bộ callback URL trong phiên đăng nhập ChatGPT. Endpoint đã xác thực nhận URL
chỉ trong POST JSON, giới hạn body bằng cách đọc stream trước khi parse JSON, kiểm tra đúng callback
HTTP `127.0.0.1` đã cấu hình và session được gán, rồi dùng lại toàn bộ state/PKCE/nonce/client-id/
expiry/one-time checks. URL không được fetch, lưu trong reducer/browser storage, ghi vào lỗi hay log.
Callback GET loopback hiện có vẫn giữ nguyên.

`ASR_PUBLIC_URL` giờ cho phép loopback HTTP/HTTPS như trước hoặc một HTTPS public origin được cấu
hình chính xác; public HTTP, credentials, path, query, fragment và wildcard bị từ chối. Host và
Origin vẫn khớp tuyệt đối, cookie được ép `Secure` khi GUI là HTTPS, Compose vẫn bind loopback và
không tin forwarded headers. Callback OAuth là cấu hình riêng, luôn HTTP `127.0.0.1`.

Registration động mới gợi ý `ASR Studio`. Đăng nhập bình thường vẫn reuse issued registration.
Thao tác GUI tạo registration mới yêu cầu xác nhận, thu hồi quyền kết nối cũ dưới operation lock,
bảo toàn host ID và chỉ xóa state cũ sau khi adapter revoke xác nhận; nếu revoke thất bại,
connection và registration cũ được giữ nguyên. Sau khi thu hồi thành công, connection version tăng
và lựa chọn notes provider cũ bị vô hiệu hóa. URL sai mã hóa hoặc state không phải ASCII bị từ chối
trước khi claim session. GUI bỏ effect callback và phản hồi cũ khi hủy; nếu yêu cầu hủy lỗi,
người dùng vẫn gửi lại được URL. Đóng Settings xóa ô nhập; kết thúc đăng nhập focus thông báo
trạng thái. Khi server xác nhận thu hồi, GUI cập nhật trạng thái chưa kết nối ngay và báo lại
trạng thái đã kết nối sau callback. Phản hồi tạo lại chỉ báo bắt đầu lượt cấp quyền mới, không
báo registration mới đã hoàn tất trước consent.

#### Đánh đổi, phương án đã loại và follow-up

Public HTTPS origin tăng khả năng truy cập qua Internet nhưng không mở rộng bind hoặc tin host tùy
ý; reverse proxy phải được quản trị riêng. Callback thủ công giúp GUI remote mà không biến callback
thành public redirect, đổi lại người dùng phải dán URL sau khi cấp quyền. Không có measurement
performance được thu thập và thay đổi này không tuyên bố một measurement. Kiểm thử OAuth dùng
adapter giả lập; test giả không chứng minh consent thật, tên hiển thị thật hoặc entitlement thật.

Issue #177. Tag: kiến trúc, bảo mật, accessibility. Tài liệu đích: `docs/user-guide.md`,
`docs/frontend-design.md`, `src/asr_studio/config.py`.

### #77/#78/#79/#107/#108/#110 — Notes synthesis giữ nguồn và có giới hạn

#### Vấn đề và thay đổi

Map tuần tự theo ngân sách và final chỉ đọc các bản nháp có thể giữ trạng thái cũ, bỏ cam kết
qua ranh giới hoặc thiếu ngữ cảnh trích dẫn. Chống lặp chỉ bằng prompt cũng chưa có bước đối chiếu
riêng. Hợp đồng nền dùng `notes-hierarchical-v16`; bản audit-only hiện tại dùng
`notes-hierarchical-v19`, schema công khai vẫn là `1`.

Độ dài là `max(end_seconds) - min(start_seconds)`. Khi span lớn hơn `3_600` giây, planner
chia phần sở hữu xấp xỉ một giờ, giữ segment nguyên vẹn và overlap tối đa `2_000` byte; phần quá
lớn vẫn được tách theo ngân sách byte, không nội suy timestamp. Hai owner model đã biết dùng tối
đa `2` map worker. Kết quả được hợp nhất theo thứ tự nguồn; tiến độ tăng khi từng map hoàn tất,
không tăng trước khi có kết quả. Call index, một lượt repair chung, recovery và tổng số request
vẫn có giới hạn nguyên tử. Usage được cô lập theo thread/call; OMP prepare được serialize.
ChatGPT serialize phần đọc/làm mới/lưu token trong cùng provider instance, nên các map không
đồng thời xoay cùng refresh token. Kiểm tra chống ghi đè khi kết nối thật sự đổi vẫn giữ nguyên;
mutex không bao phần request model nên map vẫn chạy song song.

Final dùng nguồn đã lọc và các bản nháp khi vừa ngân sách. Khi có transcript đầy đủ,
hướng dẫn dùng đúng `SYNTHESIS_INPUT_JSON.source` và cho phép khôi phục chi tiết/mã nguồn mà
draft bỏ sót. Giới hạn “chỉ đọc draft” chỉ áp dụng cho fallback không có transcript.
Prompt yêu cầu giữ các nhóm liệt kê, số lượng, điều kiện và ý kiến riêng với quyết định;
không thay nhiều chi tiết bằng một câu tổng quát để rút ngắn tài liệu. Nếu không vừa, fallback phân tầng dùng
các draft từ đầy đủ coverage và nói rõ giới hạn. Với nguồn đơn, ChatGPT dùng audit hợp nhất
schema 1 đã có bằng chứng; Antigravity dùng audit chọn catalog tham chiếu, có guard chặn prose
mới, đổi nhóm, lặp origin và metadata action mới. Cả hai chỉ chạy khi toàn nguồn và draft vừa
ngân sách, dùng cùng provider/model và có tối đa một logical audit. Tham chiếu giữ nguyên mục
hoặc tiền tố được phép; source quote chỉ ở key_topics/confirmation_items, runtime giữ ngôn ngữ
gốc, source ID và thêm nhãn. Guard không chứng minh mọi claim draft là đúng. Đường ChatGPT có
thể viết lại prose và cần source review. Không audit đệ quy; lỗi sau repair/recovery chung giữ
Notes cũ. Nếu audit không vừa, ghi `prompt_budget`.

Final phân cấp không chạy audit bổ sung: các lần thử chưa đạt lợi ích chất lượng nên ghi
`quality_not_qualified`. Chính sách phụ thuộc provider/dạng planner, không phụ thuộc dataset,
source text hay đáp án. Primary prompt/schema/model/caps không đổi. Các output đã có request
fingerprint giống hệt có thể tái dùng làm regression; phải khai báo đó là replay, không phải một
lượt inference hay đo latency mới.

Đối chiếu phân biệt chưa có đồng thuận với quyết định bác bỏ/hoãn; đề xuất chưa cam kết chỉ
ở một mục cần xác nhận, không lặp lại trạng thái chờ trong quyết định và câu hỏi mở.
Quy tắc dùng chung phân loại mức cam kết trong nguồn trước khi chọn nhóm, thay thứ tự ưu tiên
section cũ. Đề xuất và các trạng thái chưa chấp nhận/chưa có người nhận/chưa triển khai là
cùng một thông tin đang chờ; quyết định bác bỏ/hoãn cần thỏa thuận tường minh riêng. Quy tắc
này áp dụng từ source/map, không chỉ tại final audit, để tránh giữ nhãn sai từ bản nháp.
Quy tắc cũng giữ thì và mức chắc chắn: dự kiến thảo luận/khám không trở thành sự kiện đã xảy ra.
Bản nháp và kiến thức miền không phải bằng chứng; chi tiết nhiễu không có căn cứ bị bỏ, không
được tự khôi phục thành thuật ngữ hoặc thủ thuật. Quyết định không nhắc lại việc đã có trong
action_items; key_topics chỉ giữ lý do mà mục khác chưa nêu. Bỏ câu khái quát lặp nội dung
chi tiết, giữ các ý riêng. Đề xuất/điều kiện phải được thể hiện trong nội dung, không chỉ qua
tên nhóm; không thêm số, định ngữ hoặc mức cụ thể không có nguồn.

Đối chiếu giữ trạng thái cuối, nối request/acceptance, đóng câu hỏi đã có câu trả lời và không
suy diễn owner, deadline, thuật ngữ hoặc thủ thuật từ đoạn nhiễu. Nó là bước xử lý chất lượng,
không phải bằng chứng rằng mọi đầu ra đều đúng. Coalesce xác định chỉ gộp full content giống hệt
trong cùng nhóm hoặc giữa `overview`/`key_topics`, union citation trong giới hạn. Khác biệt về
loại mục, mức chắc chắn, owner, deadline hoặc phủ định được giữ; không xóa chỉ vì chung ID hay
chủ đề. Bước đối chiếu nguồn quyết định các xung đột ngữ nghĩa.

#### Kiểm chứng, đánh đổi và follow-up

Các regression kiểm tra ranh giới thời lượng, map song song và thứ tự kết quả, tiến độ khi map
còn chạy, payload source/draft, giới hạn audit/repair/recovery, coalesce giữ mức chắc chắn và
chẩn đoán lỗi audit đã lọc nội dung riêng tư. Nguồn:
`tests/test_notes_document.py`, `tests/test_issue_104_budgets.py`,
`tests/test_notes_provider.py`, `tests/test_chatgpt_transport.py`,
`tests/test_antigravity_provider.py`, `tests/test_omp_contract.py`.

Lượt đối chiếu có thêm chi phí/quota và thời gian. v19 thu hẹp về các đường nguồn đơn đã đạt;
không mở thêm vòng thử prompt. Kiểm tra routing bằng request fingerprint và replay giữ nguyên
output thực đã được chấm, không công bố điểm inference mới. Nguồn dài còn thiếu một checkpoint
nội dung nên #79 vẫn mở. Các ngưỡng cohort riêng tư và thời gian ASR/diarization/người duyệt chưa
đo vẫn chưa được công nhận; #88 theo dõi audio đại diện và duyệt của người dùng.

Tag: kiến trúc, chất lượng Notes. Tài liệu đích: `docs/specification.md`, báo cáo nghiệm thu.

### #79 / PR #181 — Đối chiếu có cấu trúc cho Notes dài ChatGPT (`notes-hierarchical-v20`)

Liên kết: [#79](https://github.com/nguyencanhtrung/asr-studio/issues/79),
[PR #181](https://github.com/nguyencanhtrung/asr-studio/pull/181).

#### Vấn đề và thay đổi

Đường phân cấp trước đây bỏ audit sau final (`quality_not_qualified`), nên ý mà map đã trích có
thể mất ở final và claim không có nguồn vẫn được công bố. Baseline đo trên `d726bf5`
(nguồn: `/tmp/asr-notes-recheck-20261108/aggregate-metadata.json`, `semantic_gate`): ca dài
`long_found` 9/12 (cần ≥10), 6 vấn đề về nguồn hỗ trợ (cần 0; không đồng nghĩa 6 sự kiện bịa),
`long_critical` 7/7, Notes ca dài
157,688 giây với 4 provider call. Chất lượng sau thay đổi **đang chờ** lượt nghiệm thu có đầu vào khóa của 5 ca; mục này không công bố số mới.

Sau final của nguồn dài, khi provider là ChatGPT với budget token đã biết, pipeline gửi đúng một
lượt `reconcile` (module `notes_reconcile.py`) trước khi khôi phục source ID. Ledger bất biến
gồm mọi mục final (`f…`) và mọi mục map sau khi bỏ trùng overlap (`m…`). Model trả bản vá theo
schema nội bộ `ledger_version` 1, không trả tài liệu: mỗi ref có đúng một disposition
`keep`/`update`/`merge`/`remove`/`confirm`, cộng tối đa 16 `additions`. Code từ chối ref thiếu,
lặp hoặc lạ; `merge` và `remove` lý do `duplicate` phải trỏ tới ref khác đang `keep`/`update`,
nên không có chuỗi hay vòng; `remove` khác cần lý do `unsupported`/`superseded`/`nonmaterial`
và không có target. Không có ngưỡng tỷ lệ xóa.

Mục `keep` giữ nguyên bản gốc, không lấy lại từ model. `update`, `confirm` có prose mới và
`additions` cần source ID hợp lệ trong nguồn chiếu toàn phần và quote nguyên văn (chuỗi con
NFC) từ segment được trích. Addition có thể lấy ý từ toàn bộ nguồn, không chỉ từ ref map. Các
guard bảo thủ: số/tên mới (token có chữ số, từ viết hoa giữa câu) phải có trong segment được
trích; `update`/`confirm` không được bỏ từ phủ định/điều kiện/không chắc chắn của mục gốc;
`confirm` chỉ hạ mục vào `confirmation_items`, và mọi số/tên còn lại phải có trong nguồn được
trích, nên tên không có căn cứ phải bị bỏ hoặc ẩn danh; owner/deadline mới phải xuất hiện trong
segment được trích (owner có thể là speaker của segment đó), hoặc là null. Đây là kiểm tra từ
vựng, **không chứng minh entailment**; đánh giá ngữ nghĩa vẫn thuộc gate frozen. Cuối cùng code
kiểm tra lại tài liệu schema 1 đã dựng; source ID được khôi phục đúng một lần khi công bố.

#### Ngân sách và lỗi

Schema nội bộ được truyền qua seam `response_schema` của transport; `None` giữ request schema 1
y như cũ. Byte schema thật được tính trong `_check_prompt` (token và request-body guard) và
trong guard body/context của adapter ChatGPT. Antigravity không bật reconcile và không đổi.
Lượt reconcile dùng chung một repair của task và trần `MAX_PROVIDER_CALLS` hiện có; không có
recovery prompt hạ cỡ (prompt đó mô tả tài liệu schema 1), không audit đệ quy. Bản vá không hợp
lệ sau repair dùng chung, hoặc khi repair đã dùng/không có (output lỗi vượt 8.000 byte), làm
task lỗi `notes_invalid_output` và giữ Notes đã lưu trước đó. Nếu toàn nguồn + ledger + schema
không vừa ngân sách, task lỗi giới hạn `notes_synthesis_limit_exceeded` thay vì công bố bản
chưa kiểm. Hệ quả: transcript quá lớn để mang toàn nguồn trong một request ChatGPT (trước đây
vẫn có Notes qua fallback chỉ đọc draft) nay thất bại. Coverage ghi
`source_audit = {"status": "applied", "mode": "reconcile", "provider_call_index", "counts"}`.
Đường ChatGPT nguồn đơn (`rewrite`), Antigravity (`references` / `quality_not_qualified`) và
model không rõ giữ nguyên.

#### Kiểm chứng và giới hạn

`tests/test_notes_reconcile.py` dùng fixture tổng hợp: thiếu/lặp/lạ ref, map fact chưa được
xử lý, merge/target/vòng, quote sai và source ID lạ, metadata action, addition từ toàn nguồn,
confirm giữ phủ định/không giữ tên không có nguồn, một lượt reconcile, repair dùng chung,
lỗi prompt budget và byte schema thật tại pipeline/adapter. Mỗi guard mới được đảo một lần và
test đỏ. Guard từ vựng có thể từ chối nhầm khi bản dịch thêm từ viết hoa không có trong nguồn
(ví dụ tên thứ trong tuần), khi đó task lỗi thay vì công bố. Ý mà cả map và final đều bỏ và model
không thêm lại vẫn mất. Thêm một model call cho nguồn dài; latency chưa đo.

Tag: kiến trúc, chất lượng Notes. Tài liệu đích: `docs/backend-contract.md`.

### #79 / PR #181 — Map theo phần 15 phút và reconcile là bước tổng hợp cuối (`notes-hierarchical-v21`)

#### Vấn đề

Bản v20 đã cố định định dạng (replay SDK đã ghi lại không cần call mới), nhưng nội dung vẫn mất
ý ở hai chỗ: map theo phần một giờ gộp nhiều ý thành chủ đề chung, và final tự do gộp thô trước
khi reconcile chạy. Baseline chỉ đọc từ file, chấm tài liệu (tạm thời) của output v20
(`/tmp/asr-notes-reconcile-20261108/score-long/report.md`): ca dài found 8/12 (cần ≥10),
critical 7/7, strict material 1 (tên chỉ có trong segment không được trích), còn cụm số nhiễu
dạng "A to B" giảm dần bị nêu như số lượng chắc chắn. Số liệu sau thay đổi **chưa công bố** cho
tới lượt nghiệm thu có đầu vào khóa.

#### Thay đổi

- Chỉ nguồn ChatGPT có budget token đã biết và khoảng thời gian > 3.600 giây
  (`NotesPlan.fact_inventory`): plan chia toàn nguồn thành phần ≤ `FACT_PART_SPAN_SECONDS`
  (900 giây), segment nguyên vẹn, owned ID một lần, overlap ≤ 2.000 byte; tối đa 2 map song
  song và trần call giữ nguyên. Prompt map có thêm quy tắc danh mục sự kiện (mỗi mục một ý;
  tách yêu cầu, ủng hộ/phản đối kèm lý do, việc sau quyết định và lập trường các bên khỏi kết
  quả chính thức; không sửa tên/số nhiễu, ghi phần chưa rõ). Prompt ≤ 1 giờ, prompt reduce/final
  và mọi đường Antigravity/model không rõ giữ nguyên từng byte.
- Với đường đó, reconcile **thay** final tự do: ledger dựng từ seed schema 1 rỗng + mọi mục map,
  kèm toàn nguồn đã chiếu; đúng một call `reconcile` có cấu trúc, khôi phục ID đúng một lần rồi
  kết thúc. Không có final/reduce tự do. Provider khác và ChatGPT chunk ≤ 1 giờ giữ final +
  reconcile/reduction như v20. Schema công khai 1 và `ledger_version` 1 không đổi.
- Gộp không mất mát: payload cho mỗi mục `allowed_merge_targets` (cùng nhóm, text gốc chứa trọn
  text mục sau khi chỉ casefold và gộp khoảng trắng/dấu câu). `merge`/`remove duplicate` chỉ
  hợp lệ khi text **đã áp dụng** của target cùng nhóm chứa trọn ý; nếu không thì giữ riêng hoặc
  update target đầy đủ hơn rồi merge. `remove superseded` cần source ID và quote nguyên văn từ
  segment đứng sau mục bị thay; target tùy chọn và echo trích dẫn của bản compat vẫn giữ.
- Gợi ý và guard nguồn (tất định, không dùng tên/ID của bộ dữ liệu):
  - Tên nhiều từ viết hoa có trong toàn nguồn nhưng không có trong segment mục trích: payload
    `attention.uncited_names` kèm tối đa 3 ID ứng viên gần nhất. Bỏ qua cụm chỉ gồm từ mà nguồn
    cũng dùng ở dạng chữ thường (tiêu đề chung), tiêu đề dịch không có trong nguồn và tên một từ.
    Mọi mục cuối (kể cả `keep`) bị từ chối nếu tên đó chỉ có ở segment không trích; merge có thể
    sửa trích dẫn. Owner của action luôn phải có nguyên văn trong segment trích hoặc là speaker
    của segment đó, kể cả khi không đổi.
  - Cụm "A to/đến B" với A > B và không có từ chỉ hướng (from/từ, giảm, drop, reduce, down…)
    trong cùng mệnh đề: payload `attention.unclear_ranges` (giá trị, segment, span thô). Mục cuối
    nêu A mà không có dấu chưa rõ bị từ chối, xét cả nguồn của mục gốc nên bỏ ID không lách được;
    chỉ một quote nguyên văn từ segment sau có A và không có cụm nhiễu mới giải được.
- `PROMPT_VERSION` = `notes-hierarchical-v21`.

#### Kiểm chứng và giới hạn

Fixture tổng hợp, tên và câu đều bịa. Test mới đỏ trên `0d8f5b5`, xanh sau thay đổi; replay
SDK đã ghi: 4 ca ngắn khớp byte và tài liệu, ca dài cache-miss như dự kiến vì request mới.
Guard là kiểm tra từ vựng, không chứng minh entailment. Toàn nguồn + ledger không đảm bảo giữ
mọi ý ngữ nghĩa: ý map bỏ sót và model không thêm lại vẫn mất. Guard tên có thể từ chối nhầm
(cụm viết hoa có cả ở nguồn mà mục trích sai segment), khi đó dùng repair chung hoặc task lỗi và
giữ Notes cũ. Guard số có thể bắt nhầm cụm giảm dần hợp lệ không có từ chỉ hướng (ví dụ giờ
"10 to 5"). Nhiều map hơn làm ledger lớn hơn; nguồn + ledger vượt ngân sách vẫn là lỗi giới hạn
có biên. Latency và chất lượng chưa đo.

### #79 — Reconcile là overlay thưa, mặc định giữ (`notes-hierarchical-v22`, `ledger_version` 2)

#### Vấn đề

Đo trước thay đổi (nguồn: `eval-atomic/results/long.json` + `long.log`, ca dài v21, 557 segment):
call `reconcile` (prompt 247.894 byte) kết thúc `provider_output_too_large` sau 676,6 giây. Model
phải trả một disposition `keep` cho mỗi ref (294 ref map), nên phần lớn đầu ra là boilerplate.

#### Thay đổi

- Hợp đồng đổi định dạng, không đổi ngưỡng chất lượng. Model chỉ trả các thao tác **có thay đổi**;
  bỏ bản ghi `keep` không đổi. Mã khởi tạo `keep` cho mọi ref bất biến của ledger, thao tác được
  nêu chỉ ghi đè đúng ref đó. Thiếu thao tác nghĩa là giữ, không bao giờ bỏ (model bỏ sót không
  gây mất ý). `counts.default_kept` cho biết số ref được mã giữ mặc định.
- `keep` tường minh vẫn được chấp nhận (tương thích), không yêu cầu. Vá đầy đủ cũ chạy với
  `ledger_version` 2.
- Schema nội bộ `ledger_version` 2: `dispositions` chỉ còn `maxItems` = số ref (bỏ `minItems`).
  Schema công khai 1 không đổi; byte các frame SDK ngắn đủ điều kiện không đổi, chỉ prompt dài
  đổi (EN/VI, có ví dụ vá rỗng `{"ledger_version":2,"dispositions":[],"additions":[]}`).
- Mọi guard giữ nguyên: ref lạ/lặp, vòng, sai hình dạng, evidence/owner/marker/range, merge không
  mất mát và metadata action (owner/deadline). Mục giữ mặc định đi qua cùng kiểm tra grounding
  toàn bộ mục cuối, nên tên/range/owner sai vẫn bị từ chối.
- `PROMPT_VERSION` = `notes-hierarchical-v22`.

#### Kiểm chứng và giới hạn

Test đơn vị đỏ trên `b2d57fb`, xanh sau thay đổi. **Không** tuyên bố chất lượng hay latency sau
thay đổi: chưa gọi model, đo sau chờ lượt nghiệm thu có đầu vào khóa.

### #79 — Reconcile gom lỗi theo ref và sửa thưa (`notes-hierarchical-v23`)

#### Vấn đề quan sát

Lượt v22 ca dài (call reconcile, đầu ra 26.129 byte, 40 thao tác) lỗi `notes_invalid_output`
(`validation_reason` = `text`). Chẩn đoán offline: 6 thao tác được nêu lỗi độc lập (bỏ từ phủ định,
owner không có trong nguồn, tên không trích, quote đổi chữ hoa/thường) và 44 ref có `attention`
mà model không nêu, nên bị giữ mặc định rồi trượt kiểm tra grounding. Validator chỉ báo lỗi đầu
tiên; repair chung gửi lại toàn bộ đầu ra nên vượt trần 8.000 byte và `repair_state` =
`unavailable`. Một lỗi là dương tính giả của mã: tên bị tách giữa hai segment liền nhau
(`… Discovery` / `Park …`) bị nhãn người nói chen vào giữa.

#### Thay đổi

- Văn bản trích dẫn dùng để kiểm tra tên: các segment được trích xếp theo thứ tự nguồn; chỉ segment
  liền kề mới nối liền, segment không liền kề và nhãn người nói được ngăn bằng dấu phân cách mà
  cụm tên không vượt qua được. Tên chỉ có trong segment không trích vẫn bị từ chối.
- `apply` kiểm tra từng ref độc lập (thao tác sai hình dạng, ref lạ/lặp, update/confirm/remove,
  merge, giữ mặc định, grounding, addition) và gom mọi lỗi vào `ReconcilePatchError`. Chẩn đoán
  công khai theo lỗi đầu tiên theo thứ tự ledger; thông điệp chỉ có số ref lỗi, không có văn bản
  nguồn. Lỗi toàn cục (không phải JSON, sai hình dạng ngoài cùng, kiểm tra tài liệu) vẫn dừng ngay.
- Prompt chính EN/VI yêu cầu nêu mọi ref có `attention`, giữ từ phủ định/chắc chắn khi update và
  chép quote đúng chữ hoa/thường. Bỏ sót vẫn là giữ mặc định; guard không nới.
- Repair dùng chung duy nhất của tác vụ: lỗi theo ref → prompt thưa chỉ gồm ref lỗi (mục gốc,
  `attention`, thao tác trước, lỗi, tên cần trích và ID ứng viên) và các segment được trích hoặc ứng
  viên. Phản hồi chỉ được nêu ref lỗi và thay tối đa số addition lỗi; mã ghép lên các thao tác đã
  chấp nhận rồi kiểm tra lại toàn bộ ledger. Repair sai ở bất kỳ đâu bị từ chối toàn bộ, tác vụ lỗi
  và giữ Notes cũ. Lỗi toàn cục dùng repair chung cũ (trần 8.000 byte). Prompt repair qua cùng kiểm
  tra ngân sách model/schema thật (`_check_prompt` với schema nội bộ); phản hồi vẫn trần 64.000 byte.
- Repair phải nêu rõ mọi ref lỗi, kể cả khi mục gốc sẽ qua guard nếu bỏ qua thao tác lỗi;
  thiếu ref không được âm thầm khôi phục mục gốc. Ref không lỗi vẫn giữ nguyên.
- Riêng map danh mục sự kiện ChatGPT dài chạy tối đa 3 phần song song (v21/v22 là 2),
  giữ nguyên số call và thứ tự kết quả theo nguồn. Các đường map khác vẫn tối đa 2.
  Mục tiêu là giảm phần thời gian map; chưa suy ra mức tăng tốc khi chưa đo lượt chạy đầy đủ.
- `PROMPT_VERSION` = `notes-hierarchical-v23`. Prompt ngắn không đổi.

#### Kiểm chứng và giới hạn

Test đơn vị mới đỏ trên `687a831`, xanh sau thay đổi. Áp lại offline phản hồi v22 thật (chỉ chẩn
đoán, prompt chính đã đổi nên không phải replay chính xác): 49 lỗi được gom (5 thao tác nêu + 44 giữ
mặc định), dương tính giả tên ranh giới hết, prompt repair khoảng 78 KB qua kiểm tra ngân sách.
Lượt kiểm chứng code ở thời điểm ghi mục này chưa xác nhận chất lượng hay độ trễ ca dài;
bằng chứng nghiệm thu cập nhật tại #79 / PR #181. Model vẫn có thể trả repair sai, khi đó tác vụ lỗi.
Regression cũng tạo một revision đã lưu, chạy lại Notes rồi nhận `notes_invalid_output` và so toàn
bộ revision trước/sau để xác nhận bản cũ được giữ nguyên. Đường Antigravity và model không rõ có
kiểm tra riêng mức song song, không chỉ kiểm tra nhánh map danh mục sự kiện.

### #79 — Liên kết ngữ cảnh tiền đề sau reconcile (`notes-hierarchical-v24`)

#### Vấn đề quan sát

Lượt chấm độc lập ca dài v23 (11 call thật): 0 thông tin bịa, nhưng 17 mục nêu đúng nguồn mà
segment được trích không tự xác lập người/tổ chức/vai trò/số văn bản: tên tự giới thiệu, lời mời
phát biểu hay số dự luật nằm ở segment liền trước, còn segment được trích chỉ có "she", "the bill"
hoặc người nói chưa gắn tên. Nhãn diarization có thể đổi giữa câu nên không dùng làm ranh giới.

#### Thay đổi

- `notes_context.link_antecedent_context` chạy bằng mã, chỉ ở đường danh mục sự kiện ChatGPT dài,
  sau call reconcile/repair cuối và sau khôi phục ID; không thêm call model, không đổi byte prompt,
  schema hay trần phản hồi.
- Mốc lấy từ mục: tên sau danh xưng/vai trò, chủ ngữ phát biểu đầu mục (`X:`, `X requested …`),
  tổ chức có từ nối thường (`Friends of the …`; ưu tiên khớp đúng, từ cuối chỉ được lệch ASR khi
  cùng độ dài ≥ 6, đúng một ký tự bên trong bị thay, 2 ký tự đầu/cuối giống; nhiều cách viết gần
  khác nhau trong một hay nhiều segment thì bỏ),
  số ≥ 5 chữ số nguyên văn, vai trò đầu mục hoặc owner là vai trò. Với mỗi cụm segment được trích
  thiếu mốc, mã lùi tối đa 16 segment và 180 giây tìm lần xuất hiện gần nhất, thêm segment đó và
  cầu nối tới cụm; không vượt qua tiêu đề mục chương trình mới. Quá 12 ID/mục hoặc vượt trần ref
  thì bỏ, không cắt. Overview không đổi.
- Chỉ thêm `source_ids` có thật theo thứ tự nguồn; không sửa văn bản, mục, owner, hạn, không bỏ
  trích dẫn cũ, không dùng nhãn người nói. Tài liệu được kiểm tra lại; lỗi thì giữ bản trước.
  Manifest ghi `source_context` (`applied` + số mục/ID, hoặc `skipped`).
- Liên kết này là nguồn gốc ngữ cảnh, không phải bằng chứng ngữ nghĩa rằng mục được nguồn hàm ý.
- `PROMPT_VERSION` = `notes-hierarchical-v24`; schema công khai 1, ledger nội bộ 2 giữ nguyên.

#### Kiểm chứng và giới hạn

Test dữ liệu bịa (tên lặp, nhãn đổi giữa câu, đại từ, chủ đề cũ ngoài cửa sổ, thiếu mốc, giới hạn)
và test pipeline đỏ trước, xanh sau. Replay chính xác 11 frame SDK của lượt v23: 0 call mới,
văn bản/metadata/trích dẫn cũ giữ nguyên. Bản cuối có 33 mục thêm 145 ID; một chuỗi ngữ cảnh dư
vượt mục chương trình được bỏ. Chấm độc lập 34 pool đã thay đổi trước đó đạt 0 lỗi hỗ trợ quan
trọng; bản cuối tái sử dụng đúng 33 pool đã chấm này và 256 pool của lượt chấm đầy đủ ban đầu,
không có pool chưa được kiểm. Kết quả cố định: 11/12, đủ 7/7 điểm quan trọng, 0 claim quan trọng
thiếu nguồn hỗ trợ, 0 ID không tồn tại; G09 còn một phần. Kết quả cuối nằm trong
[báo cáo nghiệm thu](acceptance-report.md); bằng chứng chi tiết giữ bền cục bộ (gitignored).
Lượt thực đo v23 là 474,498 giây/11 call; v24 replay đúng request cũ, không phải lượt đo
end-to-end mới. Mốc ngoài cửa sổ hoặc bị ASR đổi quá xa vẫn chưa được liên kết.

### #183 — Gemini giữ sự kiện và kiểm gắn nguồn (`notes-hierarchical-v28`)

**Vấn đề.** Cùng transcript/gold đã khóa, bản trước đạt 8/12 nội dung, 6/7 ý quan trọng,
9 khẳng định quan trọng thiếu căn cứ; mẫu ngắn 8/9. Map theo giờ bỏ mất sự kiện, final tự do
thêm số từ ASR mơ hồ và gắn tên người từ danh sách thay vì lời giới thiệu. Nguồn baseline:
`reports/.notes-gemini-local-20261108/{score-long,score-short}/assessment.json` (cục bộ).

**Thiết kế.** Chỉ Gemini đã đăng ký ngân sách, nguồn trên một giờ: map theo 900 giây thành
sổ sự kiện → reconcile toàn nguồn → một lượt kiểm gắn nguồn bằng tham chiếu bất biến →
khôi phục ID và nối ngữ cảnh cục bộ. Prompt nguồn yêu cầu số/tên có căn cứ và phân biệt
quyết định đổi lịch với công việc phát sinh; ca ngắn giữ số lượt gọi hiện có.

- `notes_reconcile.py` kiểm cả tên một từ và tên không có trong nguồn. Trong nhánh bắt buộc
  kiểm ngữ nghĩa, trích dẫn lỗi định dạng có thể được gắn lại bằng toàn bộ segment nguồn
  đã khai báo và đã trích, trong giới hạn độ dài hiện hành. Không thay câu chữ, ID hoặc
  metadata; các kiểm số, tên, modality vẫn áp dụng. Hợp đồng mặc định giữ kiểm quote nguyên văn.
- `notes_semantic.py` dùng schema riêng: model chỉ chọn câu gốc hoặc hậu tố ẩn danh do mã
  sinh, nối thêm ID nguồn và xóa owner/deadline. Không sinh câu mới hay suy người nói từ nhãn.
  Mục chưa đủ căn cứ chuyển sang confirmation bằng trích đoạn nguồn nguyên văn, bỏ khẳng định
  chưa xác minh và metadata. Ref sai/lặp/chéo, ID sai, xung đột thao tác hoặc quá giới hạn vẫn lỗi.
- `notes_context.py` thêm neo số hồ sơ ngắn có nhãn và câu kết quả gần kiến nghị cho Gemini.
  Neo có cấu trúc tìm tối đa 32 segment/360 giây: tiêu đề cùng số hồ sơ hoặc khối biểu quyết
  kế tiếp có đúng tên người bỏ phiếu. Dừng ở tiêu đề khác, kể cả bên trong khối; không suy
  hướng bỏ phiếu từ ASR. Giữ cửa sổ thường 16 segment/180 giây và trần thêm 12 ID/mục;
  khối không vừa thì không thêm, không cắt và không chiếm chỗ trích dẫn cũ. Liên kết là
  provenance, không phải chứng minh.
- Antigravity gửi và đếm đúng schema tùy chọn trong ngân sách RPC. ChatGPT/model fallback giữ
  thông điệp mặc định; schema công khai 1, ledger 2, một lượt repair dùng chung và các trần giữ nguyên.

**Đo và đánh đổi.** Kết quả cuối 12/12, 7/7 ý quan trọng, 0 khẳng định quan trọng thiếu nguồn,
0 ID sai; mẫu ngắn 9/9, VietMed 2/2. Số đo thành phần 546,46 giây, 11 lượt logic:
10 phản hồi cũ trùng từng byte request, một lượt kiểm ngữ nghĩa mới, rồi replay sau sửa liên kết.
Không phải phép đo mới toàn chuỗi. Bỏ lượt repair chỉ sửa quote tiết kiệm 106,272 giây,
không đổi nội dung hay ID ở bản reconcile. Nguồn cục bộ:
`reports/.notes-gemini-fix-local-20261108/qualification/report.md`,
`replay-v28-context/long.json`, `reference-evidence-probe.json`, `short-replay/summary.json`.

Thêm một lượt kiểm ngữ nghĩa giúp sửa việc gắn tên/ngữ cảnh mà kiểm từ vựng không xác định
được; chi phí tăng so với bản final tự do. Không dùng tóm tắt mạnh hơn vì làm mất sự kiện.
Ẩn danh chỉ bỏ phần quy thuộc/bổ ngữ đầu câu; trường hợp không có biến thể hoặc nguồn đủ rõ
phải giữ ở mục cần xác nhận. Chất lượng phán đoán vẫn phụ thuộc model. Tác vụ ghim prompt cũ
đang chờ sẽ lỗi `notes_contract_changed` sau nâng cấp và cần tạo lần chạy mới.

Issue [#183](https://github.com/nguyencanhtrung/asr-studio/issues/183).
Tag: architecture, quality, performance. Đích: `docs/backend-contract.md`,
[báo cáo nghiệm thu](acceptance-report.md). Dữ liệu chi tiết chỉ lưu cục bộ, không đưa vào tài liệu.

### #185 — Tải OMP release có giới hạn và kiểm tra toàn vẹn

Hai build ARM64 của [PR #184](https://github.com/nguyencanhtrung/asr-studio/pull/184)
bị HTTP 504 ở URL release OMP. Cùng asset với `?download=1` trả HTTP 200 và khớp SHA-256
hiện hành (nguồn cục bộ: `reports/.notes-gemini-fix-local-20261108/final-test/arm64-attempt2.log`,
`omp-arm64-body-sha256.txt`). Dockerfile dùng tham số tải asset và retry hữu hạn cho cả
binary/license: 3 retry, chờ 2 giây, cửa sổ retry 180 giây, connect 15 giây, mỗi transfer 180 giây.
Giữ OMP 18.5.0, SHA-256, HTTPS/TLS và license; lỗi cuối/checksum vẫn làm build thất bại.
Đánh đổi: lỗi mạng có thể kéo dài build trong giới hạn trên, không bỏ kiểm tra hay dùng mirror.
Issue [#185](https://github.com/nguyencanhtrung/asr-studio/issues/185).
Tag: operations. Đích: `Dockerfile`.

## Notes transcript acceptance scope — issue #88 (2026-11-08)

- Problem: the original dataset issue mixed Notes-from-transcript checks with
  audio, diarization and end-to-end acceptance, so a usable transcript benchmark
  could not resolve it.
- Decision: the operator narrowed #88 to a public continuous meeting transcript,
  immutable source/gold prepared before output, and a real bounded Notes run with
  valid schema and citation IDs. Content-quality acceptance remains in #79.
- Evidence: [the resolved criteria and measured receipt in #88](https://github.com/nguyencanhtrung/asr-studio/issues/88).
  Original audio requirements remain in that issue's historical section and are
  outside this Notes task; this change does not certify ASR accuracy or audio
  processing time.
- Trade-off: this closes the transcript-fixture gap without claiming representative
  audio coverage. It does not relax the frozen content gate for #79.
- Follow-up: #79 and PR #181; tag: acceptance; target: Notes acceptance documentation.
