---
name: project-architecture
description: >-
  Skill cứng quy định cấu trúc thư mục, ranh giới 2 tiến trình (Core Worker vs UI),
  và quy ước tìm kiếm/kiểm thử cho dự án Nuke-ComfyUI Remove/Inpaint.
---

# Project Architecture & Directory Structure

## 1. Cấu trúc thư mục cứng (Bắt buộc tuân thủ)

```text
appFLowforRemove/
├── AGENTS.md                       # Quy tắc dự án & danh sách CẤM (always-on)
├── pyproject.toml                  # Cấu hình Ruff (lint/format) & Pytest
├── docs/
│   └── FEATURE_MAP.md              # Bản đồ tính năng, trạng thái, lệnh chạy & test
├── scripts/
│   ├── ci_check.py                 # CI gatekeeper (báo đỏ cấu trúc, import, lint, compile)
│   └── dev.py                      # Script khởi chạy 2 tiến trình (Core + UI) & gom log
├── src/
│   ├── contracts/                  # Schema/Types dùng chung (API payload, job status, Nuke metadata)
│   ├── core/                       # TIẾN TRÌNH CHÍNH: FastAPI server, ComfyUI client, Media/Cache IO
│   ├── ui/                         # TIẾN TRÌNH VẼ GIAO DIỆN: Canvas mask, Timeline, Before/After
│   └── nuke_bridge/                # Cầu nối nhẹ trong Nuke (menu.py, export cache, auto-build nodes)
├── tests/                          # Unit & integration tests (pytest)
└── logs/                           # Log runtime của Core và UI để debug tự động
```

## 2. Quy tắc tìm kiếm & đặt file
- **Cần sửa xử lý ComfyUI / FFmpeg / Queue / Cache**: Tìm trong `src/core/`.
- **Cần sửa giao diện vẽ mask / player / UI kiểu Neat Video**: Tìm trong `src/ui/`.
- **Cần sửa đọc node `Read`, convert colorspace, hoặc dựng node trả về Nuke**: Tìm trong `src/nuke_bridge/`.
- **Cần sửa cấu trúc dữ liệu trao đổi giữa Nuke ↔ Core ↔ UI**: Tìm trong `src/contracts/`.
- **Tuyệt đối không** tạo file code nguồn (`.py`, `.ts`, `.tsx`, `.js`) ở thư mục gốc dự án ngoài `scripts/`.

## 3. Giao tiếp giữa 2 tiến trình
- **Core Process (`src/core/`)**: Lắng nghe trên `http://127.0.0.1:<CORE_PORT>` (mặc định `8765`), xử lý bất đồng bộ (async/background worker), cập nhật tiến độ qua WebSocket/polling.
- **UI Process (`src/ui/`)**: Chạy ở tiến trình riêng, gọi API sang Core Process. Không bao giờ block luồng render UI.

## 4. Kiểm thử nhanh theo module
- Chạy toàn bộ kiểm tra CI cứng: `python scripts/ci_check.py`
- Chạy unit tests: `python -m pytest tests/`
