---
name: ci-and-debugging
description: >-
  Quy trình mở và chạy thử app theo Feature Map, chạy lớp kiểm tra CI báo đỏ,
  thu thập lỗi báo cáo người dùng và tự động sửa.
---

# CI, Feature Testing & Auto-Debug Workflow

## 1. Lớp ràng buộc cứng (CI Gatekeeper)
Mọi thay đổi code phải vượt qua `python scripts/ci_check.py`. Script này kiểm tra 4 lớp và **báo đỏ (exit code != 0)** nếu có bất kỳ lỗi nào:
1. **Cấu trúc code (`structure_check`)**: Đúng thư mục bắt buộc, không có file code trôi nổi sai vị trí.
2. **Kiểm tra import (`import_check`)**:
   - Kiểm tra ranh giới import giữa `src/core`, `src/ui`, `src/nuke_bridge`.
   - Cấm `import nuke` bên ngoài `src/nuke_bridge`.
   - Kiểm tra import hợp lệ bằng AST.
3. **Lint (`lint_check`)**: Chạy `ruff check` (và `eslint` nếu UI đã khởi tạo).
4. **Lỗi trình biên dịch / Typecheck (`compile_check`)**: Chạy `python -m py_compile` cho toàn bộ file `.py` (và `tsc --noEmit` nếu UI TypeScript đã khởi tạo).

## 2. Quy trình chạy thử App & Tự động sửa lỗi
1. Đọc `docs/FEATURE_MAP.md` để xác định feature cần mở/chạy thử.
2. Chạy CI trước: `python scripts/ci_check.py`. Nếu đỏ → sửa dứt điểm rồi mới chạy app.
3. Khởi chạy 2 tiến trình (Core + UI) và kiểm tra log trong `logs/`:
   - Ghi lại chính xác: **Bước thực hiện**, **Log/Traceback lỗi**, **File & dòng gây lỗi**.
4. **Báo lỗi ngắn gọn cho người dùng** (kèm file/dòng và nguyên nhân gốc) → **Tự động sửa** → Chạy lại `python scripts/ci_check.py` + test feature đó để xác nhận xanh.

## 3. Quy trình GitHub Checkpoint, Release & Handoff
1. **Sau khi hoàn thành 1 version / feature và pass kiểm thử**:
   - Chạy `python scripts/ci_check.py` và `python -m pytest tests/` đảm bảo xanh 100%.
   - Cập nhật trạng thái trong `docs/FEATURE_MAP.md` (`[x] Verified`) và ghi rõ nội dung chỉnh sửa (changelog).
   - Commit & `git push` lên GitHub với commit message rõ ràng (liệt kê các file/feature đã hoàn thiện và kết quả test).
2. **Khi gần hết credit hoặc cần tạm dừng giữa chừng**:
   - **Ngừng** việc mở rộng thêm code mới để tránh để lại trạng thái nửa vời.
   - Cập nhật mục **Handoff / Next Steps** ở cuối `docs/FEATURE_MAP.md`: ghi rõ những gì đã xong, những gì đang làm dở (nếu có), và danh sách việc cần làm tiếp theo cho phiên sau.
   - Chạy `python scripts/ci_check.py` để kiểm tra trạng thái repo.
   - Commit & `git push` lên GitHub kèm comment/tóm tắt rõ ràng các bước tiếp theo để phiên sau có thể tiếp tục ngay.
