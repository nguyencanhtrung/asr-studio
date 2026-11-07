# Notes trên transcript công khai qua ChatGPT — 2026-11-07

ChatGPT đã gọi được model sau khi người vận hành đăng nhập lại và mở usage. Đây là kiểm tra Notes bằng transcript chuẩn công khai, chưa phải nghiệm thu toàn bộ các issue chất lượng cuộc họp.

## Điều kiện đo

- Mã sản phẩm và image: xem `runtime.json`. Module của image được kiểm tra hash trước mỗi ca, theo `plan.json`.
- ChatGPT `gpt-5.6-sol`, medium, English, `notes-hierarchical-v8`; dùng `generate_notes_document` thật của image đang chạy, không giả lập provider.
- Bốn cohort chọn 27 record đầu từ VIVOS, VietMed, ViMedCSS và LibriSpeech tại các revision đã dùng cho benchmark ASR. Không khẳng định đây là đúng danh sách clip của benchmark trước. URL/revision/hash trong `plan.json`, liên kết record/ID trong `plan.json` → `cohorts`.
- Các ca bổ sung lặp lại cùng nguồn VietMed, một nguồn chỉ giữ đoạn rõ và một nguồn chỉ giữ đoạn nhiễu; danh sách và hash được khóa trước khi chạy. Không sửa transcript để khớp đầu ra.
- Nguồn và kết quả đều công khai; không sử dụng hay gửi transcript cuộc họp riêng tư. Không có credential trong artifact.
- Timestamp là chỉ số dòng quy ước; `speakers=[]`, ngày họp trống. Không dùng chúng để suy thời lượng audio, số người nói hay chất lượng cuộc họp dài.

## Kết quả kỹ thuật

Bảng số liệu được sinh từ `generation-summary.json`; đầu ra đầy đủ nằm trong `results/`.

| Ca | Đoạn | Giây | Lượt gọi | Recovery | Mục | ID không tồn tại |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| vivos-1 | 27 | 31.901 | 1 | 0 | 27 | 0 |
| vietmed-1 | 27 | 31.047 | 1 | 0 | 11 | 0 |
| vimedcss-1 | 27 | 32.757 | 1 | 0 | 27 | 0 |
| librispeech-1 | 27 | 22.914 | 1 | 0 | 26 | 0 |
| vietmed-2 | 27 | 21.228 | 1 | 0 | 13 | 0 |
| vietmed-3 | 27 | 29.903 | 1 | 0 | 12 | 0 |
| vietmed-clear-1 | 4 | 8.967 | 1 | 0 | 4 | 0 |
| vietmed-noise-1 | 1 | 7.802 | 1 | 0 | 1 | 0 |

Tổng: 8 lượt gọi, 0 recovery, 186.519 giây, 121 mục.

Mỗi đầu ra đã qua `validate_document` với đúng snapshot; mọi ID trích dẫn đều tồn tại. Kiểm tra này không chứng minh đoạn được trích hỗ trợ toàn bộ khẳng định.

## Đối chiếu nội dung

Bảng sàng lọc đã đối chiếu mọi mục với đoạn nguồn được trích; xem `screening-scores.json` và `screening-report.md`. Các nhãn trong bảng là cảnh báo thăm dò, chưa phải kết luận đã được con người duyệt. Không dùng `PASS_WITH_PARTIALS` của bảng hoặc số cảnh báo làm kết luận nghiệm thu, số lỗi đã xác nhận hay accuracy tổng hợp.

Sau khi đối chiếu trực tiếp:

- Ba output VietMed đầy đủ không có khẳng định thủ thuật “speculum examination” ở mục trích nguồn 20–24. Đây là quan sát trên đúng các output này, không chứng minh #79 đã được sửa cho mọi model hoặc mọi dữ liệu.
- LibriSpeech `key_topics[22]` và `[23]` gọi người được nói đến là “brother”; các đoạn trích tương ứng chỉ nói “he”. Từ “brother” có trong đoạn nguồn trước đó. Giữ đây là cảnh báo thiếu ngữ cảnh trích dẫn, chưa kết luận model bịa thêm người hay gán sai người.
- Loại lý do cảnh báo VietMed nói không có hỗ trợ cho việc con vật nằm trong âm đạo: nguồn `537b08949b2e7f1446f4955933d4d87c` nêu rõ vị trí đó. Phải xét hợp các đoạn được trích; không yêu cầu từng đoạn riêng lẻ chứa toàn bộ ý. Ca `vietmed-2 key_topics[9]` không có lỗi theo lý do được bảng đưa ra. Từ “initially” trong hai ca khác cần thẩm định riêng.
- Các cảnh báo về sắc thái mỉa mai, dịch thành ngữ, phần nhiễu và danh từ được suy ra cần thẩm định ngữ nghĩa. Không gộp chúng thành số lỗi bịa đã xác nhận.

Quyết định thẩm định và bằng chứng riêng nằm trong `root-disposition.json`. Giữ các issue Notes mở; đợt này xác nhận đường gọi ChatGPT hoạt động và bổ sung bằng chứng công khai, chưa có bản sửa mã được nghiệm thu.

## Gold và giới hạn nghiệm thu

`input/gold.json` là gold phụ trợ đã có từ đợt trước, được khóa trước lượt ChatGPT hiện tại. Gold từng được sửa sau khi xem đầu ra Antigravity; không phải gold blind hoặc được con người duyệt. Các số liệu độ phủ chỉ là đối chiếu thăm dò, không phải accuracy được chứng nhận. Bất đồng giữa gold và nguồn phải được ghi riêng, không sửa gold theo đầu ra mới.

Các nguồn đọc/bài giảng này hỗ trợ kiểm tra khẳng định, phần nhiễu và trích dẫn cho #79. Chúng không có đầy đủ gold cuộc họp để đóng #77, #78, #107, #108, #110 hay giải quyết yêu cầu dữ liệu đại diện #88. Đặc biệt, không thay các ngưỡng 31/37, 28/38, 69/75 hoặc yêu cầu hai lượt cho mỗi cuộc họp bằng kết quả trên các đoạn đọc.

Không có thay đổi thuật toán/prompt được triển khai trong đợt đo; nhánh thử nghiệm #79 trước đó không được tính là bản sửa đã đạt.

## Tái lập

`plan.json` khóa nguồn, trường hợp, cấu hình và hash module. `verify.py` kiểm tra lại hash và số liệu mà không gọi provider; chạy bằng môi trường Python của sản phẩm. `run_case.py` nhận tên ca, kiểm tra hash rồi gọi provider đã kết nối tại chỗ. `generate.py` chạy nối tiếp và dừng khi có lỗi; các script giữ đường dẫn scratch của đợt đo trong `/tmp`, cần cài đặt sản phẩm đúng commit và quyền sử dụng model. Không copy kho credential để tái lập.

Lưu ý tái lập: `generate.py` giả định fixture, plan và runner đã được đưa vào đúng thư mục scratch trong container; `docker cp` không dùng được với tmpfs ở máy đo này, nên dữ liệu công khai được chuyển qua stdin vào `docker exec`. Không chạy lại đợt đã có output; runner từ chối ghi đè.
