#!/usr/bin/env python3
"""Build the standalone YoDb documentation site from ../docs/*.mdx."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = MODULE_ROOT.parent
DOCS_ROOT = REPOSITORY_ROOT / "docs"
OUTPUT_ROOT = MODULE_ROOT / "dist"


def main() -> None:
    config = json.loads((MODULE_ROOT / "site.config.json").read_text())
    pages = [page for group in config["navigation"] for page in group["pages"]]

    missing = [page["path"] for page in pages if not (DOCS_ROOT / f"{page['path']}.mdx").is_file()]
    if missing:
        raise SystemExit(f"Navigation references missing documentation pages: {', '.join(missing)}")

    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    shutil.copytree(MODULE_ROOT / "static", OUTPUT_ROOT)
    (OUTPUT_ROOT / "content").mkdir()

    for document in DOCS_ROOT.rglob("*.mdx"):
        destination = OUTPUT_ROOT / "content" / document.relative_to(DOCS_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(document, destination)

    (OUTPUT_ROOT / "site.config.json").write_text(json.dumps(config, indent=2) + "\n")
    (OUTPUT_ROOT / ".nojekyll").touch()
    print(f"Built {len(pages)} documentation pages in {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
