"""Rebuild the search index from the seed corpora.

    python -m incident_copilot.retrieval

Idempotent, so it is safe to run on every deploy and after editing any runbook.
"""

from __future__ import annotations

import logging

from incident_copilot.config import get_settings
from incident_copilot.db.session import session_scope
from incident_copilot.retrieval.index import reindex


def main() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)-5s %(message)s")
    with session_scope() as session:
        count = reindex(session)
    logging.info("indexed %s chunk(s) from seed/", count)


if __name__ == "__main__":
    main()
