"""Hard CI gatekeeper for structure, import boundaries, lint, and compilation."""

from __future__ import annotations

import ast
import json
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_DIRS = [
    "src/contracts",
    "src/core",
    "src/ui",
    "src/nuke_bridge",
    "tests",
    "scripts",
    "docs",
    "logs",
    ".agents/skills/project-architecture",
    ".agents/skills/ci-and-debugging",
]

REQUIRED_FILES = [
    "AGENTS.md",
    "pyproject.toml",
    "docs/FEATURE_MAP.md",
    ".agents/skills/project-architecture/SKILL.md",
    ".agents/skills/ci-and-debugging/SKILL.md",
]

SKIP_PARTS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build"}
CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx"}
UI_EXTS = {".ts", ".tsx", ".js", ".jsx", ".html", ".css"}


def iter_files(base: Path, exts: set[str] | None = None) -> list[Path]:
    """Return all files under base, skipping ignored directories."""
    results: list[Path] = []
    if not base.exists():
        return results
    for path in base.rglob("*"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_file() and (exts is None or path.suffix in exts):
            results.append(path)
    return results


def check_structure() -> list[str]:
    """Validate required folders, files, and process directory separation."""
    errors: list[str] = []
    for rel in REQUIRED_DIRS:
        if not (ROOT / rel).is_dir():
            errors.append(f"[STRUCTURE] Missing required directory: {rel}")

    for rel in REQUIRED_FILES:
        if not (ROOT / rel).is_file():
            errors.append(f"[STRUCTURE] Missing required file: {rel}")

    for item in ROOT.iterdir():
        if item.is_file() and item.suffix in CODE_EXTS:
            errors.append(
                f"[STRUCTURE] Stray code file at project root: {item.name} "
                "(move into src/ or scripts/)"
            )

    for backend_dir in ("src/core", "src/nuke_bridge", "src/contracts"):
        for ui_file in iter_files(ROOT / backend_dir, UI_EXTS):
            errors.append(
                f"[STRUCTURE] UI file '{ui_file.relative_to(ROOT)}' "
                f"must live under src/ui/, not {backend_dir}/"
            )

    return errors


def _extract_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Extract (line_number, module_name) pairs from an AST."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def check_imports() -> list[str]:
    """Enforce import boundaries between Core, UI, Nuke Bridge, and Contracts."""
    errors: list[str] = []
    banned_in_core = {"nuke", "nukescripts", "PySide2", "PySide6", "PyQt5", "PyQt6", "tkinter"}

    for py_file in iter_files(ROOT, {".py"}):
        rel = py_file.relative_to(ROOT).as_posix()
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            errors.append(f"[COMPILE] SyntaxError in {rel}:{exc.lineno}: {exc.msg}")
            continue

        imports = _extract_imports(tree)
        for lineno, mod in imports:
            top = mod.split(".")[0]
            if rel.startswith("src/core/"):
                if top in banned_in_core or mod.startswith(("src.ui", "src.nuke_bridge")):
                    errors.append(
                        f"[IMPORT] {rel}:{lineno} forbidden import '{mod}' in Core process"
                    )
            elif rel.startswith("src/ui/"):
                if top in {"nuke", "nukescripts"} or mod.startswith(
                    ("src.core", "src.nuke_bridge")
                ):
                    errors.append(
                        f"[IMPORT] {rel}:{lineno} forbidden import '{mod}' in UI process"
                    )
            elif rel.startswith("src/contracts/") and (
                top in {"nuke", "nukescripts"}
                or mod.startswith(("src.core", "src.ui", "src.nuke_bridge"))
            ):
                errors.append(
                    f"[IMPORT] {rel}:{lineno} forbidden import '{mod}' in Contracts"
                )

    return errors


def _run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a subprocess command and return (returncode, combined_output)."""
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode, output


def check_lint() -> list[str]:
    """Run Ruff for Python and ESLint for UI (when configured)."""
    errors: list[str] = []
    code, out = _run_cmd(["ruff", "check", "."], ROOT)
    if code != 0:
        errors.append(f"[LINT] ruff check failed:\n{out}")

    ui_pkg = ROOT / "src/ui/package.json"
    if ui_pkg.is_file():
        pkg_data = json.loads(ui_pkg.read_text(encoding="utf-8"))
        if "lint" in pkg_data.get("scripts", {}):
            code, out = _run_cmd(["npm", "run", "lint"], ROOT / "src/ui")
            if code != 0:
                errors.append(f"[LINT] UI npm run lint failed:\n{out}")

    return errors


def check_compile() -> list[str]:
    """Compile-check all Python files and TypeScript UI (when configured)."""
    errors: list[str] = []
    for py_file in iter_files(ROOT, {".py"}):
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"[COMPILE] {py_file.relative_to(ROOT)}: {exc.msg}")

    ui_tsconfig = ROOT / "src/ui/tsconfig.json"
    if ui_tsconfig.is_file():
        code, out = _run_cmd(["npx", "tsc", "--noEmit"], ROOT / "src/ui")
        if code != 0:
            errors.append(f"[COMPILE] TypeScript tsc --noEmit failed:\n{out}")

    return errors


def main() -> int:
    """Execute all CI checks and exit non-zero on any failure."""
    checks = [
        ("Structure", check_structure),
        ("Imports", check_imports),
        ("Lint", check_lint),
        ("Compile", check_compile),
    ]

    all_errors: list[str] = []
    for name, fn in checks:
        errs = fn()
        status = "PASS" if not errs else f"FAIL ({len(errs)})"
        print(f"==> {name}: {status}")
        for err in errs:
            print(f"  {err}")
        all_errors.extend(errs)

    if all_errors:
        print(f"\nCI RED: {len(all_errors)} issue(s) found.")
        return 1

    print("\nCI GREEN: All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
