# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Canonical domain vocabularies (see docs/architecture/data-contract.md).

Stored as VARCHAR + CHECK constraint, not native PostgreSQL ENUM types, so
adding a value is a small, reviewable migration. Extending any of these is a
contract change: update the data contract and add a migration.
"""

from enum import StrEnum


class LmsType(StrEnum):
    DEMO = "demo"
    UPLOAD = "upload"
    MOODLE = "moodle"


class CourseStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ResourceType(StrEnum):
    LECTURE = "lecture"
    LAB = "lab"
    READING = "reading"
    WORKSHOP = "workshop"
    HANDBOOK = "handbook"
    BRIEF = "brief"
    ANNOUNCEMENT = "announcement"
    PAGE = "page"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelRole(StrEnum):
    """What a call needs, resolved to a model ID by configuration."""

    FAST = "fast"
    STRONG = "strong"


class ModelCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelCallErrorCategory(StrEnum):
    """Why a model call failed. The first four are transient and retried."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    INVALID_OUTPUT = "invalid_output"
    UNEXPECTED = "unexpected"
