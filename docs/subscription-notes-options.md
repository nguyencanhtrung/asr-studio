# Tận dụng subscription để tạo notes tiếng Anh

> Trạng thái: Q12 đã chọn notes cloud qua **ChatGPT hoặc OMP + Google Antigravity**, tùy người dùng chọn khi chạy; hoãn Qwen local. [Spec v1](specification.md) và [ADR 0007](adr/0007-cloud-notes-providers.md) đã cập nhật phạm vi mạng. Đọc nguồn chính thức ngày 2026-10-15; chưa đăng nhập, kiểm tra quyền lợi thực tế hoặc gửi transcript ra ngoài. “Google Pro” được hiểu là Google AI Pro cho tài khoản cá nhân. Các đường khác dưới đây là nghiên cứu tham khảo, không tự thêm vào v1.

## Anthropic — Claude Pro

- **Thủ công:** xuất transcript thành TXT/JSON, tải vào Claude và yêu cầu notes tiếng Anh. Các định dạng này được hỗ trợ. [Upload files](https://support.claude.com/en/articles/8241126-upload-files-to-claude)
- **Tự động bằng công cụ chính thức:** Pro có Claude Code; đăng nhập bằng tài khoản Claude của chủ subscription. `claude -p` hỗ trợ stdin, đầu ra JSON và JSON Schema. [Pro + Claude Code](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan), [headless](https://code.claude.com/docs/en/headless)
- **Quota:** thông báo cập nhật 2026-10-07 xác nhận Agent SDK, `claude -p` và ứng dụng bên thứ ba vẫn có thể dùng hạn mức subscription. Thay đổi dự kiến tháng 6 đã bị hoãn. Quyền API credit mới trong thông báo dành cho Max/Team, không được suy sang Pro. [Thông báo chính thức](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- **Ranh giới tích hợp:** tài liệu hiện hành cho phép người dùng tự đăng nhập subscription qua binary Claude Code nguyên bản. Đồng thời, ứng dụng bên thứ ba không được thu thập/trung gian hóa thông tin đăng nhập hoặc session token Claude; hướng dẫn SDK yêu cầu API key cho sản phẩm của nhà phát triển. Không hiểu quyền chạy CLI cá nhân thành quyền tái sử dụng OAuth token trong HTTP client riêng. [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- **API trực tiếp:** Pro không bao gồm usage Claude Console API; phải cấu hình thanh toán riêng. [Pro plan](https://support.claude.com/en/articles/8325606-what-is-the-pro-plan)

Suy luận thiết kế: adapter gọi CLI chính thức là ứng viên cho công cụ cá nhân một người dùng. Chưa cam kết đây là entitlement cho mọi kiểu triển khai server. Cần xác minh đăng nhập, quota và cách cách ly cấu hình trước khi triển khai. `--bare` hiện bỏ qua subscription OAuth; không dùng cờ này rồi kỳ vọng tính vào Pro. [Headless](https://code.claude.com/docs/en/headless)

## OpenAI — ChatGPT Plus

- **Tích hợp dùng hạn mức gói:** tài liệu OpenAI ngày 2026-09-28 mô tả Sign in with ChatGPT cho tài khoản Plus/Pro đủ điều kiện. Ứng dụng có thể xin quyền dùng hạn mức gói cho AI requests; phạm vi ra mắt gồm dự án open-source, dự án cá nhân chạy local và một số ứng dụng private. Đây là luồng OAuth chính thức, không phải biến API key thông thường thành usage miễn phí. Chưa kiểm tra eligibility của tài khoản người dùng. [Cookbook chính thức](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)
- **Ứng dụng tự host:** tài liệu áp dụng cho open-source và locally hosted apps; mô tả host như laptop hoặc self-hosted VM với định danh được giữ qua restart. Vì repo định hướng non-commercial và chưa chọn license, không mặc nhiên gọi repo là open-source theo nghĩa cấp phép; phương án đang đánh giá là bản cài cá nhân chạy local. [Phạm vi và host](https://developers.openai.com/siwc/token-sharing-open-source)
- **Inference:** sau khi người dùng đăng nhập/cấp quyền, backend có thể gọi Responses API công khai bằng OAuth access token của luồng này. Danh sách model phải lấy theo tài khoản; không giả định mọi model hiện trên giao diện chat đều có quyền dùng qua tích hợp. [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- **Giới hạn preview:** request HTTP cần `store: false`, `stream: true`; text input được hỗ trợ nhưng audio/video, Files upload API và transcription API không được hỗ trợ ở luồng này. Với dự án, đây là ứng viên cho transcript → notes, giữ nhận dạng âm thanh ở local. Hỗ trợ structured output cụ thể cần kiểm chứng khi triển khai Q11. [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- **Đường CLI:** `codex exec` cũng hỗ trợ chạy không tương tác và dùng lại CLI authentication đã lưu. Tài liệu khuyên API key làm mặc định cho automation và có giới hạn riêng cho môi trường CI công khai; không đưa credential subscription vào CI/image công khai. [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

Q16 đã chọn hướng thử Sign in with ChatGPT trực tiếp cho bản cài cá nhân, gọi Responses API bằng quyền người dùng cấp; không thêm coding-agent runtime vào nhánh ChatGPT. Vẫn cần thử đăng nhập, model access, giới hạn gói và structured output trước khi xác nhận cấu hình hoạt động; việc chọn hướng tích hợp không chứng minh tài khoản đã đủ điều kiện.

## Google — giả định Google AI Pro

### Đường đã chọn: OMP + Antigravity

Người dùng đang sử dụng OMP với provider `google-antigravity` và muốn dùng Gemini để tạo notes. Bản OMP cài trên máy có giao thức RPC đăng nhập có thể nối tới UI; đây là bằng chứng hỗ trợ kỹ thuật, chưa kiểm chứng đăng nhập/inference trong ứng dụng Docker. `google-antigravity` khác adapter Gemini CLI và Gemini API key. [Khảo sát harness](harness-notes.md)

Điều khoản Antigravity mục 6 cấm dùng phần mềm bên thứ ba truy cập dịch vụ và nêu khả năng đình chỉ Antigravity/Gemini CLI. FAQ cũng nói bọc `agy` trong ứng dụng bên thứ ba không được hỗ trợ trên gói cá nhân; đổi từ OMP sang wrapper CLI chính thức không tự giải quyết hạn chế này. Người dùng đã được thông báo và vẫn chọn hỗ trợ cả OMP + Antigravity lẫn ChatGPT. [Điều khoản](https://www.antigravity.google/terms), [FAQ](https://www.antigravity.google/docs/faq/)

Ảnh catalog OMP không xác nhận từng model còn truy cập được, quota còn lại hoặc chất lượng notes. Model Gemini cụ thể cần thử sau; không coi mọi tên model trong ảnh là cam kết của ứng dụng.

### Các đường tham khảo khác — chưa đưa vào v1

- **Thủ công:** Gemini Apps nhận file; NotebookLM (nguồn hiện hành gọi Gemini Notebook) nhận TXT/Markdown hoặc văn bản dán và tạo tóm tắt dựa trên nguồn. AI Pro có hạn mức Notebook cao hơn. [Gemini upload](https://support.google.com/gemini/answer/14903178?hl=en), [Notebook sources](https://support.google.com/gemininotebook/answer/16215270?hl=en), [AI Pro benefits](https://support.google.com/googleone/answer/14534406?hl=en)
- **Tự động bằng Gemini CLI:** đăng nhập Google bằng chính tài khoản có AI Pro. Tài liệu xác nhận headless dùng lại thông tin xác thực đã lưu từ lần đăng nhập trước; nếu chưa đăng nhập thì cấu hình API key hoặc Vertex AI. CLI có `-p` và đầu ra JSON. [Authentication](https://geminicli.com/docs/get-started/authentication/), [headless](https://geminicli.com/docs/cli/headless/)
- **Quota CLI:** trang hiện hành công bố tối đa 1.500 model requests/người/ngày cho AI Pro; đây không phải số cuộc họp, và vẫn phụ thuộc giới hạn theo phút/model, nhu cầu dịch vụ. Đăng nhập Google và dùng API key là hai đường tính hạn mức khác nhau. [Quotas](https://geminicli.com/docs/resources/quota-and-pricing/)
- **API trực tiếp:** Gemini API có free tier cho một số model và paid tier qua billing riêng. AI Pro hiện kèm **10 USD/tháng GenAI & Cloud developer credit**: phải nhận quyền lợi và áp dụng vào billing account. Không hứa mọi API call miễn phí; cần kiểm tra credit/SKU, điều kiện tài khoản và khoản trả trước. [API billing](https://ai.google.dev/gemini-api/docs/billing), [Developer plans](https://developers.google.com/program/plans-and-pricing), [Redeem credits](https://developers.google.com/profile/help/benefits)

Các khả năng CLI/API trên không xác nhận quyền nhúng bất kỳ luồng subscription nào vào ứng dụng bên thứ ba. Nếu sau này chọn Gemini API với credit, cần quyết định phạm vi riêng; không tự thay đường OMP + Antigravity người dùng vừa chọn.

## Quyết định và việc còn lại

- Notes cloud đã được chốt; ASR/diarization và âm thanh local, Qwen tạo notes hoãn. Người dùng chọn ChatGPT hoặc OMP + Antigravity khi chạy và kết nối bên đó từ UI, không phải đăng nhập cả hai.
- Cả hai lựa chọn cần kiểm chứng chất lượng notes, ID nguồn, transcript dài, quota, timeout/hủy và đăng nhập trong Docker. Chưa có benchmark; không cam kết subscription đáp ứng vô hạn hoặc không có chi phí ngoài gói trong mọi cấu hình.
- Đề xuất ghi provider/model thực dùng và phiên bản nguồn vào metadata mỗi bản notes; không đưa thông tin vận hành này vào nội dung họp xuất ra nếu không cần.
- Q13–Q15 đã chốt lưu một tài khoản/phiên mỗi provider và có ngắt kết nối; cho chạy ASR rồi lưu transcript/kết thúc tác vụ khi provider chưa dùng được. Notes lỗi/quota giữ transcript và chờ người dùng chọn bên khác rồi chủ động tạo lại, không tự chuyển provider/tài khoản hoặc bật API billing. Xem [backend design](backend-design.md).
- Claude, upload/copy thủ công và Gemini API credit là nghiên cứu tham khảo. Không bổ sung các luồng này vào v1 qua quyết định hỗ trợ hai provider nói trên.
