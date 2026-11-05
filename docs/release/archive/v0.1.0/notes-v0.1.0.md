# Pipeline notes — hồ sơ thay đổi v0.1.0

Các mục lịch sử được chuyển từ tài liệu thay đổi chung khi chuẩn bị #17.
Số đo giữ revision và điều kiện gốc; không phải phép đo lại artifact 0.1.0.

### #72 / PR #94 — Phục hồi notes khi đầu ra của provider vượt trần

#### Vấn đề

Trong mục này, “Mẫu ghép 2 giờ”/“mẫu ghép” là audio của hai bản ghi thuộc bộ nghiệm thu được nối lại (7366 s, không phải một cuộc họp); A, B và C là các nhãn bản ghi của bộ nghiệm thu.

Notes của bản ghi dài được tạo qua nhiều lượt gọi provider (ChatGPT hoặc Antigravity): chia nguồn thành chunk, trích xuất từng chunk, hợp nhất bằng các lượt reduce theo tầng, rồi lượt cuối. Mỗi bước có trần byte cho câu trả lời (`response_limit`). Một lượt vượt trần làm cả lần tạo notes lỗi `provider_output_too_large`: 3 trong 4 nguồn dài của #16 không ra notes và mất 9,5–16 phút lượt gọi trước đó, không có lượt sửa hay checkpoint.

#### Nguyên nhân đo được

| Nguồn | Chunk | Lượt gọi lỗi | Độ dài ghi nhận | Trần | Công việc bị mất |
| --- | --- | --- | --- | --- | --- |
| Mẫu ghép 2 giờ | 16 | 25 | 10.706 byte | 10.700 | 958,6 s |
| Bản ghi A (75 phút) | 8 | 13 | 10.711 byte | 10.700 | 571,3 s |
| Bản ghi B (67 phút) | 10 | 16 | 10.703 byte | 10.700 | 638,8 s |

Nguồn: issue #72; `docs/acceptance-report.md` mục 6.1 và 6.3.

