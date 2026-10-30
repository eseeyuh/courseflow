# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The pre-declared dev / held-out split of the mapping cases.

The split file names every requirement label of the golden dataset once,
with what its label says about material: ``materials`` (targets),
``no_material`` (an explicit empty list: the answer is none) or
``unlabelled`` (no materials field). Loading checks it against the frozen
labels: same dataset hash, every requirement exactly once, and each
expectation equal to what the raw label states.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.evaluation.northbridge.labels import REPO_ROOT, GoldenDataset, LabelError, RequirementLabel

SPLIT_FILE = (
    REPO_ROOT / "evals" / "datasets" / "northbridge" / "splits" / "material-mapping-v2.json"
)


class Expect(StrEnum):
    MATERIALS = "materials"
    NO_MATERIAL = "no_material"
    UNLABELLED = "unlabelled"


class Split(StrEnum):
    DEV = "dev"
    HELD_OUT = "held_out"


@dataclass(frozen=True, slots=True)
class MappingCase:
    label_id: str
    split: Split
    expect: Expect


def expectation(label: RequirementLabel) -> Expect:
    """What a requirement label says about material, from the raw label:
    an absent field is not the same as an empty one."""
    if "materials" not in label.model_fields_set:
        return Expect.UNLABELLED
    return Expect.MATERIALS if label.materials else Expect.NO_MATERIAL


def load_split(golden: GoldenDataset, path: Path = SPLIT_FILE) -> tuple[MappingCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["dataset"]["sha256"] != golden.sha256:
        raise LabelError(["the split was declared for another dataset version"])
    requirements = {label.label_id: label for label in golden.by_kind("requirement")}
    cases = [
        MappingCase(item["label_id"], split, Expect(item["expect"]))
        for split in Split
        for item in raw[split.value]
    ]
    named = [case.label_id for case in cases]
    if sorted(named) != sorted(requirements) or len(set(named)) != len(named):
        raise LabelError(["the split must name every requirement label exactly once"])
    for case in cases:
        if case.expect is not expectation(requirements[case.label_id]):
            raise LabelError([f"{case.label_id}: the split's expectation differs from the label"])
    return tuple(cases)
