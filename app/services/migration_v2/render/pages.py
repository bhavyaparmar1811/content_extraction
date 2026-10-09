"""Page numbers for the table of contents, measured by Microsoft Word's layout.

Only a layout engine knows on which page a heading lands. When Word is
installed (Windows), ``word_page_counter`` opens the rendered file read-only in
a hidden Word, reads the page of each TOC bookmark and closes it without
saving: the document's XML stays the renderer's own. Without Word the TOC
entries are still right; Word fills in their page numbers when it updates
fields on opening.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

PageCounter = Callable[[Path, list[str]], Optional[dict[str, int]]]

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "word_pages.ps1"


def word_page_counter(timeout: int = 180) -> Optional[PageCounter]:
    """A page counter backed by Word, or None where Word cannot be driven (not Windows, no PowerShell, no script)."""
    if sys.platform != "win32" or shutil.which("powershell") is None or not SCRIPT.exists():
        return None

    def count(path: Path, bookmarks: list[str]) -> Optional[dict[str, int]]:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                 "-Source", str(Path(path).resolve()), "-Names", "|".join(bookmarks)],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"Word page count failed: {exc}")
            return None
        if result.returncode != 0:
            logger.warning(f"Word page count failed: {(result.stdout + result.stderr).strip()[:300]}")
            return None
        pages = {}
        for line in result.stdout.splitlines():
            name, _, page = line.strip().rpartition("=")
            if name and page.isdigit():
                pages[name] = int(page)
        return pages

    return count
