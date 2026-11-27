"""Run Core and Vite as independent local processes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from time import sleep

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from src.core.config import CoreConfig

    config = CoreConfig.from_env(ROOT)
    ui = ROOT / "src" / "ui"
    vite = ui / "node_modules" / "vite" / "bin" / "vite.js"
    node = shutil.which("node")
    if node is None or not vite.is_file():
        print("Install UI dependencies with npm ci --prefix src/ui.", file=sys.stderr)
        return 1

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    children: list[subprocess.Popen] = []
    with ExitStack() as stack:
        try:
            core_log = stack.enter_context((config.logs_dir / "dev_core.log").open("ab"))
            ui_log = stack.enter_context((config.logs_dir / "ui.log").open("ab"))
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "src.core.server:app",
                        "--host",
                        config.core_host,
                        "--port",
                        str(config.core_port),
                    ],
                    cwd=ROOT,
                    stdout=core_log,
                    stderr=subprocess.STDOUT,
                )
            )
            children.append(
                subprocess.Popen(
                    [node, str(vite)],
                    cwd=ui,
                    stdout=ui_log,
                    stderr=subprocess.STDOUT,
                )
            )
            print(
                f"UI http://{config.ui_host}:{config.ui_port} | logs: {config.logs_dir}", flush=True
            )
            while all(child.poll() is None for child in children):
                sleep(0.25)
            print("Core or UI exited. See dev_core.log and ui.log.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 0
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    sys.exit(main())
