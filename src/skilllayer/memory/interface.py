from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SkillMemory:
    path: Path | None = None

    def load(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, records: list[dict[str, Any]]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        records = self.load()
        records.append(record)
        self.save(records)

    def find_by_task(self, task_description: str) -> list[dict[str, Any]]:
        needle = task_description.lower()
        return [record for record in self.load() if needle in str(record.get("task_description", "")).lower()]
