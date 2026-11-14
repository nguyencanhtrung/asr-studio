# Mã hóa, backup và lưu/xóa dữ liệu cho MVP extension

Ngày nghiên cứu: 2026-11-14. Kết quả của [nghiên cứu storage/backup](https://github.com/nguyencanhtrung/asr-studio/issues/208), theo [chính sách đã chốt](https://github.com/nguyencanhtrung/asr-studio/issues/201).

**Hiện chưa đáp ứng yêu cầu mã hóa lưu trữ và backup.** Người vận hành xác nhận chưa cấu hình mã hóa và chưa thiết lập backup. Kết quả nghiên cứu này xác định khoảng thiếu và việc cần làm; không xác nhận triển khai đã được bảo vệ đầy đủ.

## Chính sách giữ nguyên

- Server URL cấu hình được; HTTP chỉ cho loopback cùng máy, remote bắt buộc HTTPS. Cả hai cần token từng thiết bị/backend đã pair.
- Server giữ audio tới khi người dùng xóa. Client chỉ xóa buffer sau khi backend xác nhận toàn bộ dữ liệu tương ứng đã nhận đủ và lưu bền; ACK một chunk chưa đồng nghĩa hoàn tất phiên.
- Dùng mã hóa hạ tầng cho storage/backup. Server cần plaintext để xử lý; không có cam kết E2EE chống chính server. Chưa chọn nơi lưu, thời hạn backup hoặc cơ chế giữ/khôi phục khóa thay người dùng.

## Bằng chứng và giới hạn kiểm tra

Chỉ đọc source và metadata cần thiết: Docker project/container/volume/port, image revision label, metadata filesystem và một request HTTPS không credential, bỏ response body. Không mở database, recording, provider session, `.env`, khóa hoặc token; không đọc command line/env/log chứa credential. Không thay dịch vụ, tạo backup, migrate hoặc thử restore production.

| Hạng mục | Bằng chứng | Kết luận |
|---|---|---|
| Mã hóa at rest | Người vận hành xác nhận chưa cấu hình. Runtime dùng named volumes với local driver, không có driver options riêng. | **Chưa có theo xác nhận người vận hành.** Driver local không tự chứng minh encrypted hay unencrypted ở mọi lớp bên dưới. |
| Backup | Người vận hành xác nhận chưa thiết lập. | **Chưa có.** Không có kết quả restore hoặc xóa khỏi backup; không coi đây là kiểm tra đạt. |
| HTTPS công khai | `curl` tới root endpoint ví dụ trả HTTP `303`, `ssl_verify_result=0`. | Certificate/TLS tới endpoint đạt trong lần kiểm tra này; chưa chứng minh ingress, origin TLS, Access policy hoặc app authentication. |
| App local | Runtime publish cổng app trên loopback. | Xác nhận binding được quan sát; không phải kiểm thử chống truy cập trái phép hay API token của extension. |
| Tunnel | Có container cloudflared chạy host network, không mount config. | Exact mapping hostname → service/origin và các tùy chọn TLS chưa xác minh. Không đọc tunnel token để suy cấu hình. |
| Filesystem | Metadata chỉ ra filesystem host; kiểm tra block-device/thuộc tính volume bị giới hạn quyền/ngữ cảnh quan sát. | Không có kiểm chứng độc lập đầy đủ các lớp mã hóa, swap hoặc recovery key. Không suy từ tên filesystem thành encryption status. |
| Volume legacy | Inventory chỉ theo tên project không thấy named volumes legacy. | Không chứng minh không tồn tại bản copy/export/snapshot ở nơi khác. |
| Phiên bản chạy | Image label khác main đã đọc; các file compose/storage/deletion/recordings được đối chiếu không có diff. | Source phù hợp để phân tích thiết kế; vẫn chưa thực thi delete/restore trên deployment. |
| Buffer extension | Chưa có extension runtime được nghiệm thu. | Quota, eviction, crash recovery và xóa sau ACK còn phải kiểm chứng. |

Source được đọc tại [`0aa0c1c82d5a717756dabee334d6f37b183311e4`](https://github.com/nguyencanhtrung/asr-studio/tree/0aa0c1c82d5a717756dabee334d6f37b183311e4). Các số/status trong bảng là output metadata/probe, không phải benchmark hay audit toàn hệ thống. Thông tin nhận diện máy, vị trí riêng tư và credential không đưa vào báo cáo.

## Các chặng dữ liệu và bên có thể đọc plaintext

```text
Local:  extension ─ HTTP loopback + app token ─→ ASR Studio

Remote: extension ─ HTTPS + app token ─→ Cloudflare edge (TLS terminates)
        edge ─ encrypted tunnel ─→ cloudflared
        cloudflared ─ configured origin protocol ─→ ASR Studio

ASR Studio → local audio / SQLite / temporary files
           → selected notes provider: transcript and source metadata
```

Cloudflared là triển khai ví dụ, không phải phụ thuộc bắt buộc. Với published application, origin `http://...` và `https://...` là hai cấu hình riêng: HTTPS tới edge không tự mã hóa hop từ connector tới local service. Nếu origin dùng HTTPS phải kiểm chứng certificate verification, không coi `noTLSVerify` là xác thực. Nếu hop cuối đi qua mạng khác máy thì cần TLS phù hợp; route loopback cùng máy phải được xác nhận từ cấu hình đã loại secrets. Không dùng generic zone SSL mode để suy ra protocol thực của Tunnel origin. [Cloudflare protocols](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/), [origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/), [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)

Browser/OS, proxy tại điểm kết thúc TLS, connector, app/worker, quản trị Docker host và chủ khóa backup nằm trong các ranh giới tin cậy tương ứng. Token ứng dụng không phải khóa mã hóa dữ liệu; Cloudflare Access credential cũng không thay token từng thiết bị của ASR Studio.

Theo [backend design](backend-design.md), notes provider đã chọn nhận transcript, ID/mốc nguồn, nhãn/tên người nói và ngày họp; adapter notes không gửi audio. Xóa local không chứng minh nội dung đã bị xóa ở provider. Không gọi đường tạo notes là xử lý hoàn toàn local.

## Storage và ý nghĩa thao tác xóa

Trong source đã đọc:

- `compose.yaml`: volumes `data`, `auth`, `provider`, `model-install`; app/worker/notes-service chia sẻ data theo vai trò. `read_only`, quyền Unix và container network không tự mã hóa các volume. Docker volumes lưu dữ liệu trên filesystem của host. [Docker volumes](https://docs.docker.com/engine/storage/volumes/)
- `storage.py`: SQLite `app.sqlite3`, WAL, quyền directory/file hạn chế; không có cấu hình mã hóa database trong đường này. `recordings.py` lưu raw upload, audio managed và `.recording-tmp` trong data root. Các vùng đó phải nằm trong phạm vi bảo vệ, cùng auth/provider state và bản export có nội dung nhạy cảm.
- `meeting_deletion.py`: chặn xóa khi task đang chạy, đánh dấu yêu cầu xóa, unlink raw/managed/temp files được quản lý rồi xóa các row transcript/notes/task/meeting; lỗi giữ marker để retry. Đây là xóa ở mức ứng dụng, không phải secure erase vật lý, purge backup hay xóa tại notes provider. SQLite có cơ chế riêng cho secure deletion, nhưng không được suy ra mọi bản sao/WAL/snapshot đã sạch từ `DELETE`. [SQLite secure_delete](https://www.sqlite.org/pragma.html#pragma_secure_delete)
- `scripts/start.sh` có migration copy volume project cũ và giữ bản cũ để rollback. Nếu migration đã tạo copy, xóa ở volume hiện hành không xóa bản đó. Giữ inventory và thời hạn giữ các bản copy trong kế hoạch vận hành.
- `/tmp` dùng tmpfs trong Compose, nhưng tmpfs có thể đưa page ra swap. Phạm vi mã hóa còn phải xét swap/hibernation, temporary exports và browser profile, không chỉ thư mục audio. [Linux tmpfs](https://docs.kernel.org/filesystems/tmpfs.html)

## Hướng cấu hình cần chốt trước triển khai

Đây là đề xuất và điều kiện kiểm chứng, chưa phải lệnh triển khai:

1. **Backup có mã hóa trước migration.** Chọn đích độc lập với dữ liệu chính và công cụ có cơ chế mã hóa sẵn; ghi rõ data/auth/provider nào cần khôi phục hoặc có thể pair/login lại. Không gọi copy cùng ổ là bảo vệ trước mất máy/ổ. Restic là một ứng viên tham khảo có repository mã hóa, chưa được chọn hay cài trong dự án. [Restic design](https://restic.readthedocs.io/en/stable/100_references.html#design)
2. **Bản khôi phục nhất quán.** SQLite/WAL và audio cần cùng trạng thái logic: snapshot/quiesce có phối hợp hoặc quy trình backup được thiết kế rõ. SQLite Online Backup API giúp lấy snapshot DB, nhưng không tự đồng bộ các file audio đang thay đổi. Thử restore ở nơi cách ly và kiểm tra đủ dữ liệu trước khi đổi storage. [SQLite Backup API](https://www.sqlite.org/backup.html)
3. **Storage được mã hóa bằng cơ chế OS/volume.** Trên Linux, dm-crypt là cơ chế mã hóa block sẵn có để đánh giá; lựa chọn cụ thể phụ thuộc ổ đĩa, mount, boot/unlock và quyền vận hành. Không format hoặc chuyển dữ liệu đang chạy khi chưa có backup đã thử khôi phục, kiểm tra đầy đủ và rollback. Mã hóa at rest bảo vệ trước đọc storage khi chưa unlock, không chặn tiến trình có quyền đọc khi hệ thống đang mở khóa. [Linux dm-crypt](https://docs.kernel.org/admin-guide/device-mapper/dm-crypt.html)
4. **Khóa và recovery.** Chốt người giữ khóa, nơi giữ recovery độc lập với máy/data backup, quyền mở khóa, cách rotation và thử mất máy/khóa. Không lưu bản duy nhất của khóa trong chính vùng cần khóa để mở; không đưa secret vào repo, issue hoặc log. App device tokens và khóa backup/volume là những thứ riêng.
5. **Retention và xóa.** Người vận hành chọn lịch, thời hạn backup và cách thông báo dữ liệu còn trong snapshot cũ tới khi purge. Chọn cách restore tránh âm thầm khôi phục meeting đã xóa. Dùng fixture vô hại để chứng minh delete/purge/restore sau này; không thử phá dữ liệu thật trong research này.

## Buffer trên Chrome

IndexedDB là lựa chọn đã đề xuất cho audio; không coi browser storage là kho bí mật đã mã hóa. Token persistent phải có scope/access policy riêng, không đồng bộ bằng `storage.sync`; bảo vệ profile/ổ đĩa thiết bị vẫn cần được xác định. `storage.session` mất qua restart/reload nên không giải quyết persistence của phiên dài. [Chrome Storage API](https://developer.chrome.com/docs/extensions/reference/api/storage)

Đo storage estimate, xử lý quota/disk-full và kiểm chứng eviction/crash/restart. `unlimitedStorage` hoặc `persist()` thay đổi quota/eviction behavior, không tạo thêm dung lượng đĩa hoặc mã hóa dữ liệu. Không dùng content-script page storage để giữ buffer của extension. [Chrome storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)

Giữ buffer cho tới ACK đầy đủ và lưu bền của đúng session/backend; không coi request thành công, ACK từng chunk hay ghi local xong là toàn phiên hoàn tất. Mất mạng phải giữ phần chưa xác nhận và báo đầy trước khi mất dữ liệu. Xóa buffer cũng không phải cam kết secure erase mọi bản sao trên thiết bị. Contract và phép thử thuộc [token từng thiết bị](https://github.com/nguyencanhtrung/asr-studio/issues/202), [phiên ghi/upload](https://github.com/nguyencanhtrung/asr-studio/issues/203) và [độ bền](https://github.com/nguyencanhtrung/asr-studio/issues/206).

## Kết quả nghiên cứu và việc tiếp theo

Đã xác định hiện trạng chưa mã hóa/chưa backup, phân biệt source với runtime và lập phạm vi bảo vệ/kiểm chứng. Còn phải chọn đích backup, retention, khóa/recovery và phương án migration; cung cấp mapping tunnel-origin đã loại secrets; rồi kiểm thử triển khai trên dữ liệu synthetic. Các phần này chưa đạt chỉ vì report hoặc CI tài liệu pass. Nghiên cứu không thay chính sách đã chốt và không triển khai production.

Các quyết định vận hành được theo dõi tại [chốt phương án mã hóa, backup và khôi phục](https://github.com/nguyencanhtrung/asr-studio/issues/211), trước [chốt thiết kế MVP](https://github.com/nguyencanhtrung/asr-studio/issues/207).
