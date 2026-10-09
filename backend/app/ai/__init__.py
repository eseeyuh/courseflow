# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""AI provider boundary (ADR-005).

Business code depends on ``AIProvider`` and the types in ``app.ai.provider``,
never on a vendor SDK. Only ``app.ai.nebius`` imports ``openai``.
"""
