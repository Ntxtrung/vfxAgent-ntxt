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
- **Trạng thái**: `[~] In Progress`. Luồng `single_frame_inpaint` đã chạy trên ComfyUI cục bộ với EXR 2048 × 802 và mask Nuke.
- **Mô tả**: FastAPI quản lý hàng đợi nền và gửi ảnh cùng mask dạng file PNG sang ComfyUI. Job một frame giữ các bản mask, patch và manifest riêng theo `job_id`. Các chế độ AI theo chuỗi như ProPainter và SAM 2 vẫn trong kế hoạch. Job mô phỏng Pillow chỉ dùng với shot demo.
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

---

## Quy trình mở chạy thử, bắt lỗi & tự động sửa
1. Chạy CI trước: `python scripts/ci_check.py`
2. Mở app theo feature cần test và theo dõi log tại `logs/core.log`, `logs/ui.log`.
3. Khi phát sinh lỗi: ghi nhận **Feature ID**, **File/Line**, **Traceback** → báo ngắn gọn cho người dùng → sửa code → chạy lại `python scripts/ci_check.py` và test của feature đó.

---

## Handoff & Next Steps (Cập nhật mỗi lần push GitHub)
- **Trạng thái hiện tại**: F0 và F1 đã hoàn tất. F2 và F4 có luồng một frame với nguồn EXR thật, mask alpha từ Nuke, inpaint thật trên ComfyUI và graph ghép trong Nuke. F3 chưa triển khai.
- **Việc cần làm tiếp theo**:
  1. Kiểm tra một đoạn EXR ngắn để đánh giá ổn định theo thời gian, VRAM và chất lượng vùng nền tái tạo.
  2. Triển khai F3 trên API job thực tế, bắt đầu từ chọn shot, xem patch và trạng thái job.
  3. Mở rộng F2/F4 cho sequence, tracking mask và hoàn thiện composite.
