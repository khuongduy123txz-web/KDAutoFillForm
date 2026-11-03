# KDautoform

Giao diện web và công cụ Python điền Google Form bằng dữ liệu giả để kiểm thử form bạn quản lý. Công cụ đọc các trang, câu hỏi, cho chỉnh tỷ lệ và tạo hồ sơ giả tiếng Việt.

## Chạy trên máy Windows

```powershell
cd C:\Users\DELL\Desktop\toolggform
python google_form_tool.py --setup
python google_form_tool.py --web
```

Mở http://127.0.0.1:8765 để vào thẳng trang chủ và dùng miễn phí 10 lượt gửi, không cần key. Dùng `--web --check` để kiểm tra mọi trang mà chưa gửi phản hồi. Khi cần mở khóa không giới hạn lượt, chủ công cụ tạo key bằng `python google_form_tool.py --key-generate`.

## Đưa lên GitHub

Repository đã có remote `origin` và nhánh `main`:

```powershell
cd C:\Users\DELL\Desktop\toolggform
git status --short
git add .gitignore .dockerignore .env.example Dockerfile requirements.txt render.yaml README.md google_form_tool.py tests/test_deployment.py tests/test_keys.py
git commit -m "Add 10 free submissions and reusable unlimited keys"
git push origin main
```

Repository: https://github.com/khuongduy123txz-web/KDFillForm

Nếu Git báo `rejected (fetch first)`, chạy `git pull --rebase origin main` rồi đẩy lại; nếu có xung đột, giải quyết trước khi tiếp tục. Không dùng `--force`. File `tests/test_batch.py` cũ không thuộc bộ kiểm tra triển khai mới.

## Triển khai miễn phí trên Render

Trước khi deploy, cấu hình kho PostgreSQL theo mục **Kho key bền vững cho Render** bên dưới. Cách khuyến nghị: dùng Blueprint để Render đọc sẵn `render.yaml`.

1. Truy cập https://dashboard.render.com và đăng nhập bằng GitHub.
2. Chọn **New > Blueprint**, kết nối repository **KDFillForm**. Nếu chưa thấy repo, cấp quyền cho Render truy cập repo đó trong phần cài đặt GitHub của Render.
3. Chọn nhánh **main**, file Blueprint **render.yaml** ở thư mục gốc. Đặt tên Blueprint, ví dụ **KDautoform**.
4. Xem lại service **kdautoform**, runtime **Docker**, compute plan **Free**. Chỉ tiếp tục khi màn hình thể hiện gói miễn phí; không thêm database hoặc dịch vụ trả phí.
5. Nhập chuỗi PostgreSQL vào ô **KD_DATABASE_URL**, rồi chọn **Deploy Blueprint** hoặc **Apply** theo giao diện hiện tại. Render cài Python, Playwright, Chromium và các thư viện Linux từ Dockerfile.
6. Khi service chuyển **Live**, mở URL `https://<ten-service>.onrender.com` được Render cấp. URL có thể có hậu tố để tránh trùng tên.
7. Mở `https://<ten-service>.onrender.com/healthz`: phải thấy JSON có `"ok": true`.
8. Vào trang chính để dùng 10 lượt miễn phí, không cần key. Lần đầu thử hãy tắt **Tự chạy sau khi đọc form**, dùng form thử nghiệm của bạn và bấm **Điền thử** để kiểm tra đầy đủ các trang trước khi gửi thật.

Nếu đã tạo Web Service bằng tay, dùng cấu hình sau thay cho tạo Blueprint mới:

| Mục trên Render | Giá trị |
| --- | --- |
| Repository | `khuongduy123txz-web/KDFillForm` |
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
- Máy chủ trừ một lượt ngay trước mỗi thao tác gửi tới Google. Nếu mất kết nối hoặc chưa xác nhận được kết quả sau khi đã bắt đầu gửi, lượt đó vẫn được tính vì Google có thể đã lưu phản hồi.
- Số lượng gửi miễn phí phải từ 1 đến số lượt còn lại. Số lượng `0` để chạy liên tục chỉ dành cho key unlimited.
- Hết 10 lượt thì khóa thao tác bắt đầu lượt mới và hiển thị phần mua key. Người dùng vẫn theo dõi lượt đang chạy, dừng và tải nhật ký được.
- **F5, Ctrl+R, mở lại URL và đăng xuất không đặt lại 10 lượt.** Cookie khách `kd_guest` được lưu tối đa 365 ngày; máy chủ lưu mã băm nhận dạng khách và số lượt đã dùng trong SQLite/PostgreSQL.
- Key unlimited dùng lại được cho nhiều đợt chạy và nhiều lần mở khóa. Phiên mở khóa kéo dài tối đa 12 giờ và vẫn giữ khi F5. Khi phiên hết hạn, đăng xuất hoặc máy chủ restart/deploy, nhập lại **cùng key** nếu key còn được bật.
- Đổi key cấp mã khác và làm mã cũ, phiên cũ hết hiệu lực. Khóa hoặc xóa key thu hồi quyền sử dụng ở lần kiểm tra quyền tiếp theo.
- F5 không tự gửi lại form. Lượt đang chạy tiếp tục trên máy chủ; tải nhật ký trước khi deploy hoặc restart.

