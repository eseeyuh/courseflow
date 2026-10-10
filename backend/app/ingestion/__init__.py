# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Provenance-preserving document ingestion.

Turns original source bytes (PDF, DOCX, PPTX, HTML) into structural blocks
that each know exactly where they came from. Ingestion is deterministic and
never calls a model: nothing in this package may import ``app.ai``.
Document text is untrusted data; it is stored verbatim, never interpreted.
"""
