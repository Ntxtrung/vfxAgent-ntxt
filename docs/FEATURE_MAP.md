# Feature Map — Nuke ↔ ComfyUI Remove/Inpaint Tool

> Tài liệu sống dùng để tra cứu, mở chạy thử từng tính năng, thu thập lỗi từ `logs/` và tự động sửa.
> Quy ước trạng thái: `[ ] Planned` | `[~] In Progress` | `[x] Verified`

## Kiến trúc 2 tiến trình
| Tiến trình | Thư mục | Vai trò | Log |
| :--- | :--- | :--- | :--- |
| **Tiến trình chính (Core/Worker)** | `src/core/` | FastAPI, ComfyUI API, SAM 2, ProPainter/Inpaint, Cache IO | `logs/core.log` |
| **Tiến trình giao diện (UI)** | `src/ui/` | Canvas vẽ mask, Timeline player, Before/After slider | `logs/ui.log` |
| **Cầu nối Nuke** | `src/nuke_bridge/` | Đọc `Read` (EXR/DPX/MOV/MP4), Colorspace prep, Auto-build node | `logs/nuke_bridge.log` |

---

## Bản đồ tính năng (Feature Map)

### F0 — Nền tảng & CI Guardrails
- **Trạng thái**: `[x] Verified`
- **Thành phần**: `AGENTS.md`, `.agents/skills/`, `scripts/ci_check.py`, `pyproject.toml`
- **Lệnh kiểm tra**: `python scripts/ci_check.py`

### F1 — Nuke Bridge: Đọc `Read` & Chuẩn hóa đầu vào cho ComfyUI
- **Trạng thái**: `[x] Verified`
- **Mô tả**: Lấy metadata từ node `Read` (EXR/DPX/MOV/MP4, frame range, colorspace), convert sang chuẩn trung gian cho AI (PNG 16-bit sRGB/Output - sRGB) vào cache của shot.
- **Thư mục**: `src/nuke_bridge/`, `src/contracts/`
- **Chạy thử / Test**: `python -m unittest discover -s tests -v`

### F2 — Tiến trình chính (Core API) & Kết nối ComfyUI
- **Trạng thái**: `[~] In Progress`. Có job `single_frame_inpaint` và `tracked_clean_plate`, cùng dùng ảnh đã chuẩn màu từ Nuke và mask rời theo frame.
- **Mô tả**: FastAPI quản lý hàng đợi nền. Với sequence, Core track nền về một frame mốc, gom phần nền lộ ra và dùng tối đa một lần inpaint ComfyUI cho vùng còn thiếu. Core dùng màu của nền quan sát được để cân lại phần sinh. Có thể cung cấp clean plate đã duyệt tại frame mốc. Mỗi job lưu riêng patch, mask và manifest, kèm `stage` và `current_frame` khi polling. Job mô phỏng Pillow dành cho shot demo. SAM 2 và ProPainter là các chế độ riêng đang chờ triển khai.
- **Thư mục**: `src/core/`
- **Chạy thử / Test**: `python -m pytest tests/test_core_api.py`

### F3 — Tiến trình giao diện (UI kiểu Neat Video)
- **Trạng thái**: `[ ] Planned`
- **Mô tả**: Cửa sổ UI riêng không làm đơ Nuke: Timeline từng frame, vẽ cọ/tẩy mask, click-to-mask & track SAM 2, chọn ảnh Reference/Clean-Plate, nhập Prompt, xem Before/After.
- **Thư mục**: `src/ui/`
- **Chạy thử / Test**: Khởi chạy 2 tiến trình qua `scripts/dev.py` (sẽ bổ sung khi vào code F2/F3).

### F4 — Nuke Auto-Composite: Trả về Colorspace gốc & Merge giữ chất lượng gốc
- **Trạng thái**: `[~] In Progress`. Đã có lệnh ghép một frame từ manifest của job ComfyUI.
- **Mô tả**: Đọc patch và mask từ cache, chuyển patch về working colorspace của script rồi `Keymix` RGB lên `Read` gốc. `Switch` chỉ bật patch tại frame đã xử lý. Auto-composite theo chuỗi và grain vẫn trong kế hoạch.
- **Thư mục**: `src/nuke_bridge/`
- **Chạy thử**: Làm theo quy trình một frame bên dưới và kiểm tra `Difference` giữa `Read` gốc và output.

### Chạy thử một frame EXR qua ComfyUI và ghép về Nuke

