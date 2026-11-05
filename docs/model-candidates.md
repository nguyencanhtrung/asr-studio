# Ứng viên model cho backend v1

> Trạng thái: Qwen3-ASR-1.7B là default ASR trên GPU theo #168; Whisper small là default trên CPU. Whisper large-v3 và PhoWhisper-large vẫn là lựa chọn explicit. Qwen làm notes local vẫn **hoãn theo Q12**; notes dùng ChatGPT hoặc OMP + Google Antigravity. Đọc nguồn ngày 2026-10-15. Phạm vi, phần cứng và tiêu chí nghiệm thu theo [spec v1](specification.md), đặc biệt AC-01–06, AC-10 và AC-13–14.

## Bộ model đã chốt để thử trước

Chọn **Qwen3-ASR-1.7B trên GPU**, **Whisper small trên CPU** và **pyannote Community-1 để phân biệt người nói** chạy local. Whisper large-v3 và PhoWhisper-large vẫn có trong catalog để chọn theo từng lần chạy. Tạo notes (English hoặc Tiếng Việt) qua **ChatGPT** hoặc **OMP + Google Antigravity**, ưu tiên khảo sát Gemini ở nhánh Antigravity; Q27 cho người dùng chọn model phù hợp từ catalog của kết nối. Qwen3-14B Q4_K_M từng được chọn thử nhưng không còn thuộc pipeline notes local.

