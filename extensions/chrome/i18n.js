"use strict";

// Like the app's i18n bridge, this script owns the persisted UI preference.
// Views translate text at render time; recording state stays in the worker.
const { t, getLocale, onLanguageChange } = (() => {
  const storageKey = "asr-extension-production-v1:lang";
  const readLanguage = () => {
    try { return localStorage.getItem(storageKey) === "en" ? "en" : "vi"; }
    catch { return "vi"; }
  };
  let language = readLanguage();
  const listeners = new Set();
  const textNodes = [];
  const attributes = [];
  const originalTitle = document.title;
  const EN = {
    "API không tương thích. Kiểm tra phiên bản extension và server.": "Incompatible API. Check the extension and server versions.",
    "Kiểm tra bản mới trên GitHub": "Check GitHub for updates",
    "Cần cấp quyền GitHub để kiểm tra bản mới.": "Allow access to GitHub to check for updates.",
    "Kiểm tra SHA-256, thay bundle tại thư mục đã cài rồi Reload.": "Verify SHA-256, replace the installed bundle and reload.",
    "Chưa hủy xong": "Not canceled yet",
    "Mở thiết lập để cấp quyền và thử hủy lại. Audio vẫn được giữ.": "Open setup to grant access and retry the cancel. Audio is retained.",
    "Đã đạt giới hạn lưu audio trên máy. Bản ghi được giữ lại.": "The local audio limit was reached. The recording is retained.",
    "Phiên đang được hủy; không thể lưu phần đã ghi nữa.": "The session is being canceled; the recorded part can no longer be saved.",
    "đã lưu phần audio thô": "raw audio saved",
    "Có thể đóng tiện ích; hãy bắt đầu chép lời cho bản ghi bị gián đoạn trong ASR Studio.": "You can close the extension; start transcription for the interrupted recording in ASR Studio.",
    "Bản ghi bị gián đoạn": "Interrupted recording",
    "Bản ghi bị gián đoạn: lưu hay hủy?": "Interrupted recording: save or cancel?",
    "Bản ghi này chưa có audio để lưu.": "This recording has no audio to save yet.",
    "Bản ghi này không bị gián đoạn.": "This recording was not interrupted.",
    "Bản ghi đã được hủy trên server.": "The recording was canceled on the server.",
    "Chưa lưu xong": "Not saved yet",
    "Chọn lưu phần đã ghi hoặc hủy phiên trên server.": "Save the recorded part or cancel the server session.",
    "Ghi âm dừng ngoài dự kiến. Bản ghi được giữ lại.": "Recording stopped unexpectedly. The recording is retained.",
    "Hủy phiên ghi trên server? Phần đã ghi sẽ không vào ASR Studio; audio vẫn giữ trên máy cho đến khi bạn bỏ.": "Cancel the server session? The recorded part will not reach ASR Studio; local audio stays until you discard it.",
    "Hủy phiên trên server": "Cancel server session",
    "Lưu phần đã ghi": "Save recorded part",
    "Lưu phần đã ghi để chép lời trong ASR Studio, hoặc hủy phiên trên server. Audio vẫn được giữ trên máy.": "Save the recorded part to transcribe it in ASR Studio, or cancel the server session. Audio stays on this device.",
    "Phần đã ghi đang được lưu; không thể hủy nữa.": "The recorded part is being saved; it can no longer be canceled.",
    "Server đã lưu bản ghi này.": "The server has saved this recording.",
    "Server đã lưu phần ghi trước khi gián đoạn.": "The server saved the audio recorded before the interruption.",
    "Server đã lưu phần audio thô.": "Server saved the raw audio from the partial recording.",
    "Thời lượng đã ghi (ước tính)": "Recorded duration (estimate)",
    "Khoảng {duration}": "About {duration}",
    "Tiện ích đã khởi động lại khi đang ghi. Bản ghi được giữ lại.": "The extension restarted while recording. The recording is retained.",
    "Trình ghi âm của Chrome báo lỗi. Bản ghi được giữ lại.": "Chrome's recorder reported an error. The recording is retained.",
    "chờ bạn chọn": "waiting for your choice",
    "{reason} Đã ghi khoảng {duration}.": "{reason} About {duration} was recorded.",
    "Đang gửi phần đã ghi lên server.": "Sending the recorded part to the server.",
    "Đang lưu": "Saving",
    "Đang dừng ghi": "Stopping recording",
    "Đang hoàn tất việc ghi âm.": "Finishing the recording.",
    "Đang lưu phần đã ghi": "Saving recorded part",
    "Đã lưu phần đã ghi": "Recorded part saved",
    "Đã lưu phần đã ghi trên server": "Recorded part saved on the server",
    "Chrome chưa cấp quyền cho server. Bấm Thử gửi lại để cấp quyền và tiếp tục.": "Chrome has not granted server access. Select Retry upload to grant access and continue.",
    "Audio vẫn được giữ. Mở thiết lập để xem lỗi và gửi lại.": "Audio is retained. Open setup to review the error and retry.",
    "Audio đã giữ lại nhưng chưa hoàn tất.": "Audio is retained, but the recording is incomplete.",
    "Bạn có thể đóng cửa sổ này; ghi âm vẫn tiếp tục.": "You can close this window; recording will continue.",
    "Bạn có thể đóng popup; ghi âm vẫn tiếp tục.": "You can close the popup; recording will continue.",
    "Bản ghi bị gián đoạn được giữ lại": "Interrupted recording retained",
    "Bản ghi chưa lưu đủ audio. Bản ghi được giữ lại.": "Some audio could not be saved. The recording is retained.",
    "Bản ghi cục bộ": "Local recording",
    "Bản ghi vượt quá giới hạn của server.": "The recording exceeds the server limit.",
    "Bản ghi đã lưu": "Saved recordings",
    "Client ghi âm ASR Studio · kết quả tiếp tục trong web app": "ASR Studio recording client · results continue in the web app",
    "Audio chỉ được dọn sau receipt bền vững từ đúng backend.": "Audio is cleaned only after a durable receipt from the bound backend.",
    "Bản tạm chỉ được xóa khi server xác nhận lưu đủ audio.": "Local audio is kept until the server confirms the complete recording.",
    "Bật": "On",
    "Bật âm thanh tab đang phát": "Unmute tab playback",
    "Bật/tắt âm thanh này trong bản ghi.": "Include or mute this source in the recording.",
    "Bắt đầu ghi": "Start recording",
    "Bị gián đoạn": "Interrupted",
    "Bỏ audio cục bộ": "Discard local audio",
    "Bỏ audio đang giữ trên máy? Hành động này không xóa dữ liệu server.": "Discard the retained audio and remove this recording from local history? This does not delete server data.",
    "Cho phép microphone và chọn microphone muốn dùng.": "Allow microphone access and choose a microphone.",
    "Chrome chưa cấp quyền cho server đã chọn.": "Chrome has not granted access to this server.",
    "Chrome chưa cấp quyền microphone.": "Chrome has not granted microphone access.",
    "Chrome chưa cấp quyền microphone. Mở thiết lập để cấp quyền.": "Microphone access is missing. Open setup to allow it.",
    "Chrome chưa tạo được bộ ghi nền. Hãy thử lại.": "Chrome could not start the background recorder. Try again.",
    "Chrome không cấp được quyền ghi tab này. Hãy quay lại tab cuộc họp rồi thử lại.": "Chrome could not capture this tab. Return to the meeting tab and try again.",
    "Chrome và tab nguồn phải còn mở để ghi âm tiếp tục.": "Keep Chrome and the source tab open while recording.",
    "Chưa có bản ghi cục bộ. Hoàn tất thiết lập rồi bắt đầu từ tab cuộc họp.": "No local recordings. Complete setup, then start from your meeting tab.",
    "Chưa ghép server": "No paired server",
    "Chưa gửi xong": "Upload incomplete",
    "Chưa kiểm tra quyền microphone.": "Microphone access has not been checked.",
    "Chưa sẵn sàng": "Not ready",
    "Chỉ ghi được từ tab HTTP hoặc HTTPS đang hoạt động. Không thể ghi trang Chrome hay trang của tiện ích.": "Select an HTTP or HTTPS tab to record. Chrome pages and extension pages cannot be recorded.",
    "Chỉ trang này xin quyền microphone.": "Microphone permission is requested only on this page.",
    "Có thể đóng tiện ích; kết quả tiếp tục được xử lý trong ASR Studio.": "You can close the extension; processing continues in ASR Studio.",
    "Cấp quyền microphone": "Allow microphone",
    "Cấp quyền microphone không thành công.": "Microphone access failed.",
    "Cấp quyền microphone trên trang thiết lập.": "Allow microphone access on the setup page.",
    "Cấp quyền để xem microphone": "Allow access to list microphones",
    "Cần microphone": "Microphone needed",
    "Cần thiết lập": "Setup needed",
    "Dán mã từ trang ghép nối": "Paste the code from the pairing page",
    "Audio được gửi dần trong khi ghi; dừng sẽ chờ phần cuối cùng được lưu. Đóng trang này không dừng ghi âm; Chrome và tab nguồn phải còn mở.": "Audio uploads while recording; stopping waits for the final part to be saved. Closing this page does not stop recording; keep Chrome and the source tab open.",
    "Dừng và lưu": "Stop and save",
    "Extension worker không phản hồi.": "The extension worker is not responding.",
    "Extension worker không phản hồi. Hãy mở lại tiện ích và thử lại.": "The extension worker is not responding. Reopen the extension and try again.",
    "Extension worker không phản hồi. Hãy thử lại.": "The extension worker is not responding. Try again.",
    "Ghi âm": "Recording",
    "Ghi âm cuộc họp": "Record a meeting",
    "Ghi âm · ASR Studio": "Record · ASR Studio",
    "Ghép server và cấp quyền microphone để bắt đầu.": "Pair a server and allow microphone access to start.",
    "Ghép với server": "Pair with server",
    "Hiện không có bản ghi đang chạy.": "No recording is running.",
    "Hãy cấp quyền microphone trên trang thiết lập.": "Allow microphone access on the setup page.",
    "Hãy cấp quyền microphone trước khi ghép.": "Allow microphone access before pairing.",
    "Hãy ghép extension với server trước khi ghi.": "Pair the extension with a server before recording.",
    "Hãy hoàn tất thiết lập microphone và server trước khi ghi.": "Set up the microphone and server before recording.",
    "Không kết nối được tới server.": "Cannot connect to the server.",
    "Không liên lạc được với extension worker. Hãy thử lại.": "Cannot reach the extension worker. Try again.",
    "Không thể bắt đầu ghi âm. Hãy thử lại.": "Cannot start recording. Try again.",
    "Không thể ghép lại khi đang ghi. Hãy dừng bản ghi trước; audio đã lưu vẫn được giữ lại.": "Pairing again is unavailable during an active capture. Stop the capture first; saved audio stays local.",
    "Không thể đổi server khi còn audio đang chờ xử lý.": "Resolve pending audio before changing servers.",
    "Không thực hiện được yêu cầu.": "The request failed.",
    "Không thực hiện được yêu cầu. Hãy thử lại.": "The request failed. Try again.",
    "Không tìm thấy bản ghi trên máy.": "The local recording was not found.",
    "Không tìm thấy tab đang hoạt động.": "The active tab was not found.",
    "Microphone không phát qua loa.": "Microphone audio is not played through speakers.",
    "Microphone mặc định": "Default microphone",
    "Microphone đã chọn trong thiết lập": "Microphone selected in setup",
    "Mã chỉ dùng cho lần ghép này.": "This code can only be used once.",
    "Mã ghép": "Pairing code",
    "Mở ASR Studio": "Open ASR Studio",
    "Mở Settings → Devices": "Open Settings → Devices",
    "Mở ASR Studio ở trang gốc, chọn Settings → Devices và tạo mã ghép.": "Open ASR Studio at its canonical root, select Settings → Devices, and create a pairing code.",
    "Mở ASR Studio ở trang gốc, chọn Settings → Devices và tạo mã ghép; khi khôi phục audio, chọn Pair again cho thiết bị hiện có.": "Open ASR Studio at its canonical root, select Settings → Devices, and create a pairing code; when recovering audio, choose Pair again for the existing device.",
    "Mở thiết lập": "Open setup",
    "Mức âm": "Audio levels",
    "Mức âm microphone": "Microphone level",
    "Mức âm tab": "Tab audio level",
    "Nguồn ghi": "Audio sources",
    "Nguồn âm thanh không còn hoạt động. Bản ghi được giữ lại.": "The audio source is inactive. The recording is retained.",
    "Nguồn âm thanh đã dừng. Bản ghi được giữ lại.": "The audio source stopped. The recording is retained.",
    "Nhập URL server ASR và dán mã từ trang ghép nối.": "Enter the ASR server URL and paste a pairing code.",
    "Nhập URL API ASR và dán mã từ trang ghép nối.": "Enter the ASR API URL and paste a pairing code.",
    "Phiên ghép server đã hết hạn hoặc bị thu hồi. Mở Settings → Devices trong ASR Studio và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại.": "Server access expired or was revoked. Open ASR Studio Settings → Devices and use Pair again for the existing device; retained audio stays local.",
    "Quay lại tab cuộc họp rồi bấm Bắt đầu ghi.": "Return to the meeting tab and select Start recording.",
    "Quay lại tab cuộc họp, mở popup rồi bấm": "Return to the meeting tab, open the popup, and select",
    "Sau khi ghép, quay lại tab cuộc họp và dùng popup để bắt đầu. Đóng trang này không dừng ghi âm.": "After pairing, return to your meeting tab and start from the popup. Closing this page does not stop recording.",
    "Sau khi ghép hoặc Pair again, quay lại tab cuộc họp và dùng popup để bắt đầu. Đóng trang này không dừng ghi âm.": "After pairing or Pair again, return to your meeting tab and start from the popup. Closing this page does not stop recording.",
    "Server ASR đã ghép": "Paired ASR server",
    "Server chưa xác nhận lưu đủ audio. Bản ghi được giữ lại.": "The server has not confirmed the complete audio. The recording is retained.",
    "Server chưa xác nhận đúng bản ghi. Bản ghi được giữ lại.": "The server receipt does not match this recording. Local audio is retained.",
    "Server không cho phép extension này.": "The server does not allow this extension.",
    "Server không nhận dạng được audio của bản ghi.": "The server could not read the recorded audio.",
    "Server không nhận yêu cầu này.": "The server rejected the request.",
    "Server không tìm thấy phiên của thiết bị này. Mở Settings → Devices và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại.": "The server could not find this device session. Open Settings → Devices and use Pair again for the existing device; retained audio stays local.",
    "Server phản hồi quá lâu.": "The server took too long to respond.",
    "Server trả về dữ liệu không đúng hợp đồng.": "The server returned an incompatible response.",
    "Server tạm thời không sẵn sàng.": "The server is temporarily unavailable.",
    "Server từ chối bản ghi do xung đột trạng thái.": "A state conflict prevented the server from accepting the recording.",
    "Server từ xa dùng HTTPS; server trên máy này có thể dùng HTTP với localhost hoặc 127.0.0.1.": "Remote servers require HTTPS. A server on this computer can use HTTP with localhost or 127.0.0.1.",
    "Server đã hết chỗ lưu. Bản ghi được giữ lại.": "The server is out of storage. The recording is retained.",
    "Server đã nhận đủ audio.": "The server has received the complete audio.",
    "Server đã nhận đủ audio; xử lý tiếp tục trong ASR Studio.": "Audio uploaded; processing continues in ASR Studio.",
    "Sẵn sàng": "Ready",
    "Tab hiện tại sẽ là nguồn ghi.": "The current tab will be recorded.",
    "Tab được chọn khi bắt đầu": "Tab selected when recording starts",
    "Thiết bị tin cậy không hết hạn": "Trusted device · never expires",
    "Thiết lập ghi âm · ASR Studio": "Recording setup · ASR Studio",
    "Thiết lập kết nối khác đang chạy. Hãy thử lại sau một lát.": "Another connection setup is running. Try again shortly.",
    "Thiết lập lần đầu": "First-time setup",
    "Thiết lập và trạng thái ghi âm": "Recording setup and status",
    "Thiết lập và xem trạng thái ghi âm ASR Studio.": "Set up and monitor ASR Studio recording.",
    "Thiết lập ở đây; bắt đầu ghi từ popup trên tab cuộc họp.": "Set up here, then start recording from the popup on your meeting tab.",
    "Thiếu một phần audio trên máy. Bản ghi được giữ lại.": "A local audio chunk is missing. The recording is retained.",
    "Thời lượng ghi âm": "Recording duration",
    "Thử gửi lại": "Retry upload",
    "Trang này không hỗ trợ microphone.": "This page does not support microphone access.",
    "Trạng thái ghi âm": "Recording status",
    "Tải lại": "Refresh",
    "Tắt": "Off",
    "Tắt âm thanh tab đang phát": "Mute tab playback",
    "URL server không hợp lệ.": "Invalid server URL.",
    "Yêu cầu không đến từ giao diện extension được tin cậy.": "The request did not come from a trusted extension page.",
    "cần kết nối lại": "reconnection needed",
    "lỗi xác nhận": "receipt error",
    "sẽ thử lại": "will retry",
    "Âm thanh tab": "Tab audio",
    "Âm thanh tab đang phát": "Tab playback",
    "Âm thanh đến quá nhanh. Bản ghi được giữ lại.": "Audio arrived faster than it could be saved. The recording is retained.",
    "Đang chờ Chrome hỏi quyền…": "Waiting for Chrome permission…",
    "Đang chờ Chrome hỏi quyền microphone…": "Waiting for Chrome microphone permission…",
    "Đã cấp quyền microphone.": "Microphone access granted.",
    "Đang ghi âm": "Recording",
    "Đang gửi": "Uploading",
    "Đang gửi audio trong khi ghi; bạn có thể đóng popup.": "Uploading audio while recording; you can close the popup.",
    "Đang gửi bản ghi lên server.": "Uploading the recording to the server.",
    "Đang khởi động": "Starting",
    "Đang kiểm tra trạng thái…": "Checking status…",
    "Đang lưu phần audio cuối cùng.": "Saving the final audio.",
    "Đang lưu…": "Saving…",
    "Đang dừng ghi…": "Stopping recording…",
    "Đang nối tab và microphone…": "Connecting tab audio and microphone…",
    "Đang tải": "Loading",
    "Điều khiển": "Controls",
    "Đã có một bản ghi đang chạy.": "A recording is already running.",
    "Đã cấp quyền. Audio kiểm tra đã dừng.": "Permission granted. The microphone check has stopped.",
    "Đã ghi · đang gửi": "Recorded · uploading",
    "Đã ghi": "Recorded",
    "Đã gửi xong": "Uploaded",
    "Server đã lưu phần audio thô": "Server saved the partial recording",
    "Server đã lưu audio thô": "Server saved the raw audio",
    "Server đã lưu audio thô.": "Server saved the raw audio.",
    "Xem trạng thái xử lý trong ASR Studio.": "View processing status in ASR Studio.",
    "Đã lưu đủ audio trên server": "Complete audio saved on server",
    "Đã xóa ghép nối. Hãy ghép lại với server đã chọn.": "Pairing removed. Pair with your chosen server again.",
    "Đóng trang này không dừng ghi âm.": "Closing this page does not stop recording.",
    "Đổi giao diện": "Change theme",
    "Đổi giao diện · sáng": "Change theme · light",
    "Đổi giao diện · tối": "Change theme · dark",
    "Đổi giao diện · đang theo hệ thống": "Change theme · system",
    "đang bật": "on",
    "đang gửi": "uploading",
    "đang tắt": "off",
    "đã lưu audio thô": "raw audio saved",
    "đang gửi trong khi ghi": "uploading while recording",
    "Ẩn thiết lập": "Hide setup",
    "Thiết bị tin cậy đến {date}": "Device trusted until {date}",
    "{count} phần": "{count} chunks",
    "{count} phần audio": "{count} audio chunks",
    "1 phần audio": "1 audio chunk",
    "{status} · {size} · {chunks} · {upload}": "{status} · {size} · {chunks} · {upload}",
    "Âm thanh vẫn được giữ trên máy. Hãy thử lại hoặc bỏ bản ghi.": "Audio is still retained locally. Retry or discard the recording.",
    "Bỏ audio và xóa bản ghi này khỏi lịch sử trên máy? Dữ liệu server không bị xóa.": "Discard the audio and remove this recording from local history? This does not delete server data.",
    "Server trả về dữ liệu của backend khác.": "The server returned data from another backend.",
    "Mã ghép thuộc server hoặc thiết bị khác. Mở Settings → Devices và dùng Pair again cho đúng thiết bị; audio vẫn được giữ lại.": "This pairing code belongs to another server or device. Open Settings → Devices and use Pair again for the existing device; retained audio stays local.",
    "URL web của server không hợp lệ để mở kết quả.": "The server web URL cannot be used for navigation.",
    "Link kết quả không thuộc web origin đã ghép.": "The result link is outside the paired web origin.",
    "URL server ASR": "ASR server URL",
    "giữ trên máy": "retained locally",
    "chờ dừng ghi": "waiting for recording to stop",
    "gửi thất bại": "upload failed",
    "chờ xác nhận": "awaiting receipt",
    "chưa gửi": "not uploaded"
};

  function t(text, params = {}) {
    const template = language === "en" && Object.hasOwn(EN, text) ? EN[text] : text;
    return String(template || "").replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(params, key) ? String(params[key]) : match);
  }

  function applyLanguage() {
    document.documentElement.lang = language;
    document.title = t(originalTitle);
    for (const [node, original] of textNodes) {
      if (node.isConnected) node.data = original.replace(original.trim(), () => t(original.trim().replace(/\s+/g, " ")));
    }
    for (const [element, name, original] of attributes) element.setAttribute(name, t(original));
    for (const button of document.querySelectorAll("[data-lang-toggle]")) {
      const other = language === "en" ? "vi" : "en";
      button.textContent = other.toUpperCase();
      button.lang = other;
      button.title = other === "en" ? "Switch to English" : "Chuyển sang tiếng Việt";
      button.setAttribute("aria-label", button.title);
    }
    for (const listener of listeners) listener();
  }

  function switchLanguage(value) {
    language = value === "en" ? "en" : "vi";
    try { localStorage.setItem(storageKey, language); } catch { /* Keep this page usable without storage. */ }
    applyLanguage();
  }

  document.documentElement.lang = language;
  document.addEventListener("readystatechange", () => {
    if (document.readyState !== "interactive") return;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.closest("script, style") && node.data.trim()) textNodes.push([node, node.data]);
    }
    for (const name of ["aria-label", "title", "placeholder", "content"]) {
      for (const element of document.querySelectorAll(`[${name}]`)) attributes.push([element, name, element.getAttribute(name)]);
    }
    for (const button of document.querySelectorAll("[data-lang-toggle]")) {
      button.addEventListener("click", () => switchLanguage(language === "en" ? "vi" : "en"));
    }
    applyLanguage();
  });
  window.addEventListener("storage", (event) => {
    if (event.storageArea !== localStorage || (event.key !== storageKey && event.key !== null)) return;
    language = readLanguage();
    applyLanguage();
  });
  return {
    t,
    getLocale: () => language === "en" ? "en-US" : "vi-VN",
    onLanguageChange: (listener) => { listeners.add(listener); return () => listeners.delete(listener); }
  };
})();
