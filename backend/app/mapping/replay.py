# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Replaying recorded verifier outputs: each call is answered with the
validated output a live run recorded for exactly that material (its
nonce-free hash). No network, no tokens."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.domain.enums import ModelRole


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mapping_material(user: str) -> str:
    """The material of a verifier message, without the per-call nonce."""
    begin = user.index(">>>\n", user.index("<<<MAPPING_MATERIAL ")) + 4
    return user[begin : user.index("\n<<<END_MAPPING_MATERIAL ")]


def material_key(user: str) -> str:
    return _sha(mapping_material(user))


class ReplayMissing(RuntimeError):
    pass


@dataclass(slots=True)
class MappingReplayProvider:
    by_material: dict[str, dict[str, Any]]
    name: str = "replay"
    outputs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @classmethod
    def from_recorded(cls, recorded: Sequence[dict[str, Any]]) -> MappingReplayProvider:
        return cls({item["material_sha256"]: item["output"] for item in recorded})

    def model_for(self, role: ModelRole) -> str:
        return "replay:recorded-mapping-outputs"

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
        thinking: bool | None = None,
    ) -> StructuredResult[T]:
        key = material_key(messages[-1].content)
        if key not in self.by_material:
            raise ReplayMissing(f"no recorded mapping output for material {key[:12]}")
        self.outputs.append((messages[-1].content, self.by_material[key]))
        return StructuredResult(
            output=schema.model_validate(self.by_material[key]),
            model=self.model_for(role),
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=0),
            attempts=1,
        )


def recorded_outputs(outputs: Sequence[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"material_sha256": material_key(user), "output": output} for user, output in outputs]
