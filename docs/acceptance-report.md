# Báo cáo nghiệm thu

## Notes — 08/11/2026

ChatGPT gpt-5.6-sol, medium; transcript công khai khoảng 2 giờ. **Đạt phạm vi #79/#88.**

| Chỉ tiêu | Kết quả cuối |
| --- | ---: |
| Nội dung | 11/12 (1 mục một phần) |
| Ý quan trọng | 7/7 |
| Khẳng định quan trọng thiếu nguồn | 0 |
| ID trích dẫn không tồn tại | 0 |
| Nội dung mẫu ngắn | 9/9 |
| VietMed | 2/2 |
| Notes đo thực tế (v23) | 474,498 giây; 11 lượt gọi |

v24 kiểm lại từ phản hồi đã lưu; chưa đo lại thời gian toàn chuỗi. Không chứng nhận ASR/audio.

Nguồn: [PR #181](https://github.com/nguyencanhtrung/asr-studio/pull/181).

## Gemini Flash 3.8 — 08/11/2026

gemini-3.8-flash, medium, v36 đo mới; cùng transcript/gold với ChatGPT. **Chưa đạt.**

| Chỉ tiêu | Kết quả cuối |
| --- | ---: |
| Nội dung mẫu dài | 12/12 |
| Ý quan trọng | 7/7 |
| Khẳng định quan trọng thiếu nguồn | 3 |
| ID trích dẫn không tồn tại | 0 |
| Mục đã kiểm | 181 |
| Notes đo thực tế (v36) | 636,865098 giây; 15 lượt gọi; 0 cache |

v37 (liên kết hồ sơ/số lượng đầy đủ, kiểm ngữ nghĩa trên nguồn hiển thị có giới hạn): chờ lần đo
thực tế.

Nguồn: [PR #184](https://github.com/nguyencanhtrung/asr-studio/pull/184);
hồ sơ cục bộ `reports/.notes-gemini-fix-local-20261108/v36-physical-qualification.json`,
`v36-score-a/report.md`, `v36-score-b/report.md`.

<details>
<summary>Nghiệm thu release v0.1.0 — lịch sử, không đo lại</summary>

## 0. Phạm vi và nguyên tắc công bố

Tài liệu này tổng hợp các kết quả nghiệm thu lịch sử đã có cho release candidate và
các lần đo bổ sung. Các số đo không được thực hiện trên release ổn định v0.1.0:
việc thay tài liệu này cũng không phải một lần đo lại, không đổi tiêu chí và không
biến kết quả fail hoặc partial thành pass.

Nguồn công khai của các mốc hiệu năng được giữ lại:

- Release candidate 0.1.0-rc.1: source [151f3e4](https://github.com/nguyencanhtrung/asr-studio/commit/151f3e4d6249ea6f75d74251d37fc7addb711557).
- Lần đo lại #99: source [4a88ec8](https://github.com/nguyencanhtrung/asr-studio/commit/4a88ec8be964aa5b818d854ef207eb0a2905718f), [issue #99](https://github.com/nguyencanhtrung/asr-studio/issues/99).
- Số đo kết hợp 12,2 phút của #104 không phải một lần chạy liên tục; nguồn là
  [source b78711d](https://github.com/nguyencanhtrung/asr-studio/commit/b78711d),
  [issue #104](https://github.com/nguyencanhtrung/asr-studio/issues/104) và
  [PR #109](https://github.com/nguyencanhtrung/asr-studio/pull/109).

Bản công khai này chỉ giữ kết quả tổng hợp, điều kiện kỹ thuật và giới hạn của phép
đo. Nó không chứa transcript, notes, tên người, tên hoặc ngày bản ghi, meeting/task
ID, số liệu nhận dạng media, hash, đường dẫn máy cá nhân, thông tin tài khoản hay
liên kết tới bằng chứng riêng tư.

Kết luận tổng thể: chưa nên tuyên bố mọi tiêu chí release đã đạt. Các kết quả fail
và partial vẫn cần được xử lý hoặc ghi nhận thành follow-up trước khi phát hành
không kèm cảnh báo.

## 1. Tóm tắt kết quả

| Nhóm | Tổng | pass | partial | fail | not done |
| --- | ---: | ---: | ---: | ---: | ---: |
| AC-01..AC-49 | 49 | 31 | 12 | 6 | 0 |
| FE-01..FE-16 | 16 | 10 | 6 | 0 | 0 |
| FE-17..FE-18 (ngoài phạm vi ban đầu) | 2 | 2 | 0 | 0 | 0 |

### Ma trận trạng thái

- AC pass: AC-07, AC-08, AC-09, AC-11, AC-12, AC-16, AC-18, AC-19,
  AC-20, AC-21, AC-22, AC-23, AC-24, AC-25, AC-26, AC-27, AC-28, AC-30,
  AC-31, AC-32, AC-33, AC-34, AC-40, AC-41, AC-42, AC-43, AC-45, AC-46,
  AC-47, AC-48, AC-49.
- AC partial: AC-01, AC-04, AC-05, AC-06, AC-14, AC-15, AC-29, AC-35,
  AC-36, AC-37, AC-38, AC-44.
- AC fail: AC-02, AC-03, AC-10, AC-13, AC-17, AC-39.
- FE pass: FE-01, FE-03, FE-04, FE-05, FE-06, FE-07, FE-08, FE-09, FE-13,
  FE-15.
- FE partial: FE-02, FE-10, FE-11, FE-12, FE-14, FE-16.
- FE-17 và FE-18: pass; đây là các tiêu chí bổ sung ngoài phạm vi 16 FE ban đầu.

## 2. Các kết quả quan trọng

### 2.1. Chất lượng transcript và notes

- AC-01 partial: bộ mẫu có tiếng Anh, tiếng Việt xen thuật ngữ và code-switch,
  nhưng chưa có một bản ghi hoàn toàn tiếng Anh đúng theo yêu cầu bộ mẫu.
- AC-02 fail: 119/126 mục quan trọng được tìm thấy; 4 mục chỉ được tìm thấy
  một phần và 3 mục bị bỏ sót. Quy tắc nghiệm thu coi việc bỏ sót bất kỳ mục
  nào là fail.
- AC-03 fail: không ghi nhận owner hoặc deadline hoàn toàn không có trong nguồn,
  nhưng 22/77 quyết định hoặc action bị nâng mức chắc chắn; 5 ý còn mơ hồ đã
  bị trình bày như quyết định hoặc action đã xác nhận.
- AC-04 partial: nhãn speaker trong các đoạn quan trọng đúng 36/42; notes có
  một lỗi và một trường hợp còn tranh luận.
- AC-05 partial: cơ chế chip timestamp, snapshot nguồn, tua và phát hoạt động;
  147/155 bullet được chấm là trỏ tới đoạn có đủ ý. Chưa có người nghe lại
  mẫu chip để xác nhận bằng tai.
- AC-06 partial: deadline tương đối đúng trong phần lớn trường hợp có ngày họp;
  hai clip không ngày không tạo ngày lịch giả. Chưa bao phủ mọi kết hợp ngôn
  ngữ và deadline tương đối.
- AC-39 fail: cơ chế chia/hợp nhất hoạt động trên các bản notes thành công,
  nhưng một số lần chạy dài thất bại ở bước reduce khi đầu ra vượt trần
  10.700 byte. Khi đó không có bản notes hoàn chỉnh để xác nhận chất lượng
  toàn chuỗi.

### 2.2. Riêng tư, mạng và provider

- AC-10 fail: chế độ ngoại tuyến giữ được ASR local, diarization, xem/sửa
  transcript và notes đã lưu. Khi có mạng, runner diarization vẫn phát telemetry
  ngoài ứng dụng; OMP có catalog/discovery/update traffic; ứng dụng cũng đọc
  danh mục model của provider. Vì vậy điều kiện “chỉ kết nối của provider đã
  chọn” chưa đạt.
- AC-35..AC-38 partial: các luồng chọn provider, dùng lại phiên, lỗi và đổi
  bên đã được kiểm tra một phần; kết nối độc lập từ đầu và tình huống quota
  chưa được đo đầy đủ.
- AC-44 partial: backend chặn ngắt hoặc đổi provider đang được task dùng, nhưng
  UI chưa khóa rõ mọi thao tác tương ứng và chưa thực hiện các bước yêu cầu
  đăng xuất tài khoản thật sau khi task dừng.
- FE-10 partial: không thấy UI demo trong phần đã kiểm tra; phần lựa chọn
  provider chưa được bao phủ trọn.
- FE-11, FE-12, FE-14, FE-16 partial: còn thiếu một phần luồng kết nối độc lập,
  quota, đổi provider chủ động, chặn provider đang dùng và ghi nhớ model.

### 2.3. Hiệu năng và cài đặt

- AC-13 fail: trên release candidate 0.1.0-rc.1, source 151f3e4, phép đo mẫu
  dài có transcript sau 128,9 phút và notes lỗi. Lần đo lại #99 trên source
  4a88ec8 có transcript sau 10,4 phút nhưng notes vẫn lỗi ở phút 24,4, nên
  không có mốc transcript và notes cùng sẵn trong 30 phút.
  Số 12,2 phút của #104 trên source b78711d được tính bằng cách cộng các số
  đo rời trên mẫu ghép; đó không phải một lần chạy liên tục và không phải
  phép đo lại release ổn định v0.1.0. Kết luận lịch sử vẫn là fail.
- AC-14 partial: hai ước lượng thời gian duyệt tương ứng 8,0 và 10,8 phút/giờ;
  một mẫu không có draft notes để đo. Đây là ước lượng, không phải đồng hồ bấm.
- AC-17 fail: đường cài từ source gặp lỗi chuẩn bị model trên Compose; đường
  pull image công khai chưa được xác nhận đủ trong đợt nghiệm thu.
- Các tiêu chí về upload bị ngắt, task tiếp tục khi trình duyệt mất kết nối,
  lịch sử, token localhost, xóa dữ liệu, version transcript và lưu notes cũ
  giữ được các kết quả pass đã ghi ở các lần đo trước. Các sai lệch câu chữ,
  trạng thái và tương phản vẫn được giữ ở các mục partial tương ứng.

## 3. FE-01..FE-18

Các kết luận frontend giữ nguyên theo ma trận trên:

- FE-01 pass: bố cục A, hệ sáng/tối, sidebar, font và các vùng chính khớp
  thiết kế.
- FE-02 partial: metadata brand và tên truy cập đúng, nhưng wordmark hiển thị
  dấu gạch chéo trong khi tài liệu gọi brand là ASR Studio; version và một số
  điểm tương phản/kích thước còn lệch.
- FE-03..FE-09 pass: lịch, tìm kiếm, điều hướng, notes/transcript, keyboard,
  viewport, player dính và trạng thái task đã được kiểm tra ở các ma trận đã
  ghi.
- FE-10 partial: chưa chứng minh đầy đủ mệnh đề lựa chọn provider trong cùng
  kết luận.
- FE-11 partial: hộp thoại và trạng thái từng provider có, nhưng chưa chạy
  đầy đủ kết nối từ đầu chỉ với một provider.
- FE-12 partial: dùng lại phiên và lỗi không tự retry hoạt động; quota và đổi
  provider chủ động chưa đủ.
- FE-13 pass: notes cũ giữ snapshot nguồn và conflict giữ draft.
- FE-14 partial: chọn thế hệ transcript hoạt động; khóa provider đang dùng và
  một số thông báo UI còn lệch.
- FE-15 pass: sửa tên/ngày/speaker theo version và source lock hoạt động.
- FE-16 partial: lỗi mạng và link nguồn hỏng được xử lý; ghi nhớ model và một
  nhánh lỗi mạng chưa bao phủ đầy đủ.
- FE-17 và FE-18 pass: tab WAI-ARIA, chip timestamp, điều hướng Back/Forward,
  responsive 360 px và reduced motion đã được kiểm tra.

## 4. Điều kiện phần cứng, runtime và cấu hình

Các giá trị dưới đây là bối cảnh kỹ thuật của đợt đo, không phải thông tin nhận
dạng bản ghi hay bằng chứng riêng tư.

| Hạng mục | Cấu hình đã dùng |
| --- | --- |
| Hệ điều hành | Ubuntu 24.04.4 LTS, Linux 6.17, x86_64 |
| CPU / RAM | AMD Ryzen 9 9950X, 16 nhân / 32 luồng; khoảng 60 GiB RAM |
| GPU / driver | NVIDIA GeForce RTX 5080, khoảng 16 GiB; driver 590.48.01; CUDA host 13.1 |
| Container | Docker 29.5.2, Compose 5.1.4, NVIDIA Container Toolkit 1.19.0 |
| Runtime | Python 3.12.3, CUDA runtime 12.8.1, torch 2.11.0+cu128 |
| ASR | faster-whisper 1.2.1; Whisper large-v3 trên GPU float16 hoặc Whisper small trên CPU int8; beam size 5 |
| Diarization | pyannote Community-1, chạy local; alignment và review flags được lưu theo segment |
| Notes | prompt phân cấp; giới hạn nguồn 1.000.000 byte; tối đa 24 chunk và 24 lượt reduce; output chunk 8.000 byte, reduce 10.700 byte, final 128.000 byte |
| Provider | ChatGPT và Google Antigravity qua OMP; model và mức suy luận được chọn theo từng lần chạy |
| Trình duyệt | Chrome và Firefox headless; ma trận viewport 1440×1000, 1280×800, 1024×768 và 360×740; có kiểm tra theme tối và reduced motion |

Mẫu đo hiệu năng dùng dữ liệu ghép để tạo thời lượng dài; đó không phải một cuộc
họp duy nhất. Các lần chấm chất lượng chủ yếu đối chiếu văn bản, không nghe
audio bằng tai. Các lần kiểm tra UI tính tràn ngang, focus, che phủ và tương phản
từ DOM/CSS; không thay cho đánh giá thị giác của người.

## 5. Giới hạn và follow-up

- Bộ mẫu chưa đủ ba loại ngôn ngữ và chưa có mẫu hai giờ là một cuộc họp liên
  tục có cấu hình người nói đúng yêu cầu.
- Kết quả provider phụ thuộc model, thời điểm và account state; quota, billing
  và một số luồng mất phiên không được mô phỏng giả.
- Chưa tuyên bố chất lượng audio hoặc chip timestamp đã được người nghe xác nhận.
- AC-13 giữ lịch sử fail dù có số đo ghép đạt ngưỡng thời gian; không dùng số đo
  ghép để thay thế một lần chạy liên tục.
- Các follow-up chính vẫn là: cải thiện diarization dài, xử lý giới hạn output
  khi reduce, loại telemetry ngoài ý muốn, hoàn tất pull/install, hoàn thiện
  vòng đời provider/quota và sửa các sai lệch UI tương phản, câu chữ, trạng thái
  và keyboard.

## 6. README và bằng chứng công khai

Hai ảnh README hiện được tạo từ fixture tổng hợp trong repository bằng
script capture riêng. Script mở app local mới trên cổng loopback tạm thời, ghi
database SQLite tạm, dùng stub provider, kiểm tra browser request chỉ đi tới
loopback và không dùng recording, provider auth hoặc inference. Ảnh README là
minh họa sản phẩm, không phải bằng chứng nghiệm thu.

Hai ảnh trong tài liệu frontend vẫn là ảnh prototype có dữ liệu mẫu; tài liệu
frontend đã ghi rõ thanh PROTOTYPE và dữ liệu đó không thuộc frontend production.

## 7. Các kiểm tra của lần cập nhật tài liệu

Đã chạy các kiểm tra tập trung sau cho lần cập nhật tài liệu; không có phép đo
nghiệm thu mới cho release ổn định v0.1.0:

1. Xác thực fixture JSON.
2. Ruff cho script capture.
3. Capture hai ảnh README bằng app local mới, fixture synthetic, stub provider,
   viewport 1440×1000 và loopback-only request check.
4. Kiểm tra trực quan hai ảnh sau capture.
5. Kiểm tra whitespace/diff cho các thay đổi tài liệu và asset.

Không chạy full suite và không thực hiện provider auth, inference, truy cập dữ
liệu người dùng, truy cập service đang chạy hay browser của người dùng trong lần
cập nhật tài liệu này.

## Hồ sơ thay đổi cho v0.1.0

Xem [bằng chứng và đánh đổi theo tag](release/performance-v0.1.0.md),
[các thay đổi notes](release/notes-v0.1.0.md) và
[chỉ mục thay đổi](architecture-changes.md). Số đo lịch sử giữ nguyên điều kiện
và revision gốc; không phải phép đo lại artifact 0.1.0.

## Mốc bổ sung: Mốc AC-13 (#16 → #99 → #104)

AC-13: bản ghi 2 giờ có transcript và notes sẵn để duyệt trong tối đa 30 phút kể từ `POST /api/tasks`. Mẫu ghép 2 giờ (7366 s) là audio của hai bản ghi nối nhau.

| Mốc | Revision đo | Nguồn đo | Số đo chính | AC-13 |
| --- | --- | --- | --- | --- |
| #16 nghiệm thu | `151f3e4` | mẫu ghép 2 giờ | transcript sẵn 128,9 phút (diarization 7.222,6 s); notes lỗi ở lượt 25 sau 958,6 s | fail |
| #72 / PR #94 | `3c96052` | bản ghi A (75 phút) | notes thành công sau 798,5 s, 16 lượt gọi, 1 lượt phục hồi (trước: lỗi ở lượt 13 sau 571,3 s) | không đo mẫu 2 giờ |
| #71 / PR #98 | `d515f36` | mẫu ghép 2 giờ | diarization 103,0 s (trước 7.222,6 s); import → transcript sẵn 768 s | không đo notes |
| #99 / PR #103 | `4a88ec8` (có #71, #72, #74) | mẫu ghép 2 giờ | transcript sẵn 626,5 s (10,4 phút); notes lỗi ở lượt 29 sau 1.463,5 s; tác vụ notes kết thúc lỗi ở 2.090,2 s (34,8 phút) | fail |
| #104 / PR #109 | `b78711d` | mẫu ghép 2 giờ, số đo ghép | **651,7 s transcript (lần chạy timed) + 0,2 s + 81,3 s notes = 733,2 s (12,2 phút)**; một lần chạy timed trên candidate trước là 723,3 s | đạt về thời gian, nhưng số đo 733,2 s là composed, không phải một lần chạy liên tục |

Nguồn: mục tương ứng của từng PR ở mục 3; `docs/acceptance-report.md` mục 6.1, 6.5 và 6.6. #74 (PR #91) không nhằm giảm thời gian và đã có trong image đo của #99. Bước chậm nhất chuyển từ diarization (#16) sang notes (#99), rồi notes còn 67,2–116,5 s ở các lượt single-pass final. #104 có mốc thời gian composed dưới 30 phút; chất lượng AC-5 còn theo dõi ở #107 và phương án nguồn dài ở #108.

</details>
