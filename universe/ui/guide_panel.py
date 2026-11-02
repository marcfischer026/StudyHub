"""Guide tab: exam sheets per topic, and per lecture the notes the student did not manage to take.

All rendered by the app from what is stored; ``notes.md`` is never written. Links carry what they
need in their href: ``cite:SL04.p12`` through the page's citation map, ``src:3`` through its list of
sources, ``accept:prof:0`` and ``dismiss:doc:1`` for suggestions.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap, QTextDocument
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QTextBrowser, QVBoxLayout

from ..professors import ProfessorStore
from ..services import fsrs, practice
from ..services.study_store import Knowledge, StudyStore
from ..services.subject_model import SubjectView, load_subject
from ..storage import Lecture, Subject
from .claude_panel import ScopedPanel, exam_date
from .style import DANGER, NEUTRAL, SUCCESS, WARNING

ROLE = Qt.ItemDataRole.UserRole
MAX_LIST_HEIGHT = 190
UNSEEN, WEAK, FAIR, STRONG = NEUTRAL, DANGER, WARNING, SUCCESS
_BRACKETS = re.compile(r"\[([^\[\]\n]+)\]")
_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL)
_LABEL_ORDER = re.compile(r"(\d+)\.p(\d+)")
ISSUES = {
    "wrong_subject": "seems to belong to another course. Accepting keeps it out of exports; Documents… on the Claude tab undoes that.",
    "outdated": "seems outdated.",
    "duplicate": "seems to be a duplicate.",
    "unreadable": "could not be read properly.",
}


def recall_colour(recall: float | None) -> str:
    if recall is None:
        return UNSEEN
    return STRONG if recall >= 0.85 else FAIR if recall >= practice.WEAK_RECALL else WEAK


def _dot(colour: str) -> QIcon:
    pixmap = QPixmap(10, 10)
    pixmap.fill(QColor(colour))
    return QIcon(pixmap)


def _e(value: object) -> str:
    return html.escape(str(value))


def link_citations(markdown: str, citations: dict) -> str:
    """``[SL04.p12, R07@751]`` becomes Markdown links. What the citation map does not know stays plain text."""

    def replace(match: re.Match) -> str:
        labels = [label.strip() for label in re.split(r"[,;]", match.group(1))]
        if not any(label in citations for label in labels):
            return match.group(0)
        return "[" + ", ".join(f"[{label}](cite:{label})" if label in citations else label for label in labels) + "]"

    return _BRACKETS.sub(replace, markdown)


def markdown_html(markdown: str) -> str:
    document = QTextDocument()
    document.setMarkdown(_shift_headings(markdown))
    match = _BODY.search(document.toHtml())
    return match.group(1) if match else ""


_HEADING = re.compile(r"^(\s{0,3})(#{1,6})(\s|$)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _shift_headings(markdown: str) -> str:
    """Claude's `#` is below the page's `<h2>` title, so `#` becomes `###`, never below `h6`."""
    out, fenced = [], False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            indent, hashes, gap = m.groups()
            line = f"{indent}{'#' * min(6, len(hashes) + 2)}{gap}{line[m.end():]}"
        out.append(line)
    return "\n".join(out)


def _source_order(item: dict) -> tuple:
    """Slide order: the first page cited, then the first moment; what cites neither comes last."""
    sources = item.get("sources") or []
    for source in sources:
        if source.get("kind") == "page" and (match := _LABEL_ORDER.search(str(source.get("label", "")))):
            return (0, int(match.group(1)), int(match.group(2)), 0.0)
    for source in sources:
        if source.get("kind") == "line":
            return (1, 0, 0, float(source.get("t", 0.0)))
    return (2, 0, 0, 0.0)


class GuidePanel(ScopedPanel):
    status = Signal(str)
    sourceActivated = Signal(dict)  # a durable source
    changed = Signal()  # a suggestion was decided

    def __init__(self, professors: ProfessorStore, parent=None):
        super().__init__(parent)
        self.professors = professors
        self.store: StudyStore | None = None
        self.view: SubjectView | None = None
        self.cards: dict = {}
        self.citations: dict = {}
        self.links: list[dict] = []
        self.current: tuple[str, str] | None = None

        self.list = QListWidget()
        self.list.setMaximumHeight(MAX_LIST_HEIGHT)
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(self._selected)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self.open_link)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.list)
        layout.addWidget(self.browser, 1)

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        self.store = StudyStore(subject) if subject else None
        super().set_scope(subject, lecture)

    # --- the list -----------------------------------------------------------

    def _header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list.addItem(item)

    def _entry(self, text: str, key: tuple[str, str], colour: str = "") -> None:
        item = QListWidgetItem(text)
        item.setData(ROLE, key)
        if colour:
            item.setIcon(_dot(colour))
        self.list.addItem(item)

    def refresh(self) -> None:
        super().refresh()
        self.list.blockSignals(True)
        self.list.clear()
        if self.store is None:
            self.list.blockSignals(False)
            self.browser.setHtml("<p>Pick a subject first.</p>")
            return
        knowledge = self.store.knowledge()
        self.view = load_subject(self.subject, self.professors, pages=False)
        open_count = self._open_suggestions(knowledge)
        if open_count:
            self._entry(f"Suggestions ({open_count})", ("suggestions", ""))
        topics = {t.id: t for t in self._progress(knowledge)}
        if topics:
            self._header("Topics")
        for key, topic in sorted(knowledge.topics.items(), key=lambda kv: (-int(kv[1].get("importance", 2)), kv[1].get("title", ""))):
            if not topic.get("retired"):
                progress = topics.get(key)
                self._entry(f"{'★' * int(topic.get('importance', 2))} {topic.get('title', key)}", ("topic", key), recall_colour(progress.recall_now if progress else None))
        lectures = [v for v in self.view.lectures if self._lecture_has_content(knowledge, v.uid)]
        if lectures:
            self._header("Lectures")
        for lecture in lectures:
            self._entry(f"{lecture.ref} {lecture.name}", ("lecture", lecture.uid))
        self.list.blockSignals(False)
        rows = sum(self.list.sizeHintForRow(row) for row in range(self.list.count()))
        self.list.setMaximumHeight(min(MAX_LIST_HEIGHT, rows + 2 * self.list.frameWidth() + 2))
        if self.list.count() == 0:
            self.browser.setHtml("<p>No guide yet. Export for Claude and import its reply on the Claude tab.</p>")
            return
        self.select(*(self.current if self.current and self._find(self.current) is not None else self._first()))

    def _first(self) -> tuple[str, str]:
        for row in range(self.list.count()):
            key = self.list.item(row).data(ROLE)
            if key and key[0] != "suggestions":
                return key
        return self.list.item(0).data(ROLE) or ("suggestions", "")

    def _find(self, key: tuple[str, str]) -> QListWidgetItem | None:
        return next((self.list.item(r) for r in range(self.list.count()) if self.list.item(r).data(ROLE) == key), None)

    def select(self, kind: str, key: str = "") -> None:
        item = self._find((kind, key))
        if item is not None:
            self.list.setCurrentItem(item)
            self._show(kind, key)  # also when it already was the current one

    def _selected(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is not None and item.data(ROLE):
            self._show(*item.data(ROLE))

    def _progress(self, knowledge: Knowledge) -> list[practice.TopicProgress]:
        events = self.store.events()
        cards = practice.replay(knowledge, events, exam_date(self.subject))
        self.cards = cards
        return practice.topic_progress(knowledge, cards, events, datetime.now(), exam_date(self.subject))

    @staticmethod
    def _open_suggestions(knowledge: Knowledge) -> int:
        return sum(s.get("state") == "open" for s in knowledge.prof_suggestions) + sum(not n.get("dismissed") for n in knowledge.doc_notes)

    @staticmethod
    def _lecture_has_content(knowledge: Knowledge, uid: str) -> bool:
        said = any(item.get("lecture") == uid for item in knowledge.said.values())
        objectives = any(uid in (o.get("lectures") or []) and not o.get("retired") for o in knowledge.objectives.values())
        return said or objectives or bool(knowledge.note_fixes.get(uid))

    # --- pages --------------------------------------------------------------

    def _show(self, kind: str, key: str) -> None:
        self.current = (kind, key)
        self.citations, self.links = {}, []
        knowledge = self.store.knowledge()
        page = {"topic": self._topic_page, "lecture": self._lecture_page, "suggestions": lambda k, _key: self._suggestions_page(k)}[kind](knowledge, key)
        self.browser.setHtml(page)

    def _sources(self, sources: list) -> str:
        links, seen = [], set()
        for source in sources or []:
            if isinstance(source, dict) and source.get("label") and source["label"] not in seen:
                seen.add(source["label"])
                self.links.append(source)
                links.append(f"<a href='src:{len(self.links) - 1}'>{_e(source['label'])}</a>")
        return " · ".join(links)

    def _objective_rows(self, objectives: list[dict]) -> str:
        now = datetime.now()
        rows = []
        for objective in objectives:
            card = self.cards.get(objective.get("id"))
            recall = fsrs.retrievability(card.memory, now) if card and card.memory else None
            state = f"{recall:.0%}" if recall is not None else "new"
            badge = " ⚠ source not verified" if objective.get("verified") is False else ""
            rows.append(
                f"<li><span style='color:{recall_colour(recall)}'>{state}</span> {_e(objective.get('statement', ''))}"
                f"{badge} <small>{self._sources(objective.get('sources'))}</small></li>"
            )
        return f"<ul>{''.join(rows)}</ul>" if rows else ""

    def _said_rows(self, said: list[dict]) -> str:
        order = {"exam_hint": 0, "correction": 1, "example": 2, "explanation": 3}
        names = {"exam_hint": "Exam hint", "correction": "Correction", "example": "Example", "explanation": "Explanation"}
        rows = []
        for item in sorted(said, key=lambda s: (order.get(s.get("kind"), 9), _source_order(s))):
            rows.append(
                f"<li><b>{names.get(item.get('kind'), 'Said')}:</b> {_e(item.get('text', ''))} "
                f"<small>{self._sources(item.get('sources'))}</small></li>"
            )
        return f"<ul>{''.join(rows)}</ul>" if rows else ""

    def _conflict_rows(self, conflicts: list[dict]) -> str:
        rows = []
        for item in conflicts:
            check = " <b>Please check this one yourself.</b>" if item.get("needs_student_check") else ""
            working = {"a": item.get("a"), "b": item.get("b")}.get(item.get("working"))
            answer = f" Working answer: {self._sources([working])}." if working else ""
            rows.append(f"<li>{_e(str(item.get('issue', '')).rstrip('.'))}.{answer}{check} <small>{self._sources([item.get('a'), item.get('b')])}</small></li>")
        return f"<ul>{''.join(rows)}</ul>" if rows else ""

    def _topic_page(self, knowledge: Knowledge, key: str) -> str:
        topic = knowledge.topics.get(key, {})
        self._progress(knowledge)
        objectives = [o for o in knowledge.objectives.values() if o.get("topic") == key and not o.get("retired")]
        guide = knowledge.guides.get(key, {})
        self.citations = guide.get("citations") or {}
        parts = [f"<h2>{_e(topic.get('title', key))}</h2>"]
        if topic.get("why"):
            parts.append(f"<p>{_e(topic['why'])} <small>{self._sources(topic.get('sources'))}</small></p>")
        parts.append(markdown_html(link_citations(guide["md"], self.citations)) if guide.get("md") else "<p><i>No sheet for this topic yet.</i></p>")
        if topic.get("misconceptions"):
            parts.append("<h3>Typical traps</h3><ul>" + "".join(f"<li>{_e(m)}</li>" for m in topic["misconceptions"]) + "</ul>")
        if objectives:
            parts.append(f"<h3>What you should be able to do</h3>{self._objective_rows(sorted(objectives, key=_source_order))}")
        lectures = set(topic.get("lectures") or [])
        said = [s for s in knowledge.said.values() if s.get("lecture") in lectures]
        if said:
            parts.append(f"<h3>Said beyond the slides</h3>{self._said_rows(said)}")
        labels = set(self.citations) | {s.get("label") for o in objectives for s in o.get("sources") or []}
        conflicts = [c for c in knowledge.conflicts.values() if {(c.get("a") or {}).get("label"), (c.get("b") or {}).get("label")} & labels]
        if conflicts:
            parts.append(f"<h3>Where the sources disagree</h3>{self._conflict_rows(conflicts)}")
        return "".join(parts)

    def _conflict_in_lecture(self, conflict: dict, uid: str) -> bool:
        lecture = self.view.lecture(uid) if self.view else None
        if lecture is None:
            return False
        for side in (conflict.get("a"), conflict.get("b")):
            side = side or {}
            if side.get("kind") == "line" and (found := self.view.recording(side.get("rec", ""))) and found[0].uid == uid:
                return True
            if side.get("kind") == "page" and (document := self.view.document(side.get("doc", ""))) and lecture.ref in document.lecture_refs:
                return True
            if side.get("lecture") == uid:
                return True
        return False

    def _lecture_page(self, knowledge: Knowledge, uid: str) -> str:
        """The notes the student did not manage to take: built here, without extra output from Claude."""
        self._progress(knowledge)
        lecture = self.view.lecture(uid) if self.view else None
        parts = [f"<h2>{_e(f'{lecture.ref} {lecture.name}' if lecture else uid)}</h2>"]
        objectives = [o for o in knowledge.objectives.values() if uid in (o.get("lectures") or []) and not o.get("retired")]
        if objectives:
            parts.append(f"<h3>In slide order</h3>{self._objective_rows(sorted(objectives, key=_source_order))}")
        said = [s for s in knowledge.said.values() if s.get("lecture") == uid]
        if said:
            parts.append(f"<h3>Said beyond the slides</h3>{self._said_rows(said)}")
        conflicts = [c for c in knowledge.conflicts.values() if self._conflict_in_lecture(c, uid)]
        if conflicts:
            parts.append(f"<h3>Where the sources disagree</h3>{self._conflict_rows(conflicts)}")
        fixes = knowledge.note_fixes.get(uid) or []
        if fixes:
            written = {line.ref: line.text for line in lecture.notes} if lecture else {}
            rows = []
            for fix in fixes:
                mine = written.get(fix.get("line"), "")
                quoted = f"“{_e(mine)}”: " if mine else ""
                rows.append(f"<li>{_e(fix.get('line', ''))} {quoted}{_e(fix.get('fix', ''))} <small>{self._sources(fix.get('sources'))}</small></li>")
            parts.append(f"<h3>Fixes to your notes</h3><p><small>Your notes file stays as you wrote it.</small></p><ul>{''.join(rows)}</ul>")
        return "".join(parts)

    def _suggestions_page(self, knowledge: Knowledge) -> str:
        parts = ["<h2>Suggestions</h2><p>Nothing here changes until you accept it. What you wrote yourself is never replaced.</p>"]
        rows = []
        for index, suggestion in enumerate(knowledge.prof_suggestions):
            if suggestion.get("state") == "open":
                profile = self.professors.get(suggestion.get("prof", ""))
                value = suggestion.get("value")
                value = ", ".join(map(str, value)) if isinstance(value, list) else value
                rows.append(
                    f"<p><b>{_e(profile.name if profile else 'Professor')}</b>, {_e(str(suggestion.get('field', '')).replace('_', ' '))}: {_e(value)} "
                    f"<small>{self._sources(suggestion.get('sources'))}</small><br>"
                    f"<a href='accept:prof:{index}'>Accept</a> · <a href='dismiss:prof:{index}'>Dismiss</a></p>"
                )
        for index, note in enumerate(knowledge.doc_notes):
            if not note.get("dismissed"):
                rows.append(
                    f"<p><b>{_e(note.get('ref', note.get('doc', '')))}</b> {_e(ISSUES.get(note.get('issue'), ''))} {_e(note.get('detail', ''))}<br>"
                    f"<a href='accept:doc:{index}'>Accept</a> · <a href='dismiss:doc:{index}'>Dismiss</a></p>"
                )
        return "".join(parts + (rows or ["<p>No open suggestions.</p>"]))

    # --- links --------------------------------------------------------------

    def open_link(self, url: QUrl) -> None:
        scheme, _, rest = url.toString().partition(":")
        if scheme == "cite" and rest in self.citations:
            self.sourceActivated.emit(self.citations[rest])
        elif scheme == "src" and rest.isdigit() and int(rest) < len(self.links):
            self.sourceActivated.emit(self.links[int(rest)])
        elif scheme in ("accept", "dismiss"):
            kind, _, index = rest.partition(":")
            if kind not in ("prof", "doc") or not index.isdigit():
                return
            if self.store is None:
                return
            known = self.store.knowledge()
            total = len(known.prof_suggestions) if kind == "prof" else len(known.doc_notes)
            if int(index) >= total:
                return
            self._decide(scheme == "accept", kind, int(index))

    def _decide(self, accept: bool, kind: str, index: int) -> None:
        if kind == "prof":
            self.store.resolve_suggestion(index, accept, self.professors)
        elif accept:
            self.store.apply_doc_note(index)
        else:
            self.store.dismiss_doc_note(index)
        self.status.emit("Accepted." if accept else "Dismissed.")
        self.current = ("suggestions", "") if self._open_suggestions(self.store.knowledge()) else None
        self.changed.emit()
        self.refresh()

