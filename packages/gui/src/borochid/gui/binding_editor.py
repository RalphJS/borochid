"""What each button of a device does: a list of buttons with their actions
spelled out, and an editor dialog per button.

Schema (``"widget": "buttons"``)::

    {"widget": "buttons", "action": "set_binding", "reset": "reset_binding",
     "buttons": [{"id": "g4", "label": "G4 (back)"}, ...],     state "bind.<id>"
     "categories": [
       {"label": "Shortcut", "icon": "input-keyboard", "kind": "keys"},
       {"label": "Mouse", "icon": "input-mouse",
        "options": [{"value": {"button": 1}, "label": "Left click"}, ...]},
       ...]}

The action is invoked as ``{"button": id, "binding": value}``. Values are
opaque JSON that the driver validates; the GUI only compares them to the
options and recognises ``{"keys": [...]}`` from the shortcut recorder.

The shortcut recorder records what is pressed by physical key (scan code),
so a shortcut means the same keys whatever the layout, and shows it as
keycaps. Desktops keep some keys and shortcuts for themselves (Print,
Super+V, Alt+Tab) and the app never sees them, so the modifiers can also be
clicked on and any key can be chosen from a menu.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from borochid.common import keys as keytable
from borochid.gui.widgets import theme_icon, widget

# Scan codes on Linux (xcb and Wayland alike) are XKB keycodes: evdev + 8.
XKB_OFFSET = 8
# The order modifiers are shown and sent in.
MODIFIER_ORDER = ["KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT", "KEY_LEFTMETA"]
_RIGHT_TO_LEFT = {
    "KEY_RIGHTCTRL": "KEY_LEFTCTRL", "KEY_RIGHTSHIFT": "KEY_LEFTSHIFT",
    "KEY_RIGHTALT": "KEY_LEFTALT", "KEY_RIGHTMETA": "KEY_LEFTMETA",
}  # fmt: skip
_EDIT_ICONS = ["document-edit", "edit-entry", "document-properties"]


def describe(value: Any, categories: list[dict[str, Any]]) -> str:
    """The binding as a user would say it."""
    if isinstance(value, dict) and isinstance(value.get("keys"), list):
        return keytable.chord_label(value["keys"])
    for cat in categories:
        for opt in cat.get("options", []):
            if opt.get("value") == value:
                return str(opt.get("label", value))
    return "—" if value is None else str(value)


def _keycap(text: str) -> QLabel:
    cap = QLabel(text)
    cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
    cap.setMinimumWidth(32)
    cap.setStyleSheet(
        "QLabel { border: 1px solid palette(mid); border-bottom-width: 3px; border-radius: 5px;"
        " padding: 4px 10px; background: palette(button); font-weight: bold; }"
    )
    return cap


class ShortcutRecorder(QFrame):
    """Press a combination and it is recorded; modifiers can also be
    toggled with the keycap buttons below."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.mods: list[str] = []
        self.key: str | None = None
        self._held: set[str] = set()
        self.recording = False

        layout = QVBoxLayout(self)
        self.prompt = QLabel()
        self.prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prompt.setWordWrap(True)
        layout.addWidget(self.prompt)
        self.caps_host = QWidget()
        self.caps = QHBoxLayout(self.caps_host)
        self.caps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.caps_host)

        layout.addWidget(
            QLabel("<small>Your desktop keeps some keys for itself (Print, Super+V, Alt+Tab). Click the modifiers, or choose the key:</small>", wordWrap=True)
        )
        toggles = QHBoxLayout()
        self.toggles: dict[str, QPushButton] = {}
        for name in MODIFIER_ORDER:
            b = QPushButton(keytable.label(name))
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # typing stays with the recorder
            b.toggled.connect(lambda on, n=name: self._toggle(n, on))
            self.toggles[name] = b
            toggles.addWidget(b)
        layout.addLayout(toggles)
        again = QHBoxLayout()
        self.choose_button = QPushButton("Choose a key")
        self.choose_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.choose_button.setMenu(self._key_menu())
        again.addWidget(self.choose_button)
        self.record_button = QPushButton("Record again")
        self.record_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.record_button.clicked.connect(self.start)
        clear = QPushButton("Clear")
        clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear.clicked.connect(lambda: self.set_chord([]))
        again.addStretch(1)
        again.addWidget(self.record_button)
        again.addWidget(clear)
        layout.addLayout(again)
        self._refresh()

    # -- value ---------------------------------------------------------------------

    def chord(self) -> list[str]:
        """Modifiers in a fixed order, then the key; empty without a key."""
        return [m for m in MODIFIER_ORDER if m in self.mods] + [self.key] if self.key else []

    def set_chord(self, keys: list[str]) -> None:
        self.mods = [k for k in keys if k in keytable.MODIFIERS]
        self.key = next((k for k in keys if k not in keytable.MODIFIERS), None)
        self._sync_toggles()
        self._refresh()
        self.changed.emit()

    def _key_menu(self) -> QMenu:
        menu = QMenu(self)
        for group, names in keytable.GROUPS:
            sub = menu.addMenu(group)
            for name in names:
                sub.addAction(keytable.label(name), lambda n=name: self.choose(n))
        return menu

    def choose(self, name: str) -> None:
        """Set the key without pressing it; held modifiers stay."""
        self.stop()
        self.key = name
        self._refresh()
        self.changed.emit()

    # -- recording -----------------------------------------------------------------

    def start(self) -> None:
        self.recording = True
        self._held.clear()
        self.setFocus()
        self.grabKeyboard()  # keeps shortcuts from other widgets (and, where supported, the desktop) away
        self._refresh()

    def stop(self) -> None:
        if self.recording:
            self.recording = False
            self.releaseKeyboard()
            self._refresh()

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().focusInEvent(event)
        if not self.key:
            self.start()

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().focusOutEvent(event)
        self.stop()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().hideEvent(event)
        self.stop()  # never leave the keyboard grabbed behind a closed dialog

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.isAutoRepeat():
            return
        name = keytable.BY_CODE.get(event.nativeScanCode() - XKB_OFFSET)
        if not self.recording:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self.start()
            else:
                super().keyPressEvent(event)
            return
        if name is None:
            self.prompt.setText("That key can't be sent by a button. Try another one.")
            return
        name = _RIGHT_TO_LEFT.get(name, name)
        if name in keytable.MODIFIERS:
            self._held.add(name)
            self.mods = sorted(self._held | {m for m, b in self.toggles.items() if b.isChecked()}, key=MODIFIER_ORDER.index)
            self._refresh()
            return
        self.mods = sorted(set(self.mods) | self._held, key=MODIFIER_ORDER.index)
        self.key = name
        self._sync_toggles()
        self.stop()
        self.changed.emit()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.recording and not event.isAutoRepeat():
            name = keytable.BY_CODE.get(event.nativeScanCode() - XKB_OFFSET)
            self._held.discard(_RIGHT_TO_LEFT.get(name or "", name))
            self.mods = sorted(self._held | {m for m, b in self.toggles.items() if b.isChecked()}, key=MODIFIER_ORDER.index)
            self._refresh()

    def _toggle(self, name: str, on: bool) -> None:
        mods = set(self.mods) | {name} if on else set(self.mods) - {name}
        self.mods = sorted(mods, key=MODIFIER_ORDER.index)
        self._refresh()
        self.changed.emit()

    def _sync_toggles(self) -> None:
        for name, b in self.toggles.items():
            b.blockSignals(True)
            b.setChecked(name in self.mods)
            b.blockSignals(False)

    def _refresh(self) -> None:
        while self.caps.count():
            if w := self.caps.takeAt(0).widget():
                w.setParent(None)  # gone now, not at the next event loop turn
                w.deleteLater()
        shown = [m for m in MODIFIER_ORDER if m in self.mods] + ([self.key] if self.key else [])
        for i, k in enumerate(shown):
            if i:
                self.caps.addWidget(QLabel("+"))
            self.caps.addWidget(_keycap(keytable.label(k)))
        if self.recording:
            self.prompt.setText("Press the shortcut…" if not shown else "…and the key")
        elif self.key:
            self.prompt.setText("This button will press:")
        else:
            self.prompt.setText("Click here, then press the shortcut")
        self.record_button.setVisible(bool(self.key) and not self.recording)


