# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.domain.enums import CourseStatus


class CourseRead(BaseModel):
    """Public representation of a course (API contract, not the ORM model).

    Only the fields declared here are exposed. Built from ORM objects with
    ``CourseRead.model_validate(course)`` (``from_attributes``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    institution_id: uuid.UUID
    title: str
    external_id: str | None
    status: CourseStatus
    created_at: datetime
