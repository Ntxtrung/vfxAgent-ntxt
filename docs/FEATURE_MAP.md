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
- **Trạng thái**: `[ ] Planned`
- **Mô tả**: FastAPI server (`127.0.0.1:8765`), quản lý job queue bất đồng bộ, gửi workflow sang ComfyUI API (RTX 5060 Ti 16GB), theo dõi tiến độ.
- **Thư mục**: `src/core/`
- **Chạy thử / Test**: `python -m pytest tests/test_core_api.py`

### F3 — Tiến trình giao diện (UI kiểu Neat Video)
- **Trạng thái**: `[ ] Planned`
- **Mô tả**: Cửa sổ UI riêng không làm đơ Nuke: Timeline từng frame, vẽ cọ/tẩy mask, click-to-mask & track SAM 2, chọn ảnh Reference/Clean-Plate, nhập Prompt, xem Before/After.
- **Thư mục**: `src/ui/`
- **Chạy thử / Test**: Khởi chạy 2 tiến trình qua `scripts/dev.py` (sẽ bổ sung khi vào code F2/F3).

### F4 — Nuke Auto-Composite: Trả về Colorspace gốc & Merge giữ chất lượng gốc
- **Trạng thái**: `[ ] Planned`
- **Mô tả**: Tự động tạo cụm node dưới `Read` gốc: `Read` kết quả + `Read` mask → `OCIOColorSpace` ngược về gốc → `EdgeBlur` → `Grain` (tùy chọn) → `Keymix` đè lên footage gốc.
- **Thư mục**: `src/nuke_bridge/`
- **Chạy thử / Test**: `python -m pytest tests/test_nuke_composite.py`

---

## Quy trình mở chạy thử, bắt lỗi & tự động sửa
1. Chạy CI trước: `python scripts/ci_check.py`
2. Mở app theo feature cần test và theo dõi log tại `logs/core.log`, `logs/ui.log`.
3. Khi phát sinh lỗi: ghi nhận **Feature ID**, **File/Line**, **Traceback** → báo ngắn gọn cho người dùng → sửa code → chạy lại `python scripts/ci_check.py` và test của feature đó.

---

## Handoff & Next Steps (Cập nhật mỗi lần push GitHub)
- **Trạng thái hiện tại**: Đã hoàn tất `F0` và `F1` (`src/contracts/models.py`, `src/nuke_bridge/colorspace.py`, `exporter.py`, `menu.py`, 5/5 unit tests pass, CI xanh).
- **Việc cần làm tiếp theo**:
  1. Triển khai `F2` (`src/core/` FastAPI + ComfyUI API cổng `8188` trên RTX 5060 Ti 16GB).
  2. Triển khai `F3` (`src/ui/` WebUI React + TypeScript App Window: vẽ mask/track/prompt/Before-After kiểu Neat Video).
  3. Triển khai `F4` (`src/nuke_bridge/` auto-build node trả về colorspace gốc + `Keymix` giữ nguyên chất lượng).