Phần mua key hiển thị **“Mua key unlimited lần giá 30k/1key”**. Liên hệ trực tiếp [Zalo 0343415673](https://zalo.me/0343415673) hoặc [Facebook Khương Duy](https://www.facebook.com/khuong.duy.081207/). Chủ công cụ nhận yêu cầu và cấp key bằng Terminal. Website chưa có thanh toán hoặc xác nhận mua hàng tự động.

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

Trong `google_form_tool.py`, tìm comment **HẠN MỨC MIỄN PHÍ** và dòng `FREE_LIMIT = 10` để đổi số lượt miễn phí. Hạn mức được máy chủ kiểm tra; không chỉ dựa vào ô số lượng trên giao diện.

Tìm comment **MUA KEY** trong phần HTML nhúng để sửa nội dung giá `30.000đ/key` và các đường dẫn Facebook/Zalo. Đây là nội dung giới thiệu, không tự tạo key hoặc xác nhận thanh toán.

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
5. Khi hết lượt, bấm liên hệ Facebook/Zalo trong phần mua key unlimited giá **30.000đ/key**.
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

Kiểm tra HTTP, xác thực, URL công khai và chế độ trình duyệt cloud, không gửi Google Form:

```powershell
python -B -m unittest discover -s tests -p test_deployment.py -v
python -B -m unittest discover -s tests -p test_keys.py -v
```

Nếu Docker Desktop đã bật, dùng Linux containers và `.env` đã có `KD_DATABASE_URL`:

```powershell
docker build -t kdautoform .
docker run --rm --name kdautoform-local --env-file .env -p 8766:10000 -e KD_PUBLIC_URL=http://localhost:8766 kdautoform
```

Mở http://localhost:8766. Dừng bằng Ctrl+C hoặc `docker stop kdautoform-local` từ cửa sổ khác. Image dùng `tini` quản lý tiến trình và Chromium headless shell để giảm dung lượng. Nếu đổi cổng `8766`, đổi cả `-p` và `KD_PUBLIC_URL` cho khớp.

## Giới hạn

- Render Free có 512 MB RAM, tự ngủ sau 15 phút không có truy cập. Chromium có thể vượt RAM với form nặng. Nếu log có `Out of memory`/`OOM`, thử form nhỏ hoặc chuyển máy chủ có nhiều RAM hơn. Không bảo đảm chạy liên tục nhiều lượt trên gói miễn phí.
- Lượt chạy, phiên mở khóa, cấu hình đang chỉnh ở RAM; nhật ký ở thư mục tạm. Chúng không được bảo toàn khi service ngủ, restart hoặc deploy. Trạng thái key và số lượt miễn phí được giữ trong PostgreSQL riêng. Sau restart, nhập lại cùng key unlimited còn hiệu lực. Tải nhật ký trước khi rời phiên; tránh cập nhật source/key lúc đang chạy.
- Nếu service dừng giữa lúc gửi, kiểm tra phản hồi đã lưu trong Google Form trước khi bắt đầu lại để tránh gửi trùng. Job web không tự tiếp tục sau restart.
- Cloud không hiện cửa sổ Chromium trên máy người dùng. Theo dõi tiến độ qua web và nhật ký.
- Form có đăng nhập, tải tệp, CAPTCHA hoặc quy tắc chưa hỗ trợ sẽ báo dừng.

## Xử lý lỗi

| Hiện tượng | Cách xử lý |
| --- | --- |
| Deploy xong nhưng 404 | Chọn **Web Service + Docker**, nhánh `main`, thư mục gốc; kiểm tra commit có Dockerfile. Dùng URL Render được cấp thay vì URL deploy Vercel cũ. |
| `No open ports detected` | Kiểm tra CMD không bị ghi đè, `KD_HOST=0.0.0.0`, `PORT` hợp lệ. Log cần có dòng đang lắng nghe. |
| `Thiếu URL công khai` | Đặt `KD_PUBLIC_URL` bằng URL HTTPS service nếu nền tảng không cấp `RENDER_EXTERNAL_URL`. |
| `Phiên truy cập không hợp lệ` | Tải lại trang; kiểm tra `KD_PUBLIC_URL`/`KD_ALLOWED_ORIGINS` khớp chính xác thanh địa chỉ, gồm cả HTTPS. |
| Hết 10 lượt miễn phí | Mua key unlimited giá 30.000đ/key qua Facebook/Zalo và nhập vào phần mở khóa. F5 không khôi phục lượt miễn phí. |
| Key không đúng hoặc đã bị khóa | Dùng chuỗi `KD-...`; kiểm tra `UNLIMITED` và trạng thái khóa bằng `--key-list`. Terminal và Render phải dùng cùng PostgreSQL. Key cũ đã tiêu thụ cần `--key-rotate` để cấp mã unlimited mới. |
| F5 vẫn hiện giao diện yêu cầu key của bản cũ | Kiểm tra Render đã deploy thành công commit mới. Bản mới luôn vào trang chủ, giữ số lượt miễn phí và giữ phiên unlimited còn hạn. Sau restart hoặc hết phiên, nhập lại cùng key. |
| Cloud cần KD_DATABASE_URL | Thêm chuỗi PostgreSQL vào Render Environment; không dùng SQLite tạm trên Render Free. |
| Không mở được Chromium | Runtime cần là Docker, Dockerfile đã chạy `playwright install --with-deps --only-shell chromium`. |
| Web khởi động chậm sau khi để lâu | Gói Free đang thức dậy; đợi khởi động xong. |

## Tài liệu chính thức

- [Render Web Services](https://render.com/docs/web-services)
- [Render Docker](https://render.com/docs/docker)
- [Render Blueprint](https://render.com/docs/blueprint-spec)
- [Render Free](https://render.com/docs/free)
- [Playwright](https://playwright.dev/python/docs/browsers)
