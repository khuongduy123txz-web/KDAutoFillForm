# KDautoform

KDautoform phục vụ học sinh, sinh viên và giảng viên kiểm thử biểu mẫu, minh họa cách phân bổ câu trả lời và thực hành thống kê bằng **dữ liệu mô phỏng**. **Google Forms là chức năng chính**, giữ đọc toàn bộ các trang, phân nhánh, điều chỉnh tỷ lệ từng câu và hồ sơ giả tiếng Việt. Công cụ còn hỗ trợ Microsoft Forms công khai, form HTML tiêu chuẩn ở chế độ thử nghiệm, lịch chạy tùy chỉnh và xuất dữ liệu SPSS/CSV.

## Chạy trên máy Windows

Dùng Python 3.11 trở lên (64-bit).

```powershell
cd C:\Users\DELL\Desktop\toolggform
python google_form_tool.py --setup
python google_form_tool.py --web
```

Mở http://127.0.0.1:8765 để vào thẳng trang chủ và dùng miễn phí 10 lượt gửi, không cần key. Dùng `--web --check` để kiểm tra mọi trang mà chưa gửi phản hồi. Khi cần mở khóa không giới hạn lượt, chủ công cụ tạo key bằng `python google_form_tool.py --key-generate`.

Giữ các file `google_form_tool.py`, `google_fast.py`, `form_scanning.py`, `work_queue.py`, `microsoft_forms.py`, `html_forms.py`, `spss_export.py`, `run_scheduling.py`, `teaching_tools.py` và `requirements.txt` cùng thư mục. `--setup` cài các thư viện trong requirements cùng Chromium; không chỉ sao chép riêng file Python chính sang máy khác.

## Chọn loại biểu mẫu

Trên web có ba lựa chọn: **Google Forms**, **Microsoft Forms** và **Form khác / HTML**. Chọn đúng loại trước khi dán link. Các loại được hỗ trợ dùng chung cấu hình tỷ lệ, nút ngẫu nhiên, hồ sơ tiếng Việt, điền thử, tiến độ, hạn mức miễn phí, key unlimited và xuất SPSS.

| Loại | Phạm vi hỗ trợ |
| --- | --- |
| **Google Forms — chức năng chính** | Giữ các tính năng có sẵn: câu ngắn, đoạn văn, một/nhiều lựa chọn, danh sách, thang điểm, lưới, ngày/giờ; toàn bộ trang và nhánh được hỗ trợ; kiểm tra cấu trúc thay đổi; xác nhận kết quả trước khi ghi nhận thành công. |
| **Microsoft Forms** | Link trả lời công khai trên miền Microsoft Forms; đọc toàn bộ câu hỏi và phần/trang; hỗ trợ câu chữ, chọn một/nhiều, danh sách, rating, NPS, ngày và bảng Likert đã được nhận diện. Form yêu cầu tài khoản, tải tệp, CAPTCHA hoặc kiểu/quy tắc chưa hỗ trợ sẽ báo dừng. |
| **Form khác / HTML — thử nghiệm** | Form HTML chuẩn một trang, dùng điều khiển nhập liệu gốc của trình duyệt và gửi POST. Không bảo đảm chạy trên mọi website khảo sát; các website có giao diện hoặc quy trình riêng cần bộ kết nối tương ứng. |

SPSS là phần mềm phân tích dữ liệu. Một “form dùng để chạy SPSS” có thể được tạo bằng Google Forms, Microsoft Forms hoặc website khác; chọn theo **địa chỉ và loại form thực tế**, sau đó tải `.sav`. File `.sav` không phải đường dẫn form có thể điền.

Chế độ HTML thử nghiệm không chạy form đăng nhập/mật khẩu, thanh toán, tải tệp, CAPTCHA, widget tùy biến hoặc luồng nhiều trang. HTML beta không tự theo chuyển hướng: nếu gặp chuyển hướng, mở trang bằng trình duyệt rồi dán URL cuối cùng. Công cụ dừng khi không xác định chắc chắn câu hỏi, quy tắc hay kết quả gửi; không tự bỏ qua các câu chưa hỗ trợ.

### Chạy bằng Terminal

Mặc định `--provider auto` tự chọn Google Forms hoặc Microsoft Forms theo link. Với website khác, cần chọn rõ chế độ HTML thử nghiệm:

```powershell
python google_form_tool.py --url "LINK_GOOGLE_FORM" --provider google --count 10 --headless
python google_form_tool.py --url "LINK_MICROSOFT_FORM" --provider microsoft --count 1 --check --headless
python google_form_tool.py --url "https://website-cua-ban.example/khao-sat" --provider html --count 1 --check --headless
```

Thay các địa chỉ minh họa bằng link form bạn quản lý. `--check` điền thử và dừng trước nút Gửi; bỏ `--check` khi đã kiểm tra xong và muốn gửi thật. `--provider auto` không tự nhận mọi URL lạ là form HTML.

## Tốc độ chạy và tạm dừng

Có hai thiết lập riêng: **cách xử lý từng form** và **khoảng cách giữa các lượt**. Bản 9.3 bổ sung cách tính khoảng cách, chế độ không chờ thêm và các số đo thực tế.

### Cách điền Google Forms

- **Nhanh** là mặc định cho Google Forms: gom thao tác điền trên mỗi trang để giảm số lần trao đổi giữa Python và trình duyệt, gồm cả danh sách chọn chuẩn và dropdown ARIA đã xác minh. Các ô chưa xử lý được bằng đường nhanh sẽ dùng cách điền tương thích.
- **Tương thích** dùng cách điền từng câu như trước, phù hợp khi cần kiểm tra một form có giao diện hoặc điều khiển khác thường.
- Cả hai cách vẫn kiểm tra cấu trúc, đi qua đủ các trang/nhánh cần thiết và chỉ ghi nhận thành công khi dịch vụ xác nhận. Microsoft Forms và HTML giữ bộ xử lý riêng hiện có.

**Không thể bảo đảm 2 giây cho mọi form Google 5 trang hoặc mọi loại câu hỏi.** Tổng thời gian còn gồm tải trang, số lượng và loại câu hỏi, chuyển trang, mạng và chờ Google xác nhận. Đặt nhịp 2 giây không cắt ngắn các bước cần thiết này.

Sau khi dán link, dùng **Điền thử** để kiểm tra đủ các trang trước. Theo dõi thời gian xử lý thực đo của lượt chạy để đánh giá tốc độ trên chính form và máy chủ của bạn.

