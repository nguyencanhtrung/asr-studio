---
status: accepted
---

# Chọn model ASR theo từng lần xử lý bản ghi

Một model không thắng trên mọi đoạn của cùng một cuộc họp: đo trên bản ghi thật, PhoWhisper giữ câu tiếng Việt tự nhiên hơn, Qwen3-ASR xử lý English và câu trộn tốt hơn, Whisper large-v3 giữ thuật ngữ tốt nhưng lặp câu nhiều nhất. Vì vậy người vận hành chọn model cho từng lần xử lý, mỗi lần tạo một thế hệ transcript riêng để đối chiếu ([ADR 0006](0006-transcript-generations.md)), thay vì đổi cấu hình toàn hệ thống. Số đo nằm ở [khảo sát model](../model-candidates-v1.md#đo-trên-bản-ghi-thật-11-2026).

## Mặc định theo phần cứng cho #168

Theo AC-04 của [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), `auto` dùng Qwen3-ASR-1.7B trên GPU và Whisper small trên CPU. Đây là mặc định phần cứng, không loại Whisper large-v3 khỏi danh mục: người vận hành vẫn có thể chọn Whisper large-v3 cho từng lần chạy hoặc giữ các task cũ có đường dẫn model đó.

### Nguyên nhân đo được của quyết định #168

AC-01 của issue #168: hai dạng lỗi của large-v3 được đo riêng và phản ứng khác nhau với cùng thay đổi. Với câu ngắn, large-v3 bịa dòng ở 8/27, 12/27 và 6/27 mẫu tương ứng với input as-is, thêm 0,5 giây cuối và thêm 0,5 giây đầu; `no_context` cho đúng các con số đó. Lỗi nhạy với biên input và không được sửa bởi bất kỳ cấu hình decoder hoặc segmentation nào trong 12 cấu hình đã thử. Với clip dài, WER của base là 0,789/0,896/0,970 và của `no_context` là 0,246/0,275/0,684 theo forward 0,5 giây, reverse 0,5 giây và forward 2 giây: conditioning là một yếu tố góp phần đo được, nhưng chưa phải toàn bộ. Cơ chế sâu hơn vẫn chưa biết.

| Engine | Dòng bịa, câu ngắn as-is/tail/head | Dòng bịa, clip dài forward/reverse/2 s | Dòng bịa, VIVOS/mixed/English | WER forward 0,5 s | VIVOS | mixed | English |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| large-v3 base | 8/12/6 | 0/7/9 | 0/0/0 | 0,7890 | 0,3572 | 0,6683 | 0,1988 |
| large-v3 `no_context` | 8/12/6 | 0/1/5 | 0/0/0 | 0,2457 | 0,1342 | 0,2670 | 0,0357 |
| Qwen product | 0/0/0 | 0/0/0 | 0/0/0 | 0,0592 | 0,0498 | 0,1456 | 0,0405 |
| PhoWhisper product | 0/0/0 | 0/0/0 | 0/0/0 | 0,0564 | 0,0281 | 0,3641 | 0,1071 |

PhoWhisper thắng ở forward 0,5 giây và VIVOS; `no_context` thắng ở English. Trên ba meeting, số đoạn trùng liền kề và phần trăm cụm lặp lần đầu lần lượt là: A — base 159/3,0251%, `no_context` 0/0%, Qwen 1/0,0188%; B — base 76/2,2880%, `no_context` 8/0,5830%, Qwen 2/0,0389%; C — base 67/2,7735%, `no_context` 0/0%, Qwen 0/0%. Phần trăm cụm lặp ở đây dùng một định nghĩa cho cả ba cấu hình, trong cùng phép đo. Khảo sát model không ghi định nghĩa của hai số 4,2 và 5,7, nên không so sánh được với hai số đó. Đoạn 10 phút có số output khác nhau giữa các lần chạy là base 3, `no_context` 1, Qwen 1, PhoWhisper 3.

Số large-v3 trên đây dùng PCM WAV, còn product đưa audio qua AAC normalization; trên cùng clip, điều đó đổi reverse từ 0,8960 thành 0,1185 và mixed từ 0,6683 thành 0,2945. Vì vậy số v0.2.1 đã công bố chưa được tái lập đầy đủ. Phép so sánh ban đầu chưa có Whisper small; phần đo riêng nằm ở cuối mục này.

Qwen là lựa chọn GPU tổng quát theo cân bằng này, không phải vì một decoder-only fix: với app batch 4, đỉnh VRAM là 8336 MiB. Qwen không cung cấp confidence tương thích Whisper, nên worker không tạo `asr_uncertain` cho Qwen; việc duyệt vẫn dựa trên đoạn lặp và vấn đề người nói.

| | Trước | Sau |
| --- | --- | --- |
| GPU hardware default | Whisper large-v3 | Qwen3-ASR-1.7B, Transformers, bfloat16 |
| Đỉnh VRAM product đã đo | 5554 MiB với cấu hình Whisper app | 8336 MiB, batch 4 |
| Tín hiệu `asr_uncertain` | Các ngưỡng Whisper | Không kích hoạt từ Qwen; không có confidence tương đương |

Nguồn: [issue #168](https://github.com/nguyencanhtrung/asr-studio/issues/168), [số đo ứng viên model](../model-candidates-v1.md#đo-trên-bản-ghi-thật-11-2026). Đánh đổi là GPU cần model Qwen khoảng 4,1 GB và đỉnh VRAM 8336 MiB; Whisper large-v3 vẫn giữ làm lựa chọn explicit để bảo toàn đối chiếu và tương thích task cũ.

**Đo lại qua đường âm thanh của sản phẩm (AAC 160 kbit/s).** Một lần cho Qwen, hai lần cho large-v3 `no_context`, kết quả hai lần giống nhau:

| Dạng | Qwen | large-v3 `no_context` |
| --- | ---: | ---: |
| 27 đoạn lẻ: câu tự chế, WER | 0/27, 0,0462 | 10/27, 0,3049 |
| Clip ghép 0,5 s | 0,0592 | 0,2442 |
| Clip đảo 0,5 s | 0,0636 | 0,1069 |
| Clip ghép 2 s | 0,0751 | 0,7038 |
| VIVOS | 0,0519 | 0,1342 |
| Mixed | 0,1521 | 0,2654 |
| English | 0,0429 | 0,0381 |

**Không mất lời (AC-03 của #168).** Số từ mỗi phút trên phần có tiếng nói của ba cuộc họp: Qwen 189,6 / 221,7 / 187,2; large-v3 cấu hình cũ 187,3 / 201,5 / 187,8; `no_context` 173,1 / 181,1 / 171,2. Qwen cao hơn ở một cuộc họp và ngang ở hai cuộc còn lại (chênh dưới 1,3%). `no_context` thấp hơn khoảng 8 đến 10%, một phần vì bớt được đoạn lặp, nhưng phép đo này không tách được phần lời bị mất. Không có bản chép tay chuẩn nên đây là số gián tiếp.

**Whisper small trên CPU, `int8`, đo một lần qua AAC, có so với không có nối ngữ cảnh:** clip ghép 0,5 s 0,5809 và 0,2587; clip đảo 0,2688 và 0,2009; clip ghép 2 s 0,1936 và 0,3121; VIVOS 0,1970 và 0,2100; mixed 0,2443 và 0,2735; English 0,0714 và 0,0738; gộp 0,2780 và 0,2324. Tắt nối ngữ cảnh tốt hơn khi gộp và rõ nhất ở clip 0,5 s, nhưng xấu hơn ở clip 2 s và mixed. Quyết định giữ một cấu hình cho engine `whisper`, chấp nhận đánh đổi này.

Một bản cài cũ giữ large-v3 nếu file `.env` hiện tại vẫn có `ASR_GPU_MODEL_PATH=/models/whisper-large-v3`; `.env.example` không tự ghi đè file đó. Muốn chuyển sang Qwen, sửa dòng này thành `/models/qwen3-asr-1.7b` rồi chuẩn bị model, hoặc chọn Qwen làm model mặc định trong Settings.

**Danh mục cố định, không phải đường dẫn tự do.** Client gửi `asr_model` là `auto` hoặc một ID trong `asr_models.py`; server mới biết đường dẫn, engine và compute type. `auto` giữ đúng hành vi cũ, là model worker chọn theo phần cứng lúc khởi động, không suy từ ngôn ngữ gợi ý. Dữ liệu chưa đủ để viết luật tự chọn, vì model thắng theo từng đoạn chứ không theo từng ngôn ngữ. Lựa chọn không ghi vào `worker.runtime.json`.

**Model nào cũng phải chuẩn bị trước.** Readiness được kiểm tra khi đọc cuộc họp và lại kiểm tra khi nhận tác vụ. Server từ chối model chưa chuẩn bị bằng mã `model_unavailable`, còn trang hiện lệnh `model prepare --asr-model <id>`. Lệnh này chỉ tải trọng số và không đọc bản ghi; [ADR 0012](0012-model-installer-network-boundary.md) thêm cách cài từ Cài đặt bằng một service riêng có mạng, còn `app` và `worker` vẫn offline; runner vẫn chạy offline với `local_files_only`, không bật `trust_remote_code`.

**Ba cách biến âm thanh thành đoạn.**

| Engine | Model | Đoạn lấy từ | Lý do |
| --- | --- | --- | --- |
| `whisper` | Whisper large-v3, small | decoder | Giữ nguyên cấu hình cũ; đổi cách giải mã là việc của #80 |
| `whisper_vi` | PhoWhisper-large | VAD, im lặng 1,5 s | Tự chạy thì đoạn dài ~30 s, gán một người nói cho cả đoạn. Đoạn VAD quá ngắn làm model tự chế câu nên gộp theo khoảng lặng dài hơn |
| `qwen` | Qwen3-ASR-1.7B | VAD, im lặng 0,5 s | Aligner của Qwen không có tiếng Việt nên mốc thời gian lấy từ VAD |

PhoWhisper bị khóa tiếng Việt vì đó là model tiếng Việt, không phải vì ngôn ngữ gợi ý; `language_hint` vẫn chỉ là metadata. Qwen tự nhận ngôn ngữ từng đoạn và nhận nhầm tiếng Trung, Mã Lai, Thái trên đoạn tiếng Việt ngắn, nên đoạn ngoài Vietnamese/English được giải mã lại theo ngôn ngữ của đoạn gần nhất đã nhận hợp lý. Đây là heuristic: sản phẩm chỉ cam kết English và tiếng Việt.

**Thêm Qwen vượt ba lựa chọn nêu ở #28.** Thử nghiệm cho thấy đây là model nhanh nhất, và khi đọc đối chiếu thì tốt nhất trên nội dung trộn, nên được giữ thêm. Đánh đổi: thêm `transformers` vào image và cần GPU. Logprob của nó không so được với Whisper, nên engine này không báo `avg_logprob`; **Nghe lại** chỉ còn bắt lỗi lặp qua `compression_ratio`, tính trên văn bản thô của decoder vì parser của Qwen đã gộp câu lặp.

**Thế hệ ghi runtime thực tế.** `transcript_generations` lưu `device` và `compute_type` lấy từ task mà worker đã chạy; bản cũ được điền lại từ task khi migrate. Bỏ một model khỏi danh mục không làm mất thế hệ đã có, vì thế hệ lưu giá trị chứ không tham chiếu danh mục.