class BindingDialog(QDialog):
    """Categories on the left, the choices of the selected one on the right."""

    def __init__(self, parent: QWidget | None, title: str, categories: list[dict[str, Any]], current: Any, can_reset: bool):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 340)
        self.categories = categories
        self.value: Any = None
        self.reset_requested = False

        grid = QGridLayout(self)
        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        self.pages = QStackedWidget()
        self._choices: dict[int, QListWidget] = {}
        self.recorder: ShortcutRecorder | None = None
        start = 0
        for i, cat in enumerate(categories):
            item = QListWidgetItem(str(cat.get("label", "")))
            if icon := theme_icon(cat.get("icon", "")):
                item.setIcon(icon)
            self.nav.addItem(item)
            if cat.get("kind") == "keys":
                self.recorder = ShortcutRecorder()
                if isinstance(current, dict) and isinstance(current.get("keys"), list):
                    self.recorder.set_chord(current["keys"])
                    start = i
                self.recorder.changed.connect(self._update)
                self.pages.addWidget(self.recorder)
                continue
            choices = QListWidget()
            for opt in cat.get("options", []):
                row = QListWidgetItem(str(opt.get("label", opt.get("value"))))
                row.setData(Qt.ItemDataRole.UserRole, opt.get("value"))
                choices.addItem(row)
                if opt.get("value") == current:
                    choices.setCurrentItem(row)
                    start = i
            choices.currentItemChanged.connect(self._update)
            choices.itemDoubleClicked.connect(lambda _item: self._accept_if_valid())
            self._choices[i] = choices
            self.pages.addWidget(choices)
        self.nav.currentRowChanged.connect(self._show)
        grid.addWidget(self.nav, 0, 0)
        grid.addWidget(self.pages, 0, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        if can_reset:
            reset = self.buttons.addButton("Reset to default", QDialogButtonBox.ButtonRole.ResetRole)
            reset.clicked.connect(self._reset)
        for b in self.buttons.buttons():
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Enter/Space must reach the recorder
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        grid.addWidget(self.buttons, 1, 0, 1, 2)
        self.nav.setCurrentRow(start)
        self._update()

    def _show(self, row: int) -> None:
        self.pages.setCurrentIndex(row)
        if self.pages.currentWidget() is self.recorder and not self.recorder.key:
            self.recorder.start()
        self._update()

    def selected(self) -> Any:
        page = self.pages.currentWidget()
        if page is self.recorder:
            chord = self.recorder.chord()
            return {"keys": chord} if chord else None
        item = page.currentItem() if isinstance(page, QListWidget) else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update(self, *_args) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected() is not None)

    def _accept_if_valid(self) -> None:
        if (value := self.selected()) is not None:
            self.value = value
            self.accept()

    def _reset(self) -> None:
        self.reset_requested = True
        self.accept()