Trong **Tốc độ, thời gian chạy & tái tạo dữ liệu**, chọn **Cách điền Google Forms → Nhanh**. Muốn chạy nhanh nhất có thể, chọn **Tốc độ yêu cầu → Tối đa · không chờ thêm** (0 giây). Công cụ vẫn điền và xác minh đủ các trang trước khi chạy lượt tiếp theo. Kết quả hiển thị thời gian xử lý lượt vừa xong và trung bình, gồm tạo/đóng phiên, kiểm tra hạn mức và xác nhận; không tính thời gian xếp hàng/chờ lịch. Lần điền bị tạm dừng giữa chừng được làm lại với cùng đáp án, nên số đo lượt thành công chỉ tính lần điền lại đó. Các dòng **Hai lần bắt đầu** và **Hai xác nhận** là khoảng cách thực tế đo giữa hai lượt hoàn tất xử lý liên tiếp, chỉ có từ lượt thứ hai. **Xử lý vượt nhịp** là phần thời gian xử lý lớn hơn nhịp đặt; **Chờ quá lịch** là thời gian được xử lý trễ hơn mốc đủ điều kiện (ví dụ đang chia lượt cho người khác). Rê chuột vào dòng thời gian để xem các pha tải, điền, chuyển trang, xác nhận và số ô dùng đường nhanh/tương thích. Nhật ký JSONL lưu sự kiện `performance` với đơn vị pha là mili giây; gói ZIP lưu lựa chọn `engine` trong metadata.

Dùng Terminal để kiểm tra đường nhanh, dừng trước nút Gửi:

```powershell
python google_form_tool.py --url "LINK_FORM_CUA_BAN" --check --headless --engine fast
```

Đổi sang `--engine compatible` để đối chiếu. Tham số CLI `--delay` vẫn là số giây nghỉ **sau** mỗi lượt; thiết lập nhịp bắt đầu 1 phút/form ở phần web.

### Kết quả đo hiệu năng

Form mô phỏng 4 trang, 40 câu chữ/trắc nghiệm, chạy trong Docker với 1 CPU và RAM 512 MB:

| Phép đo | Tương thích | Nhanh |
| --- | ---: | ---: |
| Tổng chuẩn bị và xác nhận | 3,506 giây | 0,608 giây |
| Riêng thao tác điền | 2,835 giây | 0,084 giây |

Form mô phỏng **5 trang/20 câu** gồm chữ, dropdown chuẩn, dropdown ARIA và checkbox mất **0,566 giây**, đi đủ 5 trang, kiểm tra chính xác 20 đáp án và đúng một yêu cầu gửi mô phỏng. Đây là phép đo chi phí xử lý trên máy chủ, không có độ trễ Google.

Đây là phản hồi mạng được mô phỏng cục bộ để so sánh chi phí xử lý, không phải tốc độ gửi đến Google. Cả hai chế độ đi đủ 4 trang, giữ cùng 40 đáp án và chỉ ghi nhận sau xác nhận.

Trên form Google thực tế đã cung cấp (5 trang, 20 câu), các lần **điền thử, chưa gửi** từng đo được khoảng **5,86–10,58 giây**. Lần 10,58 giây gồm tải 4,37 giây, điền 1,00 giây, chuyển trang 4,60 giây, cả 20 ô dùng đường nhanh. Đây là số đo có mạng thực, khác bài mô phỏng; độ trễ tải/chuyển trang vẫn là phần lớn thời gian.

Bản 9.3 tái sử dụng tối đa 4 kết nối PostgreSQL, giữ giao dịch/quota nguyên tử và không tự chạy lại khi commit chưa rõ kết quả. Phép đo chỉ đọc `SELECT 1` trên CSDL đang cấu hình: mở kết nối mới trung bình 439 ms; dùng lại kết nối 129 ms (mẫu 3 và 7 lần sau khi làm nóng). Đây là chi phí truy vấn/kết nối, không phải tổng thời gian điền form.

Một lượt điền thử tiếp theo không dùng cache tự viết đo được 5,86 giây (tải 1,09 giây, điền 1,26 giây, chuyển trang 2,83 giây). Các con số biến động theo mạng nên không dùng chúng để khẳng định mức tăng tốc cố định. Bản phát hành giữ cơ chế tải của Chromium; HTML, cookie và mã phiên của mỗi phản hồi vẫn được tạo mới.


### Chọn tốc độ trên web

Trong phần tốc độ trên web, chọn một cấu hình nhanh hoặc đặt khoảng cách riêng:

| Cấu hình | Cách hoạt động |
| --- | --- |
| Tối đa · không chờ thêm | Đặt 0 giây, không thêm khoảng chờ; vẫn chờ form hiện tại hoàn tất và chờ vị trí trong hàng đợi. |
| Nhịp 2 giây | Khoảng cách bắt đầu tối thiểu giữa hai lượt là 2 giây; đây là cấu hình mặc định. Không đồng nghĩa thời gian điền chắc chắn là 2 giây. |
| Mỗi 3 giây | Cố định 3 giây giữa các lần bắt đầu. |
| Mỗi 30 giây | Cố định 30 giây giữa các lần bắt đầu. |
| Mỗi 1 phút | Cố định 60 giây giữa các lần bắt đầu. |
| Ngẫu nhiên 30 giây–3 phút | Mỗi lượt tiếp theo lấy một khoảng từ 30 đến 180 giây. |
| Mỗi 3 phút | Cố định 180 giây giữa các lần bắt đầu. |
| Tùy chỉnh | Cố định hoặc ngẫu nhiên trong khoảng 0–86.400 giây. |

**Cách tính khoảng cách** có hai lựa chọn:

- **Giữa hai lần bắt đầu** (mặc định): đặt 60 giây và form mất 8 giây thì chờ khoảng 52 giây còn lại.
- **Nghỉ sau khi hoàn tất**: đặt 60 giây và form mất 8 giây thì chờ thêm đủ 60 giây; khoảng cách bắt đầu khoảng 68 giây, cộng thời gian xếp hàng.

Để dùng: dán link → chọn số lượt/tỷ lệ → chọn **Tốc độ yêu cầu** và **Cách tính khoảng cách** → bấm **Bắt đầu**. Muốn khoảng ngẫu nhiên, chọn **Tùy chỉnh · trong khoảng**, nhập Từ/Đến (ví dụ 30–180 giây). Các lựa chọn được giữ khi F5 và bị khóa chỉnh trong lúc chạy; tạm dừng/dừng vẫn dùng được.

 Mỗi người dùng chạy lần lượt; lượt đầu bắt đầu khi đến vị trí trong hàng đợi: nếu một form mất lâu hơn khoảng cách đã đặt, lượt kế tiếp chờ form đó hoàn tất. Không dồn các lượt của cùng một đợt để bù thời gian bị trễ. Các người dùng khác được chia lượt xử lý theo hàng đợi. Trước khi có số đo, giao diện chỉ hiển thị tổng khoảng chờ/nhịp tối thiểu, không gọi đó là tổng thời gian hoàn thành. Sau một lượt, ước tính dùng thời gian xử lý trung bình vừa đo và không tính thời gian chờ người khác.

Ví dụ muốn nhịp **1 phút/form**, chọn cấu hình **1 phút / 60 giây**. Muốn “30 giây–3 phút mới có một phản hồi mới”, chọn cấu hình ngẫu nhiên 30–180 giây. Đây là nhịp bắt đầu lượt; không bảo đảm biểu mẫu dài sẽ hoàn thành trong 30 giây.

