---
status: accepted
---

# Cài model từ giao diện qua một service riêng có mạng

Người dùng muốn cài PhoWhisper và Qwen3-ASR ngay trong Cài đặt thay vì chạy lệnh trong terminal ([ADR 0011](0011-asr-model-per-run.md) cũ coi `model prepare` là bước duy nhất dùng mạng). Muốn có nút bấm thì ai đó phải tải và ghi `/models` lúc ứng dụng đang chạy, mà `app` và `worker` đang offline và chỉ đọc. Quyết định: thêm service **`model-installer`**, không nới quyền của `app` hay `worker`.

**Ranh giới.** `model-installer` là service duy nhất được tải từ model hub (`HF_HUB_OFFLINE=0`, proxy qua `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`) và duy nhất mount `/models:rw`. (`app` vẫn gọi nhà cung cấp notes như ADR 0007; nó và `worker` đặt `HF_HUB_OFFLINE=1` và không ghi `/models`.) Nó không mount `data`, `auth` hay `provider`, không nhận biến môi trường của chúng và không có cổng, GPU hay Docker socket, nên không thấy bản ghi, transcript hay thông tin đăng nhập. Nó chạy với `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp`, bằng user sở hữu thư mục model trên host (`ASR_MODEL_UID`/`ASR_MODEL_GID`, `start.sh` đặt từ `id -u`/`id -g`). `app` và `worker` vẫn `/models:ro` và offline; `tests/test_compose_contract.py` khóa điều đó. Chỉ tải model công khai trong danh mục: giao diện không nhận token, URL hay đường dẫn.

**Giao tiếp qua một volume nhỏ, không qua cơ sở dữ liệu.** `app` và `model-installer` cùng mount volume `model-install`; `worker` không mount. Volume không chứa gì ngoài tệp điều phối: `requests/<model>.json` (`app` tạo, loại trừ lẫn nhau, có mặt khi job đang chờ hoặc chạy), `cancel/<model>.<job>` (một tệp cho mỗi job), `status/<model>.json` (tiến độ, và lỗi cuối), `installer.json` (nhịp tim). Mọi ghi là tệp tạm rồi `rename`. `app` tạo request bằng `link()` nên hai yêu cầu cùng model chỉ ra một job. Heartbeat quá 10 giây nghĩa là không có installer: giao diện quay về lệnh sao chép như trước. Hai service chạy bằng hai user nên thư mục của volume mở `0777` và tệp `0644`; volume chỉ chung cho hai service này. Không đổi schema.

**Công bố nguyên tử.** Bản tải nằm ở `/models/.install-<model>-<id>`, cạnh đích để `rename` cùng filesystem. Sau khi tải (và chuyển đổi, với PhoWhisper) nó được kiểm bằng cùng `prepared_engine_directory` mà `worker` dùng, rồi đổi tên thành thư mục đích bằng một `rename`. `worker` không bao giờ thấy thư mục dở dang. Model đã chuẩn bị không bao giờ bị ghi; thư mục đích có tệp khác mà chưa chuẩn bị bị từ chối (`target_not_empty`), không ghi đè. Cùng một routine dùng cho CLI `model prepare --asr-model` và installer.

**Phiên bản.** Danh mục ghim revision Hugging Face và kích thước cho từng model; cả CLI lẫn installer tải đúng revision đó.

**Job.** Mỗi lần tải chạy trong một process group con để Hủy dừng cả tiến trình phụ; cái thiếu dung lượng bị chặn trước khi tải. Mã lỗi: `insufficient_disk`, `network`, `disk_full`, `integrity`, `target_not_empty`, `models_not_writable`, `interrupted`. Khi installer khởi động lại, job đang chạy dở bị đánh dấu `interrupted`, thư mục tạm mồ côi bị xóa và lệnh hủy đang chờ được thực hiện, nên không job nào bị kẹt.

**Chấp nhận.** Dung lượng cần có chỉ là ước lượng với PhoWhisper (phần chuyển đổi); mọi thư mục `.install-*` lạ trong `/models` bị installer xóa khi khởi động, kể cả của một lệnh CLI đang chạy cùng lúc; volume `model-install` mở quyền cho hai service. Đổi lại, `app` và `worker` không phải nới quyền và ứng dụng không cần Docker socket.
