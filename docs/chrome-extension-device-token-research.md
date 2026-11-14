# Nghiên cứu token thiết bị cho Chrome extension nội bộ

Ngày: 2026-11-14 · [Thiết kế token từng thiết bị](https://github.com/nguyencanhtrung/asr-studio/issues/202) · Đề xuất contract MVP, chưa triển khai API

## Quyết định ngắn gọn

Chọn **pairing một lần do người vận hành phê duyệt**, sau đó cấp một bearer token riêng cho từng thiết bị/cài đặt và đổi token đó lấy session ngắn hạn. Không cho extension nhập hoặc tái sử dụng token UI hiện có. Pairing code có thời hạn 10 phút, chỉ dùng một lần; device token có hạn tuyệt đối 30 ngày, scope hẹp, được lưu phía server dưới dạng HMAC-SHA-256 với khóa bí mật riêng (pepper) và có thể thu hồi riêng; session token có hạn 15 phút và chỉ nằm trong bộ nhớ extension. MVP chỉ dùng HTTP resumable upload. Nếu tương lai cần WebSocket, xác thực phải là application message đầu tiên bằng session ticket, không phải `Authorization` giả định hay token trong URL.

Thời hạn pairing 10 phút, device token 30 ngày và session 15 phút là tham số thiết kế đề xuất, không phải số đo: đủ reconnect sau restart nhưng giới hạn lộ bearer; cần cấu hình và test khi hiện thực. Không xây multi-tenancy, nhiều server đồng thời, live engine hay credential production.

## R1 — Ranh giới hiện có và API riêng

Backend hiện có một ranh giới cho UI. `src/asr_studio/config.py:10-90` mặc định `http://127.0.0.1:8765`, chỉ cho HTTP với `LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}`, và chỉ nhận public URL không có base path. `src/asr_studio/app.py:45-89` chuẩn hóa Host/origin thành scheme-host-port; middleware `:170-218` từ chối Host/origin sai và cross-site unsafe request. `/api/access/session` (`src/asr_studio/routes.py:1192-1225`) đổi token thành cookie HttpOnly, SameSite=Strict; `src/asr_studio/auth.py:127-183` giữ token và xoay generation. Tests xác nhận credential sai không echo/log, Origin/Host sai bị từ chối, thiếu cookie 401 và mất auth state 503 (`tests/test_authentication.py:55-243`, `tests/test_auth_state.py:16-55`). Cookie không tự hết hạn theo ADR 0002.

Extension không nên đi qua `/api/access/session`: token UI là quyền operator dùng chung, không có định danh/revoke riêng từng thiết bị, và cookie UI không phải contract cho extension origin. Đề xuất namespace mới `/api/extension/v1/`, với middleware riêng. Dispatch chính xác namespace này trước guard cookie/same-origin của UI, giữ nguyên guard UI. API có allowlist authority/base path cấu hình rõ ràng cho local hoặc remote; không suy danh tính từ Host/Forwarded tùy ý. Việc khai báo alias không tự di chuyển token hay phiên:

* `GET /api/extension/v1/capabilities` là discovery không chứa secret. Trả `protocol`, `api_version`, `backend_id` ổn định do backend sinh, normalized base path/origin, `server_fingerprint` hiển thị được, và capability như `pairing`, `resumable_upload`, `websocket: false`.
* `POST /api/extension/v1/pair` nhận pairing code trong JSON body, `device_id`, extension ID và API version; chỉ thành công khi Origin đúng extension ID đã cấu hình và pairing intent còn hạn.
* `POST /api/extension/v1/session` nhận device token qua `Authorization: Bearer` và trả session token; các endpoint upload/status của extension chỉ nhận session token.

`backend_id + normalized URL (gồm base path) + api_version` là backend identity của client. Một backend vẫn có thư viện dùng chung cho nhiều thiết bị; device record dùng để thu hồi và giới hạn tài nguyên, không tạo tenant mới. Capability check phải chạy trước pair để báo sai API/server rõ ràng.

## R2 — Pairing, credential và vòng đời

So với copy một token per-device dài hạn, mã pairing một lần giảm việc người dùng phải xử lý credential dài hạn và tự hết hiệu lực nhanh. Cả hai cách đều có thể cấp scope/revoke riêng; chọn mã pairing cho MVP, giữ cấp token per-device thủ công làm phương án dự phòng sau. Tuyệt đối không tái sử dụng token UI dùng chung hoặc nhúng secret vào bundle.

Contract đề xuất:

1. Extension sinh `device_id` random 128-bit khi cài lần đầu, giữ trong `chrome.storage.local`; đây không phải secret. Người vận hành nhập URL, client normalize, xin host permission và gọi capabilities không redirect.
2. Người vận hành đang đăng nhập UI/CLI của đúng backend bấm “Add device”. Backend tạo `pairing_intent_id`, random code tối thiểu 128-bit, `expires_at = now + 10 min`, attempt limit và trạng thái `unused`. Code chỉ hiển thị trong UI/clipboard có chủ ý, không nằm trong URL, log, QR link hay content script. Server lưu HMAC(code) chứ không lưu code thô.
3. Extension gửi code, device ID, public extension ID và client nonce tới `/pair`. Backend khóa/consume intent nguyên tử; lần thứ hai trả `409 pairing_replayed`, không cấp lại token. Token vừa cấp ở trạng thái pending, tự vô hiệu nếu không được dùng trước khi intent hết hạn. Lần `/session` đầu thành công kích hoạt device record. Nếu response bị mất sau consume, UI cho revoke device pending rồi pair bằng intent mới; không biến retry thành kênh tiết lộ token.
4. Backend sinh device token 32-byte random, chỉ trả một lần. Lưu `device_id`, `backend_id`, normalized endpoint, `token_hash = HMAC-SHA-256(device_pepper, token)`, scope, `issued_at`, `expires_at`, `revoked_at`, `token_generation`, `last_seen`. Không log token/code/body nhạy cảm. Token random đủ entropy nên HMAC/pepper phù hợp hơn việc coi nó như password người dùng; code ngắn phải có rate limit và pepper bảo vệ trong auth directory.
5. Scope MVP là `recording:create`, `recording:upload`, `recording:resume`, `recording:finalize`, `recording:status:read` và tùy chọn `transcript:read`/`notes:read` cho recording do thiết bị tạo. Không có provider credential, admin, arbitrary filesystem, UI token hay notes mutation. `notes:run` chỉ thêm khi issue204 chốt thao tác và idempotency.

Revoke đặt `revoked_at`, tăng generation và chấm dứt session; mọi request kiểm tra record. Rotate thủ công hoặc sau 30 ngày tạo token mới, vô hiệu token cũ nguyên tử. Reconnect đọc token local, tạo session mới; 401 `device_token_expired`/`device_revoked` dừng retry và yêu cầu pair lại. Session hết hạn sau 15 phút **không** làm device token hết hạn: trong recording 1–4 giờ, service worker refresh session trước hạn (hoặc sau 401) bằng device token, rồi tiếp tục upload cùng endpoint. Device token hết hạn/revoked dừng retry upload và giữ buffer; phần capture chỉ tiếp tục trong giới hạn buffer đã công bố, rồi Stop có kiểm soát khi không còn chỗ. Timeout cũng giữ buffer; không hứa ghi vô hạn khi mất kết nối.

## R3 — Chrome permission, storage và URL

Manifest nên giữ public key ổn định để ID không đổi giữa update; Chrome ghi nhận `manifest.key` duy trì unique ID và có thể dùng để server nhận đúng extension origin ([Manifest key](https://developer.chrome.com/docs/extensions/reference/manifest/key)). ID công khai, không phải secret. Reload/update giữ storage; remove làm `storage.local` mất, còn `storage.session` bị xóa khi reload, update, disable hoặc restart. `storage.sync` có thể đồng bộ sang Chrome khác, nên cấm cho device token. Chrome cũng nói `storage.local` mặc định có thể lộ cho content scripts và không phải kho bí mật mã hóa ([browser.storage](https://developer.chrome.com/docs/extensions/reference/api/storage)). Gọi và kiểm tra thành công `storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})` trước khi lưu credential; nếu không hỗ trợ thì dừng pairing. Chỉ service worker đọc token; message handler chỉ nhận lệnh từ trang extension tin cậy, không cho content script chuyển tiếp URL hoặc đọc credential. Buffer audio/metadata nằm ở IndexedDB, kèm backend identity/recording ID, không kèm token.

Cross-origin fetch từ service worker/extension page cần host permission; content script vẫn mang same-origin của trang host. Chrome khuyến nghị optional permissions và cảnh báo handler không được cho content script chỉ định arbitrary URL ([cross-origin network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)). Dùng `optional_host_permissions`, không cấp `<all_urls>` hoặc `http://*/*`. Với local có thể khai báo các pattern hẹp cho `http://localhost/*` và `http://127.0.0.1/*`, rồi request subset có port đã chọn; remote động cần `https://*/*` để Chrome cho phép origin phát hiện runtime, đây là khai báo các host có thể xin quyền, không phải cấp sẵn quyền mọi host. Chỉ request scheme/host/port đã chọn qua user gesture; không request wildcard mọi website. `chrome.permissions.request()` chỉ dùng cho permission đã khai báo, và `remove()` thu hồi khi user xóa endpoint ([Permissions API](https://developer.chrome.com/docs/extensions/reference/api/permissions)). Trang match-pattern hiện hành quy định port là tùy chọn; bỏ port nghĩa là wildcard, chỉ rõ port thì match port đó, còn path của host permission bị bỏ qua ([Match patterns](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns)). Vì vậy permission không thay thế kiểm soát URL chính xác trong client/server. Pattern IPv6 cần kiểm tra trên Chrome target; hiện source chỉ có localhost/127.0.0.1, chưa tuyên bố hỗ trợ `[::1]`.

Normalizer phải kiểm tra chuỗi thô **trước** WHATWG `URL`: reject control/space/backslash, userinfo, query/fragment, scheme ngoài HTTP(S), port sai, dot-segment/path ambiguity và authority rỗng. HTTP chỉ chấp nhận raw host đúng một trong `localhost`, `127.0.0.1`, hoặc `[::1]` khi API tương lai đã bổ sung; không chấp nhận LAN/public, `localhost.example`, `127.0.0.1.nip.io`, hay hostname tùy ý dù DNS hiện resolve về loopback. Cũng reject decimal/octal/hex IPv4 alias (`2130706433`, `0177.0.0.1`, `0x7f000001`) trước WHATWG vì parser có thể canonicalize chúng thành 127.0.0.1. Remote chỉ nhận HTTPS; không tự hạ xuống HTTP. RFC 9110 định nghĩa HTTPS là HTTP qua TLS có xác thực authority và bảo vệ confidentiality/integrity, đồng thời tách http/https thành các origin khác nhau ([RFC 9110 §4.2.2](https://www.rfc-editor.org/rfc/rfc9110#section-4.2.2)).

Sau raw check, WHATWG normalization lowercase host, bỏ default port, giữ custom port, và chuẩn hóa base path deterministic (root `/`, bỏ trailing slash; không query/fragment). Current `config.py` không nhận IPv6 hoặc base path; đây là API/config work tương lai. Mọi fetch dùng `redirect: "error"` ([Fetch Standard](https://fetch.spec.whatwg.org/#concept-request-redirect-mode)), kiểm tra status và identity; redirect không mang Authorization/audio. Đổi URL khi đang ghi/buffer phải giữ buffer theo endpoint cũ. Alias localhost/tunnel không tự tương đương: gọi capabilities ở địa chỉ mới, so `backend_id`/fingerprint chỉ như thông tin, hiển thị xác nhận và pair/re-bind; khác backend bắt buộc pair mới.

## R4 — Session authorization và WebSocket

Discovery là GET không credential. Pair chỉ yêu cầu Host đúng authority, `Origin: chrome-extension://<fixed-id>` đúng tuyệt đối (không `null`, UI origin hay web origin) và one-time pairing code; nó **chưa** có Authorization. Session yêu cầu device token qua `Authorization: Bearer`; upload/status yêu cầu session token. Mọi request dùng `credentials: "omit"`, không trộn cookie UI vào API extension. Unsafe methods yêu cầu đúng extension Origin; GET/HEAD có thể thiếu Origin, nhưng nếu có thì phải đúng. Protected GET/HEAD vẫn bắt buộc token và kiểm tra quyền; chỉ discovery là public. `OPTIONS` chỉ trả metadata CORS cho origin/method/header được phép, không thực hiện nghiệp vụ và không yêu cầu bearer của request thật. 

CORS chỉ cho phép exact extension origin, methods/headers cần thiết. Host permission và CORS là các cơ chế khác nhau; privileged extension fetch được grant host permission có thể đọc phản hồi mà không có CORS response headers. Vì vậy không dùng CORS làm hàng rào xác thực: server tự kiểm tra Origin khi áp dụng, credential, scope và ownership. 

Server lấy device owner từ credential đã xác thực, không tin `device_id` trong body. Truy vấn recording/session/status phải cùng owner và scope, với ID không thuộc quyền trả 404 để không lộ dữ liệu. Re-pair sau mất/hết hạn token không tự nhận lại phiên cũ chỉ vì client khai cùng device ID: operator phải phê duyệt gắn credential mới vào device record cũ, hoặc giữ buffer để phục hồi riêng. Rotate cùng device giữ ownership; nếu mất response cấp token mới thì yêu cầu re-pair, không khôi phục token đã revoke. 

Server trả contract ổn định: 401 cho thiếu/sai/hết hạn credential; 401 `device_revoked` cho token revoked; 403 `invalid_origin` hoặc `scope_denied`; 409 cho pairing replay/resource state conflict. Không echo credential trong lỗi/log.

`fetch()` gửi `Authorization` từ service worker cho HTTP. Không đưa token vào URL, fragment, query, Referer, log hoặc content script. WebSocket browser chỉ nhận `url` và `protocols`; handshake có credentials mode `include` và redirect mode `error`, nhưng không có tham số tùy ý cho `Authorization` ([WebSockets Standard](https://websockets.spec.whatwg.org/#the-websocket-interface)). Vì vậy `capabilities.websocket = false` cho MVP. Nếu cần: cấp short-lived WS ticket bằng HTTP, mở `wss://` đúng endpoint, gửi `authenticate` đầu tiên, chờ `authenticated` trước audio, đóng 1008 khi credential sai/hết hạn/revoked; không để ticket trong URL.

HTTP loopback được cho phép nhưng không mã hóa; process khác trên máy có thể giả endpoint hoặc quan sát credential/audio. Token xác thực caller và giới hạn quyền thiết bị, nhưng không xác thực một endpoint HTTP local giả hoặc giải quyết local MITM. Đây là rủi ro MVP đã chấp nhận; mTLS/named pipe hoặc HTTPS local để sau.

## R5 — Ma trận ca kiểm chứng

| Ca | Hành vi/tiêu chí contract | Bằng chứng hiện có |
|---|---|---|
| Local custom port; remote HTTPS | Normalize, request đúng host permission, capabilities trước pair, token/session sau pair | Synthetic URL/parser và loopback Chromium bên dưới; permission prompt/remote API chưa chạy |
| Từ chối host permission | Không gọi network, giải thích và cho retry sau user grant | Chưa chạy |
| Sai API/version | capabilities trả `unsupported_api`, không tạo pairing intent/token | Thiết kế, chưa implementation |
| URL giả, userinfo, query, decimal/octal/hex loopback | Client reject trước network; không DNS exemption | Synthetic check; chưa Chrome |
| HTTP loopback / HTTP LAN-public / fake localhost | Chỉ loopback raw được nhận; LAN/public và hostname giả bị reject | Synthetic check; current config chỉ chứng minh hai host |
| Redirect ra ngoài loopback/origin khác | `redirect:error`; không gửi request kế tiếp | Phép thử Chromium bên dưới chặn redirect đổi host loopback; remote TLS/redirect khác chưa chạy |
| Thiếu/sai/hết hạn/revoked token | 401, không retry mù; revoke per device chặn session | Existing UI tests chỉ tương tự token/cookie; extension API chưa có |
| Client ngoài scope hoặc Origin sai; GET thiếu Origin | Sai scope 403, resource ngoài quyền 404; sai Origin 403; GET thiếu Origin vẫn xác thực bearer | Đã quan sát header Chromium; các guard API extension chưa triển khai |
| Pairing replay | Atomic consume, 409 lần hai, không cấp token | Chưa implementation |
| Đổi URL khi recording/buffer còn dở | Giữ buffer với old identity, pause; pair/rebind explicit | Capture/distribution docs là đề xuất; chưa chạy |
| Alias cùng instance / server khác | So backend ID/fingerprint, vẫn cần xác nhận/re-pair; server khác không nhận old token | Chưa server |

Phương pháp tái lập: chạy harness Node không network; với mỗi input, kiểm tra raw authority trước, sau đó gọi WHATWG `new URL(raw)` và in JSONL gồm `raw`, `whatwg`, `strict`. Input gồm local custom port, HTTPS base path, HTTP LAN/public, fake localhost, decimal/octal/hex loopback, userinfo và query. Check bounded, không resolve DNS, không chứng minh Chrome match permission, TLS, redirect hay server responses. Source/output giữ trong evidence run. Đầu ra: WHATWG biến cả ba alias `2130706433`, `0177.0.0.1`, `0x7f000001` thành `127.0.0.1`, trong khi raw guard reject cả ba; loopback custom port và HTTPS base path được normalize; HTTP LAN/public/fake localhost bị reject. Không chạy full suite, browser hay production endpoint.

### Phép thử mạng bằng Chromium cô lập

Nguồn kết quả: JSON thu từ harness Python `ThreadingHTTPServer` + Playwright persistent context mới; phiên bản trả bởi `browser.version` là **156.0.8078.4** trên Linux. Tạo MV3 extension tối thiểu, service worker rỗng và trang extension, grant `http://127.0.0.1/*` trong manifest. Server bind loopback trên port do OS cấp, trả JSON **không có CORS headers**; chỉ dùng bearer marker tổng hợp. Từ cả trang extension và service worker, gọi GET/POST bằng `fetch` với `credentials: "omit"`, `redirect: "error"`, `Authorization` marker và JSON body cho POST. Cách nạp dùng [Playwright Chromium extension guide](https://playwright.dev/python/docs/chrome-extensions); không mở profile người dùng.

| Quan sát thực thi | Kết quả |
|---|---|
| GET từ trang extension và service worker | HTTP 200, có Authorization, **không có Origin**, `Sec-Fetch-Site: none` |
| POST từ cả hai context | HTTP 200, có Authorization và `Origin: chrome-extension://<id>`, `Sec-Fetch-Site: none` |
| Service worker GET/POST tới route trả HTTP 307 sang `localhost` trên cùng port | Fetch báo lỗi; log server không có request tới đích redirect |

Do đó contract không bắt Origin trên safe reads hoặc trông chờ CORS ngăn extension có host permission. Đây là thử nghiệm mạng nhỏ, không phải API sản phẩm: chưa thử prompt optional permissions, Google Chrome Stable, Windows/macOS, IPv6, TLS remote, capture hoặc token lifecycle thực. Reproduce bằng setup trên, ghi riêng method/path/Origin/Sec-Fetch-Site/boolean có Authorization; không log credential thật. Numeric-loopback check có thể đối chiếu bằng `new URL("http://2130706433").hostname`, tương tự `0177.0.0.1` và `0x7f000001`; raw-host allowlist phải chạy trước parser.

## R6 — Handoff và điều chưa làm

[Thiết kế phiên ghi/upload](https://github.com/nguyencanhtrung/asr-studio/issues/203) cần nhận `backend_identity`, `device_id`, session expiry, recording/upload ID, sequence/offset, idempotency key và durable ACK; retry chỉ cùng endpoint đã pair. `complete/finalize` chỉ được xóa IndexedDB buffer sau ACK đủ bytes và đúng recording/device. [Luồng transcript/notes](https://github.com/nguyencanhtrung/asr-studio/issues/204) nên chỉ nhận status/result read scope và request ID; không chuyển provider credential vào extension, không để extension tự sửa shared library ngoài scope đã chốt. Shared library vẫn thuộc một operator/backend, không biến device ID thành tenant.

Chưa làm: API/database, server hash store, manifest/permission code, browser matrix, WebSocket/redirect/TLS tests, live ASR, production pairing, multi-server UI, hardware-bound key/DPoP và local MITM hardening. Minimum Chrome, nhất là IPv6 match pattern, optional permission runtime và capture APIs, phải chốt rồi chạy trên Chrome Stable; không suy từ unit test backend/tài liệu thành nghiệm thu browser.

## Nguồn chính

* [Chrome — cross-origin network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)
* [Chrome — Permissions API](https://developer.chrome.com/docs/extensions/reference/api/permissions)
* [Chrome — Match patterns](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns)
* [Chrome — browser.storage](https://developer.chrome.com/docs/extensions/reference/api/storage) và [storage/cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)
* [Chrome — manifest key](https://developer.chrome.com/docs/extensions/reference/manifest/key) và [message passing](https://developer.chrome.com/docs/extensions/develop/concepts/messaging)
* [WHATWG URL Standard](https://url.spec.whatwg.org/), [Fetch Standard](https://fetch.spec.whatwg.org/), [WebSockets Standard](https://websockets.spec.whatwg.org/)
* [RFC 9110 — HTTPS URI scheme and origins](https://www.rfc-editor.org/rfc/rfc9110#section-4.2.2)

Local evidence: `src/asr_studio/config.py`, `src/asr_studio/app.py`, `src/asr_studio/auth.py`, `src/asr_studio/routes.py`, `tests/test_authentication.py`, `tests/test_auth_state.py`, `docs/adr/0002-localhost-reusable-token.md`, `docs/chrome-extension-distribution-research.md`, `docs/chrome-extension-audio-capture-research.md`, `docs/backend-contract.md`.
