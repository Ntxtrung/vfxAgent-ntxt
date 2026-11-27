"""Run the upstream CLI with its local imports available in embedded Python."""

from __future__ import annotations

import runpy
import sys
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path


def main() -> None:
    script = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(script.parent))
    # The upstream checkout uses namespace packages. Embedded ComfyUI has its own utils package.
    for name in ("core", "utils"):
        package = ModuleSpec(name, loader=None, is_package=True)
        package.submodule_search_locations = [str(script.parent / name)]
        sys.modules[name] = module_from_spec(package)
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
