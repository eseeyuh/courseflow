# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Format parsers: pure functions from original bytes to structural blocks.

Each parser keeps the source's own boundaries (page, slide, heading path)
and raises ``IngestionError`` for problems it recognises. No database, no
file system, no network and no model calls.
"""
