"""Build an offline executable Python archive from the application sources."""

import shutil
import tempfile
import zipapp
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    destination = root / "build" / "qixing.pyz"
    destination.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        stage = Path(work)
        shutil.copytree(
            root / "qixing", stage / "qixing", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        zipapp.create_archive(
            stage,
            destination,
            interpreter="/usr/bin/env python3",
            main="qixing.__main__:main",
            compressed=True,
        )
    print(f"构建完成: {destination}")


if __name__ == "__main__":
    main()
