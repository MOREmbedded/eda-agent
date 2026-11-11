# SPDX-License-Identifier: Apache-2.0
"""MkDocs hooks for the product website.

The documents in docs/ are written to read well on GitHub, so they link
to files outside docs/ (``../CHANGELOG.md``, ``../LICENSE``,
``../assets/dashboard.png``, source files). The website only publishes
docs/, so without this those links would 404. Two fixes:

* the screenshots in the repository's ``assets/`` folder are added to
  the site as ``assets/<name>``, and image links are pointed at them;
* every other link that leaves docs/ is sent to the file on GitHub.

Nothing here edits a file on disk; it only changes what is published.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

REPO = "https://github.com/MOREmbedded/eda-agent"
BRANCH = "main"
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

# Markdown links and images, plus src="..." in raw HTML.
_MD_LINK = re.compile(r"(\]\()(?!https?:|mailto:|#)([^)\s]+)(\))")
_HTML_SRC = re.compile(r'((?:src|href)=")(?!https?:|mailto:|#)([^"]+)(")')


def on_files(files, config):
    """Publish the repository's screenshots and logo with the site."""
    for path in sorted(ASSETS.glob("*")):
        if path.is_file():
            files.append(File(
                f"assets/{path.name}", src_dir=str(ROOT),
                dest_dir=config["site_dir"],
                use_directory_urls=config["use_directory_urls"]))
    return files


def _rewrite(target: str, page_src: str) -> str:
    path, sep, frag = target.partition("#")
    if not path:
        return target
    # Where the link points, relative to the repository root.
    here = posixpath.dirname(f"docs/{page_src}")
    resolved = posixpath.normpath(posixpath.join(here, path))
    if resolved.startswith("docs/"):
        return target  # stays inside the site; MkDocs resolves it
    if resolved.startswith("assets/"):
        # In the site the asset sits at <site root>/assets/x.
        site_rel = posixpath.relpath(
            resolved, posixpath.dirname(page_src) or ".")
        return site_rel + (sep + frag if sep else "")
    kind = "tree" if (ROOT / resolved).is_dir() else "blob"
    return f"{REPO}/{kind}/{BRANCH}/{resolved}" + (sep + frag if sep else "")


def on_page_markdown(markdown, page, config, files):
    src = page.file.src_uri

    def md(m):
        return m.group(1) + _rewrite(m.group(2), src) + m.group(3)

    markdown = _MD_LINK.sub(md, markdown)
    markdown = _HTML_SRC.sub(md, markdown)
    return markdown
