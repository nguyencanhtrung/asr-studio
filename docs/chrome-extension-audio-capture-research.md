# Thu âm tab và microphone cho extension Chrome

Ngày nghiên cứu: 2026-11-14. Giải quyết [lựa chọn capture](https://github.com/nguyencanhtrung/asr-studio/issues/200) trong [bản đồ MVP](https://github.com/nguyencanhtrung/asr-studio/issues/196). Đây là quyết định thiết kế để prototype kiểm chứng, chưa có extension chạy thực tế hay bằng chứng ghi dài.

## Lựa chọn cho MVP

Chọn Chrome APIs trực tiếp: `chrome.tabCapture` + offscreen document + `getUserMedia` + Web Audio + `MediaRecorder`. Giữ tab và mic thành hai nhánh để điều khiển/đo mức âm, sau đó ghi **một audio track đã trộn**. Không thêm RecordRTC, Screenity hoặc libobs làm dependency.

Server URL do người dùng cấu hình; HTTP chỉ cho loopback cùng máy, remote dùng HTTPS, cả hai cần token. Capture chạy trong origin extension, độc lập với việc backend dùng loopback hay tunnel. Không hard-code `asr.bibo.id.vn`.

Một archive trộn phù hợp với backend hiện tại và giảm số encoder, hàng đợi, timeline cần đồng bộ. Đổi lại, không thể khôi phục riêng tab/mic từ bản trộn. Metadata chỉ ghi nguồn và trạng thái mute, không khôi phục được source audio hoặc tên người nói. Nếu sau này cần lưu nguồn riêng, phải đổi contract ingest và downstream; không tự coi stereo hay nhiều track là diarization.

## So sánh và mã nguồn tham khảo

| Lựa chọn | Bằng chứng | Quyết định |
|---|---|---|
| Chrome APIs | [Capture guide](https://developer.chrome.com/docs/extensions/how-to/web-platform/screen-capture) và [tabCapture](https://developer.chrome.com/docs/extensions/reference/api/tabCapture) có luồng service worker → offscreen; ghi trong popup dừng khi popup đóng. | Chọn cho Chrome desktop và âm thanh tab được người dùng chọn. Không hứa thu mọi system audio. |
| RecordRTC | [Source đã kiểm tra](https://github.com/muaz-khan/RecordRTC/tree/725284477c1e7432c828971f1cff09dfcd5093d4), [MIT license](https://github.com/muaz-khan/RecordRTC/blob/725284477c1e7432c828971f1cff09dfcd5093d4/LICENSE). | Wrapper có thể hữu ích cho fallback cụ thể; hiện chưa có lý do thêm lớp này trên native Chrome APIs. |
| Screenity | [Source đã kiểm tra](https://github.com/alyssaxuu/screenity/tree/0db811b6b192ab2d51950c8de42594d170add172), [GPL-3.0 license](https://github.com/alyssaxuu/screenity/blob/0db811b6b192ab2d51950c8de42594d170add172/LICENSE). | Tham khảo sản phẩm/storage; không lấy implementation vào MVP. |
| OBS/libobs | [Backend design](https://docs.obsproject.com/backend-design), [source đã kiểm tra](https://github.com/obsproject/obs-studio/tree/fe1ed7955f300efd17fc59f65938388e43d8bb06), [GPL-2.0 COPYING](https://github.com/obsproject/obs-studio/blob/fe1ed7955f300efd17fc59f65938388e43d8bb06/COPYING). | Native engine với media pipeline riêng; không phải thư viện JS nhúng trực tiếp vào extension. Desktop companion nằm ngoài phạm vi. |

Có thể adapt lifecycle skeleton từ [Google sample tại revision `657a9416588676fdd5cf22041651d0663bffaba2`](https://github.com/GoogleChrome/chrome-extensions-samples/tree/657a9416588676fdd5cf22041651d0663bffaba2/functional-samples/sample.tabcapture-recorder), giữ copyright/notice theo [Apache-2.0](https://github.com/GoogleChrome/chrome-extensions-samples/blob/657a9416588676fdd5cf22041651d0663bffaba2/LICENSE). Sample có video và gom blob trong RAM; chỉ tham khảo API, phải bỏ video, thêm mic và thay cơ chế lưu. Chưa copy hoặc vendor mã nào trong thay đổi này.

## Ownership và khởi tạo

Mức API nền của kiến trúc này là Chrome 116: stream ID từ service worker có thể dùng trong offscreen, và `runtime.getContexts` giúp tìm document hiện có. Dùng namespace `chrome.*`; kiểm chứng trên Chrome Stable thực tế, không khuyến nghị cài Chrome cũ chỉ vì đủ API. ID chỉ dùng một lần, hết hạn sau vài giây. Tạo offscreen trước khi lấy ID và tiêu thụ ngay. [tabCapture](https://developer.chrome.com/docs/extensions/reference/api/tabCapture)

Offscreen sở hữu streams, audio graph, recorder và hàng đợi lưu; service worker điều phối, UI chỉ gửi lệnh/hiển thị trạng thái. Khai báo `tabCapture` và `offscreen`; chỉ Start từ thao tác người dùng trên tab đích. Offscreen dùng HTML bundled, reason `USER_MEDIA`, chỉ có `runtime` trong các extension API. Một normal profile có một offscreen document. Không dùng reason `AUDIO_PLAYBACK` để giữ phiên ghi vì có giới hạn 30 giây không phát audio. Không có giới hạn lifetime đó với `USER_MEDIA` cũng không bảo đảm sống qua crash/sleep. [Offscreen API](https://developer.chrome.com/docs/extensions/reference/api/offscreen)

Flow mic đề xuất: lần đầu mở trang extension nhìn thấy được, người dùng bấm cấp mic qua `getUserMedia({audio:true, video:false})`; dừng track thử sau khi cấp quyền. Sau đó quay lại tab đích, Start và lấy stream ID mới. Offscreen tự mở lại mic; không chuyển `MediaStream` bằng `runtime.sendMessage`, vì messaging dùng JSON. Không coi hidden-document prompt là đã được nghiệm thu; phải thử quyền browser lẫn OS và revoke. [Chrome messaging](https://developer.chrome.com/docs/extensions/develop/concepts/messaging), [thảo luận Chromium về mic offscreen](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/V09VMCLzvWM)

Chặn double Start bằng state machine và kiểm tra owner sống. Nếu thiếu một nguồn hoặc `AudioContext` không chạy, cleanup và báo lỗi; không âm thầm ghi chỉ một phía. Service worker khởi động lại phải hỏi owner và đối chiếu trạng thái lưu bền, không bắt đầu phiên mới chỉ vì mất biến trong RAM.

## Graph âm thanh đề xuất

```text
Start → ensure offscreen → get stream ID → consume immediately

tab source ──→ monitor gain ─────────────────────→ speakers/headphones
          └──→ mono downmix → record gain/mute ─┐
mic source ──→ mono downmix → record gain/mute ─┴→ mono mix bus
                                                 → MediaStreamAudioDestination
                                                 → one audio MediaRecorder
                                                 → ordered durable chunks
                                                 → resumable upload
Stop → final blob + persistence drain → server finalize → transcript → notes
```

Tab capture cần nối lại playback để người dùng vẫn nghe cuộc họp. Không route mic tới loa. Monitor tab tách khỏi gain ghi để mute bản ghi không làm mất tiếng đang nghe. [tabCapture playback](https://developer.chrome.com/docs/extensions/reference/api/tabCapture#preserve-system-audio)

Các lựa chọn dưới đây là thiết kế cần đo bằng prototype:

- Dùng một `AudioContext`; các source được resample về sample rate của context. Ghi settings thực của track/context; không giả định đầu vào luôn cùng sample rate. Clock chung không loại bỏ input latency/drift. Dùng marker đầu/giữa/cuối để đo lệch nguồn; không tính thời lượng bằng số blob nhân timeslice. [Web Audio source/resampling](https://www.w3.org/TR/webaudio/#MediaStreamAudioSourceNode)
- Downmix mỗi nhánh về mono, để headroom trước khi cộng và đo peak/clipping lúc hai bên nói đồng thời. Gain/mute điều khiển nhánh ghi, không đổi track set của recorder giữa phiên. Không đặt ngưỡng gain “đạt” khi chưa đo.
- Không route mic ra loa chỉ tránh feedback trực tiếp, không ngăn mic thu lại âm loa ngoài. Thử tai nghe trước rồi loa ngoài; yêu cầu `echoCancellation` nếu hỗ trợ và ghi settings thực. Không cam kết AEC loại mọi echo hoặc giữ nguyên chất lượng khi nói chồng lấn. [Media Capture constraints](https://www.w3.org/TR/mediacapture-streams/#dom-mediatrackconstraintset-echocancellation)
- Mute mic trong Meet, mute tab Chrome và mute mic/tab-record của extension là các điều khiển riêng. UI phải thể hiện rõ “microphone bản ghi”; không suy mute Meet thành mute extension. Kiểm chứng file và mức âm cho từng thao tác, không chỉ kiểm icon.
- Tab audio chứa tổng âm thanh của tab, gồm nhiều người và nội dung phát/chia sẻ; mic cũng có thể thu nhiều người trong phòng. Hai nguồn không tương đương hai speaker identities.

## Archive, Stop và phục hồi

Ưu tiên `audio/webm;codecs=opus`, probe bằng `MediaRecorder.isTypeSupported`. Chỉ dùng fallback như `audio/webm` khi codec/container thực tế nằm trong capability contract backend đã kiểm chứng. Không tự nhận MIME mặc định bất kỳ rồi đợi cuối cuộc họp mới phát hiện không hỗ trợ. Lưu `recorder.mimeType` thực; constructor/start/encoder vẫn có thể lỗi dù capability probe thành công. [MediaRecorder spec](https://www.w3.org/TR/2026/WD-mediastream-recording-20260316/)

Ghi blob theo sequence/byte length/checksum vào storage có giới hạn; không gom toàn buổi trong RAM. Timeslice không phải deadline hay cam kết kích thước blob tối đa. ACK local persistence và ACK server lưu bền là hai trạng thái khác nhau. Không xóa buffer trước ACK đầy đủ của server; giữ metadata endpoint/backend đã pair để không gửi phiên cũ sang server khác khi đổi URL.

Một blob riêng lẻ không nhất thiết tự decode được; toàn bộ blob đúng thứ tự của recording hoàn tất mới phải playable. Stop phát blob cuối trước event `stop`, nhưng event không chờ async persistence handler. Phải chờ cả `stop` và hàng đợi ghi bền, sau đó đóng tracks/context/offscreen khi an toàn và finalize server. “Playable” chưa đồng nghĩa đáp ứng validation của ASR Studio. [Recording data handling và stop](https://www.w3.org/TR/2026/WD-mediastream-recording-20260316/#data-handling)

Theo dõi track `mute/unmute/ended`, tabCapture status, context state và recorder errors. Nếu một nguồn kết thúc bất ngờ, đánh dấu gián đoạn, dừng phần còn lại an toàn và giữ bytes đã lưu. Phân biệt resume upload với resume capture: không thể phục hồi âm thanh chưa thu trong sleep/crash. Quota/backpressure phải có lỗi và hành vi rõ; không âm thầm bỏ chunk.

Upload trong lúc ghi phục vụ độ bền, chưa phải ASR online. MVP chỉ xử lý sau finalize. Live tương lai có thể cần decoder liên tục hoặc nhánh AudioWorklet/PCM có sample sequence/clock riêng; không đưa mỗi WebM blob vào ASR như file độc lập. Contract upload/remux thuộc [phiên ghi](https://github.com/nguyencanhtrung/asr-studio/issues/203), còn retry/idempotency transcript và notes thuộc [xử lý sau Stop](https://github.com/nguyencanhtrung/asr-studio/issues/204).

## Tương thích backend và bằng chứng tổng hợp

Đọc backend tại revision [`52c63ed9af96544d024a4623605cac781d2288cd`](https://github.com/nguyencanhtrung/asr-studio/tree/52c63ed9af96544d024a4623605cac781d2288cd/src/asr_studio):

- `recordings.py` yêu cầu biết `size_bytes` lúc tạo upload; cần contract khác để nhận phiên chưa biết tổng kích thước.
- `media.py::probe_media` nhận WebM/Opus nhưng yêu cầu duration hợp lệ của audio stream hoặc format. `normalize_media` probe trước, chọn một audio stream rồi chuyển sang AAC/M4A. Thiếu duration có thể bị từ chối trước khi convert.
- `diarization_runner.py::_decode_waveform` yêu cầu mono 16 kHz; `runner.py` cũng decode ở 16 kHz trước ASR. Lưu nhiều track riêng không tự làm downstream dùng được chúng.

Phép thử cục bộ dùng FFmpeg `6.1.1-3ubuntu5`, sine-wave tổng hợp, không dùng microphone, Chrome hoặc model. Số liệu dưới đây lấy từ output thực của phép thử; lệnh tái lập ở ngay sau bảng.

| Mẫu | Kết quả |
|---|---|
| WebM/Opus seekable, tín hiệu 2 giây, mono 48 kHz | `ffprobe` duration `2.008000`; probe accepted; normalize accepted, duration `2.007`. |
| Cùng tín hiệu, WebM `-live 1` | Không có `format.duration`; `probe_media` trả `invalid_media` trước normalization. |
| Stream-copy remux mẫu `-live 1` thành WebM file | Probe accepted với duration `2.028`; đây là thử nghiệm riêng, chưa nằm trong app. |

Tái lập từ repo ở revision trên, Python và FFmpeg/ffprobe có trong PATH:

```sh
PYTHONPATH=src python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
from asr_studio.media import probe_media, normalize_media, MediaProcessingError

with TemporaryDirectory() as directory:
    root = Path(directory)
    for mode, options in (("seekable", []), ("live", ["-live", "1"])):
        path = root / f"{mode}.webm"
        subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-vn", "-c:a", "libopus", "-b:a", "96k", *options, str(path)
        ], check=True, timeout=30)
        try:
            print(mode, probe_media(path, max_duration_seconds=30))
            print("normalized", normalize_media(
                path, root / f"{mode}.m4a", max_duration_seconds=30))
        except MediaProcessingError as error:
            print(mode, str(error))
    remuxed = root / "remuxed.webm"
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(root / "live.webm"),
        "-map", "0:a:0", "-c:a", "copy", str(remuxed)
    ], check=True, timeout=30)
    print("remuxed", probe_media(remuxed, max_duration_seconds=30))
PY
```

Phép thử chỉ chứng minh một dạng WebM thiếu metadata bị backend hiện tại từ chối; không chứng minh Chrome tạo đúng dạng đó. Cần thử bytes MediaRecorder thực sau Stop. Ticket ingest phải chốt validation/remux có giới hạn byte, thời lượng, CPU/wall-clock và disk trước đường probe hiện tại nếu cần; không tin duration do client khai, bỏ giới hạn, hoặc coi nối blob là đủ. Các lệnh trên chỉ dùng mẫu nhỏ tin cậy, không phải implementation ingest cho dữ liệu không tin cậy.

## Ma trận prototype và bằng chứng còn thiếu

Tất cả các ca browser bên dưới **chưa chạy**. Ghi OS/hardware, Chrome version, build extension, actual MIME, track/context settings, marker, seq/byte counts, error/cleanup và hash artifact. Không suy hỗ trợ OS khác hoặc ghi dài từ unit tests backend.

| Ca thử | Điều kiện đạt cần kiểm chứng |
|---|---|
| Start/cấp mic lần đầu, deny/revoke/quyền OS, Start lặp | Chỉ bắt đầu sau thao tác người dùng; đủ hai nguồn, một owner; lỗi cleanup đầy đủ; retry không nhân đôi phiên. |
| Tab phát marker, mic có tín hiệu biết trước | Decode archive có cả hai nguồn và phần đầu/cuối; tab vẫn nghe được, mic không tự phát ra loa. |
| Mute Meet, mute tab Chrome, hai record gate riêng | Ghi lại hành vi thực; không suy trạng thái Meet thành extension mute; record gate chỉ tác động nhánh tương ứng. |
| Nói chồng lấn, tai nghe rồi loa ngoài | Đo clipping, echo/lặp, mức âm và drift; ghi giới hạn hỗ trợ nếu chưa đạt. |
| Đóng popup/chuyển tab/navigation/restart service worker | Ghi vẫn do offscreen sở hữu; trạng thái UI phục hồi đúng; không tạo recorder thứ hai. |
| Đóng target tab, rút mic, context suspend, sleep/crash | Phát hiện gián đoạn, giữ phần đã lưu, không báo buổi ghi hoàn chỉnh giả hoặc hứa bù audio chưa thu. |
| Stop với blob cuối và persistence bị làm chậm | Finalize đợi event `stop` cùng persistence queue; marker cuối còn đủ. |
| Exact finalized browser bytes → backend probe/normalize/playback | Hoặc accepted trực tiếp, hoặc remux/validation có giới hạn đã kiểm chứng; MIME fallback không được chấp nhận mù. |
| Local loopback và endpoint HTTPS tùy chọn; đổi URL khi còn buffer | Token đúng backend; không chuyển audio/ACK sang server khác; cùng bundle không rebuild. |
| Mất mạng/retry, quota/backpressure, finalize lặp | Không mất/đảo chunk, không tạo meeting/task/notes trùng; local persistence không bị coi là server ACK. |
| Ghi 1h, 2h, 4h và biên giới hạn ingest | Marker đầu/giữa/cuối, CPU/memory/disk/drift, byte integrity và finalize; số đo thuộc từng cấu hình thực, không cam kết từ spec. |

Lựa chọn capture/mix đã đủ để làm [prototype](https://github.com/nguyencanhtrung/asr-studio/issues/205); prototype và [thử độ bền](https://github.com/nguyencanhtrung/asr-studio/issues/206) vẫn mở. Nếu bằng chứng thực bác bỏ lựa chọn, điều chỉnh theo kết quả đo. Quyết định phân phối nội bộ và thiết kế auth vẫn nằm trong các ticket riêng.
