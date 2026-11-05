# Hiệu năng — hồ sơ thay đổi v0.1.0

Các mục lịch sử được chuyển từ tài liệu thay đổi chung khi chuẩn bị #17.
Số đo giữ revision và điều kiện gốc; không phải phép đo lại artifact 0.1.0.

### #71 / PR #98 — Diarization giải mã bản ghi một lần thành waveform 16 kHz

#### Vấn đề

Diarization (pyannote Community-1 trên CUDA) tăng theo bình phương thời lượng. Mẫu ghép 2 giờ (7366 s, audio của hai bản ghi nối nhau, không phải một cuộc họp) mất 7.222,6 s ở diarization và có transcript sau 128,9 phút, gấp 4,3 lần mục tiêu 30 phút của AC-13 (transcript cùng notes sẵn để duyệt).

#### Nguyên nhân đo được

| Thời lượng audio | Cả tác vụ | ASR | Diarization | Diarization ÷ thời lượng² |
| --- | --- | --- | --- | --- |
| 3340 s | 1779 s | 267 s | 1509 s | 1,352×10⁻⁴ |
| 4026 s | 2496 s | 287 s | 2207 s | 1,362×10⁻⁴ |
| 4498 s | 3059 s | 340 s | 2715 s | 1,342×10⁻⁴ |
| 7366 s (ghép) | 7734 s | 510 s | 7223 s | 1,331×10⁻⁴ |

Nguồn: issue #71 (bảng Quan sát); khớp `docs/acceptance-report.md` mục 6.1.

- Thời gian diarization = (1,331–1,362)×10⁻⁴ × thời lượng² (lệch ±1,2%), chiếm 85–93% tác vụ, chạy trên khoảng một lõi CPU (110–125%), GPU trung bình 2–3%. ASR tuyến tính, 12,5–14,4× thời gian thực. Riêng diarization chạm 30 phút ở khoảng 61 phút audio.
- Cơ chế: khi nhận đường dẫn file, mỗi `Audio.crop` của pyannote 4.0.7 tạo `AudioDecoder` mới và giải mã file AAC từ đầu tới vị trí cần cắt. Một crop 10 s ở giây t tốn khoảng 0,255 ms × t (0,02 s ở t=10; 0,93 s ở t=3600; 1,53 s ở t=6000). Bước embeddings crop một lần cho mỗi chunk (bước 1 s) nên tổng ≈ 0,000255 × d²/2 s; với d = 7366 s là khoảng 6900 s, khớp 7223 s đo được.

#### Thay đổi thiết kế

- `diarization_runner._decode_waveform` giải mã cả bản ghi đúng một lần bằng `torchcodec.decoders.AudioDecoder(path, sample_rate=16000, num_channels=1).get_all_samples()`. Decoder tự downmix và resample nên dữ liệu ở tốc độ mẫu gốc không vào Python. Bước này thay lần giải mã thử 1 giây trước đây.
- `diarize_file` truyền `{"waveform": (1, N) float32, "sample_rate": 16000}` vào pipeline Community-1, không truyền đường dẫn file. Test đổ nếu pipeline nhận đường dẫn (`str`, `Path`, dict chứa đường dẫn) hoặc decoder không được gọi đúng một lần với `sample_rate=16000, num_channels=1`.
- Kết quả giải mã không hợp lệ (lỗi mở/giải mã, rỗng, không mono, sai tốc độ mẫu) báo `diarization_input_decode_failed` ở stage `decoding_input`. Sự kiện, mã lỗi, thông điệp và thứ tự stage (`loading_diarization_model` → `decoding_input` → `diarizing`) giữ nguyên.
- Đổi hành vi có chủ đích: file m4a cắt cụt nhưng còn header trước đây qua kiểm tra 1 giây rồi lỗi `diarization_failed` ở `diarizing`; nay báo `diarization_input_decode_failed` ở `decoding_input`.

#### Số đo trước và sau

Mẫu ghép 2 giờ (7366 s), RTX 5080, torch 2.11.0+cu128, CUDA 12.8, pyannote.audio 4.0.7, torchcodec 0.11.1.

