# SPDX-License-Identifier: Apache-2.0
"""A part's official documents, fetched from its manufacturer unattended.

See :mod:`.fetch` for the route a fetch takes and where files are kept.
"""

from .fetch import DEFAULT_KINDS, KINDS, docs_root, fetch_part_docs

__all__ = ["DEFAULT_KINDS", "KINDS", "docs_root", "fetch_part_docs"]
