# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.db.changes import SyncRead, syncs

router = APIRouter(tags=["changes"])


@router.get("/changes")
async def get_changes(session: SessionDep) -> list[SyncRead]:
    """Each sync: the versions it brought in (real version state), what they
    deterministically affected, and the study plan before vs after by task key."""
    return await syncs(session)