| | Trước: #16 (`151f3e4`) | Sau: PR #98 (`d515f36`) | Đo lại: #99 (`4a88ec8`) |
| --- | --- | --- | --- |
| Diarization, tổng | 7.222,6 s (nạp model + chạy) | 103,0 s | 96,73 s |
| Nạp model / giải mã đầu vào / diarizing | không tách | 3,0 / 4,3 / 95,7 s | 4,26 / 2,85 / 89,62 s |
| Tới transcript sẵn | 128,9 phút (từ `POST /api/tasks`) | 768 s = 12,8 phút (từ import) | 626,5 s = 10,4 phút (từ `POST /api/tasks`) |

| Chỉ số (PR #98) | Đo được | Tiêu chí |
| --- | --- | --- |
| Diarizing mỗi giây audio, 2 giờ / 56 phút | 0,01299 / 0,01179 s/s, tỷ lệ 1,10 | ≤ 2 lần |
| Mẫu tổng hợp 4 giờ | 0,0124 s/s | tuyến tính |
| RSS đỉnh (VmHWM) của runner, 2 giờ / 56 phút | 3,14 / 2,40 GiB | ≤ 4 GiB |
| RSS đỉnh, 4 giờ (mẫu tổng hợp) | 4,93 GiB | vượt 4 GiB từ khoảng 3 giờ |
| Số người nói, % thời lượng cùng nhãn với đầu ra `151f3e4` | mẫu ghép 12 → 13, 92,0%; bản 56 phút 10 → 10, 99,2% | chỉ tham khảo |
| Từng bước, mẫu 2 giờ (hook pyannote) | giải mã 3,6 s; segmentation 4,1 s; embeddings 78,8 s; clustering 4,5 s (kèm reconstruct 5,5 s) | embeddings chiếm 87% pipeline |

Nguồn: PR #98 (tiêu chí 2–6); `docs/acceptance-report.md` mục 6.1 và 6.5 (cột #16 và #99). Thời gian stage và thời gian từng bước đo bằng hai cách khác nhau nên không cộng chéo. Chênh lệch nguồn: PR #98 và #99 là hai lần chạy khác nhau của cùng mẫu, mốc bắt đầu khác nhau (768 s tính từ import, 626,5 s từ `POST /api/tasks`); issue #97, tạo trước PR #98, ghi RSS đỉnh 3,10 GiB (2,05 giờ) và 2,35 GiB (0,93 giờ), nên dùng số của PR #98 vì đo sau.

#### Đánh đổi và phương án đã loại

- Chọn giải mã một lần ở decoder với downmix và resample về 16 kHz mono. Loại để pyannote nhận đường dẫn (thời gian bậc hai) và loại giải mã cả file ở 48 kHz stereo rồi resample (RSS đỉnh 16 GiB); decoder torchcodec giải mã cùng file trong 3,8 s với RSS đỉnh 1,7 GiB. Issue vẫn cho phép downmix/resample theo cửa sổ nếu giữ chất lượng; PR không dùng.
- Đánh đổi chất lượng: số học bộ resample của decoder (FFmpeg) khác torchaudio của pyannote, nên mẫu ghép đổi từ 12 sang 13 người nói (92,0% thời lượng cùng nhãn); bản 56 phút giữ 10 người nói (99,2%). Độ lệch âm lượng +3 dB của downmix không ảnh hưởng (nhãn giống hệt sau khi bù). Chủ sở hữu xác nhận đầu ra cũ không phải nguồn chuẩn (không biết 12 hay 13 người nói là đúng), nên số khớp chỉ để tham khảo.
- Chưa thử: diarization theo cửa sổ rồi nối người nói, tham số clustering, số thread, batch size, model khác. Embeddings chiếm 87% pipeline và tăng tuyến tính nên chưa cần.
- Bộ nhớ: waveform tăng tuyến tính (0,215 GiB mỗi giờ audio) nhưng RSS đỉnh có thêm thành phần bậc hai ở bước clustering (khoảng 0,1 GiB × giờ²), có từ trước thay đổi này; bản ghi 4 giờ (trần thời lượng mặc định) vượt 4 GiB (#97).

#### Follow-up

- #97: RSS đỉnh của diarization có thành phần bậc hai ở clustering; xác nhận nguồn, chọn giới hạn bộ nhớ cho bản ghi 4 giờ rồi giảm hoặc ghi yêu cầu RAM tối thiểu.

#### Liên kết issue/PR

Issue #71, PR #98; liên quan #16 (AC-13), #97, #99.

#### Tag

Hiệu năng

#### Tài liệu đích

- `docs/backend-contract.md` mục 3 (dòng "Pipeline recording": diarization nhận waveform đã giải mã một lần; stage `decoding_input`, mã lỗi `diarization_input_decode_failed`).
- `docs/backend-design.md` mục 5, hàng "Pipeline model" (số đo thời gian và bộ nhớ).
- `docs/acceptance-report.md` mục 6.5 (AC-13 sau thay đổi).
- Release notes (#17): giới hạn bộ nhớ của bản ghi 4 giờ.


### #99 / PR #103 — Đo lại AC-13 trên main sau #71, #72, #74

#### Vấn đề

AC-13 yêu cầu bản ghi 2 giờ có cả transcript và notes sẵn để duyệt trong tối đa 30 phút kể từ khi bắt đầu xử lý (`POST /api/tasks`). Ở #16 nó fail: transcript sẵn sau 128,9 phút và notes lỗi `provider_output_too_large`. Sau #71, #72, #74 chưa có lần đo liền mạch của mẫu ghép 2 giờ (7366 s) từ import tới notes trên main.

#### Nguyên nhân đo được

Lần đo trên main `4a88ec8` (có #71, #72, #74), ChatGPT `gpt-5.6-sol` medium, đầu ra English: AC-13 vẫn fail và bước chậm nhất chuyển từ diarization sang notes.

- Notes chạy 1.463,5 s (70,0% của 2.090,2 s) với 29 lượt gọi ChatGPT nối tiếp (14 chunk, độ song song 1), rồi lỗi `provider_output_too_large` ở lượt 29 (stage `reduce`, lượt `recovery`, `output_bytes` 10.701 so với trần 10.700; `output_bytes` là cận dưới).
- Sau transcript (10,4 phút) chỉ còn 30 − 10,4 = 19,6 phút trong ngân sách; notes đã dùng 24,4 phút mà chưa tới lượt cuối.
- 14 lượt đầu (bằng số chunk) mất 615 s, trung vị 41,3 s mỗi lượt; 15 lượt sau mất 847 s, trung vị 61,7 s. Cách chia hai nhóm là suy ra, vì chỉ stage của lượt cuối được ghi.
- Ước tính trước khi đo (issue #99): notes khoảng 13–20 phút; đo được 24,4 phút tới lúc lỗi.

#### Thay đổi thiết kế

Không đổi mã sản phẩm. PR cập nhật `docs/acceptance-report.md`: thêm mục 6.5 (bảng stage, lượt gọi, nguyên nhân, giới hạn phép đo); cập nhật mục 1.1 (tiêu chí 5), 1.3 và 5.1 (AC-13); ghi chú số byte vượt trần là cận dưới (mục 6.3, dòng AC-39, mục 8.3); thêm follow-up #101 và #102 ở mục 9. Phương pháp giữ như #16: mốc bắt đầu `POST /api/tasks`, import tính riêng ngoài AC-13, khoảng chờ trước khi bấm "Tạo notes" ghi riêng.

#### Số đo trước và sau

Mẫu ghép 2 giờ (7366 s), bản cài eval (RTX 5080).

| | Trước: #16 (`151f3e4`) | Sau: #99 (`4a88ec8`) |
| --- | --- | --- |
| Upload / kiểm tra phía server (ngoài AC-13) | 0,19 s / 81,2 s | 0,24 s / 81,0 s |
| ASR (`transcribing`) | 510,0 s, 14,4× thời gian thực | 525,55 s, 14,0× |
| Diarization | 7.222,6 s | 96,73 s (nạp model 4,26 + giải mã 2,85 + chạy 89,62) |
| Segment / nhãn người nói | 1838 / 12 | 1706 / 13 |
| Transcript sẵn (từ `POST /api/tasks`) | 128,9 phút (7.734 s) | 10,4 phút (626,53 s) |
| Từ transcript sẵn tới bấm "Tạo notes" | 0,87 s | 0,75 s (0,17 s từ lúc UI báo sẵn) |
| Notes: chunk / byte nguồn / lượt gọi | 16 / 340.521 / 25 | 14 / 307.154 / 29 |
| Kết quả notes | lỗi ở lượt 25 sau 958,6 s (16,0 phút) | lỗi ở lượt 29 sau 1.463,47 s (24,4 phút) |
| AC-13: bắt đầu → transcript và notes sẵn | không có mốc; tác vụ notes kết thúc lỗi sau 144,9 phút | không có mốc; tác vụ notes kết thúc lỗi ở 2.090,2 s (34,8 phút) |

Nguồn: `docs/acceptance-report.md` mục 6.1 (cột mẫu 2 giờ) và 6.5; PR #103; issue #99. Tỷ trọng trong 2.090,2 s: ASR 25,1%, diarization 4,6%, notes 70,0%. Diarization nhanh khoảng 75 lần, transcript sẵn nhanh khoảng 12 lần. Thời gian stage lấy từ việc hỏi API tác vụ khoảng mỗi 0,7 s nên sai số tới khoảng 1 s.

#### Đánh đổi và phương án đã loại

- Một lần đo, một provider và model, trên mẫu ghép (không phải một cuộc họp 2 giờ liền); độ trễ phụ thuộc giờ trong ngày; không gặp quota hay rate-limit.
- Không đo lại: lỗi notes là giới hạn của sản phẩm, không phải sự cố hạ tầng (không có lỗi mạng, quota, máy chủ hay GPU).
- Không sửa mã sản phẩm trong PR đo; bước chậm nhất và lỗi còn lại đi vào follow-up.
- Không có mốc "notes sẵn" nên báo cáo ghi tổng tới lúc tác vụ notes kết thúc lỗi: 2.090,2 s từ `POST /api/tasks`, 2.171,6 s từ lúc bấm tạo (gồm import 81,4 s). Thời gian notes khi thành công chưa đo được; 24,4 phút chỉ là cận dưới.
- Công cụ đo bấm "Tạo notes" ngay (0,17 s và 0,75 s); người thật có thể chờ lâu hơn. Khoảng chờ đó có nằm trong AC-13 hay không là quyết định đặc tả (#73).

#### Follow-up

- #101: notes là bước chậm nhất (24,4 phút, 29 lượt gọi tuần tự); mục tiêu tổng ≤ 30 phút (chặn #17).
- #102: lỗi vượt trần còn lại sau khi hết lượt phục hồi (chặn #17).

#### Liên kết issue/PR

Issue #99, PR #103; liên quan #16 (AC-13), #71, #72, #73, #74.

#### Tag

Hiệu năng

#### Tài liệu đích

- `docs/acceptance-report.md` mục 6.5 (PR #103 đã cập nhật), cùng mục 1.1, 1.3 và 5.1 (AC-13).
- Release notes (#17): kết quả nghiệm thu AC-13 và giới hạn đã biết.


### #104 / PR #109 — Notes một lượt theo context thật của model

#### Vấn đề

AC-13 cần transcript và notes của mẫu 2 giờ sẵn trong tối đa 30 phút từ `POST /api/tasks`. Trước #104, planner dùng ngân sách bảo thủ tương đương context 64K: mẫu ghép 2 giờ cần 14 chunk và 29 lượt gọi tuần tự; #99 mất 1.463,5 s ở notes rồi lỗi `provider_output_too_large`, tác vụ kết thúc lỗi sau 2.090,2 s. #104 mở đường một lượt chỉ khi transcript vừa ngân sách thật của đúng hai cấu hình chủ sở hữu; nguồn quá lớn và model khác vẫn dùng chia/hợp nhất hiện có.

#### Nguyên nhân đo được

- Giới hạn thực của `gpt-5.6-sol` là context 272.000 token; Codex dùng 95%, tương đương 258.400 token có thể lập kế hoạch. Không có trần output model được công bố; ứng dụng giữ guard body 1 MiB cho request ChatGPT, và các lần gọi thật đã đạt các kích thước được đo.
- `gemini-3.8-flash` qua OMP có context 1.048.576 token và trần 65.536 token cho output, gồm cả thinking; RPC frame của ứng dụng là 1 MiB. Vì vậy trần transport và trần token phải được kiểm riêng.
- Ước tính token của đường mới là `ceil(UTF-8 bytes × 2 / 3)` trên prompt, schema, phần dự phòng repair và headroom; đường single-pass dành 65.536 token cho output và 4.000 token headroom. `PROMPT_VERSION` là `notes-hierarchical-v7`.
- Schema enum nghiêm ngặt của structured outputs chỉ chứa tối đa 999 mã nguồn trong một scope. Với hơn 999 segment, pipeline gom các segment liên tiếp thành unit, `unit_size = ceil(n / 999)`; mỗi mục chịu giới hạn `200 // unit_size` unit và khi khôi phục sẽ trả lại toàn bộ mã segment thành viên.

Nguồn: PR #109 (AC checklist và Evidence); issue #104; giới hạn từ [Codex rust-v0.154.0 `codex-rs/protocol/src/openai_models.rs`](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/protocol/src/openai_models.rs#L400-L525) và [ChatGPT model catalog](https://chatgpt.com/backend-api/codex/models?client_version=0.154.0); [OMP v18.5.0 `packages/catalog/src/models.json`](https://github.com/can1357/oh-my-pi/blob/v18.5.0/packages/catalog/src/models.json#L1); code tại `src/asr_en_vi/notes_document.py`.

#### Thay đổi thiết kế

- Thêm bảng ngân sách tĩnh theo cặp `(provider, model)` đúng hai cấu hình: ChatGPT `gpt-5.6-sol` và Antigravity `gemini-3.8-flash`. Cặp khác giữ ngân sách bảo thủ trước #104.
- Chỉ chọn single-pass khi phép kiểm context, request guard và repair reserve cùng đạt. Chunk/reduce giữ các trần transport hiện có; không chuyển prompt rút gọn của #100 vào đường này.
- Giữ enum source ID bằng citation units thay vì đổi sang chuỗi tự do. Các unit liên tiếp có cùng alias trong prompt; validator vẫn kiểm tra scope và bước restore mở alias thành các segment gốc.
- Prompt single-pass thêm điều khoản bao quát: một mục riêng cho mỗi chủ đề, quyết định, câu hỏi, hành động và mục cần xác nhận khác nhau trong toàn bộ cuộc họp, theo thứ tự nguồn; mục ngắn nhưng không được bỏ hoặc gộp ý riêng. Prompt chunk/reduce không đổi. Việc ghi usage chỉ giữ số đếm `input/output/reasoning`, estimate và kích thước prompt, không ghi nội dung.

#### Số đo trước và sau

Điều kiện trước là #99 trên mẫu ghép 2 giờ, ChatGPT `gpt-5.6-sol` medium, đường chunk/reduce bảo thủ: 14 chunk, 29 lượt tuần tự, notes lỗi sau 1.463,5 s và tác vụ kết thúc lỗi ở 2.090,2 s. Điều kiện sau là lượt đo final trên revision `b78711d`, notes English, prompt `notes-hierarchical-v7`, ba lượt ChatGPT `gpt-5.6-sol` medium và một lượt Flash final chạy notes-only; nguồn trên 999 segment dùng citation units.

| Lượt | Segment / citation unit | POST → ready | Lượt gọi | Prompt B | Estimate → input (tỷ lệ) | Output / reasoning | Tìm thấy / partly / thiếu / bịa | Mơ hồ nâng (chắc / giáp ranh) | Bullet / từ / phút đọc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| Single 1 | 661 / — | 116,5 s | 1 | 109.426 | 77.812 → 33.184 (2,34×) | 6.111 / 1.825 | 31 / 4 / 2 / 0 trên 37 | 2 / 3 | 85 / 1.585 / 6,9 |
| Single 2 | 1.269 / 635 (unit 2) | 67,2 s | 1 | 186.630 | 127.849 → 56.286 (2,27×) | 3.451 / 482 | 28 / 7 / 3 / 0 trên 38 | 2 / 1 | 65 / 1.286 / 5,6 |
| Single 3, mẫu ghép | 1.779 / 890 (unit 2) | 81,3 s | 1 | 262.375 | 179.196 → 77.048 (2,33×) | 4.258 / 239 | 48 / 19 / 8 / 0 trên 75 | 1 / 1 | 92 / 1.725 / 7,5 |
| Flash final | 1.779 / 890 (unit 2) | 241,7 s | 1 | 262.375 | 179.312 → 96.692 (1,85×) | 33.200 / 28.325 | không chấm | — | 79 / 1.227 / 5,3 |
| Baseline C, chunked | — | — | — | — | — | — | baseline tìm thấy 34/37 | — | 63 / 1.458 / 6,3 |
| Baseline B, chunked | — | — | — | — | — | — | baseline tìm thấy 35/38 | — | 97 / 2.469 / 10,7 |

Số tìm thấy cộng partly (cận trên) của ba lượt sol là 35/37, 35/38 và 67/75; ba lượt sol đều có 0 mục bịa. Flash final không chấm quality. Các mục partly được ghi nhận chủ yếu vì ngày dự kiến và kế hoạch được đặt dưới `Items Requiring Confirmation` thay vì `Decisions`. Các lượt đã ghi quality đều có estimate lớn hơn `input_tokens`; tỷ lệ thực tế trong bảng là phép chia estimate cho `input_tokens`.

Ghi chú candidate trước (prompt v7, trước citation units): Flash thành công một lượt nhưng recall chỉ 37/75 so với baseline 69/75; không dùng làm số đo final.

Nguồn: PR #109 (AC checklist và Evidence); issue #104; lượt đo final và các baseline trong Evidence.

#### Đánh đổi và phương án đã loại

- Chọn single-pass chỉ cho model và nguồn vừa ngân sách thật. Đánh đổi: nguồn quá lớn vẫn có nhiều lượt gọi; các nguồn trên 999 segment dùng citation unit nên độ hạt của tham chiếu thô hơn trong prompt, nhưng restore vẫn trả đủ segment gốc.
- Loại schema `source_id` dạng chuỗi không có enum: candidate trước từng trả `unknown_source_id`; output 21.527 B vượt giới hạn repair 8.000 B nên không còn repair, làm cả lượt single-pass lỗi.
- Điều khoản completeness là cần thiết nhưng chưa đủ: Flash v7 vẫn chỉ tìm thấy 37/75, partly 21 và thiếu 17; không dùng nó làm bằng chứng đã đạt AC-5.
- Hoãn phương án chia nguồn dài thành các phần khoảng một giờ chạy song song rồi hợp nhất một lượt sang #108. Không đưa quy tắc rút ngắn của #100 vào PR này; chủ sở hữu đã bỏ mục tiêu đọc 2–3 phút, nên không rút ngắn các notes mới theo mục tiêu đó.

#### Follow-up

- #107: chuyển các ngày dự kiến và kế hoạch đã chốt trong cuộc họp từ `Items Requiring Confirmation` sang `Decisions`; các đề xuất chưa được thống nhất vẫn ở `Items Requiring Confirmation`; AC-5 của #104 vẫn để **Not done** với các số đo trên.
- #108: nguồn dài hơn khoảng một giờ → chia thành các phần khoảng một giờ, chạy song song rồi hợp nhất một lượt.
- #100: giữ mục tiêu mỗi ý một mục và schema sáu nhóm theo hàng đã cập nhật ở mục 5; bỏ mục tiêu đọc 2–3 phút, không còn là blocker của #104.

#### Liên kết issue/PR

Issue #104, PR #109; liên quan #99, #100, #101, #102, #107, #108.

#### Tag

Notes; Hiệu năng

#### Tài liệu đích

- `docs/backend-contract.md` mục 5 (ngân sách, single-pass, citation và phục hồi).
- `docs/backend-design.md` mục 4 và 8 (Q17–Q18, model/provider và đánh đổi chia/hợp nhất).
- `docs/acceptance-report.md` mục 6.6 và hàng AC-13 (mốc đo #104); release notes (#17) cho giới hạn đã biết.
