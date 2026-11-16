"""Names that keep two roots of the same tool apart.

Xcode's coding assistants write the same JSONL formats as the terminal tools,
just under ~/Library/Developer/Xcode/CodingAssistant/. The parsers are shared;
a Variant is everything that must differ so the two never overwrite each
other: the `source` tag on rows (which the dashboard splits on), the row-id
prefix, and the `ingest_state` key prefix (state keys are relative to the
archive, so the same relative path under two archives would otherwise collide).
"""

from __future__ import annotations

from typing import NamedTuple


class Variant(NamedTuple):
    source: str
    id_prefix: str
    state_prefix: str