- Cả ba lỗi ở lượt reduce cấp 2 đầu tiên (HTTP 200, lần thử `primary`). Nguồn không cần reduce cấp 2 (bản ghi C, 56 phút, 6 chunk) thành công sau 320,8 s.
- `request_document` chỉ sửa lỗi validation; lỗi vượt trần ném ở transport trước khi tới validation. Trần reduce là 10.700 byte (`MAX_PROMPT_BYTES // 3 // 100 * 100`), trần câu trả lời chunk là 8000.
- Độ dài ghi nhận là cận dưới: client ChatGPT ngừng đọc stream ngay khi tổng vượt trần (ghi nhận sau #99, xem #102). Mức vượt thật có thể lớn hơn 3–11 byte.

#### Thay đổi thiết kế

Lượt vượt trần không dừng ngay. Với ChatGPT là `provider_output_too_large`; với Antigravity (OMP không áp trần phản hồi) là kiểm tra kích thước `size` sau khi nhận tài liệu. Pipeline gửi lại đúng prompt chính kèm một câu nêu mục tiêu độ dài thấp hơn (lượt `recovery`), trong giới hạn cố định:

- Tối đa 2 lượt mỗi yêu cầu (`MAX_OUTPUT_RECOVERY_ATTEMPTS`) và 4 lượt mỗi tác vụ (`MAX_OUTPUT_RECOVERY_CALLS`); `MAX_PROVIDER_CALLS` 49 → 53.
- Mục tiêu giảm còn 3/4 mỗi lượt, làm tròn xuống 100 byte: reduce và chunk 8000 → 6000 → 4500; final/single 128000 → 96000 → 72000.
- Trần phản hồi giữ nguyên; prompt phục hồi vẫn qua kiểm tra `MAX_PROMPT_BYTES`; không vừa thì không gửi và trả lỗi gốc.
- Hết lượt: lỗi của lượt cuối kèm chẩn đoán (stage, `attempt=recovery`, chỉ số lượt gọi, `output_bytes`, `response_limit`), không công bố notes. Antigravity vẫn lỗi `notes_invalid_output`.
- Prompt chính không đổi từng byte (test băm 8 prompt en/vi) nên `PROMPT_VERSION` giữ `notes-hierarchical-v6`; manifest coverage thêm `provider_calls` và `output_recoveries`.

#### Số đo trước và sau

Bản ghi A (75 phút, 8 chunk), ChatGPT `gpt-5.6-sol` medium, đầu ra English, bản cài eval (bản cài thử riêng, không phải bản cài chính).

| | Trước (#16, `151f3e4`) | Sau (PR #94, `3c96052`) |
| --- | --- | --- |
| Kết quả | lỗi `provider_output_too_large` ở lượt 13 (reduce, 10.711 > 10.700 byte), không có notes | thành công, đủ 8 chunk |
| Thời gian | 571,3 s tới lúc lỗi | 798,5 s tới khi hoàn tất |
| Lượt gọi / lượt phục hồi | 13 / 0 | 16 / 1 |

Nguồn: PR #94 (AC-5 và Evidence); `docs/acceptance-report.md` mục 2.6. Sau sửa, lượt reduce thứ 13 trả 10.702 byte (vượt 10.700) và được gọi lại ở lượt 14. Hai thời gian không so trực tiếp được vì "trước" dừng ở lúc lỗi.

#### Đánh đổi và phương án đã loại

- Chọn gọi lại với mục tiêu độ dài thấp hơn. Đánh đổi: một tác vụ có lượt vượt trần tốn thêm tối đa 4 lượt gọi provider.
- Loại chia nhỏ đầu vào reduce: các lần vượt trần quan sát chỉ 0,03–0,1%, mục tiêu thấp hơn không đổi kế hoạch chunk/reduce và chỉ tốn thêm một lượt gọi. Issue còn nêu cắt bớt, khoảng đệm giữa mục tiêu prompt và trần cứng, checkpoint; PR không đánh giá các hướng này.
- Giới hạn lộ ra ở #99: mục tiêu trong prompt không giới hạn được độ dài thật; reduce của mẫu ghép 2 giờ vẫn vượt trần 10.700 sau khi hết lượt phục hồi (8000 → 6000 → 4500) và tác vụ lỗi sau 1.463,47 s. Mức vượt 0,03–0,1% chỉ là cận dưới, nên lý do chọn ở trên chưa được xác nhận (#102).
- Ngoài phạm vi: lượt repair (sửa lỗi validation, một lần mỗi tác vụ) vượt trần không được phục hồi; đầu vào repair ≤ 8000 byte nên hiếm gặp (#96).

#### Follow-up

- #102: lỗi vượt trần còn lại ở mẫu ghép 2 giờ sau khi hết lượt phục hồi (chặn #17).
- #96: lượt repair vượt trần không được phục hồi (mức thấp, chưa thấy ở lần chạy thật).
- #95: trạng thái provider (`last_diagnostic`) vẫn báo vượt trần sau khi tác vụ đã phục hồi và thành công.

#### Liên kết issue/PR

Issue #72, PR #94; liên quan #16 (AC-13, AC-39), #99.

#### Tag

Notes

#### Tài liệu đích

- `docs/backend-contract.md` mục 5 (đoạn lượt gọi có đầu ra vượt trần; đã mô tả cơ chế, cập nhật khi #102 đổi hành vi).
- `docs/backend-design.md` mục 4 và 8 (Q17, Q18: giới hạn lượt gọi lại của notes; thêm quan hệ giữa lượt sửa Q18 và lượt phục hồi).
- `docs/acceptance-report.md` mục 8.3 (AC-39).


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


### #100 / PR #111 — Notes văn phong telegraph, mỗi ý một mục

#### Vấn đề

Đường single-call v7 tạo notes dài 5,6–6,9 phút đọc và còn lặp ý giữa các mục; các ngày đã thống nhất bị xếp vào `Items Requiring Confirmation` (#107). Chủ sở hữu bỏ mục tiêu đọc 2–3 phút ngày 2026-10-26, giữ ưu tiên độ đầy đủ.

#### Nguyên nhân đo được

Theo thứ tự A, C, B, baseline v7 có 27/38/15 ý lặp và 4/5/4 mục Overview; notes có 1.337/1.585/1.286 từ (5,8/6,9/5,6 phút đọc), với 1/2/2 mục #107 đặt sai.

#### Thay đổi thiết kế

- Thêm một bộ quy tắc dùng chung `_TELEGRAPH_RULE` và `_PLACEMENT_RULE` vào phần base text của `_source_instructions` (single + chunk) và `_reduction_instructions` (intermediate + final), cho cả EN và VI.
- Giữ nguyên các câu yêu cầu chất lượng hiện có; không đổi schema sáu mục, validator, ngân sách hoặc cap. `PROMPT_VERSION` tăng lên `notes-hierarchical-v8`. Test phủ từng cặp step × language.

#### Số đo trước và sau

Điều kiện đo: ChatGPT `gpt-5.6-sol` medium, EN, một lượt chạy mỗi cuộc họp, một AI scorer mỗi lượt; baseline là v7 single-call (các baseline C và B lấy từ #104).

| Cuộc họp | Phiên bản | Tìm thấy / partly / thiếu | Tìm thấy + partly | Ý lặp | Mục Overview | Số mục | Từ | Phút đọc | Ký tự MD | POST → ready | Lượt gọi | Token output | #107 đặt sai | Bịa | Sai owner |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | v7 (trước) | 41 / 5 / 5 | 46 | 27 | 4 | 74 | 1.337 | 5,8 | 9.861 | 83,2 | 1 | 4.264 | 1 | 0 | 0 |
| A | candidate c1 | 41 / 6 / 4 | 47 | 10 | 3 | 72 | 891 | 3,9 | 7.288 | 83,3 | 1 | 4.397 | 1 | 0 | 0 |
| A | c2 (phát hành) | 44 / 3 / 4 | 47 | 12 | 2 | 57 | 832 | 3,6 | 6.700 | 112,4 | 1 | 5.994 | 0 | 0 | 0 |
| C | v7 (trước) | 31 / 4 / 2 | 35 | 38 | 5 | 85 | 1.585 | 6,9 | 11.189 | 116,5 | 1 | 6.111 | 2 | 0 | 1 |
| C | candidate c1 | 29 / 6 / 2 | 35 | 14 | 2 | 80 | 981 | 4,3 | 7.772 | 76,2 | 1 | 3.984 | 1 | 0 | 2 |
| C | c2 (phát hành) | 33 / 3 / 1 | 36 | 19 | 3 | 63 | 805 | 3,5 | 6.466 | 91,3 | 1 | 4.682 | 0 | 0 | 0 |
| B | v7 (trước) | 28 / 7 / 3 | 35 | 15 | 4 | 65 | 1.286 | 5,6 | 9.083 | 67,2 | 1 | 3.451 | 2 | 0 | 0 |
| B | candidate c1 | 27 / 10 / 1 | 37 | 11 | 3 | 84 | 882 | 3,8 | 7.163 | 77,2 | 1 | 3.931 | 2 | 0 | 0 |
| B | c2 (phát hành) | 26 / 7 / 5 | 33 | 15 | 3 | 66 | 837 | 3,6 | 6.567 | 81,4 | 1 | 4.269 | 2 | 0 | 0 |

Nguồn: issue #100; PR #111 (AC checklist và Evidence); các bảng đo tester-0/1/2 và baseline #104.

#### Đánh đổi và phương án đã loại

- Chọn c2 thay c1: tổng `Found` strict là 103 so với 97 của c1 và 100 của baseline; tổng #107 misplaced giảm còn 2 so với 4 của c1 và 5 của baseline v7; đổi lại tổng repeats tăng lên 46 so với 35. Một lượt chạy mỗi cuộc họp có nhiễu khoảng ±2 mục tìm thấy và khoảng ±3 lần lặp.
- Lần thử đầu (chunk/reduce c1–c5, branch `feat/issue-100-concise-notes`): mục tiêu độ dài mềm bị bỏ qua (vòng 2 của lần thử đầu: 890–1.192 từ); budget cứng rút ngắn notes nhưng làm giảm recall và đẩy ý sang confirmation; repeats còn 6–11, không về 0; effort high mất khoảng 2,1× thời gian mà không tăng chất lượng.
- Loại schema `maxItems`/validator cho Overview: sẽ buộc repair và làm các tài liệu cũ không còn hợp lệ.

#### Follow-up

- #110: repeats còn lại; AC-2 chưa đạt; strict Found của mẫu B là 26 so với 28, nên AC-4 và điều kiện chồng lấp của AC-3 chưa đạt ở đó; #110 yêu cầu ít nhất hai lượt chạy mỗi cuộc họp.
- #107: placement đã cải thiện nhưng một cuộc họp vẫn còn mục đặt sai; giữ issue mở.
- Prompt chunk/reduce chưa được đo bằng real runs.

#### Liên kết issue/PR

Issue #100, PR #111; liên quan #104, #107, #110, #16.

#### Tag

Notes

#### Tài liệu đích

- `docs/backend-contract.md` mục 5 (prompt version).
- `docs/backend-design.md` (quy tắc prompt notes).
- `docs/acceptance-report.md` (số liệu chất lượng).
- Release notes (#17).