- **Tạm dừng** đóng phiên điền chưa gửi để nhường tài nguyên cho người khác. Khi tiếp tục, công cụ điền lại phản hồi đó với cùng đáp án, seed và hồ sơ; lượt chưa gửi được hoàn ngay nếu đã giữ chỗ. Một yêu cầu gửi đã bắt đầu vẫn có thể hoàn tất trước khi tạm dừng có hiệu lực.
- **Tiếp tục** đưa đợt chạy trở lại hàng đợi. Nếu đang chờ đến lượt sau, phần thời gian chờ còn lại được giữ khi tạm dừng.
- **Dừng** kết thúc đợt chạy. Các phản hồi đã được xác nhận vẫn xem và tải được.
- Đọc form, chờ theo lịch hoặc tạm dừng không tự trừ lượt miễn phí. Hạn mức chỉ giảm ngay trước thao tác gửi như trước.

Đợt chạy và lịch hiện tại nằm trong RAM của máy chủ. Render Free có thể ngủ hoặc khởi động lại; công cụ không bảo đảm lịch chạy dài hay chạy qua đêm sẽ tự tiếp tục sau restart. Giữ nhật ký và kiểm tra phản hồi đã lưu trước khi bắt đầu lại.

## Nhiều người dùng cùng lúc

Mỗi người có phiên, form, cấu hình tỷ lệ, hàng đợi và kết quả riêng. Người A đang điền không chặn người B đọc Google Forms và tạo đợt chạy. Mỗi trình duyệt/phiên được giữ một đợt đang hoạt động để tránh bấm trùng; số người truy cập không bị khóa bởi một bộ đếm người dùng chung.

Công việc điền được chia theo **từng phản hồi**: A1 → B1 → A2 khi cả A và B đang chờ. Lượt theo lịch 1 phút hoặc đang tạm dừng nhường tài nguyên cho người khác. Giao diện hiển thị **Đang xếp hàng** và vị trí; đợi/hủy hàng không trừ lượt miễn phí. Phản hồi chưa được máy chủ nguồn xác nhận không được tự gửi lại.

Google Forms được đọc bằng GET lấy metadata, không cần mở Chromium chỉ để xem câu hỏi. Microsoft Forms và HTML dùng hàng đợi trình duyệt riêng cùng các lượt điền, giữ bộ xử lý hiện có. Trên Render Free, cấu hình mặc định:

| Biến môi trường | Mặc định | Ý nghĩa |
| --- | ---: | --- |
| `KD_BROWSER_WORKERS` | 1 | Số tác vụ trình duyệt đồng thời, dùng chung cho toàn máy chủ. |
| `KD_SCAN_WORKERS` | 2 | Số tác vụ đọc metadata Google đồng thời. |

Hai biến nằm trong `render.yaml`; có thể đặt trong Render → **Environment**. Với máy chủ có nhiều RAM hơn, tăng `KD_BROWSER_WORKERS` lên 2 rồi theo dõi RAM/CPU. Cấu hình nhận 1–10 worker; tăng số này không tự cấp thêm tài nguyên. Chromium được tái sử dụng theo worker, còn context/cookie của mỗi phản hồi được tạo mới. Số worker không tăng theo số người chờ.

### Tối đa 10 người xử lý đồng thời

Đặt `KD_BROWSER_WORKERS=10` trên máy chủ đủ tài nguyên. Mỗi người có tối đa một đợt đang hoạt động; mười worker xử lý tối đa mười phản hồi đồng thời. Người thứ 11 trở đi chờ FIFO (đến trước được xử lý trước), tự vào khi có chỗ trống. Hủy/chờ không trừ lượt miễn phí; sau mỗi phản hồi, đợt dài nhường lượt cho các yêu cầu khác đang chờ.

Ví dụ chạy trên máy cá nhân có đủ RAM:

```powershell
cd C:\Users\DELL\Desktop\toolggform
$env:KD_BROWSER_WORKERS="10"
python google_form_tool.py --web
```

Đây là web trên máy cá nhân; để người ngoài mạng sử dụng cần thêm endpoint/tunnel công khai và máy phải luôn bật. Trên Render, chỉ đổi Environment sang 10 sau khi đã có máy chủ đủ RAM. `render.yaml` hiện vẫn giữ 1 worker cho gói Free.

Phép đo tài nguyên với 10 Chromium độc lập và form HTML mô phỏng 40 ô đã dùng khoảng **1.975 MiB RAM**, ngay cả khi không tải Google qua mạng. Bởi vậy gói Free 512 MB không phù hợp cho 10 worker. Máy chủ một instance khoảng 4 GB RAM/2 CPU là điểm bắt đầu để kiểm thử tải thực tế; nhu cầu thật phụ thuộc form. Không tự đổi sang gói trả phí chỉ bằng thay biến worker.

Bài kiểm tra Chromium thực tế trên Docker 4 GB/2 CPU đã chuẩn bị đồng thời 10 form, tự đưa người 11/12 vào FIFO, đi đủ 4 trang/40 câu cho cả 12 phản hồi với dữ liệu POST mô phỏng chính xác. RAM cao nhất khoảng **1.835 MiB**; đây là fixture mạng mô phỏng, không phải bảo đảm tốc độ/độ ổn định của mọi form Google thật.

Kiểm tra logic 10 người và thứ tự người 11/12:

```powershell
python -B -m unittest discover -s tests -p test_ten_users.py -v
```

Bài kiểm tra 10 Chromium thật yêu cầu máy đủ RAM và mặc định được bỏ qua trong bộ kiểm tra 512 MB. Bật riêng trên máy có ít nhất khoảng 4 GB dành cho tiến trình kiểm thử:

```powershell
$env:KD_TEN_BROWSER_TESTS="1"
python -B -m unittest discover -s tests -p test_ten_users_browser.py -v
```

