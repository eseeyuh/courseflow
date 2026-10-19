# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Dataset-authoring diagnostic: how many of a query's words its target uses.

For every positive case and target alternative: the query's and the target
quote's PostgreSQL-normalised lexemes (the same ``english`` normalisation
lexical search uses), their intersection, and two exact ratios. It measures
the wording of the dataset, not the behaviour of any retriever: nothing is
ranked or embedded, and no chunk is scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy.ext.asyncio import AsyncSession

from app.evaluation.retrieval.dataset import Dataset
from app.retrieval.rankers import query_lexemes


@dataclass(frozen=True, slots=True)
class LexicalOverlap:
    case_id: str
    category: str
    group_id: str
    target: str  # course/resource/vN
    query_lexemes: tuple[str, ...]
    target_lexemes: tuple[str, ...]
    shared: tuple[str, ...]
    # shared / query lexemes: how much of the query the target repeats.
    query_ratio: Fraction
    # shared / target lexemes: how much of the target the query repeats.
    target_ratio: Fraction


async def lexical_overlap(session: AsyncSession, dataset: Dataset) -> list[LexicalOverlap]:
    rows = []
    for case in dataset.cases:
        if not case.is_positive:
            continue
        query = await query_lexemes(session, case.query)
        for group in case.target_groups:
            for ref in group.alternatives:
                target = await query_lexemes(session, ref.quote)
                shared = sorted(set(query) & set(target))
                rows.append(
                    LexicalOverlap(
                        case_id=case.case_id,
                        category=case.category.value,
                        group_id=group.group_id,
                        target=f"{ref.course}/{ref.resource}/v{ref.version}",
                        query_lexemes=tuple(query),
                        target_lexemes=tuple(target),
                        shared=tuple(shared),
                        query_ratio=Fraction(len(shared), len(query)) if query else Fraction(0),
                        target_ratio=Fraction(len(shared), len(target)) if target else Fraction(0),
                    )
                )
    return rows
