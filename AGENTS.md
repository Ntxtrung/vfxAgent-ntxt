# Project Rules & Guardrails — Nuke ↔ ComfyUI Remove/Inpaint Tool

Dự án này xây dựng tool Remove/Inpaint vật thể và người cho footage phim quay thật (hỗ trợ Nuke `Read` EXR/DPX/MOV/MP4 ↔ Giao diện kiểu Neat Video ↔ ComfyUI trên RTX 5060 Ti 16GB).

## 1. Kiến trúc bắt buộc: Tách 2 tiến trình riêng biệt
Để tránh giật lag giao diện khi xử lý AI/Video nặng, hệ thống **BẮT BUỘC** tách thành 2 tiến trình độc lập:
1. **Tiến trình chính (Core / Worker Process — `src/core/`)**:
   - Chạy FastAPI + Worker queue độc lập.
   - Đảm nhiệm: giao tiếp ComfyUI API, xử lý FFmpeg/OpenImageIO/OpenCV, SAM 2 tracking, ProPainter/Inpaint, quản lý cache frame.
   - **CẤM** chứa code UI (React, PySide, Tkinter) hay chặn vòng lặp vẽ giao diện.
2. **Tiến trình vẽ giao diện (UI Process — `src/ui/`)**:
   - Chạy tách biệt (Web/Electron/Desktop UI), chuyên vẽ canvas mask, timeline player 60fps, Before/After slider.
   - Chỉ giao tiếp với Core Process qua HTTP/WebSocket (`contracts`).
   - **CẤM** chạy model AI, đọc/ghi file video raw nặng, hoặc import trực tiếp code từ `src/core/`.
3. **Cầu nối Nuke (`src/nuke_bridge/`)**:
   - Chỉ chứa script Python nhẹ chạy trong Foundry Nuke (`menu.py`, gizmo, render cache, auto-build node graph).

## 2. Ràng buộc mềm — CẤM TUYỆT ĐỐI (Agent Anti-Patterns)
Các lỗi mà AI agent hay mắc phải — **CẤM LÀM**:
1. **CẤM gộp UI và tác vụ nặng vào cùng 1 tiến trình**: Không chạy inference AI, FFmpeg, hoặc `time.sleep` đồng bộ trên UI thread hoặc trong `menu.py` của Nuke.
2. **CẤM vi phạm ranh giới import**:
   - `src/ui/` KHÔNG được import từ `src/core/` hoặc `src/nuke_bridge/`.
   - `src/core/` KHÔNG được `import nuke` (vì `nuke` chỉ tồn tại bên trong Foundry Nuke) và KHÔNG được import thư viện UI.
3. **CẤM truyền ảnh/video raw qua JSON Base64** cho sequence dài: Phải lưu cache file trên đĩa và truyền đường dẫn / URL endpoint.
4. **CẤM bỏ qua xử lý Colorspace (EXR/DPX)**: Không đẩy thẳng Linear/ACEScg vào ComfyUI mà không convert sang chuẩn phù hợp (sRGB/Log), và không merge ngược về Nuke mà thiếu node trả về đúng Colorspace gốc của `Read`.
5. **CẤM hardcode đường dẫn máy cá nhân** (như `C:\Users\Admin\...`) trong source code; phải dùng biến môi trường hoặc file config.
6. **CẤM lách CI**: Không thêm `# noqa`, `# type: ignore`, `@ts-ignore`, hoặc `any` chỉ để vượt qua lint/typecheck.
7. **CẤM kết thúc task khi CI chưa xanh**: Sau mọi thay đổi code, bắt buộc chạy `python scripts/ci_check.py`.

## 3. Kỹ năng dự án (Skills)
- Đọc `.agents/skills/project-architecture/SKILL.md` trước khi tạo file mới hoặc sửa cấu trúc.
- Đọc `.agents/skills/ci-and-debugging/SKILL.md` trước khi chạy thử app, kiểm tra lỗi, hoặc sửa bug.
- Tham chiếu `docs/FEATURE_MAP.md` để biết trạng thái tính năng và lệnh kiểm thử tương ứng.

## 4. Quy tắc GitHub & Handoff
- Sau mỗi version hoàn thành hoặc sau khi kiểm thử pass toàn bộ: bắt buộc cập nhật `docs/FEATURE_MAP.md`, commit kèm ghi chú nội dung chỉnh sửa và đẩy lên GitHub.
- Khi gần hết credit / tạm dừng: dừng code tính năng mới, cập nhật phần **Handoff & Next Steps** trong `docs/FEATURE_MAP.md`, đẩy trạng thái hiện tại lên GitHub và comment rõ những việc cần làm tiếp theo.