**Không có máy chủ miễn phí phục vụ vô hạn người cùng thời điểm.** Hàng đợi giúp nhiều người cùng sử dụng mà không bị lỗi khóa chung; khi tải cao, thời gian chờ tăng. [Render Free chỉ có một instance và không hỗ trợ mở rộng nhiều instance](https://render.com/docs/free); [autoscaling cần gói và tài nguyên phù hợp](https://render.com/docs/scaling). Bản hiện tại dùng một tiến trình ứng dụng; không tăng số replica/WEB_CONCURRENCY vì phiên và hàng đợi chưa được chia sẻ giữa nhiều tiến trình.

Kết quả đợt hoàn tất được giữ tối đa 24 giờ; form đã đọc không còn dùng bởi đợt hoạt động được giữ khoảng 1 giờ, dọn khi có lần đọc mới. Hàng đợi, tiến độ và nhật ký tạm vẫn có thể mất khi Render ngủ/restart/deploy. Key và hạn mức miễn phí tiếp tục được lưu trong PostgreSQL. Tải kết quả trước khi khởi động lại dịch vụ.

## Công cụ thực hành thống kê

Sau khi đọc form, có thể chọn một mẫu phân bổ nhanh:

- **Cân bằng:** chia đều các đáp án của những câu trắc nghiệm phù hợp.
- **Trung lập:** tập trung hơn vào mức giữa của thang điểm/đồng ý đã nhận diện.
- **Đồng ý nhẹ:** dịch xác suất vừa phải về phía mức đồng ý hoặc điểm cao.

Các mẫu giữ cấu hình câu chữ, thông tin nhân khẩu học và câu chọn nhiều; công cụ chỉ chỉnh những câu phù hợp với mẫu đó. Bạn vẫn sửa từng tỷ lệ, dùng nút ngẫu nhiên và hoàn tác như trước. Mẫu phân bổ là cấu hình để thực hành, không phải kết quả kiểm định Cronbach's Alpha, EFA hay ý nghĩa thống kê được bảo đảm.

Ô **seed** giúp tái tạo việc sinh câu trả lời: để trống để lấy seed ngẫu nhiên, hoặc nhập số nguyên từ `0` đến `9007199254740991`. Cùng seed, cấu trúc form, cấu hình và ngày tham chiếu giúp tạo lại cùng bộ câu trả lời mô phỏng. Seed không cố định thời điểm mạng, dấu thời gian của dịch vụ hoặc trạng thái CAPTCHA; form đã thay đổi cũng có thể cho kết quả khác.

**Ngày tham chiếu** là ngày dùng để tạo hồ sơ, tính tuổi và chọn các giá trị ngày liên quan. Mặc định dùng ngày hiện tại theo trình duyệt; có thể chọn lại theo định dạng `YYYY-MM-DD` để tái tạo một buổi thực hành trước. Seed và ngày tham chiếu được giữ trong nhật ký và metadata của gói ZIP. Khi muốn so sánh hai lần chạy, giữ cả hai giá trị cùng cấu hình và cấu trúc form.

Báo cáo kết quả mô tả **các lượt gửi đã được xác nhận**, gồm tần số và tỷ lệ đáp án, số quan sát hợp lệ và số bỏ qua do phân nhánh. Trung bình và độ lệch chuẩn mẫu chỉ hiển thị với thang điểm hoặc nhãn mức độ có thể mã hóa rõ ràng; không tự gán ý nghĩa số cho các nhóm như trường học hay giới tính. Câu chọn nhiều có thể có tổng tỷ lệ đáp án vượt 100%. Trung bình/SD dùng đúng bảng mã CSV/SPSS, kể cả khi đáp án hiển thị theo thứ tự đảo; mở tần suất từng câu để xem mã tương ứng.

### Tải bộ dữ liệu CSV cho bài thực hành

Trong phần kết quả, tải gói ZIP để mở bằng Excel hoặc phần mềm thống kê:

| File trong ZIP | Nội dung |
| --- | --- |
| `responses.csv` | Mỗi hàng là một lượt gửi đã xác nhận, mã hóa đáp án giống file SPSS. |
| `codebook.csv` | Tên biến, nhãn câu hỏi, kiểu biến và mã/nhãn đáp án. |
| `summary.json` | Thống kê mô tả tại thời điểm xuất. |
| `metadata.json` | Nguồn form, cấu hình phiên chạy và cờ `synthetic: true`. |
| `README.txt` | Giải thích dữ liệu mô phỏng, quy tắc mã hóa và giá trị thiếu. |

CSV dùng UTF-8 BOM để giữ tiếng Việt khi mở trong Excel. Câu bị bỏ qua theo nhánh được để thiếu; các lượt chưa xác nhận, lượt điền thử và xác nhận trùng không trở thành hàng dữ liệu. Văn bản có thể bị Excel hiểu là công thức được thêm dấu nháy đơn khi xuất. Tải gói dữ liệu không trừ thêm lượt và không thay đổi hiệu lực key unlimited.

## Các tối ưu trong bản này

- Google Forms có đường điền nhanh theo từng trang, giảm thao tác lặp với trình duyệt; các điều khiển chưa hỗ trợ vẫn quay về cách điền từng câu. Không gửi thẳng dữ liệu để bỏ qua các trang hay bỏ kiểm tra kết quả.
- Giảm truy vấn đọc hạn mức lặp lại trong lúc điền từng câu bằng bộ nhớ đệm tối đa 2.048 mục, hết hạn sau 1 giây. Kiểm tra hạn mức mới được thực hiện khi bắt đầu đợt; việc trừ lượt vẫn là một câu SQL nguyên tử, bảo đảm không gửi vượt 10 lượt miễn phí dù dữ liệu hiển thị tạm thời còn cũ.
- Gộp thông tin quyền sử dụng vào phản hồi tiến độ, giảm một lượt HTTP cho mỗi lần cập nhật. Giao diện cập nhật khoảng 2 giây khi chạy, 5 giây khi chờ và 15 giây khi tab bị ẩn; lỗi mạng tạm thời được thử lại.
- Chỉ gửi bản chụp tiến độ gọn cho giao diện. Cấu hình đầy đủ, báo cáo và dữ liệu xuất được xử lý qua thao tác tương ứng.
- Giữ cách tách phiên trình duyệt cho từng phản hồi và kiểm tra kết quả gửi; tối ưu không bỏ qua câu hỏi hoặc xác nhận để chạy nhanh hơn.

Phép đo cục bộ với SQLite trên máy phát triển: **100 lần kiểm tra quyền khách giảm từ 100 kết nối/79,91 ms xuống 1 kết nối/0,74 ms**. Một lần trừ lượt giảm từ 2 kết nối xuống 1. Đây là phép đo riêng phần truy cập hạn mức, không có nghĩa toàn bộ việc điền form thực tế nhanh hơn 100 lần; độ dài form, máy chủ dịch vụ và mạng vẫn chi phối thời gian chạy.

## Xuất dữ liệu sang SPSS

Trên web, sau khi có lượt gửi được xác nhận, dùng nút **Tải SPSS (.sav)** trong phần kết quả. File có thể chứa các lượt đã xác nhận tại thời điểm tải; các lượt đang điền hoặc chưa rõ kết quả không được thêm vào.

Chạy một đợt hữu hạn từ Terminal và xuất khi hoàn tất:

```powershell
python google_form_tool.py --url "LINK_FORM_CUA_BAN" --count 10 --headless --spss-out ket_qua.sav
```

Nếu là form HTML, thêm `--provider html`. Sau đó mở SPSS, chọn **File > Open > Data**, tìm `ket_qua.sav`. Trong **Variable View**, xem tên biến, nhãn câu hỏi và nhãn giá trị; trong **Data View**, mỗi hàng là một lượt đã được xác nhận.

- Câu một lựa chọn được mã hóa số kèm nhãn đáp án; thang điểm có nhãn số giữ giá trị gốc, kể cả điểm `0`.
- Câu nhiều lựa chọn được tách thành các biến `0` = không chọn, `1` = đã chọn. Mỗi hàng lưới có biến riêng và nhãn chứa tên hàng.
- Văn bản tiếng Việt được giữ nguyên. Ngày/giờ được xuất thành chuỗi ISO để giữ đúng giá trị; có thể chuyển kiểu ngày trong SPSS nếu cần tính toán theo ngày.
- Câu hoặc trang bị bỏ qua do phân nhánh được để thiếu. Không đưa đáp án đã sinh sẵn cho nhánh không đi qua vào dữ liệu.
- Điền thử, lần gửi chưa xác nhận và bản ghi xác nhận trùng không trở thành phản hồi trong file. File có ghi chú **dữ liệu mô phỏng**, không phải phản hồi thu thập từ người tham gia thật.
- Một lần xuất tối đa 50.000 phản hồi, 2 triệu ô dữ liệu, 32 MiB văn bản và nhật ký 200 MiB. Giới hạn này bảo vệ RAM máy chủ; không giới hạn số lượt của key unlimited. Với đợt lớn, chia thành các đợt nhỏ để xuất.

Giữ file `.sav` và nhật ký đã tải trước khi Render restart/deploy. SPSS xuất từ nhật ký trong phiên, không tự lưu file kết quả lâu dài vào PostgreSQL.

## Đưa lên GitHub

Repository đã có remote `origin` và nhánh `main`:

```powershell
cd C:\Users\DELL\Desktop\toolggform
git status --short
git add .gitignore .dockerignore .env.example Dockerfile requirements.txt render.yaml README.md google_form_tool.py google_fast.py form_scanning.py work_queue.py microsoft_forms.py html_forms.py spss_export.py run_scheduling.py teaching_tools.py tests/test_deployment.py tests/test_keys.py tests/test_google_regression.py tests/test_google_speed.py tests/test_form_scanning.py tests/test_work_queue.py tests/test_multiuser.py tests/test_multiuser_browser.py tests/test_ten_users.py tests/test_ten_users_browser.py tests/test_microsoft_forms.py tests/test_html_forms.py tests/test_spss_export.py tests/test_provider_exports.py tests/test_performance.py tests/test_run_scheduling.py tests/test_teaching_tools.py tests/test_workflow.py
git commit -m "Support isolated multi-user scans and fair response queues"
git push origin main
```

Repository: https://github.com/khuongduy123txz-web/KDAutoFillForm

Nếu Git báo `rejected (fetch first)`, chạy `git pull --rebase origin main` rồi đẩy lại; nếu có xung đột, giải quyết trước khi tiếp tục. Không dùng `--force`.

## Triển khai miễn phí trên Render

Trước khi deploy, cấu hình kho PostgreSQL theo mục **Kho key bền vững cho Render** bên dưới. Cách khuyến nghị: dùng Blueprint để Render đọc sẵn `render.yaml`.

1. Truy cập https://dashboard.render.com và đăng nhập bằng GitHub.
2. Chọn **New > Blueprint**, kết nối repository **KDAutoFillForm**. Nếu chưa thấy repo, cấp quyền cho Render truy cập repo đó trong phần cài đặt GitHub của Render.
3. Chọn nhánh **main**, file Blueprint **render.yaml** ở thư mục gốc. Đặt tên Blueprint, ví dụ **KDautoform**.
4. Xem lại service **kdautoform**, runtime **Docker**, compute plan **Free**. Chỉ tiếp tục khi màn hình thể hiện gói miễn phí; không thêm database hoặc dịch vụ trả phí.
5. Nhập chuỗi PostgreSQL vào ô **KD_DATABASE_URL**, rồi chọn **Deploy Blueprint** hoặc **Apply** theo giao diện hiện tại. Render cài Python, Playwright, Chromium và các thư viện Linux từ Dockerfile.
6. Khi service chuyển **Live**, mở URL `https://<ten-service>.onrender.com` được Render cấp. URL có thể có hậu tố để tránh trùng tên.
7. Mở `https://<ten-service>.onrender.com/healthz`: phải thấy JSON có `"ok": true`.
8. Vào trang chính để dùng 10 lượt miễn phí, không cần key. Lần đầu thử hãy tắt **Tự chạy sau khi đọc form**, dùng form thử nghiệm của bạn và bấm **Điền thử** để kiểm tra đầy đủ các trang trước khi gửi thật.

Nếu đã tạo Web Service bằng tay, dùng cấu hình sau thay cho tạo Blueprint mới:

| Mục trên Render | Giá trị |
| --- | --- |
| Repository | `khuongduy123txz-web/KDAutoFillForm` |
| Branch | `main` |
| Root Directory | Để trống |
| Language / Runtime | `Docker` |
| Dockerfile Path | `./Dockerfile` |
| Docker Context | `.` |
| Docker Command | Để trống, dùng CMD trong Dockerfile |
| Instance Type | `Free` |
| Health Check Path | `/healthz` |

Không nhập lệnh `pip install` vào Build Command khi dùng Docker; Dockerfile đã cài đặt. Chọn Web Service + Docker cho cấu hình này.

Render tự cấp `PORT` và `RENDER_EXTERNAL_URL`. Ứng dụng lắng nghe trên `0.0.0.0`, dùng đúng cổng, chấp nhận API từ URL công khai và chạy Chromium ở chế độ ẩn. Không cần mở cổng modem hoặc giữ máy Windows bật sau khi triển khai thành công.

## URL riêng và các nền tảng Docker khác

Nếu gắn tên miền riêng, thêm biến trong **Render > Service > Environment**:

```text
KD_PUBLIC_URL=https://ten-mien-cua-ban.example
KD_ALLOWED_ORIGINS=https://ten-service.onrender.com
```

Thay địa chỉ ví dụ bằng địa chỉ thật; không thêm `/api` hoặc đường dẫn phía sau. Lưu để khởi động lại service. `KD_PUBLIC_URL` cũng dùng khi triển khai Docker ở Railway hoặc máy chủ khác. `KD_ALLOWED_ORIGINS` là các địa chỉ phụ, phân cách bằng dấu phẩy.

| Biến | Ý nghĩa |
| --- | --- |
| `PORT` | Cổng nền tảng cấp; nếu chưa có, cloud dùng `10000`, local dùng `8765`. `--port` ưu tiên hơn. |
| `KD_HOST` | Địa chỉ lắng nghe; Docker dùng `0.0.0.0`. Ghi đè bằng `--host`. |
| `KD_CLOUD` | `1` bật chế độ cloud: Chromium ẩn, một hoạt động trình duyệt tại một thời điểm. |
| `RENDER_EXTERNAL_URL` | Render tự cấp; làm URL chính nếu chưa có `KD_PUBLIC_URL`. |
| `KD_PUBLIC_URL` | URL công khai, ví dụ `https://kdautoform-abc.onrender.com`. |
| `KD_ALLOWED_ORIGINS` | Các URL phụ được phép gọi API, phân cách dấu phẩy; không dùng `*`. |

Không cần đặt key người dùng vào biến môi trường. CSDL lưu mã băm SHA-256 của key; giữ riêng chuỗi key dùng mở khóa.

## 10 lượt miễn phí và key unlimited

Website mở thẳng trang chủ. Mỗi trình duyệt được **10 lượt gửi miễn phí cộng dồn**, không cần tài khoản hoặc key. Một đợt gửi 5 phản hồi sử dụng 5 lượt; có thể chia thành nhiều đợt cho đến khi hết hạn mức.

- **Đọc form và Điền thử không trừ lượt.** Lỗi xảy ra trước thao tác gửi cũng không trừ lượt.
- Máy chủ trừ một lượt ngay trước mỗi thao tác gửi tới dịch vụ biểu mẫu. Nếu mất kết nối hoặc chưa xác nhận được kết quả sau khi đã bắt đầu gửi, lượt đó vẫn được tính vì dịch vụ có thể đã lưu phản hồi. Hạn mức dùng chung cho các loại form, không có thêm 10 lượt riêng khi đổi tab. Nếu dừng sau bước giữ chỗ nhưng trước khi bắt đầu gửi, công cụ hoàn lại lượt đó; lỗi kết nối khi hoàn lượt được ghi trong nhật ký.
- Số lượng gửi miễn phí phải từ 1 đến số lượt còn lại. Số lượng `0` để chạy liên tục chỉ dành cho key unlimited.
- Hết 10 lượt thì khóa thao tác bắt đầu lượt mới và hiển thị phần nhập key. Người dùng vẫn theo dõi lượt đang chạy, dừng và tải nhật ký được.
- **F5, Ctrl+R, mở lại URL và đăng xuất không đặt lại 10 lượt.** Cookie khách `kd_guest` được lưu tối đa 365 ngày; máy chủ lưu mã băm nhận dạng khách và số lượt đã dùng trong SQLite/PostgreSQL.
- Key unlimited dùng lại được cho nhiều đợt chạy và nhiều lần mở khóa. Phiên mở khóa kéo dài tối đa 12 giờ và vẫn giữ khi F5. Khi phiên hết hạn, đăng xuất hoặc máy chủ restart/deploy, nhập lại **cùng key** nếu key còn được bật.
- Đổi key cấp mã khác và làm mã cũ, phiên cũ hết hiệu lực. Khóa hoặc xóa key thu hồi quyền sử dụng ở lần kiểm tra quyền tiếp theo.
- F5 không tự gửi lại form. Lượt đang chạy tiếp tục trên máy chủ; tải nhật ký trước khi deploy hoặc restart.

Sau khi dùng hết 10 lượt miễn phí, người dùng cần nhập key được cấp để tiếp tục. Liên hệ trực tiếp [Zalo 0343415673](https://zalo.me/0343415673) hoặc [Facebook Khương Duy](https://www.facebook.com/khuong.duy.081207/) để nhận key. Chủ công cụ tạo key bằng Terminal. Website không niêm yết giá key.

**Hạn mức miễn phí được tính theo nhận dạng trình duyệt, không phải theo một người.** Xóa cookie, dùng chế độ riêng tư hoặc đổi trình duyệt có thể tạo nhận dạng mới. Muốn giới hạn chắc chắn theo từng người cần thêm cơ chế tài khoản hoặc xác minh danh tính.

CSDL SQLite/PostgreSQL cũ được nâng cấp tự động khi khởi động bản mới. Giữ nguyên CSDL đang dùng. Key cũ chưa từng kích hoạt và chưa từng mở đợt chạy được chuyển sang unlimited; key đã tiêu thụ ở bản một lần vẫn bị từ chối. Dùng `--key-rotate TEN_KEY` để cấp mã unlimited mới thay cho một key cũ đã tiêu thụ.

### 1. Tạo key trong Terminal

Mở Terminal trong VS Code:

```powershell
cd C:\Users\DELL\Desktop\toolggform
python google_form_tool.py --key-generate
```

Lệnh trên tự tạo và đặt tên **1 key**. Tạo 10 key:

```powershell
python google_form_tool.py --key-generate 10
```

Đặt tên riêng cho một key:

```powershell
python google_form_tool.py --key-add khach-04
```

Kết quả có dạng:

```text
Kho key: C:\...\data\access_keys.sqlite3
Tên: key-...
KEY: KD-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX
Đã lưu bản key thật tại: C:\...\private_keys\keys_YYYYMMDD_HHMMSS_XXXXXXXX.csv
```

Dòng `KEY:` là mã mở khóa unlimited, dùng lại được. Tên khách và mã băm không phải key. Ví dụ trên chỉ minh họa; dùng key thật do lệnh tạo ra.

### 2. Key ở đâu, comment nào trong code?

| Vị trí | Nội dung và cách dùng |
| --- | --- |
| `private_keys/keys_*.csv` | **Key thật**, tên và ngày tạo. Mở file CSV được in ra bằng VS Code/Excel, sao chép cột `key` để cấp cho khách. |
| `data/access_keys.sqlite3` | Mã băm SHA-256, trạng thái key, lịch sử đợt gần nhất và hạn mức khách miễn phí. Đây là kho mặc định khi chạy local. |
| PostgreSQL, bảng `kd_access_keys` và `kd_guest_usage` | Key và số lượt miễn phí, dùng chung cho cloud khi cấu hình `KD_DATABASE_URL`. |
| `.env` | Kết nối CSDL và đường dẫn riêng của chủ công cụ. File được Git bỏ qua. |

Trong `google_form_tool.py` tìm comment **KHO KEY**:

```python
# KHO KEY: đổi đường dẫn mặc định tại đây, hoặc đặt KD_KEY_DB_PATH / KD_KEY_EXPORT_DIR trong .env.
# data/access_keys.sqlite3 lưu mã băm + trạng thái key; private_keys/*.csv chứa KEY THẬT để bạn cấp cho khách.
KEY_DB_PATH = Path(__file__).resolve().parent / "data" / "access_keys.sqlite3"
KEY_EXPORT_DIR = Path(__file__).resolve().parent / "private_keys"
```

Không cần sửa nếu dùng thư mục mặc định. Đổi đường dẫn mà không sửa code bằng `.env`:

```text
KD_KEY_DB_PATH=D:/KDautoform/data/access_keys.sqlite3
KD_KEY_EXPORT_DIR=D:/KDautoform/private_keys
```

Trong `google_form_tool.py`, tìm comment **HẠN MỨC MIỄN PHÍ** và dòng `FREE_LIMIT = 10` để đổi số lượt miễn phí. Khi đổi hạn mức, cập nhật cả các nhãn giới thiệu đang ghi “10 lượt” trong HTML và README để nội dung khớp với cấu hình. Hạn mức được máy chủ kiểm tra; không chỉ dựa vào ô số lượng trên giao diện.

Tìm comment **LIÊN HỆ NHẬN KEY** trong phần HTML nhúng để sửa nội dung hướng dẫn nhận key và các đường dẫn Facebook/Zalo. Key được tạo bằng các lệnh quản lý trong Terminal.

Hàm `KeyStore.activate` kiểm tra key unlimited còn được bật; hàm `KeyStore.claim` ghi nhận đợt chạy gần nhất. Các thao tác này không tiêu thụ key unlimited.

`LEGACY_ACCESS_KEYS` là mã băm ba key cũ, chỉ nhập một lần khi tạo kho SQLite local đầu tiên. Sửa mục đó không thêm/sửa key trong kho đang dùng. Từ nay dùng lệnh quản lý. PostgreSQL mới không tự nạp ba key cũ; hãy tạo key mới trong đúng kho cloud.

`.gitignore` bỏ qua `data/`, `private_keys/`, `.env` và SQLite. `.dockerignore` chỉ cho phép source cần thiết vào image. Giữ CSV key thật ở máy bạn; không upload lên GitHub hoặc thư mục web công khai.

### 3. Xem, khóa, mở, đổi và xóa key

Chọn lệnh bạn cần, chạy riêng từng lệnh:

```powershell
python google_form_tool.py --key-paths
python google_form_tool.py --key-list
python google_form_tool.py --key-disable khach-04
python google_form_tool.py --key-enable khach-04
python google_form_tool.py --key-rotate khach-04
python google_form_tool.py --key-delete khach-04
```

- `--key-paths`: in kho đang dùng và thư mục CSV.
- `--key-list`: `UNLIMITED` là key dùng lại được; `KEY CŨ ĐÃ DÙNG` là key đã tiêu thụ ở bản một lần; `KHÓA` là đang khóa.
- `--key-disable`: khóa key; phiên/lượt chạy dừng ở lần kiểm tra quyền tiếp theo.
- `--key-enable`: bật lại key unlimited. Key cũ đã tiêu thụ vẫn cần `--key-rotate` để cấp mã unlimited mới.
- `--key-rotate`: cấp mã mới cho cùng tên và lưu CSV mới; mã cũ, phiên cũ hết hiệu lực.
- `--key-delete`: xóa key khỏi kho.

Các lệnh không chỉnh source và **không cần git push/redeploy**, miễn Terminal và web dùng cùng CSDL. Thư mục CSV giữ cả những mã đã cấp trước đây; dùng file mới nhất khi bạn đổi mã.

### 4. Dùng trên website

1. Mở KDautoform để vào trang chủ, xem số lượt miễn phí còn lại.
2. Dán link form được phép kiểm thử, chọn số lượt trong hạn mức và điều chỉnh tỷ lệ từng câu hỏi.
3. Bấm **Điền thử** nếu cần; thao tác này chưa gửi và không trừ lượt miễn phí.
4. Bắt đầu gửi thật. Số lượt miễn phí giảm trước mỗi thao tác gửi. Hoàn tất thì tải nhật ký.
5. Khi hết lượt, nhập key đã được cấp hoặc liên hệ Facebook/Zalo để nhận key.
6. Chủ công cụ chạy `python google_form_tool.py --key-generate`, sao chép mã `KD-...` từ Terminal hoặc CSV và gửi cho khách.
7. Khách nhập key vào phần mở khóa trên website. Có thể chạy nhiều đợt và dùng số lượng `0` để chạy liên tục đến khi dừng.
8. Giữ key để mở khóa lại khi phiên hết hạn hoặc máy chủ khởi động lại. F5 vẫn giữ phiên hiện tại và không tiêu thụ key.

Unlimited nghĩa là công cụ không áp hạn mức số lượt cho key; việc chạy thực tế vẫn phụ thuộc tài nguyên máy chủ và khả năng truy cập form.

## Kho key bền vững cho Render

Render Free mất file local khi ngủ/restart/deploy. Bản cloud này yêu cầu **`KD_DATABASE_URL` trỏ tới PostgreSQL** để giữ key và số lượt miễn phí đã dùng qua các lần khởi động. SQLite mặc định dành cho máy bạn.

Dùng PostgreSQL sẵn có hoặc [Neon Free](https://neon.com/docs/introduction/plans). Đây là cấu hình CSDL của chủ công cụ; người dùng web không cần tài khoản.

### 1. Lấy kết nối PostgreSQL

1. Vào [Neon Console](https://console.neon.tech), tạo project với gói **Free**.
2. Chọn khu vực gần Render nếu có, giữ database mặc định.
3. Bấm **Connect**, chọn đúng project/branch/database và sao chép toàn bộ chuỗi PostgreSQL, gồm tham số TLS do Neon cấp.
4. Không cần tạo bảng thủ công; chương trình tự tạo khi kết nối lần đầu.

Xem [hướng dẫn kết nối Neon](https://neon.com/docs/connect/connect-from-any-app). Giữ nguyên chuỗi được cấp. Không dán chuỗi thật vào source, README hoặc cuộc trò chuyện.

### 2. Cấu hình Render

Với service đã có, vào **Render > KDautoform > Environment > Add Environment Variable**:

| Key | Value |
| --- | --- |
| `KD_DATABASE_URL` | Chuỗi kết nối PostgreSQL vừa lấy |

Lưu và deploy code mới. Blueprint mới sẽ hỏi giá trị này lúc tạo. Khi cập nhật Blueprint cũ, thêm secret bằng tay vào Environment của service.

### 3. Cho Terminal dùng cùng kho cloud

```powershell
python -m pip install -r requirements.txt
if (!(Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
code .env
```

Trong `.env` đặt **đúng cùng chuỗi kết nối đã lưu trên Render**:

```text
KD_DATABASE_URL=postgresql://...chuoi-ket-noi-that-do-Neon-cap...
```

Chương trình tự đọc `.env` bên cạnh `google_form_tool.py` khi chạy Terminal. Biến đã đặt trong môi trường Terminal được ưu tiên hơn file.

```powershell
python google_form_tool.py --key-paths
python google_form_tool.py --key-generate 5
python google_form_tool.py --key-list
```

Dòng `Kho key: PostgreSQL (biến KD_DATABASE_URL)` xác nhận đang thao tác kho cloud. Key mới có hiệu lực trên web mà không cần redeploy; CSV key thật vẫn lưu ở máy Windows.

Nếu dòng kho là đường dẫn SQLite, bạn đang dùng local. Key tạo ở SQLite không tự chuyển sang PostgreSQL. Bỏ trống `KD_DATABASE_URL` thì local dùng SQLite; cloud báo thiếu cấu hình.

Giữ CSDL hiện tại qua các lần deploy. Xóa CSDL hoặc khôi phục bản sao cũ sẽ xóa/quay lại trạng thái key và số lượt miễn phí trong bản sao đó.


## Kiểm tra tại máy

Kiểm tra HTTP, key, hạn mức, hồi quy Google Forms, các bộ đọc form và file SPSS, không gửi form thật:

```powershell
python -B -m unittest discover -s tests -p test_deployment.py -v
python -B -m unittest discover -s tests -p test_keys.py -v
python -B -m unittest discover -s tests -p test_google_regression.py -v
python -B -m unittest discover -s tests -p test_google_speed.py -v
python -B -m unittest discover -s tests -p test_microsoft_forms.py -v
python -B -m unittest discover -s tests -p test_html_forms.py -v
python -B -m unittest discover -s tests -p test_spss_export.py -v
python -B -m unittest discover -s tests -p test_provider_exports.py -v
python -B -m unittest discover -s tests -p test_performance.py -v
python -B -m unittest discover -s tests -p test_run_scheduling.py -v
python -B -m unittest discover -s tests -p test_teaching_tools.py -v
python -B -m unittest discover -s tests -p test_workflow.py -v
```

Chạy toàn bộ bộ kiểm thử hiện tại:

```powershell
python -B -m unittest discover -s tests -v
```

Các kiểm thử vòng đọc/ghi `.sav` cần cài đủ `requirements.txt`. Bật các bài kiểm thử trình duyệt bằng fixture mô phỏng khi Chromium đã được cài:

```powershell
$env:KD_BROWSER_TESTS="1"
python -B -m unittest discover -s tests -p test_google_regression.py -v
python -B -m unittest discover -s tests -p test_google_speed.py -v
python -B -m unittest discover -s tests -p test_microsoft_forms.py -v
python -B -m unittest discover -s tests -p test_html_forms.py -v
```

Fixture mô phỏng kiểm tra khả năng đọc/điền và xác nhận kết quả trong môi trường kiểm thử. Chúng không bảo đảm mọi website hoặc mọi thay đổi giao diện trong tương lai đều tương thích.

Nếu Docker Desktop đã bật, dùng Linux containers và `.env` đã có `KD_DATABASE_URL`:

```powershell
docker build -t kdautoform .
docker run --rm --name kdautoform-local --env-file .env -p 8766:10000 -e KD_PUBLIC_URL=http://localhost:8766 kdautoform
```

Mở http://localhost:8766. Dừng bằng Ctrl+C hoặc `docker stop kdautoform-local` từ cửa sổ khác. Image dùng `tini` quản lý tiến trình và Chromium headless shell để giảm dung lượng. Nếu đổi cổng `8766`, đổi cả `-p` và `KD_PUBLIC_URL` cho khớp.

## Giới hạn

- Render Free có 512 MB RAM, tự ngủ sau 15 phút không có truy cập. Chromium có thể vượt RAM với form nặng. Nếu log có `Out of memory`/`OOM`, thử form nhỏ hoặc chuyển máy chủ có nhiều RAM hơn. Không bảo đảm chạy liên tục nhiều lượt trên gói miễn phí.
- Lượt chạy, phiên mở khóa, cấu hình đang chỉnh ở RAM; nhật ký ở thư mục tạm. Chúng không được bảo toàn khi service ngủ, restart hoặc deploy. Trạng thái key và số lượt miễn phí được giữ trong PostgreSQL riêng. Sau restart, nhập lại cùng key unlimited còn hiệu lực. Tải nhật ký trước khi rời phiên; tránh cập nhật source/key lúc đang chạy.
- Nếu service dừng giữa lúc gửi, kiểm tra phản hồi đã lưu trong form nguồn trước khi bắt đầu lại để tránh gửi trùng. Job web và lịch chờ không tự tiếp tục sau restart; tạm dừng không tạo bản lưu bền vững.
- Cloud không hiện cửa sổ Chromium trên máy người dùng. Theo dõi tiến độ qua web và nhật ký.
- Form có đăng nhập, tải tệp, CAPTCHA hoặc quy tắc chưa hỗ trợ sẽ báo dừng.

## Xử lý lỗi

| Hiện tượng | Cách xử lý |
| --- | --- |
| Deploy xong nhưng 404 | Chọn **Web Service + Docker**, nhánh `main`, thư mục gốc; kiểm tra commit có Dockerfile. Dùng URL Render được cấp thay vì URL deploy Vercel cũ. |
| `No open ports detected` | Kiểm tra CMD không bị ghi đè, `KD_HOST=0.0.0.0`, `PORT` hợp lệ. Log cần có dòng đang lắng nghe. |
| `Thiếu URL công khai` | Đặt `KD_PUBLIC_URL` bằng URL HTTPS service nếu nền tảng không cấp `RENDER_EXTERNAL_URL`. |
| `Phiên truy cập không hợp lệ` | Tải lại trang; kiểm tra `KD_PUBLIC_URL`/`KD_ALLOWED_ORIGINS` khớp chính xác thanh địa chỉ, gồm cả HTTPS. |
| Hết 10 lượt miễn phí | Nhập key được cấp để tiếp tục; có thể liên hệ Facebook/Zalo để nhận key. F5 không khôi phục lượt miễn phí. |
| Key không đúng hoặc đã bị khóa | Dùng chuỗi `KD-...`; kiểm tra `UNLIMITED` và trạng thái khóa bằng `--key-list`. Terminal và Render phải dùng cùng PostgreSQL. Key cũ đã tiêu thụ cần `--key-rotate` để cấp mã unlimited mới. |
| F5 vẫn hiện giao diện yêu cầu key của bản cũ | Kiểm tra Render đã deploy thành công commit mới. Bản mới luôn vào trang chủ, giữ số lượt miễn phí và giữ phiên unlimited còn hạn. Sau restart hoặc hết phiên, nhập lại cùng key. |
| Cloud cần KD_DATABASE_URL | Thêm chuỗi PostgreSQL vào Render Environment; không dùng SQLite tạm trên Render Free. |
| Không mở được Chromium | Runtime cần là Docker, Dockerfile đã chạy `playwright install --with-deps --only-shell chromium`. |
| Microsoft Forms yêu cầu đăng nhập | Dùng link trả lời công khai mà người quản lý cho phép mọi người trả lời. Công cụ không vượt đăng nhập hoặc hạn chế của tổ chức. |
| Form khác báo chưa hỗ trợ | Chọn đúng chế độ HTML thử nghiệm. Website dùng widget, luồng nhiều trang hoặc cơ chế gửi riêng cần bộ kết nối riêng; không đổi URL lạ thành Google Form. |
| Không có nút tải SPSS hoặc chưa có lượt xác nhận | Điền thử và lượt chưa rõ kết quả không được xuất thành dữ liệu. Chờ có phản hồi đã xác nhận rồi tải. |
| Xuất SPSS báo thiếu thư viện | Chạy `python -m pip install -r requirements.txt`; khi cloud dùng Docker, deploy lại image có requirements mới. |
| Lượt sau bắt đầu chậm hơn khoảng cách đã đặt | Kiểm tra Cách tính khoảng cách: nghỉ sau hoàn tất cộng thêm thời gian xử lý. Với nhịp bắt đầu, form dài/mạng chậm phải hoàn tất trước; xem số đo Xử lý, Hai lần bắt đầu, Chờ quá lịch và vị trí hàng đợi. |
| Đã bấm tạm dừng nhưng vẫn có một phản hồi hoàn tất | Yêu cầu mạng đang gửi có thể hoàn tất; công cụ tạm dừng ở điểm kiểm tra tiếp theo. |
| Xuất SPSS vượt giới hạn | Chia số lượt thành đợt nhỏ hơn; giới hạn xuất bảo vệ RAM và không làm mất hiệu lực key unlimited. |
| Web khởi động chậm sau khi để lâu | Gói Free đang thức dậy; đợi khởi động xong. |

## Tài liệu chính thức

- [Render Web Services](https://render.com/docs/web-services)
- [Render Docker](https://render.com/docs/docker)
- [Render Blueprint](https://render.com/docs/blueprint-spec)
- [Render Free](https://render.com/docs/free)
- [Playwright](https://playwright.dev/python/docs/browsers)
- [Microsoft Forms: ai có thể trả lời](https://support.microsoft.com/en-us/forms/choose-who-can-fill-out-a-form-or-quiz)
- [Microsoft Forms: phân nhánh](https://support.microsoft.com/en-gb/forms/use-branching-logic-in-microsoft-forms)
- [pyreadstat: đọc/ghi SPSS](https://ofajardo.github.io/pyreadstat_documentation/_build/html/index.html)
