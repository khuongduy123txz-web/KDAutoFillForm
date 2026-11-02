# KDautoform

Giao diện web và công cụ Python điền Google Form bằng dữ liệu giả để kiểm thử form bạn quản lý. Công cụ đọc các trang, câu hỏi, cho chỉnh tỷ lệ và tạo hồ sơ giả tiếng Việt.

## Chạy trên máy Windows

```powershell
cd C:\Users\DELL\Desktop\toolggform
python google_form_tool.py --setup
python google_form_tool.py --web
```

Mở http://127.0.0.1:8765 và nhập key hiện có. Dùng `--web --check` để kiểm tra mọi trang mà chưa gửi phản hồi.

## Đưa lên GitHub

Repository đã có remote `origin` và nhánh `main`:

```powershell
cd C:\Users\DELL\Desktop\toolggform
git status --short
git add .gitignore .dockerignore Dockerfile requirements.txt render.yaml README.md google_form_tool.py tests/test_deployment.py
git commit -m "Prepare KDautoform for Render Docker deployment"
git push origin main
```

Repository: https://github.com/khuongduy123txz-web/KDFillForm

Nếu Git báo `rejected (fetch first)`, chạy `git pull --rebase origin main` rồi đẩy lại; nếu có xung đột, giải quyết trước khi tiếp tục. Không dùng `--force`. File `tests/test_batch.py` cũ không thuộc bộ kiểm tra triển khai mới.

## Triển khai miễn phí trên Render

Cách khuyến nghị: dùng Blueprint để Render đọc sẵn `render.yaml`.

1. Truy cập https://dashboard.render.com và đăng nhập bằng GitHub.
2. Chọn **New > Blueprint**, kết nối repository **KDFillForm**. Nếu chưa thấy repo, cấp quyền cho Render truy cập repo đó trong phần cài đặt GitHub của Render.
3. Chọn nhánh **main**, file Blueprint **render.yaml** ở thư mục gốc. Đặt tên Blueprint, ví dụ **KDautoform**.
4. Xem lại service **kdautoform**, runtime **Docker**, compute plan **Free**. Chỉ tiếp tục khi màn hình thể hiện gói miễn phí; không thêm database hoặc dịch vụ trả phí.
5. Chọn **Deploy Blueprint** hoặc **Apply** theo giao diện hiện tại. Render cài Python, Playwright, Chromium và các thư viện Linux từ Dockerfile.
6. Khi service chuyển **Live**, mở URL `https://<ten-service>.onrender.com` được Render cấp. URL có thể có hậu tố để tránh trùng tên.
7. Mở `https://<ten-service>.onrender.com/healthz`: phải thấy JSON có `"ok": true`.
8. Vào trang chính, nhập key hiện có. Lần đầu thử hãy tắt **Tự chạy sau khi đọc form**, dùng form thử nghiệm của bạn và bấm **Điền thử** để kiểm tra đầy đủ các trang trước khi gửi thật.

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

Không cần đặt key người dùng vào biến môi trường. Source chỉ lưu mã băm SHA-256 của key; giữ riêng chuỗi key dùng đăng nhập.

## Thêm, khóa, đổi và xóa key

Chạy trên máy Windows trong thư mục dự án:

```powershell
python google_form_tool.py --key-list
python google_form_tool.py --key-add khach-04
python google_form_tool.py --key-disable khach-04
python google_form_tool.py --key-enable khach-04
python google_form_tool.py --key-rotate khach-04
python google_form_tool.py --key-delete khach-04
```

Chọn lệnh bạn cần, không chạy liên tiếp toàn bộ ví dụ vì lệnh cuối sẽ xóa key vừa tạo. Lệnh tạo/đổi chỉ hiển thị key mới một lần; tự lưu key ở nơi riêng tư. Các lệnh sửa phần `ACCESS_KEYS` trong `google_form_tool.py`.

Sau khi thay đổi key, cập nhật bản cloud:

```powershell
git add google_form_tool.py
git commit -m "Update access keys"
git push origin main
```

Chờ Render triển khai commit mới. Không sửa key trực tiếp trong container vì thay đổi có thể mất khi khởi động lại. Render Free không có shell quản trị trên dashboard.

## Kiểm tra tại máy

Kiểm tra HTTP, xác thực, URL công khai và chế độ trình duyệt cloud, không gửi Google Form:

```powershell
python -B -m unittest discover -s tests -p test_deployment.py -v
```

Nếu Docker Desktop đã bật và dùng Linux containers:

```powershell
docker build -t kdautoform .
docker run --rm --name kdautoform-local -p 8766:10000 -e KD_PUBLIC_URL=http://localhost:8766 kdautoform
```

Mở http://localhost:8766. Dừng bằng Ctrl+C hoặc `docker stop kdautoform-local` từ cửa sổ khác. Image dùng `tini` quản lý tiến trình và Chromium headless shell để giảm dung lượng. Nếu đổi cổng `8766`, đổi cả `-p` và `KD_PUBLIC_URL` cho khớp.

## Giới hạn

- Render Free có 512 MB RAM, tự ngủ sau 15 phút không có truy cập. Chromium có thể vượt RAM với form nặng. Nếu log có `Out of memory`/`OOM`, thử form nhỏ hoặc chuyển máy chủ có nhiều RAM hơn. Không bảo đảm chạy liên tục nhiều lượt trên gói miễn phí.
- Lượt chạy, phiên đăng nhập, cấu hình đang chỉnh ở RAM; nhật ký ở thư mục tạm. Chúng không được bảo toàn khi service ngủ, restart hoặc deploy. Tải nhật ký trước khi rời phiên; tránh cập nhật source/key lúc đang chạy.
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
| Key không đúng | Dùng chuỗi key thay vì mã băm; kiểm tra key đang bật và Render đã triển khai commit mới. Nếu nhập sai nhiều lần, chờ theo thông báo. |
| Không mở được Chromium | Runtime cần là Docker, Dockerfile đã chạy `playwright install --with-deps --only-shell chromium`. |
| Web khởi động chậm sau khi để lâu | Gói Free đang thức dậy; đợi khởi động xong. |

## Tài liệu chính thức

- [Render Web Services](https://render.com/docs/web-services)
- [Render Docker](https://render.com/docs/docker)
- [Render Blueprint](https://render.com/docs/blueprint-spec)
- [Render Free](https://render.com/docs/free)
- [Playwright](https://playwright.dev/python/docs/browsers)
