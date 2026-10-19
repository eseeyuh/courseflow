# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The versioned semantics of a retrieval dataset.

A dataset names each rule by its versioned ID in ``manifest.json`` and
repeats its canonical definition; the loader refuses a manifest whose
definitions differ from these. Changing a rule's meaning is a new rule
version, never an edit.

``full-target-containment/v1``: a target is the absolute range
[T_start, T_end) of its quote in one resource version's extracted text. A
retrieved chunk of that same version with range [C_start, C_end) satisfies
the target if and only if C_start <= T_start and C_end >= T_end. Partial
overlap does not count. A target group is satisfied when any one of its
alternatives is satisfied.

``single-source-span/v1``: every labelled quote lies wholly inside one
SourceSpan: S_start <= T_start and S_end >= T_end for exactly one span.

``sha256-path-and-bytes/v1``: see ``dataset.dataset_sha256``.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable

RELEVANCE_RULE = "full-target-containment/v1"
TARGET_SPAN_RULE = "single-source-span/v1"
DATASET_HASH_SCHEME = "sha256-path-and-bytes/v1"

DEFINITIONS: dict[str, str] = {
    RELEVANCE_RULE: (
        "A target is the absolute range [T_start, T_end) of its quote in one resource "
        "version's extracted text. A retrieved chunk of that same resource version with "
        "range [C_start, C_end) satisfies the target if and only if C_start <= T_start and "
        "C_end >= T_end. Partial overlap does not count. A target group is satisfied when "
        "any one of its alternatives is satisfied."
    ),
    TARGET_SPAN_RULE: (
        "Every labelled quote lies wholly inside exactly one SourceSpan of its resource "
        "version: S_start <= T_start and S_end >= T_end."
    ),
    DATASET_HASH_SCHEME: (
        "SHA-256 over the ASCII line 'sha256-path-and-bytes/v1' and a LF, then, for each "
        "input file in ascending bytewise order of its relative POSIX path encoded as UTF-8: "
        "the path bytes, one NUL byte, the file's byte length as ASCII decimal digits with no "
        "leading zeros, one NUL byte, then the file bytes. Inputs are manifest.json, "
        "cases.jsonl and every corpus file the manifest lists; dataset.sha256 and README.md "
        "are not inputs. The result is written to dataset.sha256 as 64 lowercase hexadecimal "
        "digits and a LF."
    ),
}


def contains(outer_start: int, outer_end: int, inner_start: int, inner_end: int) -> bool:
    """[outer_start, outer_end) wholly contains [inner_start, inner_end)."""
    return outer_start <= inner_start and outer_end >= inner_end


def satisfies_target(
    chunk_version: Hashable,
    chunk_start: int,
    chunk_end: int,
    target_version: Hashable,
    target_start: int,
    target_end: int,
) -> bool:
    """``full-target-containment/v1`` for one chunk and one target alternative."""
    return chunk_version == target_version and contains(
        chunk_start, chunk_end, target_start, target_end
    )


def satisfies_group(
    chunk: tuple[Hashable, int, int],
    alternatives: Iterable[tuple[Hashable, int, int]],
) -> bool:
    """A target group is satisfied when any alternative is."""
    return any(satisfies_target(*chunk, *alternative) for alternative in alternatives)
