# Phân phối và cập nhật extension audio nội bộ

> Phạm vi: extension Chrome MV3 nội bộ, không Chrome Web Store, không desktop app. Backend do người dùng cấu hình bằng Server URL: localhost cùng máy hoặc endpoint HTTPS từ xa; `https://asr.bibo.id.vn` chỉ là ví dụ expose dịch vụ local qua cloudflared, không phải địa chỉ cố định hay phụ thuộc bắt buộc. Một người vận hành, nhiều thiết bị, token riêng theo thiết bị/backend đã pair. Chưa chạy thử trình duyệt, chưa triển khai production.

## Kết luận

Cho MVP, chọn **unpacked extension + bundle có phiên bản + `manifest.json` chứa public key ổn định**. Người vận hành tải bundle qua kênh riêng, kiểm tra SHA-256, vào `chrome://extensions`, bật Developer mode và chọn **Load unpacked**. Đây là luồng Chrome tài liệu hóa cho nhu cầu cá nhân trên desktop, không cần Web Store hay app desktop, nhưng cập nhật và rollback là thủ công. Chrome mô tả unpacked là cách dành cho extension cá nhân và mã tin cậy. [Distribute your extension](https://developer.chrome.com/docs/extensions/how-to/distribute)

Không chọn self-host CRX làm đường chính cho MVP. Không có quản trị Chrome, luồng cài CRX tự host thông thường chỉ khả dụng trên Linux. Windows và macOS cần enterprise policy. [Distribute your extension](https://developer.chrome.com/docs/extensions/how-to/distribute)

## A1 — So sánh các đường cài đặt

| Nền tảng | Không Web Store | Cập nhật và giới hạn |
|---|---|---|
| Windows | Unpacked cho cá nhân. Self-host CRX qua `ExtensionSettings`/enterprise policy. | Cài CRX ngoài Web Store bằng luồng người dùng thông thường không được hỗ trợ. Force-install ngoài Web Store yêu cầu AD, Azure AD hoặc Chrome Enterprise Core. [Định nghĩa policy hiện hành](https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallForcelist.yaml) |
| macOS | Unpacked cho cá nhân. Self-host qua enterprise policy; force-install ngoài Web Store cần MDM, MCX domain hoặc Chrome Enterprise Core. [Định nghĩa policy](https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallForcelist.yaml) | Không dùng external-extension route để né Web Store: tài liệu cài đặt quy định Windows/macOS phải dùng Web Store cho `update_URL` của route đó. [Alternative installation methods](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions) |
| Linux | Unpacked, CRX cục bộ, hoặc CRX trên server riêng với update XML. | Luồng CRX tự host thông thường không cần enterprise policy được tài liệu hóa cho Linux. CRX mới phải dùng cùng private key. [Self-host for Linux](https://developer.chrome.com/docs/extensions/how-to/distribute/host-on-linux) |
| Windows/macOS/Linux managed | `ExtensionSettings`: `force_installed` hoặc `normal_installed`, `update_url`, và tùy chọn `override_update_url`. | `force_installed` không cho người dùng gỡ/tắt. `normal_installed` cho phép tắt. Chính sách áp dụng cho cả Windows, Mac và Linux. [ExtensionSettings](https://support.google.com/chrome/a/answer/9867568) |

Unpacked: vào `chrome://extensions` → bật Developer mode → **Load unpacked**. Manifest, service worker và content script cần reload; content script còn cần refresh trang host. Popup và HTML extension thường không cần reload. [Hello World: load and reload](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world)

## A2 — MVP, quyền sở hữu cập nhật và kiểm tra toàn vẹn

Các bước dưới đây là **thiết kế được đề xuất**, chưa phải quy trình đã nghiệm thu trên thiết bị.

1. Người duy trì tạo bundle có phiên bản, public manifest key ổn định và ghi chú thay đổi permission/schema. Giữ private signing key ngoài repo và bundle. Phân phối qua nguồn HTTPS đáng tin cậy, có quyền truy cập phù hợp; đối chiếu SHA-256 với metadata phát hành từ nguồn đã xác thực. Checksum đi kèm file chỉ phát hiện sai khác, không tự chứng minh tác giả nếu cả hai cùng bị thay thế.
2. Giải nén vào thư mục cài cố định, ví dụ `asr-extension/installed`; không Load unpacked trực tiếp từ thư mục tải xuống tạm. Bật Developer mode → Load unpacked → chọn thư mục này → ghi nhận ID/phiên bản. Pair riêng từng Chrome profile/thiết bị; không sao chép token bằng bundle hoặc đồng bộ profile.
3. Người dùng nhập Server URL rồi cấp quyền cho host đã chọn; cùng bundle phải dùng được với localhost và các endpoint HTTPS mà không build lại. Đề xuất khai báo `optional_host_permissions` đủ cho các địa chỉ hỗ trợ, chỉ xin quyền host tương ứng qua `chrome.permissions.request` trong tương tác người dùng. Phạm vi có thể yêu cầu trong manifest không đồng nghĩa đã cấp sẵn quyền mọi website. Gọi API từ extension context; client vẫn kiểm soát chính xác scheme/host/port/base path, vì host permission không giới hạn một API path và không thay thế token. Cách khai báo/match pattern và browser checks cụ thể thuộc [thiết kế token](https://github.com/nguyencanhtrung/asr-studio/issues/202). [Permissions API](https://developer.chrome.com/docs/extensions/reference/api/permissions), [Match patterns](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns), [Network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)
4. Trước cập nhật bình thường: Stop; chờ hoàn tất lưu cục bộ, upload và ACK lưu bền; kiểm tra không còn phiên đang ghi hoặc dữ liệu chờ gửi. Không cần đợi ASR/notes kết thúc nếu server đã nhận đủ audio. Khi mạng lỗi, hoãn cập nhật và giữ nguyên buffer; phục hồi khẩn cấp sẽ cần đường migration/export được kiểm thử riêng.
5. Tải và kiểm tra bundle mới vào thư mục staging. Xác nhận cùng public key/ID, permission và schema tương thích; giữ bản code cũ. Đóng Chrome sau khi không còn dữ liệu chờ gửi, thay **toàn bộ** nội dung thư mục cài bằng bundle đã kiểm tra, tránh trộn file hai phiên bản. Mở lại Chrome và Reload extension tại `chrome://extensions`; kiểm tra ID, phiên bản, pairing và ghi thử dữ liệu synthetic. Không Remove extension để cập nhật. Giải nén sang thư mục khác rồi Reload không khiến Chrome tự chuyển đường dẫn đã nạp.
6. Rollback theo cùng điều kiện dừng/đồng bộ, chỉ với phiên bản được đánh dấu tương thích schema hiện tại. Thay code ở **cùng thư mục cài**, giữ public key và Chrome profile. Nếu bản cũ không đọc được dữ liệu mới, không ép downgrade hay xóa database: cần bản sửa tiến hoặc công cụ phục hồi đã kiểm thử. Với CRX có cập nhật tự động, phát hành code khôi phục bằng version cao hơn và cùng signing key; không mặc định có thể hạ version qua update feed. [Update lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/extensions-update-lifecycle)

Người duy trì chịu trách nhiệm bundle, checksum, public key, bảng tương thích và hướng dẫn; người vận hành cập nhật từng thiết bị. Chưa cần dựng CRX update endpoint hoặc quản trị máy cho MVP. Nếu dùng managed distribution sau này, phải kiểm tra policy thực tế và tác động uninstall khi bỏ force-install policy; không coi đây là cách chuyển đổi miễn phí về dữ liệu. [Force-install policy](https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallForcelist.yaml)

## A3 — ID, key, permission và pairing

`key` trong manifest là **public key**, không phải private signing key. Chrome giải mã Base64 của key rồi tính ID từ public-key bytes. Nếu không có key, Chromium tạo ID từ đường dẫn extension. ID theo đường dẫn ổn định qua reload trên một máy nhưng không phù hợp để ghép nhiều OS/thư mục. [Manifest key](https://developer.chrome.com/docs/extensions/reference/manifest/key), [ID computation](https://chromium.googlesource.com/chromium/src/+/main/extensions/common/extension.cc), [ID algorithm](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/components/crx_file/id_util.cc)

Có thể tạo public manifest key **không dùng Web Store**:

    # Pack Extension cục bộ một lần để tạo asr-extension.pem và asr-extension.crx.
    openssl pkey -in asr-extension.pem -pubout -outform DER | base64 | tr -d '\r\n'

Đặt chuỗi Base64 một dòng vào `"key"`. Đây là quy trình suy ra từ việc Chrome tạo `.pem` private key khi Pack Extension, tài liệu manifest yêu cầu phần nội dung của `BEGIN PUBLIC KEY`/`END PUBLIC KEY`, và Chromium Base64-decode key trước khi tính ID. Kiểm tra bắt buộc sau đó: Load unpacked trên từng OS và so sánh ID hiển thị. Chưa chạy lệnh hoặc browser check trong phiên này.

[Pack Extension và key cục bộ](https://developer.chrome.com/docs/extensions/how-to/distribute/host-on-linux) mô tả việc tạo CRX/PEM. Private `.pem` phải được giữ bí mật và dùng lại khi đóng gói bản cập nhật. Mất key làm mất chuỗi ký của CRX và buộc quy trình cài mới. Public key giữ ID ổn định nhưng **không** xác nhận checksum của bundle và không cấp permission.

`tabCapture` là permission/API riêng cho audio/video của tab và chỉ được gọi sau tương tác người dùng. Quyền host được cấp nên chỉ bao phủ backend người dùng đã chọn, không cố định một domain. [tabCapture](https://developer.chrome.com/docs/extensions/reference/api/tabCapture), [Declare permissions](https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions)

ID ổn định tạo origin `chrome-extension://<id>` ổn định. Vì vậy backend có thể allowlist origin/ID, còn token vẫn phải bind theo `device_id`; không dùng ID làm bí mật hay bằng chứng xác thực: public key có thể được sao chép và HTTP client khác có thể giả header Origin. Nếu ID đổi, CORS/origin allowlist, pairing và namespace storage có thể không còn khớp.

## A4 — Reload, cập nhật, rollback, token và buffer

- **Token:** nơi lưu và bảo vệ token sẽ được quyết định trong [thiết kế token từng thiết bị](https://github.com/nguyencanhtrung/asr-studio/issues/202); `chrome.storage.local` cung cấp persistence nhưng không nên được xem là kho bí mật đã mã hóa; không dùng `storage.sync` cho token riêng. `storage.session` không phù hợp vì bị xóa khi extension bị reload, update, disable hoặc Chrome restart. `storage.local` bị xóa khi extension bị remove, nên uninstall/reinstall phải được coi là mất pairing. [Storage API](https://developer.chrome.com/docs/extensions/reference/api/storage)
- **Đổi server (yêu cầu thiết kế):** token, phiên và buffer gắn với endpoint/backend đã pair. Đổi URL không tự chuyển credential hoặc audio đang chờ sang đích mới, kể cả localhost và tunnel được cho là trỏ cùng instance; cần xác minh/re-pair theo contract. Không follow redirect sang origin khác kèm token/audio. Người vận hành đã xác nhận: remote bắt buộc HTTPS, cho phép HTTP chỉ với loopback cùng máy (`localhost`, `127.0.0.1`, `[::1]`), token bắt buộc cả local lẫn remote. Không cho HTTP LAN/public, không tự hạ HTTPS xuống HTTP và không coi HTTP loopback là kênh đã mã hóa. Cách xác thực URL/đích loopback và xử lý redirect còn phải kiểm chứng. Theo dõi ở [thiết kế token](https://github.com/nguyencanhtrung/asr-studio/issues/202), [phiên ghi](https://github.com/nguyencanhtrung/asr-studio/issues/203) và [kiểm chứng mã hóa](https://github.com/nguyencanhtrung/asr-studio/issues/208).
- **Audio buffer (đề xuất):** dùng IndexedDB cho chunk/metadata lớn; IndexedDB và Cache Storage dùng được trong service worker. Ghi `{backend_identity, recording_id, chunk_no, schema_version, acked}` và idempotency key. [Storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)
- **Stop flow (yêu cầu thiết kế):** Stop hoàn tất luồng ghi/upload; server finalize đủ dữ liệu rồi tự chạy transcript và notes. Chỉ xóa buffer sau ACK xác nhận toàn bộ dữ liệu tương ứng đã lưu bền vững, có kiểm tra danh tính phiên và tính đầy đủ; ACK từng chunk không đồng nghĩa toàn phiên đã hoàn tất. Timeout thì giữ và retry idempotently. Chi tiết nằm trong [thiết kế phiên ghi/upload](https://github.com/nguyencanhtrung/asr-studio/issues/203).
- **Schema khác retention:** version storage/database riêng với `manifest.version`. Update không phải lệnh xóa dữ liệu. Migration phải transactional, giữ field cũ cho đến khi bản mới xác nhận đọc được. Rollback chỉ an toàn khi bản cũ vẫn đọc schema/data mới hoặc có migration ngược. Retention audio phía server vẫn là “giữ đến khi người dùng xóa”, độc lập với migration.
- **Giới hạn:** extension storage có quota và có thể bị eviction dưới áp lực bộ nhớ. Với audio dài, phải theo dõi quota, báo đầy và giữ server ACK protocol rõ ràng. [Storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)

Reload, disable, gỡ extension, đóng Chrome hoặc crash có thể gián đoạn capture. Quy trình cập nhật có thể trì hoãn thao tác do ứng dụng quản lý, nhưng không thể bảo đảm ngăn người dùng bấm Reload/Remove ở Chrome. Không hứa phục hồi audio chưa kịp lưu hoặc ghi xuyên thời gian trình duyệt không chạy.

## A5 — Ma trận kiểm chứng

| Hạng mục | Fact đã tài liệu hóa | Chưa thực hiện |
|---|---|---|
| Cài unpacked | Chrome hỗ trợ Load unpacked + Developer mode. | Chưa thử trên Windows/macOS/Linux. |
| CRX/self-host | Linux hỗ trợ CRX/server update XML; Win/macOS cần enterprise policy. | Chưa có policy, endpoint, hoặc CRX thử nghiệm. |
| ID/key | Chromium có thuật toán public-key và path fallback nêu trên. | Chưa chạy OpenSSL, chưa so sánh ID giữa máy. |
| Update/reload | Reload cần thiết cho manifest/service worker/content script; update có thể chờ extension idle. | Chưa đo thời gian, idle, hay hành vi đang capture. |
| Capture | `tabCapture` yêu cầu user gesture; luồng service-worker/offscreen phụ thuộc phiên bản Chrome, sẽ chốt trong [nghiên cứu capture](https://github.com/nguyencanhtrung/asr-studio/issues/200). | Chưa thử tab+mic, quyền microphone, navigation, sleep, crash. |
| Data | `storage.session` là volatile; local/IndexedDB có persistence/quota caveat. | Chưa đo quota audio, eviction, migration, ACK hoặc rollback. |

Ma trận thực thi cần ghi riêng **Windows / macOS / Linux × phiên bản Chrome Stable thực dùng**. Mỗi hàng dưới đây hiện đều **chưa chạy trên cả ba OS**; không suy từ unit tests của backend thành hỗ trợ extension đã nghiệm thu.

| Ca thử trên từng OS/Chrome | Điều kiện đạt dự kiến |
|---|---|
| Localhost/custom port và endpoint HTTPS tùy chọn | Cùng bundle không rebuild; xin đúng quyền host, token bắt buộc, kết nối API đúng đích. Chưa chạy. |
| HTTP loopback so với HTTP LAN/public và hostname giả localhost | Chỉ loopback cùng máy được phép dùng HTTP; vẫn cần token; không gửi credential/audio theo redirect ra ngoài. Chưa chạy. |
| Đổi URL khi còn phiên/buffer, redirect sang host khác | Không chuyển token/audio/ACK sang server khác; alias local/tunnel cần xác minh theo contract. Chưa chạy. |
| Cài mới cùng bundle ở đường dẫn khác nhau | Cùng extension ID; từng profile pair riêng; không có token trong bundle. |
| Cập nhật ở trạng thái idle, khởi động lại Chrome | ID và pairing giữ nguyên, version đúng, ghi thử và upload thành công. |
| Còn buffer chưa ACK hoặc đang capture | Quy trình cập nhật hoãn; không xóa buffer. Reload cưỡng bức phải báo gián đoạn, không hiển thị phiên hoàn chỉnh giả. |
| Mạng mất rồi có lại, server đã lưu nhưng client chưa nhận ACK | Khôi phục/retry không tạo bản ghi trùng; chỉ xóa buffer sau xác nhận đúng phiên đủ dữ liệu. |
| Rollback schema tương thích/không tương thích | Bản tương thích đọc được dữ liệu; bản không tương thích bị chặn bởi quy trình, không xóa để chạy tiếp. |
| Remove/reinstall trong profile thử nghiệm | Mất pairing được xử lý bằng pair lại; không cam kết phục hồi buffer đã bị xóa. |

## A6 — Uncertainty và follow-up

1. Chốt Chrome stable/minimum version, đặc biệt nếu dùng service worker → offscreen → `tabCapture`.
2. Ghi OS, phiên bản Chrome và chính sách thực tế của các máy sẽ dùng. Chỉ khảo sát enrollment/management khi cần chuyển sang managed distribution; không chặn MVP unpacked để yêu cầu quản trị doanh nghiệp.
3. Chạy ma trận browser thật: install, ID, permission prompt, tab capture, microphone, Stop, retry/ACK, reload, update, rollback và uninstall/re-pair trên ba OS.
4. Khi triển khai, hiện thực bundle/metadata kiểm chứng, nơi giữ `.pem` và schema migration contract theo quy trình trên.
5. Xác nhận backend CORS/origin, token rotation, idempotency và định nghĩa “durable ACK”.

Báo cáo này giải quyết lựa chọn phân phối; các phép thử sản phẩm nằm trong [prototype](https://github.com/nguyencanhtrung/asr-studio/issues/205) và [kiểm chứng độ bền](https://github.com/nguyencanhtrung/asr-studio/issues/206). Tham chiếu [câu hỏi nghiên cứu](https://github.com/nguyencanhtrung/asr-studio/issues/199) và [bản đồ MVP](https://github.com/nguyencanhtrung/asr-studio/issues/196).
