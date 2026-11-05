# Chọn model tạo notes: Google Antigravity và ChatGPT

> Trạng thái: khảo sát ngày 2026-10-21 trên một tài khoản Google AI Pro trả phí và một tài khoản ChatGPT, với một cuộc họp thật, pipeline của PR #40. Tài liệu chỉ ghi số liệu, không chép nội dung cuộc họp.

## Kết luận

- **Kết quả tốt nhất:** Google Antigravity với **Gemini 3.8 Flash** (`gemini-3.8-flash`) ở mức suy nghĩ **High**. Cả hai lần chạy đều đứng đầu bảng chấm mù, chủ yếu nhờ bắt được nhiều action item nhất. Đổi lại, mỗi cuộc họp 56 phút mất khoảng 7–10 phút (434–572 giây qua 3 lần chạy).
- **Tốt nhất phía ChatGPT:** `gpt-5.6-sol` ở effort low hoặc medium. Model này nắm quyết định tốt ngang Gemini High, phần tổng quan tốt nhất, nhưng bỏ sót nhiều action item hơn. Mỗi lần chạy mất 4–6 phút.
- **Effort cao hơn không giúp ChatGPT:** `gpt-5.6-sol` high chậm gấp ba (730 giây) và điểm thấp hơn bản low/medium.
- **Khi cần nhanh:** Gemini 3.8 Flash Medium chỉ mất khoảng 1,5 phút, không sai dữ kiện nào, nhưng bỏ sót nhiều quyết định hơn.

Khác biệt giữa các cấu hình dẫn đầu là vừa phải và chỉ đo trên một cuộc họp, một người chấm. Vì vậy app cho người vận hành chọn: Antigravity mặc định High, có thêm Medium; ChatGPT giữ lựa chọn effort như trước.

## Rủi ro điều khoản và dữ liệu của Antigravity

