# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Content identity.

``content_hash`` (raw bytes) identifies content and decides whether an import
needs a new version: the bytes are the source of truth, and a parser upgrade
must not look like a source change.
``text_hash`` (extracted text) lets change analysis tell a re-saved file
from changed content.
"""

import hashlib


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_hash(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))
