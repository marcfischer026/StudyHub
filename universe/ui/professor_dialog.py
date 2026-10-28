"""Create, edit and assign professor profiles."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..professors import ProfessorProfile, ProfessorStore

ROLE = Qt.ItemDataRole.UserRole


def _split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


class ProfessorManager(QDialog):
    def __init__(self, store: ProfessorStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Professor Profiles")
        self.resize(760, 520)
        self.store = store
        self.current: ProfessorProfile | None = None

        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._load_selected)
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete)
        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(new_btn)
        row.addWidget(delete_btn)
        left.addLayout(row)

        self.name = QLineEdit()
        self.department = QLineEdit()
        self.testing_style = QPlainTextEdit(placeholderText="e.g. 60% multiple choice, one long derivation, closed book")
        self.focus_areas = QLineEdit(placeholderText="comma separated: WACC, capital budgeting, …")
        self.question_types = QLineEdit(placeholderText="comma separated: calculation, case study, definition")
        self.grading_notes = QPlainTextEdit(placeholderText="Partial credit? Penalties? What earns full marks?")
        self.style_notes = QPlainTextEdit(placeholderText="Pet topics, phrases they repeat, hints dropped in class…")
        for box in (self.testing_style, self.grading_notes, self.style_notes):
            box.setFixedHeight(70)

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Department", self.department)
        form.addRow("Testing style", self.testing_style)
        form.addRow("Focus areas", self.focus_areas)
        form.addRow("Question types", self.question_types)
        form.addRow("Grading notes", self.grading_notes)
        form.addRow("Style notes", self.style_notes)

        save_btn = QPushButton("Save Profile")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(save_btn, alignment=Qt.AlignmentFlag.AlignRight)
        self.form_widget = QWidget()
        self.form_widget.setLayout(right)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addWidget(self.form_widget, 2)
        outer = QVBoxLayout(self)
        outer.addLayout(body)
        outer.addWidget(close)

        self._reload()

    def _reload(self, select_id: str = "") -> None:
        self.list.clear()
        for profile in self.store.all():
            item = QListWidgetItem(profile.name)
            item.setData(ROLE, profile.id)
            self.list.addItem(item)
            if profile.id == select_id:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.form_widget.setEnabled(self.list.count() > 0 or self.current is not None)

    def _load_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        self.current = self.store.get(item.data(ROLE)) if item else None
        self._fill_form(self.current or ProfessorProfile(name=""))

    def _fill_form(self, p: ProfessorProfile) -> None:
        self.name.setText(p.name)
        self.department.setText(p.department)
        self.testing_style.setPlainText(p.testing_style)
        self.focus_areas.setText(", ".join(p.focus_areas))
        self.question_types.setText(", ".join(p.question_types))
        self.grading_notes.setPlainText(p.grading_notes)
        self.style_notes.setPlainText(p.style_notes)

    def _new(self) -> None:
        self.list.blockSignals(True)
        self.list.setCurrentItem(None)
        self.list.blockSignals(False)
        self.current = ProfessorProfile(name="")
        self._fill_form(self.current)
        self.form_widget.setEnabled(True)
        self.name.setFocus()

    def _save(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Professor", "A name is required.")
            return
        profile = self.current or ProfessorProfile(name="")
        profile.name = self.name.text().strip()
        profile.department = self.department.text().strip()
        profile.testing_style = self.testing_style.toPlainText().strip()
        profile.focus_areas = _split_list(self.focus_areas.text())
        profile.question_types = _split_list(self.question_types.text())
        profile.grading_notes = self.grading_notes.toPlainText().strip()
        profile.style_notes = self.style_notes.toPlainText().strip()
        self.store.save(profile)
        self._reload(select_id=profile.id)

    def _delete(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        if QMessageBox.question(self, "Delete", f"Delete profile “{item.text()}”?") == QMessageBox.StandardButton.Yes:
            self.store.delete(item.data(ROLE))
            self.current = None
            self._reload()


class AssignProfessorDialog(QDialog):
    """Pick a professor for a subject or lecture; returns the id ('' = none)."""

    def __init__(self, store: ProfessorStore, target_name: str, current_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign Professor")
        self.store = store
        self.combo = QComboBox()
        self._fill(current_id)

        manage = QPushButton("Manage Profiles…")
        manage.clicked.connect(self._manage)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Professor for <b>{target_name}</b>:"))
        layout.addWidget(self.combo)
        layout.addWidget(manage)
        layout.addWidget(buttons)

    def _fill(self, select_id: str) -> None:
        self.combo.clear()
        self.combo.addItem("— none —", "")
        for profile in self.store.all():
            self.combo.addItem(profile.name, profile.id)
        index = self.combo.findData(select_id)
        self.combo.setCurrentIndex(max(0, index))

    def _manage(self) -> None:
        selected = self.selected_id()
        ProfessorManager(self.store, self).exec()
        self._fill(selected)

    def selected_id(self) -> str:
        return self.combo.currentData() or ""