| Vai trò | Model / tài nguyên | Runtime đề xuất và căn cứ |
| --- | --- | --- |
| Âm thanh → văn bản | `openai/whisper-large-v3` | `faster-whisper` dùng CTranslate2; hỗ trợ FP16, INT8 và timestamp theo đoạn. Model đa ngôn ngữ, có chế độ chép lời giữ ngôn ngữ nguồn. [Model card](https://huggingface.co/openai/whisper-large-v3), [runtime](https://github.com/SYSTRAN/faster-whisper) |
| Ai nói, lúc nào | `pyannote/speaker-diarization-community-1` | `pyannote.audio`; trả đoạn thời gian theo nhãn người nói, có đầu ra exclusive giúp ghép với transcript. Chạy local được sau tải model. [Model card](https://huggingface.co/pyannote/speaker-diarization-community-1) |
| Transcript → notes | Model được tài khoản ChatGPT hoặc Antigravity cho phép dùng; nhánh Antigravity ưu tiên khảo sát Gemini | Cloud qua adapter đã chọn; không nạp LLM local. Xem [subscription](subscription-notes-options.md), [harness](harness-notes.md) và ràng buộc provider tại [ADR 0007](adr/0007-cloud-notes-providers.md) |

`faster-whisper`, `llama.cpp` và `vLLM` là phần mềm chạy model. WhisperX là bộ ghép ASR, căn chỉnh thời gian và diarization; không phải một model thay thế Whisper. V1 chỉ cần timestamp theo đoạn, nên chưa cần buộc thêm model căn chỉnh từng từ. [WhisperX](https://github.com/m-bain/whisperX)

## Ứng viên đối chiếu

| Model | Lý do giữ trong danh sách | Điều phải kiểm chứng |
| --- | --- | --- |
| `vinai/PhoWhisper-large` | VinAI tinh chỉnh Whisper trên 844 giờ tiếng Việt với nhiều giọng vùng miền; bản large có 1,55 tỷ tham số. [Nguồn VinAI](https://github.com/VinAIResearch/PhoWhisper) | So với Whisper trên câu Việt xen thuật ngữ Anh và cuộc họp đổi ngôn ngữ. Benchmark tiếng Việt không chứng minh chất lượng tiếng Anh; chưa đề xuất tự chuyển sang PhoWhisper chỉ vì người dùng chọn Vietnamese. |
| `Qwen/Qwen3-ASR-1.7B` | Có hỗ trợ chính thức English và Vietnamese; runtime `qwen-asr` dùng Transformers hoặc vLLM. [Model card](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Chất lượng, tốc độ và cách giữ timestamp tiếng Việt. `Qwen3-ForcedAligner-0.6B` đi kèm chỉ liệt kê 11 ngôn ngữ, không có Vietnamese; không được suy ra ASR hỗ trợ tiếng Việt thì aligner cũng hỗ trợ. [Danh sách hỗ trợ](https://huggingface.co/Qwen/Qwen3-ASR-1.7B#released-models-description-and-download) |
| `Qwen/Qwen3.5-9B` — hoãn | Đã phát hành; nhận văn bản, có chế độ tắt thinking. Qwen xác nhận llama.cpp hỗ trợ dòng 3.5. [Model card](https://huggingface.co/Qwen/Qwen3.5-9B), [repo Qwen](https://github.com/QwenLM/Qwen3.8#local-use) | Chỉ giữ làm tham khảo nếu mở lại nhánh LLM local; không đánh giá trong v1 hiện tại. Chưa xác minh GGUF 9B do chính Qwen phát hành. |

Qwen3-14B từng được chọn làm mốc vì có artifact GGUF chính thức đã xác minh, không phải kết luận nó mới nhất hoặc tốt hơn Qwen3.5-9B. Chưa có phép đo nào trong repo chứng minh model tạo notes, local hoặc cloud, đáp ứng dữ liệu cuộc họp này. Việc hoãn Qwen tạo notes không tự loại ứng viên ASR cùng thương hiệu khỏi nghiên cứu âm thanh.

## Đo trên bản ghi thật (11-2026)

Phép đo phục vụ #28, chạy trên GPU 16 GB, trong network namespace rỗng để âm thanh không thể ra ngoài: hai cuộc họp đầy đủ (67 và 75 phút) và năm đoạn 5 đến 10 phút gồm tiếng Việt, English và chuyển ngôn ngữ giữa chừng. Bản ghi riêng tư nên không nằm trong repo.

**Giới hạn: không có bản chép tay chuẩn nên không có WER trên cuộc họp thật.** Các cột dưới là số đo gián tiếp; bản thân độ đúng chỉ được đọc đối chiếu trên vài đoạn.

| Model và cấu hình | RTF | VRAM đỉnh (MiB) | Đoạn trên 2 cuộc họp | % từ trong cụm lặp | Đoạn trùng liền kề |
| --- | --- | --- | --- | --- | --- |
| Whisper large-v3, cấu hình app (default GPU cũ) | 0,075 đến 0,076 | 5554 | 1717 | 4,2 và 5,7 | 117 và 57 |
| Whisper large-v3, `vad_filter` và không nối ngữ cảnh (thử nghiệm, app không bật `vad_filter`) | 0,025 đến 0,029 | 5362 | 1172 | 1,1 và 1,5 | 0 và 0 |
| PhoWhisper-large, VAD im lặng 1,5 s (`whisper_vi`) | 0,032 đến 0,036 | 5362 | 480 | 2,5 và 2,3 | 0 và 0 |
| Qwen3-ASR-1.7B (`qwen`), batch 8 | 0,008 đến 0,009 | 12456 | 1597 | 3,1 và 2,4 | 2 và 1 |
| Zipformer-30M-6000h, CPU | 0,006 | 0 | 1597 | không so | không so |

Đọc đối chiếu:

- **Qwen3-ASR**: tốt nhất trên English và câu trộn, có viết hoa và dấu câu; có đoạn nghe nhầm từ kỹ thuật. Không dùng được logprob làm độ tin cậy (trung vị khoảng −4,4 cả trên đoạn rõ).
- **PhoWhisper-large**: tiếng Việt tự nhiên; trên English thì dịch sang tiếng Việt hoặc phiên âm, và có tự chế câu trên đoạn ngắn hoặc nhiễu. Ngưỡng `_asr_uncertain` không bắt được các câu này. Tăng khoảng lặng VAD từ 0,5 lên 1,5 s giảm số đoạn dày chữ bất thường (hơn 30 ký tự mỗi giây) từ 38 xuống 9.
- **Zipformer**: chính xác trên từ và tên tiếng Việt và dẫn đầu N-WER trên [bảng xếp hạng tiếng Việt](https://vietaudio-team-vietnamese-asr-leaderboard.hf.space/), nhưng toàn chữ hoa, không dấu câu, English sai, và giấy phép CC-BY-NC-ND. Chưa đưa vào danh mục.
- **Parakeet CTC Vietnamese**: vỡ âm tiết, English sai, dependency NeMo nặng. Loại.
- **Whisper large-v3**: `vad_filter=True` với `condition_on_previous_text=False` giảm lặp mạnh và nhanh gấp khoảng 2,6 lần, nhưng có đoạn mất tiếng Việt ngay sau khi đổi ngôn ngữ. App không dùng cấu hình này: large-v3 chọn được chỉ tắt nối ngữ cảnh, không bật `vad_filter`. Default GPU đã chuyển sang Qwen theo #168.

Giải thích cột: RTF là thời gian xử lý chia độ dài âm thanh, nhỏ hơn là nhanh hơn. Cột có hai giá trị theo thứ tự cuộc họp 67 và 75 phút. Số đoạn khác nhau vì cách chia đoạn khác nhau. App chạy Qwen với batch 4: VRAM đỉnh 8312 MiB trên cuộc họp 75 phút, RTF khoảng 0,016 (72 giây phần ASR); phép đo product/private khác ghi nhận 8336 MiB. Qwen không có confidence tương thích Whisper.

Việc chọn model theo từng lần chạy: [ADR 0011](adr/0011-asr-model-per-run.md).

## Bộ nhớ và cách chạy

Pipeline hiện tại chạy lần lượt ASR và diarization local, giải phóng GPU giữa các model; sau đó gọi provider cloud cho notes. Cách dùng tiến trình con và thu hồi tài nguyên còn cần kiểm chứng. Huấn luyện nhận diện danh tính là phase sau, không bổ sung model training vào v1.

**Nghiên cứu LLM local bên dưới được giữ để tham khảo, đã hoãn theo Q12.**

File Qwen3-14B Q4_K_M chính thức khoảng **9 GB trên đĩa**. Đây không phải tổng VRAM: runtime còn cần bộ nhớ ngữ cảnh, buffer tính toán và tài nguyên CUDA. [Danh sách file](https://huggingface.co/Qwen/Qwen3-14B-GGUF/tree/main)

Với model cỡ 32 tỷ tham số, ước tính riêng trọng số ở đúng 4 bit là `32 × 10^9 × 4 / 8 = 16 GB`, khoảng **14,9 GiB**, chưa gồm overhead lượng tử hóa và bộ nhớ chạy. Vì vậy Qwen2.5-32B-Instruct trong nghiên cứu ban đầu không nên là cấu hình mặc định trên GPU 16 GB. CPU offload có thể là phương án thử sau; chưa biết có đạt thời gian mục tiêu. [Model được tham chiếu](https://huggingface.co/Qwen/Qwen2.5-32B-Instruct)

Q7 ban đầu dự kiến chạy lần lượt ASR, diarization và LLM local. Q12 bỏ giai đoạn LLM local khỏi cấu hình cần thử, không yêu cầu tải Qwen/llama.cpp để khởi động v1.

Đo trên RTX 5080 thực tế với revision cố định, batch size và giới hạn context cụ thể. Benchmark faster-whisper công bố dùng GPU và dữ liệu khác; không suy ra toàn bộ cuộc họp hai giờ sẽ hoàn thành trong 30 phút. [Điều kiện benchmark upstream](https://github.com/SYSTRAN/faster-whisper#benchmark)

## Transcript dài và bằng chứng

Đề xuất chép lời bằng tác vụ `transcribe`; không dùng `translate` để biến transcript thành tiếng Anh. Whisper hỗ trợ hai chế độ này riêng biệt. Khả năng giữ code-switch phải đo trên dữ liệu của nhóm. [Whisper](https://huggingface.co/openai/whisper-large-v3)

Đầu vào LLM gồm văn bản, ID đoạn, mốc thời gian, nhãn/tên người nói và ngày họp đã nhập. Khi tạo notes, nguồn của cuộc họp bị khóa chỉnh sửa theo quyết định Q3 đã chốt.

Q17 đã chốt xử lý theo ngân sách model/provider: nguồn vừa ngân sách thì xử lý một lượt; nguồn dài thì trích xuất theo đoạn phát biểu có phần chồng, giữ ID bằng chứng, rồi hợp nhất bằng cùng provider/model. Mọi bước, gồm hợp nhất, phải nằm trong ngân sách. Không cắt phần cuối hoặc để harness tự rút gọn mất nguồn. Q18 cho tối đa một lượt yêu cầu sửa lỗi cấu trúc/tham chiếu trong cả tác vụ, không phải mỗi chunk; quá giới hạn thì báo lỗi và không công bố notes không hợp lệ. Phép kiểm tra ID không tự bảo đảm nội dung đúng. Context, giới hạn đầu ra và số lượt gọi thực tế cần đo riêng cho mỗi provider.

## Thiết lập ASR offline và bước xác minh

Tải trước trọng số, tokenizer, cấu hình và các tài nguyên phụ của ASR/diarization; runtime nạp bằng đường dẫn local. faster-whisper có thể tự tải tokenizer nếu thiếu file khi chuyển đổi, nên bước setup phải kiểm tra đủ artifact. Tạo notes cloud cần Internet và kết nối tài khoản provider, không thuộc cam kết offline. [Hướng dẫn chuyển đổi](https://github.com/SYSTRAN/faster-whisper#model-conversion)

Community-1 yêu cầu người dùng chấp nhận điều kiện truy cập và tạo token Hugging Face khi thiết lập; model card hướng dẫn chạy offline. Đặt `PYANNOTE_METRICS_ENABLED=0` để tắt telemetry, rồi nghiệm thu cả khi mất mạng và khi có mạng theo AC-10. [Điều kiện và offline](https://huggingface.co/pyannote/speaker-diarization-community-1), [cấu hình telemetry](https://github.com/pyannote/pyannote-audio#configuring-telemetry)

Bước tiếp theo là so sánh trên ba loại bản ghi của spec, đo riêng lỗi nội dung, người nói, timestamp, notes, thời gian từng giai đoạn và VRAM đỉnh. Phải có bản ghi hai giờ để kiểm chứng mục tiêu 30 phút. Tài liệu này chưa tải/cài model, chấp nhận điều khoản truy cập hoặc thực hiện benchmark.
