# Nghiệm thu Notes gọn — 2026-11-07

Issue [#180](https://github.com/nguyencanhtrung/asr-studio/issues/180) ghi bộ nghiệm thu đã được đồng ý và khóa trước output. Kết quả baseline **FAIL** vì thiếu ngày chuẩn hóa của một action. Bản sửa validator cho kết quả **PASS khi kiểm tra lại output đã lưu**; không gọi lại model và không thay đáp án. Đây là phép kiểm tra có giới hạn, không phải accuracy tổng quát.

## Nguồn, đáp án và điều kiện

MeetingBank `SeattleCityCouncil_06102019`: metadata 7231 giây (2h00m31s), transcript liên tục đủ 557 đoạn, 19698 từ và 7 nhãn người nói. Nguồn gốc/member/hash nằm trong `long-provenance.json`; [trang dataset](https://meetingbank.github.io/dataset/) và [bản phát hành](https://zenodo.org/records/7989108). Chọn cuộc họp gần nhất nhưng không ngắn hơn 2 giờ, tối đa 8100 giây, có ít nhất 5 mục nghị sự tham chiếu; hòa thì theo ID. Quy tắc chọn trước output nằm trong provenance.

Dùng transcript do dataset cung cấp, không tuyên bố đã nghe audio hoặc transcript được người chép duyệt. Đáp án nghiệm thu được lập từ transcript/trạng thái biểu quyết rồi kiểm tra độc lập trước inference; không coi mục nghị sự là quyết định đã được thông qua. File gốc dùng 100ns ticks: duration 72320000000 / 10000000 = 7232 giây, lệch metadata 1 giây; không dùng hệ số microsecond gây sai 10 lần. Timestamp thực được giữ. Không ghép snippet thành cuộc họp dài.

`plan.json` khóa revision sản phẩm `705f706fcddb44d83131aae12948d1163c9068c5`, hash module/source/gold, prompt `notes-hierarchical-v8`, ChatGPT `gpt-5.6-sol` / `medium`, ngôn ngữ EN cho nguồn EN và VI cho ca Việt–Anh. SHA256 plan gốc: `8e33762b69de9a0aa105b4074dc3f1beab5ddf492b23b5a767e656bfd687fe7b`. Trước output đã đối chiếu nguồn/gold, đủ 65 witness của nguồn dài và 9 kiểm tra ngắn. Ba hội thoại ngắn trong `controls/` là fixture tổng hợp, có đáp án trước output; timestamp thiết kế, không phải audio thật.

Archive ghi CC BY-NC-ND 4.0. Đợt này giữ transcript, gold nội dung và Notes dẫn xuất của MeetingBank ở artifact cục bộ; chỉ công bố provenance/hash, vị trí/ID bằng chứng và số liệu. Bảng đối chiếu đầy đủ có excerpt được lưu tại `/tmp/asr-notes-compact-20261107/assessment.json` trên máy đo. Repo không chứa bản sao nội dung cuộc họp dài. Có thể tự lấy nguồn tại Zenodo theo member/hash đã ghi để đối chiếu ID (ID = SHA256(meeting_id + ':segment:' + index)[:32], index bắt đầu từ 0).

## Bốn lượt tạo thật

Số liệu từ `generation-summary.json`; chạy provider sản phẩm trong image main đã kết nối, không giả lập.

| Ca | Đoạn nguồn | Giây Notes | Lượt gọi | Recovery | ID không tồn tại |
| --- | ---: | ---: | ---: | ---: | ---: |
| long | 557 | 115.459 | 1 | 0 | 0 |
| mixed | 10 | 20.789 | 1 | 0 | 0 |
| proposal | 6 | 12.063 | 1 | 0 | 0 |
| antecedent | 5 | 14.719 | 1 | 0 | 0 |

Tổng 4 lượt gọi mới, 0 recovery, 163.03 giây. Ca dài đạt giới hạn Notes ≤600 giây; giữ nguyên đủ đầu vào. Thời gian là Notes từ transcript, không bao gồm ASR/diarization và không chứng minh AC13 end-to-end 30 phút. Không đo lại latency bằng replay.

## Đối chiếu đã khóa

Ngưỡng nguồn dài: đủ 7 quyết định trọng yếu và ít nhất 10/12 checkpoint; kết quả 7/7 trọng yếu, 12/12 tổng. `assessment.json` ghi bảng vị trí, ID và hash excerpt hỗ trợ; dấu * là trọng yếu. Vị trí dưới đây bắt đầu từ 1.

| ID | Baseline | Vị trí output | ID nguồn hỗ trợ |
| --- | --- | --- | --- |
| L01* | PASS | document.decisions[5], document.decisions[6] | 0422f635b8b91a4df81e1f7c20218ad5, 1238041e1177c8bfda0fa20f21eb25e1, 561698cd40389b362a8cc9896a1a564d, 18bd6f8205c3c956ae0998aa4f3822e2, 092505e091b1e8955fcb43fcdda58333 |
| L02* | PASS | document.decisions[7] | 13dcb728d9232e5b452200aa76206d7e, b230c435d26c1ec6886dce8719715f3e, 22f5d224c2e51334f75e335066b0227e, 2baf69b5c59c586fb8754932da7a3e1a, 3cffa42b37c2041be277446969a43b68 |
| L03* | PASS | document.decisions[8] | 49a7b1a00036fc7793865a1cf62ade22, 709b51f0b516c00a40ad54400ec3502c, c0a58d0e1d08a1544d4fad83ce5d8161, 549d1d2e7c3d8a464968b76d48f612c7, aada58d491f4e1fab379f91ffa42de92 |
| L04* | PASS | document.decisions[9], document.decisions[10], document.decisions[11] | 053647958ca41824ed1dc626234905ab, 9f5db637f459cc0aa54010cee86fcb94, d5996e8774c69e355edb2d05df3e91ad, 9d85364ee005fcbc58bc35715933803e, 555014727e362d8a6d115e65c7e93da1, b705c7ad50b5a69bfeed3be14de3638e |
| L05* | PASS | document.decisions[15] | 545a6e082b07b8ab7e3e25268c4437ab, 11c0228231fa93c4289fbf6cb9270ecc, f7c5c3d298c66405f69f0d1bb40fd8a1, 9ef73d06022fb35de858715b99251aab, c20faa459352e4a719da259da1c2c8d5 |
| L06* | PASS | document.decisions[16] | a1d322574b1a63e791b8a763fb539d09, 5447917fe79da3a8bc977ae1a32dd130, e6871e744363841d53b0407af7433a63, 3ee4f91672d71261651ac52f5bc4845a, 74520c3e96b2c74be77f83193849e0f5, 08816ac13d590396ca0dfce582b643f3 |
| L07* | PASS | document.decisions[17] | 23308e0d3f6422080fe4bfe28bafc08a, d8ca22c1799842246f45f608baad4109, c192b0f048e932f1752e7e3be429d0f9, 3523e6c5cd510fa40a9a1a1f669967f7, e7744a86f3fb458428f1aeff6509d178 |
| L08 | PASS | document.key_topics[12] | 4155b6dbc81da9ee2a1400a9e4aff749, c225bb771bfa0fb602ff63e84574e968, 494c3bf396649134f9e44bd6bec27666, 761b3c80c2bf02687d93a56913682eb1, 36e6c56e982217748946d6b1162337d3 |
| L09 | PASS | document.key_topics[13] | ed6c8c3b2d9c81abccc2883c1c9ce015, f71762a399cd15fe31a13b4ee9346618, 69297d7e61476a64346b8d190b61f8c6, a71d9c6431635a06006f06e7da2fc00f, 438da6e95c1c92cb0c8cec653aefcef5 |
| L10 | PASS | document.key_topics[11], document.open_questions[6], document.action_items[6] | 04e05e65c44f1712bf0e392a174ae503, ff5e76df74e8cde5402dc2309ab72e9a, 112bfdd39309e6e6fa9b91627117aaba |
| L11 | PASS | document.key_topics[1] | 2e42ae131ca3fe8215f57070008a6c07, fb0d00f597e7da535be37a333751d041, e2654d4ac88b5971e2324d2d930df998, e710751d80a2c9f45eb35be2bc2746fd, 28a1abb2bfc47c3b363f755473967aa0, 726fa20bfbe149360f8b355364ac1c5d, 845f1c8ef83f11a84b928e59be02deec, 7a183a2682d530309fb1759754d06ebf, 30691c889931096b22a322c6e0232e54, 3a0487e17d20dc51768851b358a2375c |
| L12 | PASS | document.key_topics[21], document.open_questions[9] | 7c89fed4eb3141b73731247c6f1693e5, a6a6f19da48bfcff53ad5c33110d3495, 8620e5ad401b49717329f24b1eb8863c, 596afa55bf40d199bad40fabdaabdf78, 488c903768d73d3f25175b3e7c5704d2, 3ae1f38141c25a29fd9301de5975cbb4 |

Ca ngắn baseline đạt 8/9; từng check và witness tổng hợp nằm trong `assessment.json` và `controls/gold.json`. Ca mixed giữ Lan là người nhận việc, Wednesday noon thay Thursday, demo và lời đề nghị của Omar chưa thành cam kết. Ca antecedent giữ Jordan và Leo đúng vai trò; xét hợp các trích dẫn, không yêu cầu một câu chứa toàn bộ tiền ngữ. Giữ nguyên cụm deadline gốc theo hợp đồng Notes, kể cả khi thân Notes bằng tiếng Việt.

Đã kiểm tra 40 mục quyết định/action: không thấy owner/deadline bịa hoặc nâng proposal thành cam kết trong phạm vi kiểm tra. Còn 3 cảnh báo ngữ cảnh trích dẫn: tên owner có trong transcript lân cận nhưng không đủ ở riêng tập ID action. Đây là giới hạn độ tự chứa của trích dẫn, không coi là bịa người và không tuyên bố mọi bullet được hỗ trợ hoàn toàn. ID hợp lệ không tự chứng minh nội dung đúng.

## Lỗi phát hiện và bản sửa

P2/Priya: meeting_date `2026-11-10`, deadline_text `by tomorrow`, baseline `deadline_date=null`; đáp án đã khóa yêu cầu `2026-11-11`. Validator luôn tính lại deadline_date từ text và chỉ nhận whitelist chính xác, làm mất ngày dù cụm từ rõ nghĩa. Bản sửa bỏ tiền tố `by ` khi tính ngày rồi dùng whitelist cũ; giữ nguyên deadline_text. `before tomorrow`, `by Friday` và ngày tương đối thiếu meeting_date vẫn trả null.

Regression tại `tests/test_notes_document.py::test_deadlines_do_not_invent_dates` có 3 ca dương fail trên mã cũ và pass sau sửa; toàn file validator đạt 81 test (`unit-red.log`/`unit-green.log` trong scratch). Hai bản hash request trong `requests-before-fix.json` và `requests-after-fix.json` giống nhau: input, prompt, schema, model và effort không đổi. `replay-receipt.json` xác nhận revalidate cả 4 output qua validator mới chỉ đổi ngày của Priya; nguồn dài và các document còn lại giữ nguyên, số lượt model mới bằng 0. Candidate đạt 9/9 check ngắn trên output đã lưu; baseline FAIL vẫn được giữ riêng. Đây là kiểm tra validator trên document cache, không phải lượt inference mới của candidate.

## Quan hệ với các issue Notes cũ

- #77: bằng chứng trạng thái quyết định ở cuộc họp công khai và ca mixed; chưa đo lại cohort riêng tư hoặc ngưỡng 31/37.
- #78: phân biệt proposal/action, owner và hạn trên nguồn công khai/tổng hợp; chưa thay thế ngưỡng 28/38 của cohort cũ.
- #79: nguồn dài có trích dẫn và kiểm tra các quyết định/action; tái sử dụng riêng quan sát VietMed không bịa thủ thuật ở cùng source/model/code trong báo cáo công khai trước (hash ở plan). Không tính sàng lọc cũ thành accuracy, không tuyên bố bản thử v9 đã được nghiệm thu.
- #88: bổ sung nguồn họp thật dài và fixture Việt–Anh; chưa có cuộc họp Việt–Anh tự nhiên dài tương đương.
- #107: ca mixed giữ deadline cuối đã chốt, không xếp lại thành proposal; chưa đo lại cohort cũ.
- #108: ca dài giữ đủ nguồn trong một request; không kiểm chứng thiết kế chia phần chạy song song rồi hợp nhất.
- #110: chưa đo lặp ý giữa các mục; không dùng kết quả checkpoint để đóng yêu cầu dedup.
- Không tự tick ngưỡng 69/75 hoặc các yêu cầu lượt lặp/cohort riêng tư cũ bằng dataset khác.

Bộ gọn là cổng nghiệm thu được người vận hành đồng ý cho đợt này. Các tiêu chí cũ chưa đo vẫn mở; không tự đóng các issue trên bằng kết quả thay thế. Không đánh giá mọi model hoặc chấm toàn bộ từng đoạn nguồn. PR không tự merge hoặc cập nhật production.

## Kiểm tra lại

Chạy `.venv/bin/python reports/notes-compact-2026-11-07/verify.py` để kiểm tra hash, schema, regression và bất biến document của các fixture tổng hợp mà không gọi provider. Kết quả cuộc họp dài chỉ được công bố ở mức hash/receipt và bảng checkpoint; cần artifact cục bộ/nguồn upstream để tái kiểm tra toàn bộ nội dung. Không khẳng định bản repo một mình tái tạo được gold nội dung dài. Plan baseline từ chối giả định đã triển khai parser mới vào image main; replay chỉ dùng mã candidate cục bộ.
