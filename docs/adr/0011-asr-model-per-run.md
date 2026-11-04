---
status: accepted
---

# Chọn model ASR theo từng lần xử lý bản ghi

Một model không thắng trên mọi đoạn của cùng một cuộc họp: đo trên bản ghi thật, PhoWhisper giữ câu tiếng Việt tự nhiên hơn, Qwen3-ASR xử lý English và câu trộn tốt hơn, Whisper large-v3 giữ thuật ngữ tốt nhưng lặp câu nhiều nhất. Vì vậy người vận hành chọn model cho từng lần xử lý, mỗi lần tạo một thế hệ transcript riêng để đối chiếu ([ADR 0006](0006-transcript-generations.md)), thay vì đổi cấu hình toàn hệ thống. Số đo nằm ở [khảo sát model](../model-candidates-v1.md#đo-trên-bản-ghi-thật-11-2026).

**Danh mục cố định, không phải đường dẫn tự do.** Client gửi `asr_model` là `auto` hoặc một ID trong `asr_models.py`; server mới biết đường dẫn, engine và compute type. `auto` giữ đúng hành vi cũ, là model worker chọn theo phần cứng lúc khởi động, không suy từ ngôn ngữ gợi ý. Dữ liệu chưa đủ để viết luật tự chọn, vì model thắng theo từng đoạn chứ không theo từng ngôn ngữ. Lựa chọn không ghi vào `worker.runtime.json`.

**Model nào cũng phải chuẩn bị trước.** Readiness được kiểm tra khi đọc cuộc họp và lại kiểm tra khi nhận tác vụ. Server từ chối model chưa chuẩn bị bằng mã `model_unavailable`, còn trang hiện lệnh `model prepare --asr-model <id>`. Lệnh này là bước duy nhất dùng mạng, chỉ tải trọng số và không đọc bản ghi; runner vẫn chạy offline với `local_files_only`, không bật `trust_remote_code`.

**Ba cách biến âm thanh thành đoạn.**

| Engine | Model | Đoạn lấy từ | Lý do |
| --- | --- | --- | --- |
| `whisper` | Whisper large-v3, small | decoder | Giữ nguyên cấu hình cũ; đổi cách giải mã là việc của #80 |
| `whisper_vi` | PhoWhisper-large | VAD, im lặng 1,5 s | Tự chạy thì đoạn dài ~30 s, gán một người nói cho cả đoạn. Đoạn VAD quá ngắn làm model tự chế câu nên gộp theo khoảng lặng dài hơn |
| `qwen` | Qwen3-ASR-1.7B | VAD, im lặng 0,5 s | Aligner của Qwen không có tiếng Việt nên mốc thời gian lấy từ VAD |

PhoWhisper bị khóa tiếng Việt vì đó là model tiếng Việt, không phải vì ngôn ngữ gợi ý; `language_hint` vẫn chỉ là metadata. Qwen tự nhận ngôn ngữ từng đoạn và nhận nhầm tiếng Trung, Mã Lai, Thái trên đoạn tiếng Việt ngắn, nên đoạn ngoài Vietnamese/English được giải mã lại theo ngôn ngữ của đoạn gần nhất đã nhận hợp lý. Đây là heuristic: sản phẩm chỉ cam kết English và tiếng Việt.

**Thêm Qwen vượt ba lựa chọn nêu ở #28.** Thử nghiệm cho thấy đây là model nhanh nhất, và khi đọc đối chiếu thì tốt nhất trên nội dung trộn, nên được giữ thêm. Đánh đổi: thêm `transformers` vào image và cần GPU. Logprob của nó không so được với Whisper, nên engine này không báo `avg_logprob`; **Nghe lại** chỉ còn bắt lỗi lặp qua `compression_ratio`, tính trên văn bản thô của decoder vì parser của Qwen đã gộp câu lặp.

**Thế hệ ghi runtime thực tế.** `transcript_generations` lưu `device` và `compute_type` lấy từ task mà worker đã chạy; bản cũ được điền lại từ task khi migrate. Bỏ một model khỏi danh mục không làm mất thế hệ đã có, vì thế hệ lưu giá trị chứ không tham chiếu danh mục.
