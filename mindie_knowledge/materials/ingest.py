"""One deterministic material preparation path for live capture and explicit import.

Native source records stay with the Harness. Only the authorized projection is
redacted here, then divided without omission into stable bounded UTF-8 blocks.
The small scanner state commits with the cursor, never in a second scheduler.
"""
from __future__ import annotations

import hashlib
import json
import re
from ..loop.transcript_redaction import redact_increment

BLOCK_BYTES = 16 * 1024
POLICY = 'complete-increment/1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def split_material(text, maximum=BLOCK_BYTES):
    """Prefer message/paragraph boundaries; preserve every character in order.

    An oversized single message is split at a Unicode boundary. Unlike a
    head/tail envelope, this never drops an interior span.
    """
    if type(maximum) is not int or maximum < 256:
        raise ValueError('invalid block byte limit')
    encoded = text.encode('utf-8')
    offset = 0
    while offset < len(encoded):
        if len(encoded) - offset <= maximum:
            yield encoded[offset:].decode('utf-8')
            return
        piece = encoded[offset:offset + maximum].decode('utf-8', 'ignore')
        boundary = max(piece.rfind('\n\n### '), piece.rfind('\n\n'))
        if boundary >= len(piece) // 2:
            piece = piece[:boundary + 2]
        if not piece:
            raise ValueError('block limit cannot hold a Unicode character')
        yield piece
        offset += len(piece.encode('utf-8'))


def prepare_increment(*, task_id, text, start, end, source_digest, scanner_state,
                      executable, key, private_paths, cancel=None):
    if not isinstance(text, str) or not (0 <= start < end):
        raise ValueError('invalid public source increment')
    masked, rules, state = redact_increment(text, state=scanner_state,
                                           executable=executable, key=key,
                                           private_paths=private_paths, cancel=cancel)
    if masked:
        masked += '\n\n'
    blocks = []
    for ordinal, piece in enumerate(split_material(masked)):
        block_id = _digest([POLICY, task_id, start, end, source_digest, ordinal])
        blocks.append(dict(block_id=block_id, text=piece,
                           source_range=dict(start=start, end=end, part=ordinal),
                           title='', summary=''))
    return dict(blocks=blocks, redaction_state=state, redaction_rules=rules,
                batch_id=_digest([POLICY, task_id, start, end, source_digest]))