- Điều khoản Antigravity ghi: "Using third party software, tools, or services to access the Service (e.g. using OpenClaw with Antigravity OAuth) is a breach of this Agreement. Such actions may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts." OMP là phần mềm bên thứ ba dùng OAuth của Antigravity. [Terms](https://antigravity.google/terms)
- FAQ nhắc lại rằng việc này có thể dẫn tới đình chỉ tài khoản. Với agent bên thứ ba, Google khuyến nghị dùng API key Gemini Enterprise hoặc Google AI Studio. [FAQ](https://antigravity.google/docs/faq/)
- Google dùng dữ liệu tương tác (Interactions) để cải thiện sản phẩm, và "Google employees and contractors may access, view, review and use Interactions", trừ khi người dùng tắt trong phần cài đặt. Transcript gửi qua nhánh này thuộc phạm vi đó. [Terms](https://antigravity.google/terms)

Người vận hành đã chọn giữ nhánh Antigravity sau khi biết rủi ro này. Xem thêm [ADR 0007](adr/0007-cloud-notes-providers.md).

## Model dùng được

Cột "Gọi thử" là kết quả một yêu cầu kiểm tra tổng hợp, không gửi transcript.

| Model | Gói theo bảng chính thức | Gọi thử | Ghi chú |
| --- | --- | --- | --- |
| Gemini 3.8 Flash | Mọi gói Antigravity | Được | GA từ 2026-09-02; 1.048.576 token vào, 65.536 token ra; mức suy nghĩ low/medium/high. [Model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [thinking](https://ai.google.dev/gemini-api/docs/thinking) |
| Claude Sonnet 5.5, Opus 5.5 | Pro trả phí (không gồm bản dùng thử), Ultra, Enterprise | Được | Dùng chung hạn mức với GPT-OSS, tách khỏi hạn mức Gemini. [Models](https://antigravity.google/docs/models/) |
| Gemini 4 Argon | Pro trả phí (không gồm bản dùng thử), Ultra, Enterprise | 404 | GA từ 2026-10-13, sau ngày phát hành OMP 18.5.0; thử lại khi nâng OMP. [Models](https://antigravity.google/docs/models/) |
| Gemini 3.1 Pro, 3.7 Flash, 3.6 Flash | Chỉ Enterprise | 404 | Vẫn có trong catalog OMP. [Models](https://antigravity.google/docs/models/) |
| GPT-OSS-120b, Claude 4.6 | Free/Plus, Pro (GPT-OSS cả Ultra) | GPT-OSS được, Claude 4.6 lỗi | Bị gỡ ngày 2026-11-02. [Models](https://antigravity.google/docs/models/) |
| ChatGPT: `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` | Catalog của tài khoản ChatGPT | Được | Effort theo model, đã có lựa chọn trong app. |

Google không công bố con số hạn mức Antigravity. Mỗi nhóm model có giới hạn 5 giờ và giới hạn theo tuần. [Models](https://antigravity.google/docs/models/)

## Benchmark

**Cách chạy:** dùng cùng một bản chụp nguồn: cuộc họp 56 phút, 736 đoạn, tiếng Anh xen tiếng Việt, notes tiếng Việt. Cả hai provider đi qua cùng pipeline: 6 chunk, rồi bản cuối hoặc bước gộp.

**Cách chấm:**
- Một người chấm độc lập đọc toàn bộ transcript, tự lập bộ đáp án (11 quyết định, 19 action item), rồi đối chiếu từng mục của 16 bản notes với transcript.
- Người chấm không biết bản nào của model nào.
- Độ phủ là tỉ lệ mục trong bộ đáp án mà bản notes bắt được. Độ chính xác là tỉ lệ mục trong bản notes đúng với transcript.

| Cấu hình | Thời gian | Độ phủ action | Độ phủ quyết định | Độ chính xác action / quyết định | Lỗi dữ kiện | Hữu ích (1–5) |
| --- | --- | --- | --- | --- | --- | --- |
| Antigravity 3.8 Flash High, lần 1 | 572 s | 0,74 | 0,73 | 1,00 / 0,90 | 1 | 3 |
| Antigravity 3.8 Flash High, lần 2 | 508 s | 0,79 | 0,64 | 1,00 / 0,89 | 0 | 4 |
| ChatGPT `gpt-5.6-sol` low | 254 s | 0,58 | 0,73 | 0,87 / 0,80 | 1 | 4 |
| ChatGPT `gpt-5.6-sol` medium | 340 s | 0,58 | 0,73 | 0,93 / 0,89 | 1 | 4 |
| ChatGPT `gpt-6-luna` medium | 194 s | 0,74 | 0,45 | 1,00 / 1,00 | 0 | 4 |
| Antigravity 3.8 Flash Medium (3 lần) | 78–88 s | 0,58–0,63 | 0,36–0,55 | 1,00 / 0,88–1,00 | 0 | 4 |
| ChatGPT `gpt-5.6-sol` high | 730 s | 0,47 | 0,64 | 0,71 / 0,88 | 1 | 3 |
| ChatGPT `gpt-6-luna` high | 234 s | 0,63 | 0,45 | 1,00 / 1,00 | 0 | 4 |
| ChatGPT `gpt-6-astra` medium | 244 s | 0,53 | 0,55 | 0,91 / 1,00 | 1 | 4 |
| ChatGPT `gpt-6.1-sol` medium | 237 s | 0,53 | 0,55 | 0,91 / 1,00 | 1 | 4 |
| Antigravity 3.8 Flash Low | 86 s | 0,63 | 0,45 | 1,00 / 1,00 | 2 | 3 |
| Antigravity 3.5 Flash-Lite | 81 s | 0,26 | 0,64 | 1,00 / 0,75 | 0 | 2 |
| Antigravity GPT-OSS-120b | 566 s | 0,32 | 0,36 | 0,63 / 0,36 | 5 | 1 |
| ChatGPT `gpt-6-sol` medium | 175 s | 0,47 | 0,09 | 0,90 / 1,00 | 1 | 2 |
| Antigravity Claude Sonnet 5.5 Medium | 209 s | lỗi `notes_synthesis_limit_exceeded` | – | – | – | – |

Quan sát thêm:

- **Hai lỗi chung:** gần như mọi bản đều bỏ sót một vài action item được giao rõ ràng. Năm bản (cả hai provider) gán nhầm người phụ trách cho cùng một action item.
- **Token:** Gemini High dùng khoảng 68 nghìn token reasoning và 80 nghìn token đầu ra cho cả cuộc họp; Medium dùng khoảng 10–12 nghìn token đầu ra. Lần gọi High lâu nhất mất 194 s, trong giới hạn 15 phút mỗi lần gọi.
- **Low:** phải dùng lượt sửa duy nhất của tác vụ, nên app không đề xuất mức này.
- **Claude Sonnet 5.5:** tóm tắt chunk dài gấp 2–3 lần Gemini nên pipeline của PR #40 không gộp được trong ngân sách. Ngân sách gộp thích ứng đã có từ #11: trần đầu ra của tóm tắt trung gian được suy từ ngân sách prompt và tóm tắt lớn hơn được rút gọn riêng khi hai bản không ghép cặp được. Chưa chạy lại Claude 5.5 trên pipeline mới nên chưa có số đo.
- **Lỗi khi chưa có system prompt trung tính:** một lần chạy High dừng sau 159 s với `MALFORMED_FUNCTION_CALL`. Sau khi PR #40 thay prompt, mọi lần High đều hợp lệ.

## Thay đổi trong app (PR #40)

- Antigravity có lựa chọn mức suy nghĩ High (mặc định) hoặc Medium, lưu cùng model. Mỗi tác vụ ghim mức này; mỗi lần gọi OMP gửi `set_thinking_level` rồi xác nhận bằng `get_state` trước khi gửi prompt. [RPC `set_thinking_level`](https://github.com/can1357/oh-my-pi/blob/v18.5.0/packages/coding-agent/src/modes/rpc/rpc-types.ts#L75)
- Lỗi 404 `Requested entity was not found` được báo là model không dùng được với gói hiện tại, thay vì "không kết nối được".

Việc còn lại: thử lại Gemini 4 Argon khi nâng OMP; đo lại Claude 5.5 với ngân sách gộp thích ứng của #11; bỏ GPT-OSS-120b và Claude 4.6 khỏi đề xuất từ 2026-11-02.

## Giới hạn

- **Mẫu đo:** chỉ có một cuộc họp, một người chấm, và mỗi cấu hình chạy 1–3 lần. Kết quả dao động giữa các lần: ba lần Medium có độ phủ quyết định từ 0,36 đến 0,55.
- **Cuộc họp dài:** chưa đo cuộc họp 2–4 giờ. Với High, 24 chunk có thể chạm giới hạn 60 phút của tác vụ.
- **Hạn mức:** hạn mức thực tế của các gói chưa đo được.