1. Chạy `python scripts/ci_check.py`. Khởi động ComfyUI cục bộ và đặt `COMFYUI_INPAINT_CHECKPOINT` cho Core bằng tên checkpoint tương thích với `CheckpointLoaderSimple`, ví dụ một checkpoint SD 1.5 có CLIP và VAE. Dùng cùng một `CACHE_DIR` tuyệt đối cho Core và Nuke. Core đọc cấu hình môi trường hoặc `.env`, còn Nuke lấy `CACHE_DIR` từ môi trường khi khởi động.
2. Từ thư mục repo, chạy `.venv/Scripts/python.exe -m uvicorn src.core.server:app --host 127.0.0.1 --port 8765`. Trong Nuke, đưa thư mục repo vào Python path và gọi `from src.nuke_bridge.menu import register_menu; register_menu()` một lần để đăng ký menu. Core và Nuke chạy riêng.
3. Trong Nuke, mở plate ở đúng working colorspace, chọn `Read` và một node mask có alpha cùng format, rồi chọn **ComfyRemove → Prepare Current Frame + Mask**. Ghi lại `shot_id` và số frame được báo.
4. Tại `http://127.0.0.1:8765/docs`, gửi `POST /api/jobs` với `shot_id`, `mode: single_frame_inpaint`, `first_frame` và `last_frame` bằng cùng số frame, và `prompt` mô tả hậu cảnh cần dựng. Có thể thêm `negative_prompt` và `seed`. Dùng `GET /api/jobs/{job_id}` để xem tiến độ hoặc lỗi ComfyUI. Khi `completed`, có thể xem patch qua `GET /api/jobs/{job_id}/result/{frame}`.
5. Chọn `Read` gốc, mở **ComfyRemove → Apply Single-Frame Patch** và chọn file tại `apply_manifest_path` trả về bởi job. Kết nối comp hoặc Viewer với `Switch` mới. Kiểm tra vùng mask và xác nhận vùng nền ngoài mask giữ nguyên.

Core cắt vùng quanh mask và thu nhỏ đầu vào AI nếu cạnh dài vượt 1024 px. Patch được đặt lại đúng kích thước footage trước khi Nuke ghép. Vùng bị che rộng cần đánh giá lại màu, hình học và texture của nền được tạo.

### Chạy một job clean plate qua nhiều frame

1. Cài OpenCV vào môi trường Core bằng `.venv/Scripts/python.exe -m pip install opencv-python-headless`.
2. Dùng `export_prep_sequence` trong Nuke Bridge để chuẩn bị frame range. Tải một mask PNG cùng kích thước cho mỗi frame qua `POST /api/shots/{shot_id}/mask/{frame}`. Mask grayscale hoặc kênh đỏ RGB dùng giá trị trắng cho vùng sửa. Dùng mask đen cho frame cần giữ nguyên.
3. Gửi `POST /api/jobs` với `mode: tracked_clean_plate`, `shot_id`, `first_frame` và `last_frame`. Có thể chỉ định `anchor_frame`, mặc định là giữa đoạn. Để tự dựng clean plate, điền `prompt` và cấu hình checkpoint ComfyUI như job một frame. Để dùng clean plate đã duyệt, tải PNG của frame mốc trong cùng AI prep colorspace qua `POST /api/shots/{shot_id}/reference`, rồi gửi `reference_image_path` trả về cùng `anchor_frame` tương ứng.
4. Poll `GET /api/jobs/{job_id}`. Sequence báo `tracking_background`, `submitting_to_comfyui` và `waiting_for_comfyui` khi cần inpaint, rồi `writing_frames`. Job báo rõ frame nếu thiếu mask hoặc tracking mất bám. Khi hoàn thành, dùng `GET /api/jobs/{job_id}/result/{frame}` để xem patch. Mask theo frame được lưu cạnh patch để điều khiển độ hòa trộn trong Nuke.
5. Manifest sequence lưu frame range, frame mốc, đường dẫn patch và mask, cùng colorspace cho F4. `tracking.json` lưu phép biến đổi frame về mốc. Luồng F4 tự dựng graph từ manifest sequence là hạng mục tiếp theo.

Tracking nền hiện dùng một phép biến đổi toàn cục cho mỗi frame. Kết quả phù hợp nhất với đoạn ngắn đủ chi tiết nền chung. Camera parallax mạnh, đổi góc hoặc vùng nền bị che nhiều cần kiểm tra patch và mép mask trong Nuke.

Kiểm thử trên năm frame EXR 2048 × 802. Hai job, tự dựng nền và dùng clean plate tham chiếu, đều trả đủ patch và mask. Chế độ tự dựng gọi ComfyUI một lần. Trên shot này, clean plate tham chiếu giữ bố cục nền sát hơn ở vùng bị che lớn.

---

## Quy trình mở chạy thử, bắt lỗi & tự động sửa
1. Chạy CI trước: `python scripts/ci_check.py`
2. Mở app theo feature cần test và theo dõi log tại `logs/core.log`, `logs/ui.log`.
3. Khi phát sinh lỗi: ghi nhận **Feature ID**, **File/Line**, **Traceback** → báo ngắn gọn cho người dùng → sửa code → chạy lại `python scripts/ci_check.py` và test của feature đó.

---

## Handoff & Next Steps (Cập nhật mỗi lần push GitHub)
- **Trạng thái hiện tại**: F0 và F1 đã hoàn tất. F2 có job một frame và job nhiều frame dùng chung clean plate đã track. F4 tự ghép một frame. F3 chưa triển khai.
- **Việc cần làm tiếp theo**:
  1. Nối lệnh Apply của F4 với manifest sequence từ F2 và kiểm tra Difference trên toàn đoạn.
  2. Triển khai F3 trên API job thực tế, bắt đầu từ chọn shot, xem patch và trạng thái job.
  3. Mở rộng F2 với SAM 2 và ProPainter cho shot cần theo dõi mask hoặc inpaint theo thời gian.
