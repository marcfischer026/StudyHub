"""Workspace sidebar: subjects and their lectures."""

from __future__ import annotations

import re
import subprocess

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..storage import Lecture, Subject, Workspace

ROLE = Qt.ItemDataRole.UserRole


class Sidebar(QWidget):
    lectureSelected = Signal(object)  # Lecture
    subjectSelected = Signal(object)  # Subject
    assignProfessorRequested = Signal(object)  # Subject | Lecture
    lectureDeleted = Signal(object, str)  # Subject, lecture name
    lectureRenamed = Signal(str, object)  # old name, renamed Lecture
    aboutToChange = Signal()  # before a rename/delete moves files on disk
    subjectChanged = Signal(object)  # Subject metadata edited (e.g. abbreviation)

    def __init__(self, workspace: Workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace

        self.filter = QLineEdit(placeholderText="Search subjects & lectures…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)

        self.tree = QTreeWidget()
        self.tree.setObjectName("subjectTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setRootIsDecorated(False)  # subjects stay expanded; no arrow column
        self.tree.setItemsExpandable(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._on_current_changed)

        self.empty = QLabel("No subjects yet.\nCreate one for each course you take.", wordWrap=True)
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        add_subject = QPushButton("+ Subject")
        add_subject.setToolTip("New subject (⌘⇧N)")
        add_subject.clicked.connect(self.new_subject)
        add_lecture = QPushButton("+ Lecture")
        add_lecture.setToolTip("New lecture in the selected subject (⌘N)")
        add_lecture.clicked.connect(self.new_lecture)

        buttons = QHBoxLayout()
        buttons.addWidget(add_subject)
        buttons.addWidget(add_lecture)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.addWidget(self.filter)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.empty, 1)
        layout.addLayout(buttons)

        self.refresh()

    # --- tree ---------------------------------------------------------------

    def refresh(self, select: Subject | Lecture | None = None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        target = None
        subjects = self.workspace.subjects()
        self.empty.setVisible(not subjects)
        self.tree.setVisible(bool(subjects))
        for subject in subjects:
            s_item = QTreeWidgetItem([f"📚  {subject.name}"])
            s_item.setData(0, ROLE, subject)
            font = s_item.font(0)
            font.setBold(True)
            s_item.setFont(0, font)
            self.tree.addTopLevelItem(s_item)
            if isinstance(select, Subject) and select.path == subject.path:
                target = s_item
            for lecture in subject.lectures():
                icon = "🎙" if lecture.audio_files() else "📝"
                l_item = QTreeWidgetItem([f"{icon}  {lecture.name}"])
                l_item.setData(0, ROLE, lecture)
                s_item.addChild(l_item)
                if isinstance(select, Lecture) and select.path == lecture.path:
                    target = l_item
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._apply_filter(self.filter.text())
        if target is not None:
            self.tree.setCurrentItem(target)

    def select_by_name(self, subject_name: str, lecture_name: str) -> None:
        subject = self.workspace.subject(subject_name) if subject_name else None
        lecture = subject.lecture(lecture_name) if subject and lecture_name else None
        if lecture or subject:
            self.refresh(select=lecture or subject)

    def current_subject(self) -> Subject | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        obj = item.data(0, ROLE)
        return obj.subject if isinstance(obj, Lecture) else obj

    def _on_current_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        obj = current.data(0, ROLE)
        if isinstance(obj, Lecture):
            self.lectureSelected.emit(obj)
        else:
            self.subjectSelected.emit(obj)

    def _apply_filter(self, text: str) -> None:
        needle = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            s_item = self.tree.topLevelItem(i)
            subject_hit = needle in s_item.text(0).lower()
            any_child = False
            for j in range(s_item.childCount()):
                child = s_item.child(j)
                visible = not needle or subject_hit or needle in child.text(0).lower()
                child.setHidden(not visible)
                any_child |= visible
            s_item.setHidden(bool(needle) and not (subject_hit or any_child))

    # --- actions ------------------------------------------------------------

    def new_subject(self) -> Subject | None:
        name, ok = QInputDialog.getText(self, "New Subject", "Subject name (e.g. Corporate Finance):")
        if ok and name.strip():
            try:
                subject = self.workspace.create_subject(name.strip())
            except (FileExistsError, ValueError) as exc:
                QMessageBox.warning(self, "New Subject", str(exc))
                return None
            self.refresh(select=subject)
            return subject
        return None

    def new_lecture(self) -> None:
        subject = self.current_subject() or self._only_subject() or self.new_subject()
        if subject is None:
            return
        default = f"Lecture {len(subject.lectures()) + 1}"
        name, ok = QInputDialog.getText(self, "New Lecture", f"Lecture name in {subject.name}:", text=default)
        if ok and name.strip():
            try:
                lecture = subject.create_lecture(name.strip())
            except (FileExistsError, ValueError) as exc:
                QMessageBox.warning(self, "New Lecture", str(exc))
                return
            self.refresh(select=lecture)

    def _only_subject(self) -> Subject | None:
        subjects = self.workspace.subjects()
        return subjects[0] if len(subjects) == 1 else None

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("New Subject…", self.new_subject)
        else:
            obj = item.data(0, ROLE)
            if isinstance(obj, Subject):
                menu.addAction("New Lecture…", self.new_lecture)
            menu.addAction("Rename…", lambda: self._rename(obj))
            if isinstance(obj, Subject):
                menu.addAction(f"Abbreviation ({obj.short_name})…", lambda: self._set_abbreviation(obj))
            menu.addAction("Assign Professor…", lambda: self.assignProfessorRequested.emit(obj))
            menu.addAction("Reveal in Finder", lambda: subprocess.run(["open", str(obj.path)]))
            menu.addSeparator()
            menu.addAction("Delete…", lambda: self._delete(obj))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _set_abbreviation(self, subject: Subject) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Abbreviation",
            f"Short code for “{subject.name}”, used to name recordings\n(e.g. BWL → “BWL 2026-10-28 15.30”):",
            text=subject.short_name,
        )
        if ok:
            code = re.sub(r"[^\w-]", "", text.strip())[:12]
            subject.update_meta(short_name=code)  # empty → derived from the name again
            self.subjectChanged.emit(subject)

    def _rename(self, obj: Subject | Lecture) -> None:
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=obj.name)
        if not ok or not name.strip() or name.strip() == obj.name:
            return
        self.aboutToChange.emit()
        try:
            if isinstance(obj, Lecture):
                old_name = obj.name
                renamed = obj.subject.rename_lecture(obj, name.strip())
                self.lectureRenamed.emit(old_name, renamed)
            else:
                renamed = self.workspace.rename_subject(obj, name.strip())
        except (FileExistsError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "Rename", str(exc))
            return
        self.refresh(select=renamed)

    def _delete(self, obj: Subject | Lecture) -> None:
        kind = "lecture" if isinstance(obj, Lecture) else "subject and all its lectures"
        answer = QMessageBox.question(
            self,
            "Delete",
            f"Delete the {kind} “{obj.name}”?\nAll recordings, notes and slides in it are removed from disk.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.aboutToChange.emit()
        if isinstance(obj, Lecture):
            obj.subject.delete_lecture(obj)
            self.lectureDeleted.emit(obj.subject, obj.name)
        else:
            self.workspace.delete_subject(obj)
        self.refresh()
