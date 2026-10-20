# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Structured extraction of academic claims from course content.

The model proposes; deterministic code decides. Every extractor follows
the same shape: small units of one current version's spans, server-issued
local span IDs, a versioned prompt with delimited untrusted material, a
typed structured call recorded as a ModelCall, then deterministic parsing
and the evidence gate before anything is persisted.
"""
