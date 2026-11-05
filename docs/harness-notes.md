# Harness cho bước transcript → notes

> Khảo sát ngày 2026-10-15, phục vụ quyết định Q12. Chỉ đọc binary/package, tài liệu và source công khai; không chạy đăng nhập, đọc credential hay gửi transcript/model request. Các phương án tích hợp dưới đây chưa phải cấu hình đã được kiểm thử.

Q12 sau khảo sát đã chọn hỗ trợ **ChatGPT và OMP + Google Antigravity**, người dùng chọn khi chạy và kết nối bên đó từ UI; không cần cả hai tài khoản. Người dùng giữ lựa chọn Antigravity sau khi được thông báo hạn chế điều khoản bên dưới. Xem [ADR 0007](adr/0007-cloud-notes-providers.md).

## Kết quả chính

**OMP có sẵn giao thức để backend nối luồng đăng nhập vào UI, không cần điều khiển TUI.** Tuy nhiên, việc OMP có provider `google-antigravity` không xác nhận Google cho phép dùng đường đó: điều khoản Antigravity mục 6 cấm phần mềm bên thứ ba truy cập dịch vụ và nêu khả năng đình chỉ Antigravity/Gemini CLI. Vì vậy phải tách lựa chọn cloud notes của người dùng khỏi lựa chọn đường truy cập được nhà cung cấp hỗ trợ. [Điều khoản Google](https://www.antigravity.google/terms)

Ảnh người dùng cung cấp cho thấy catalog OMP có nhiều model dưới `google-antigravity`; ảnh không kiểm chứng quyền gọi từng model, quota còn lại hoặc thời hạn hỗ trợ.

## Bản cài đã quan sát

| Thành phần | Bằng chứng local | Kết quả |
|---|---|---|
| OMP | `$(command -v omp)`, ELF chứa package metadata `@oh-my-pi/pi-coding-agent` và source bundle | Phiên bản `18.5.0`; upstream `can1357/oh-my-pi` |
| Pi | package.json (`@earendil-works/pi-coding-agent/package.json`, bản cài 0.84.4) | `@earendil-works/pi-coding-agent` `0.84.4`; binary `pi` trỏ tới `dist/bundle/cli.js` |

Đây là phiên bản cài trên máy, không phải kết luận về phiên bản mới nhất. OMP được đọc tĩnh; không chạy startup vì có thể tự khám phá cấu hình cá nhân.

## OMP: giao thức và đăng nhập

- `omp -p` chạy một yêu cầu; `--mode json` trả luồng event; `--mode rpc` nhận/trả NDJSON qua stdin/stdout. RPC phù hợp backend Python vì không cần nhúng runtime Bun. SDK OMP hiện yêu cầu Bun, dù README có đoạn gọi là Node SDK; khi triển khai phải pin version và theo tài liệu SDK chi tiết. [Run modes](https://github.com/can1357/oh-my-pi#four-entry-points-interactive-one-shot-rpc-and-acp), [SDK](https://omp.sh/docs/sdk)
- RPC có `get_login_providers` và `login` với `providerId`. Khi đăng nhập, harness phát `extension_ui_request` với `method: open_url`; có thể yêu cầu dán code/redirect URL bằng `input`. Host trả `extension_ui_response` cùng ID. Source `v18.5.0` khớp nhánh đã đọc trong binary. [Source đăng nhập](https://github.com/can1357/oh-my-pi/blob/v18.5.0/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L2147)
- RPC từ chối prompt bí mật và prompt xuất hiện trước URL đăng nhập. Vì vậy không phải mọi provider trong catalog đều có thể dùng cùng một UI flow. RPC login hiện không truyền `AbortSignal` vào OAuth; chưa thể khẳng định `abort` hủy được đăng nhập. Cần kiểm chứng deadline/hủy bằng tiến trình đăng nhập riêng. [Source giới hạn](https://github.com/can1357/oh-my-pi/blob/v18.5.0/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L2182)
- Bundle có các provider OAuth `google-antigravity`, `google-gemini-cli`, `openai-codex`; đây là các adapter khác nhau. Chưa kiểm tra credential hoặc quyền truy cập tài khoản nào.
- Chọn model cụ thể qua `set_model {provider, modelId}`; lấy catalog qua `get_available_models`. Phải lưu cả provider và model thực dùng. Catalog không phải phép thử inference thành công. [RPC reference](https://omp.sh/docs/rpc)

## Pi: có thể dùng nhưng không thay thế OMP nguyên trạng

- Pi cài trên máy có print/JSON/RPC, `--provider`, `--model`, `--no-session`, `--no-tools`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, `--no-context-files`. [CLI local](https://github.com/earendil-works/pi/tree/main/packages/coding-agent), flags đã cài (`@earendil-works/pi-coding-agent/README.md`, bản cài 0.84.4)
- Tài liệu provider của bản `0.84.4` liệt kê ChatGPT/Codex OAuth nhưng không liệt kê Antigravity/Gemini CLI OAuth. Source provider Google hiện dùng `GEMINI_API_KEY`; các mục Antigravity trong changelog cũ không đủ để suy ra hỗ trợ hiện tại. Providers local (`@earendil-works/pi-coding-agent/docs/providers.md`, bản cài 0.84.4), Google source local (`@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/providers/google.js`, bản cài 0.84.4)
- SDK Pi có `ModelRuntime.login(providerId, type, interaction)`; `AuthInteraction` cung cấp `prompt`, `notify`, `signal`, các event URL/device code và prompt manual code. Có thể xây cầu nối UI qua SDK; tài liệu RPC đã cài không mô tả lệnh login tương ứng OMP. SDK local (`@earendil-works/pi-coding-agent/docs/sdk.md`, bản cài 0.84.4), Auth interaction local (`@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/auth/types.d.ts`, bản cài 0.84.4)

## Ràng buộc cho adapter notes — gồm đề xuất và quyết định đã chốt

1. Backend giữ một interface notes chung: chọn provider/model, gửi snapshot transcript, nhận kết quả, hủy và chuẩn hóa lỗi. Worker tiếp tục quản lý tác vụ và kiểm tra cấu trúc notes/ID đoạn trước khi lưu.
2. Với lựa chọn OMP + Antigravity đã chốt về phạm vi sản phẩm, RPC có sẵn là đường kỹ thuật ít thay đổi hơn việc viết adapter Pi mới; hạn chế điều khoản vẫn tồn tại. Đề xuất luồng auth và luồng tạo notes dùng tiến trình riêng; không tái sử dụng phiên coding cá nhân.
3. Cấu hình runner phải tắt tools, extensions, skills, rules, MCP và discovery không cần thiết; dùng thư mục làm việc và credential store riêng của ứng dụng. OMP bundle nhận `--no-tools`, `--no-extensions`, `--no-skills`, `--no-rules`, `--no-session`; SDK có tùy chọn tắt MCP/LSP và settings cô lập. Cờ `--no-session` chỉ xác nhận không lưu lịch sử session, chưa bảo đảm không có diagnostic log/cache. Cần xác minh tất cả đường ghi trước khi chạy dữ liệu họp. [SDK configuration](https://omp.sh/docs/sdk)
4. Đầu ra JSON của CLI là **envelope sự kiện**, không tự bảo đảm nội dung notes đúng JSON Schema. Backend vẫn cần trích nội dung assistant và kiểm tra theo Q11.
5. Với RPC, acknowledgment `success: true` của prompt chỉ là nhận yêu cầu. Theo dõi kết thúc lượt, `stopReason`, lỗi model và exit code; khi hủy phải chờ runner dừng trước khi mở khóa nguồn. Không kết luận thành công chỉ từ tiến trình thoát mã 0. [RPC lifecycle](https://omp.sh/docs/rpc)
6. **Đã chốt Q13–Q15/Q22/Q25/Q27:** lưu một tài khoản/phiên mỗi provider; cho ASR/diarization hoàn tất rồi kết thúc tác vụ khi provider chưa sẵn sàng. Chọn/nhớ provider + model nhưng giữ nguyên trong tác vụ; lỗi/quota không tự đổi bên hoặc bật API billing. Không tự gửi lại inference khi lỗi mạng, cần kiểm chứng khả năng tắt resend của SDK/harness. Chặn ngắt/đổi tài khoản đang được tác vụ dùng; hủy và chờ dừng trước. Các giá trị timeout cần được đo khi triển khai.

## Chưa kiểm chứng

- Luồng callback khi browser ở máy khác hoặc runner nằm trong Docker; quyền bind port, manual callback và hủy đăng nhập.
- Model Gemini cụ thể phù hợp transcript tiếng Việt/Anh, context dài, quota và chất lượng notes có nguồn.
- Cấu hình cô lập thật sự, không auto-compaction làm mất nguồn, không gọi model phụ/fallback ngoài lựa chọn.
- Khả năng chạy end-to-end của tài khoản người dùng. Chưa có benchmark hoặc cam kết miễn phí.

Q16 đã chọn thử direct Sign in with ChatGPT cho nhánh ChatGPT. Với Antigravity, không coi wrapper `agy` là phương án tránh hạn chế: FAQ hiện nói bọc CLI trong ứng dụng bên thứ ba không được hỗ trợ trên gói cá nhân. [FAQ chính thức](https://www.antigravity.google/docs/faq/). Các quyết định được ghi trong [thiết kế backend](backend-design.md).

## Làm rõ adapter ChatGPT sau Q15

- Direct Sign in with ChatGPT dành cho ứng dụng local hỗ trợ đăng ký động, không cần chờ cấp client ID trước như luồng commercial partner. Cần được cấp quyền sử dụng hạn mức gói, không chỉ đăng nhập danh tính. Callback của luồng này dùng HTTP `127.0.0.1`; v1 chỉ dùng trình duyệt trên cùng máy nên có thể thiết kế publish callback port của Docker về loopback. Đây là hướng khả thi theo tài liệu, chưa kiểm thử callback thực tế. [Registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- Codex chính thức có `app-server` với các API bắt đầu/hủy login, logout, tài khoản và quota; có thể nối với UI mà không parse TUI hoặc output đăng nhập CLI. Device auth còn điều kiện bật trong cài đặt và trạng thái beta; không coi nó mặc nhiên khả dụng cho mọi tài khoản. [App-server authentication](https://learn.chatgpt.com/docs/app-server#authentication-endpoints), [headless authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
- OMP `openai-codex` có thể giúp dùng chung harness, nhưng không đồng nghĩa direct SIWC đăng ký ứng dụng riêng. Mỗi đường vẫn phải kiểm chứng refresh, restart, disconnect, model access và đầu ra notes. V1 không cần giải quyết đăng nhập từ browser trên máy khác vì LAN nằm ngoài phạm vi.

Q16 đã chốt direct SIWC cho nhánh ChatGPT trên bản cài cá nhân localhost, giữ OMP RPC cho Antigravity. Direct yêu cầu ứng dụng quản lý OAuth/refresh; đổi lại không phải thêm runtime coding agent cho nhánh ChatGPT. Đây là hướng triển khai thử, chưa xác nhận tài khoản hoặc cấu hình phát hành hoạt động.