@widget("buttons")
def _buttons(item, ctx):
    host = QWidget()
    grid = QGridLayout(host)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setColumnStretch(1, 1)
    categories = item.get("categories", [])
    values: dict[str, Any] = {}

    for row, b in enumerate(item.get("buttons", [])):
        bid, label = b["id"], str(b.get("label", b["id"]))
        name = QLabel(label)
        action = QPushButton("—")
        action.setFlat(True)
        action.setStyleSheet("text-align: left; font-weight: bold;")
        action.setCursor(Qt.CursorShape.PointingHandCursor)
        action.setToolTip(f"Change what {label} does")
        edit = QToolButton()
        edit.setAutoRaise(True)
        if icon := theme_icon(_EDIT_ICONS):
            edit.setIcon(icon)
        else:
            edit.setText("…")
        edit.setToolTip(f"Change what {label} does")

        def show(v, bid=bid, action=action):
            values[bid] = v
            action.setText(describe(v, categories))

        def open_editor(_=False, bid=bid, label=label):
            dlg = BindingDialog(host, f"{label}", categories, values.get(bid), bool(item.get("reset")))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            if dlg.reset_requested:
                ctx.invoke(item["reset"], {"button": bid})
            elif dlg.value is not None:
                ctx.invoke(item["action"], {"button": bid, "binding": dlg.value})

        action.clicked.connect(open_editor)
        edit.clicked.connect(open_editor)
        ctx.watch(b.get("state", f"bind.{bid}"), show)
        grid.addWidget(name, row, 0)
        grid.addWidget(action, row, 1)
        grid.addWidget(edit, row, 2)
    return host
