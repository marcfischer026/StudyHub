"""Professor profiles: how a lecturer teaches and tests, stored as JSON.

Each profile is one file, ``~/.universe_data/Professors/<id>.json``. Subjects
carry a default ``professor_id``; a lecture may override it.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass
class ProfessorProfile:
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    department: str = ""
    testing_style: str = ""  # e.g. "multiple choice + one long derivation, closed book"
    focus_areas: list[str] = field(default_factory=list)
    question_types: list[str] = field(default_factory=list)  # e.g. ["calculation", "case study"]
    grading_notes: str = ""
    style_notes: str = ""  # free-form observations: pet topics, phrasing, what they repeat

    def to_markdown(self) -> str:
        lines = [f"**Name:** {self.name}"]
        if self.department:
            lines.append(f"**Department:** {self.department}")
        if self.testing_style:
            lines.append(f"**Testing style:** {self.testing_style}")
        if self.focus_areas:
            lines.append(f"**Focus areas:** {', '.join(self.focus_areas)}")
        if self.question_types:
            lines.append(f"**Typical question types:** {', '.join(self.question_types)}")
        if self.grading_notes:
            lines.append(f"**Grading notes:** {self.grading_notes}")
        if self.style_notes:
            lines.append(f"**Style notes:** {self.style_notes}")
        return "\n".join(f"- {line}" for line in lines)


class ProfessorStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config.professors_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, prof_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", prof_id):
            raise ValueError(f"Invalid professor id: {prof_id!r}")
        return self.root / f"{prof_id}.json"

    def all(self) -> list[ProfessorProfile]:
        profiles = []
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            profiles.append(self._from_dict(raw))
        return sorted(profiles, key=lambda p: p.name.lower())

    def get(self, prof_id: str) -> ProfessorProfile | None:
        if not prof_id:
            return None
        path = self._path(prof_id)
        if not path.exists():
            return None
        return self._from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, profile: ProfessorProfile) -> None:
        self._path(profile.id).write_text(json.dumps(asdict(profile), indent=2, ensure_ascii=False), encoding="utf-8")

    def delete(self, prof_id: str) -> None:
        self._path(prof_id).unlink(missing_ok=True)

    @staticmethod
    def _from_dict(raw: dict) -> ProfessorProfile:
        known = {k: v for k, v in raw.items() if k in ProfessorProfile.__dataclass_fields__}
        return ProfessorProfile(**known)
