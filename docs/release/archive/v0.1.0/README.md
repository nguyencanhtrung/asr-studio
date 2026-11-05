# Hồ sơ phát hành 0.1.0 (#17)

License code đã được chủ sở hữu duyệt: [tiếng Anh chính](../../../../LICENSE),
[tiếng Việt tham khảo](https://github.com/nguyencanhtrung/asr-studio/blob/fe1e685/LICENSE.vi). Tag dự kiến `v0.1.0`; image dự kiến
`ghcr.io/nguyencanhtrung/asr-en-vi:v0.1.0`, `linux/amd64` và `linux/arm64`.
**Chưa công bố hoặc xác minh artifact registry cuối.**

## Thành phần và quyền phân phối

[Inventory amd64](../../runtime-inventory-amd64.json) ghi 135 package Python và 347
package hệ thống của image kiểm thử local, gồm cả package bị apt hold.
Trường `artifact` xác định đúng image; đây không phải chứng nhận image công bố.
Các license metadata, hash notice và quyết định phân phối được phân biệt.
PyAV được build từ source với FFmpeg 8.1.2 LGPL 2.1 nguyên bản; không phân phối
bản vendor nhúng x264/x265. FFmpeg CLI hệ thống và PyAV là hai bản build riêng. Runtime
đặt `/opt/asr-media/lib` ở đầu `LD_LIBRARY_PATH` nhưng giữ các đường dẫn NVIDIA kế thừa.
TorchCodec `0.11.1` dùng PyPI trên amd64; Linux aarch64 dùng wheel `+cu128` chính thức,
khớp Torch/Torchaudio CUDA 12.8 hiện có, không thêm CUDA 13.

- Ứng dụng: PolyForm Noncommercial 1.0.0; hai bản license trong wheel, sdist và image.
- Python và native dependencies: [bundle notice](../../../../third_party/README.md),
  version/hash upstream trong `third_party/bundle-spec.json`; có notice bổ sung
  CTranslate2, oneDNN, oneMKL, FlatBuffers, primePy và 127 thành phần Rust Tokenizers.
- Ubuntu/FFmpeg: giữ copyright ở `/usr/share/doc`. Collector xử lý cả DEP-5 và
  copyright dạng văn xuôi; tải source package đúng version, gồm orig tarball,
  bản vá Debian và `.dsc`. Không thay nghĩa vụ source bằng link upstream chung.
- CTranslate2 trong container dùng runtime GNU Ubuntu. Hash trước/sau được ghi trong
  `/usr/local/share/licenses/asr-en-vi/openmp-runtime.json`; GCC source tương ứng
  nằm trong bundle. Không đổi license ứng dụng thành GPL vì dependency runtime.
- NVIDIA: giữ nguyên [điều khoản container](../../../../third_party/notices/nvidia/base-image/NGC-DL-CONTAINER-LICENSE),
  CUDA 12.8.1 EULA, cuDNN/NCCL và notice wheel. `cuda-libraries` là metadata;
  driver compatibility libraries theo phần Driver Libraries/điều khoản container.
  CUDA-GDB không có trong runtime; phần GPL nói riêng về CUDA-GDB trong EULA
  chung không phải bằng chứng package runtime đó chứa GDB.
- OpenSSL: notice nằm trong package `libssl3t64`, không suy ra thiếu license chỉ
  vì `/usr/share/doc/openssl/copyright` không tồn tại.
- OMP 18.5.0: LICENSE và THIRD-PARTY-NOTICES giữ trong image.
- Inter, Newsreader, JetBrains Mono: SIL OFL 1.1, notice đi cùng font.
- Whisper large-v3/small và pyannote Community-1: người dùng tự tải ở setup;
  không phân phối weights hoặc HF token trong source/image. Community-1 cần
  người dùng chấp thuận điều kiện truy cập; model giữ license riêng.

## Bundle cho đúng artifact

`scripts/prepare-release-bundle.py IMAGE --version 0.1.0 --output DIR` kiểm tra
version, actual OpenMP bytes, decode TorchCodec/PyAV và thư viện FFmpeg thực sự trong
`/proc/self/maps`, đồng thời từ chối libavcodec hệ thống hoặc x264/x265 GPL và CUDA-GDB;
xuất inventory, thu thập nguồn đúng version và tạo tarball/hash. Pipeline upload source archive tới GitHub
Release công khai **trước** khi push image platform. Tên archive chứa kiến trúc
và image configuration digest. Manifest ghi từng source URL/hash; không dùng
image ID local thay cho registry manifest digest.

Sau push, pipeline pull digest bằng Docker config rỗng, kiểm tra version/revision
và đối chiếu configuration ID của image đã pull với candidate đã dùng để tạo source
archive, rồi chạy smoke media/health. Chỉ tạo tag image đa kiến trúc khi cả platform pass.
Đây chưa thay thế smoke cài mới và kết nối provider sau công bố.

## Quyền riêng tư

Build context dùng allowlist. Báo cáo công khai bỏ tên/ngày/ID bản ghi và đường
đẫn bằng chứng riêng. Ảnh README lấy từ fixture tổng hợp, tạo bằng app tạm riêng.
Log runtime mặc định dùng event/stage/code cố định và không chuyển tiếp stderr
thư viện; startup non-TTY không in access token/QR. Các lệnh chẩn đoán chủ ý có
thể hiển thị dữ liệu của bản cài và không được tự đưa vào báo cáo công khai.

Quét archive và mọi layer image cuối, kiểm tra screenshot, version và revision
trước khi công bố. Kiểm tra chuỗi không chứng minh mọi dạng bí mật đều vắng mặt.

## Trạng thái nghiệm thu

Bản local `0.1.0` đã build và qua smoke torchcodec, media, healthz. OpenMP mới
nạp được và CTranslate2 báo compute types CPU. Chưa có phép đo chất lượng/hiệu
năng mới; giữ nguyên fail/partial trong [báo cáo lịch sử](../../../acceptance-report.md).

Còn phải xác nhận toàn bộ kiểm thử/review, image arm64, digest công bố, pull ẩn
danh, hai đường cài từ artifact công bố và đăng nhập provider bằng tài khoản
của người dùng trên bản cài mới. Không tick tiêu chí #17 khi chưa có bằng chứng.
