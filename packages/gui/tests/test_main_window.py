"""Home grid and device page, driven by a fake service connection."""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6 import QtCore  # noqa: E402
from PyQt6.QtCore import QObject, Qt, pyqtSignal  # noqa: E402

from borochid.common.rpc import PROTOCOL_VERSION  # noqa: E402
from borochid.gui.main_window import BATTERY_ROLE, CONNECTION_ROLE, WIDE_ROLE, DeviceGrid, MainWindow  # noqa: E402

UI = [
    {"widget": "readout", "label": "Battery", "state": "battery", "suffix": "%"},
    {"widget": "slider", "label": "Brightness", "state": "brightness", "action": "set_brightness", "param": "value"},
]


def summary(uid, name, status="ready", **kw):
    return {"uid": uid, "bus": "usb", "vid": 0x1209, "pid": 0xB0C1, "display_name": name, "category": "keypad",
            "status": status, "status_text": None, "error": None, "needs": None, "package": None, **kw}


class FakeClient(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    notification = pyqtSignal(str, dict)

    def __init__(self, devices):
        super().__init__()
        self.devices = {d["uid"]: d for d in devices}
        self.calls = []
        self.image_store = None
        self.profiles = {"profiles": [{"id": "default", "name": "Default"}, {"id": "p1", "name": "Games"}], "active": "default"}

    def call(self, method, params=None, callback=None):
        self.calls.append((method, params))
        result = {
            "service.info": lambda: {"protocol": PROTOCOL_VERSION, "version": "test", "image_store": self.image_store},
            "devices.list": lambda: list(self.devices.values()),
            "device.get": lambda: {**self.devices[params["uid"]], "ui": UI, "state": {"brightness": 7, "battery": 80}},
            "profiles.list": lambda: self.profiles,
            "profiles.select": lambda: {**self.profiles, "active": params["id"]},
        }.get(method, lambda: {})()
        if callback:
            callback(result, None)


def names(win):
    return [win.grid.item(i).text() for i in range(win.grid.count())]


def test_devices_pop_up_on_the_home_grid(app):
    client = FakeClient([summary("usb:2", "Zeta Mouse")])
    win = MainWindow(client)
    assert win.home.currentIndex() == 0  # empty state before connecting

    client.connected.emit()
    assert names(win) == ["Zeta Mouse"] and win.home.currentIndex() == 1

    client.notification.emit("device.added", summary("usb:1", "Acme Macropad"))
    assert names(win) == ["Acme Macropad", "Zeta Mouse"]  # sorted, so icons don't jump around
    assert not win.grid.item(0).icon().isNull()

    client.notification.emit("device.added", summary("usb:3", "Mystery", status="unsupported"))
    assert "Mystery" not in names(win)

    client.notification.emit("device.removed", {"uid": "usb:1"})
    client.notification.emit("device.removed", {"uid": "usb:2"})
    assert win.home.currentIndex() == 0


def test_clicking_a_device_opens_its_settings_and_back_returns(app):
    client = FakeClient([summary("usb:1", "Acme Macropad")])
    win = MainWindow(client)
    client.connected.emit()

    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    assert win.pages.currentIndex() == 1
    slider = win.panel.findChild(QtWidgets.QSlider)
    assert slider.value() == 7

    client.notification.emit("device.state", {"uid": "usb:1", "changes": {"brightness": 42}})
    assert slider.value() == 42

    win._go_home()
    assert win.pages.currentIndex() == 0 and win.panel is None


def test_unplugging_the_open_device_returns_home(app):
    client = FakeClient([summary("usb:1", "Acme Macropad")])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))

    client.notification.emit("device.removed", {"uid": "usb:1"})
    assert win.pages.currentIndex() == 0 and win.home.currentIndex() == 0


def test_error_text_from_the_device_is_not_rendered_as_html(app):
    client = FakeClient([summary("usb:1", "Acme", status="error", error="<img src=x> read failed")])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    [label] = [w for w in win.panel.findChildren(QtWidgets.QLabel) if "read failed" in w.text()]
    assert label.textFormat() == QtWidgets.QLabel().textFormat().PlainText


def test_package_picture_replaces_the_theme_icon(app, tmp_path, png):
    from borochid.common import images

    digest = images.store(tmp_path, png(64, 64, (255, 0, 0, 255)))
    client = FakeClient([summary("usb:1", "Acme", image=digest), summary("usb:2", "Bogus", image="f" * 64)])
    client.image_store = str(tmp_path)
    win = MainWindow(client)
    client.connected.emit()

    def colour(item):
        return item.icon().pixmap(64, 64).toImage().pixelColor(32, 32).name()

    assert colour(win.grid.item(0)) == "#ff0000"
    assert colour(win.grid.item(1)) != "#ff0000"  # missing from the store: falls back to the icon
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    header = [w for w in win.panel.findChildren(QtWidgets.QLabel) if w.pixmap() and not w.pixmap().isNull()][0]
    image = header.pixmap().toImage()
    assert image.pixelColor(image.width() // 2, image.height() // 2).name() == "#ff0000"


def test_settings_sit_left_of_the_status(app):
    client = FakeClient([summary("usb:1", "Acme Macropad")])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    win.resize(900, 560)
    win.show()
    app.processEvents()

    [battery] = [w for w in win.panel.findChildren(QtWidgets.QLabel) if w.text() == "80%"]
    slider = win.panel.findChild(QtWidgets.QSlider)
    battery_x = battery.mapTo(win.panel, battery.rect().topLeft()).x()
    slider_x = slider.mapTo(win.panel, slider.rect().topLeft()).x()
    assert slider_x < battery_x  # settings on the left, status on the right
    win.close()


def test_a_group_can_ask_for_the_status_column():
    from borochid.gui.main_window import _is_status

    assert _is_status({"widget": "group", "section": "status", "children": []})
    assert _is_status({"widget": "readout"})
    assert not _is_status({"widget": "readout", "section": "settings"})
    assert not _is_status({"widget": "group", "children": [{"widget": "readout"}]})


def test_show_unsupported_toggle_reloads_the_list(app):
    client = FakeClient([summary("usb:1", "Acme")])
    win = MainWindow(client)
    client.connected.emit()
    win.show_unsupported.click()
    assert client.calls[-1] == ("devices.list", {"include_unsupported": True})
    assert "Hide" in win.show_unsupported.toolTip()


def test_battery_updates_in_place(app):
    battery = {"level": 40, "charging": False}
    client = FakeClient([summary("usb:1", "Acme", battery=battery)])
    win = MainWindow(client)
    client.connected.emit()
    assert win.grid.item(0).data(BATTERY_ROLE)["level"] == 40
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    panel = win.panel
    assert panel.battery.text.text() == "40%"

    client.notification.emit("device.changed", summary("usb:1", "Acme", battery={**battery, "level": 90, "charging": True}))
    assert win.panel is panel  # same page: sliders being dragged are not reset
    assert panel.battery.text.text() == "90%, charging"


def test_hovering_the_tile_battery_shows_the_percentage(app):
    from PyQt6.QtCore import QEvent, QPoint
    from PyQt6.QtGui import QHelpEvent

    battery = {"level": 87, "charging": True}
    client = FakeClient([summary("usb:1", "Acme", battery=battery)])
    win = MainWindow(client)
    client.connected.emit()
    win.resize(900, 560)
    win.show()
    app.processEvents()

    delegate, index = win.grid.itemDelegate(), win.grid.model().index(0, 0)
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = win.grid.visualRect(index)
    shown = []
    QtWidgets.QToolTip.showText = lambda pos, text, *a: shown.append(text)  # capture instead of popping up
    try:
        at_battery = delegate.battery_rect(option).center()
        delegate.helpEvent(QHelpEvent(QEvent.Type.ToolTip, at_battery, QPoint()), win.grid, option, index)
        assert shown == ["Battery 87%, charging"]
        delegate.helpEvent(QHelpEvent(QEvent.Type.ToolTip, option.rect.topLeft() + QPoint(5, 5), QPoint()),
                           win.grid, option, index)
        assert all("Battery" not in t for t in shown[1:])  # elsewhere: name and status
    finally:
        del QtWidgets.QToolTip.showText
    win.close()


def test_card_layout_name_then_battery_then_centred_picture(app):
    from PyQt6.QtCore import QRect

    from borochid.gui.main_window import TileDelegate

    delegate = TileDelegate()
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = QRect(100, 50, TileDelegate.CARD.width(), TileDelegate.CARD.height())
    name, battery, picture = delegate._name_rect(option), delegate.battery_rect(option), delegate.picture_rect(option)
    assert name.topLeft() == option.rect.topLeft() + QtCore.QPoint(TileDelegate.PAD, TileDelegate.PAD)
    assert battery.left() == name.left() and battery.top() > name.bottom()
    assert picture.top() > battery.bottom() and picture.bottom() <= option.rect.bottom()
    assert abs(picture.center().x() - option.rect.center().x()) <= 1


def test_device_details_hide_behind_an_info_button_that_copies_them(app):
    client = FakeClient([summary("usb:1", "Acme")])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    assert not [w for w in win.panel.findChildren(QtWidgets.QLabel) if "1209:b0c1" in w.text()]
    [info] = [b for b in win.panel.findChildren(QtWidgets.QToolButton) if "1209:b0c1" in b.toolTip()]
    info.click()
    assert "1209:b0c1" in QtWidgets.QApplication.clipboard().text()


GROUPED_UI = [
    {"widget": "group", "label": "Lighting", "icon": "preferences-desktop-color", "enabled_if": "wireless", "children": [
        {"widget": "slider", "label": "Brightness", "state": "brightness", "action": "set_brightness", "param": "value"}]},
    {"widget": "group", "label": "Audio", "children": [
        {"widget": "toggle", "label": "Tone", "state": "tone", "action": "set_tone", "param": "value"}]},
]


def test_groups_become_icon_sections_and_the_choice_is_remembered(app, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "UI", GROUPED_UI)
    client = FakeClient([summary("usb:1", "Acme")])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    tabs = win.panel.tabs
    assert [tabs.label(i) for i in range(tabs.count())] == ["Lighting", "Audio"]
    assert not tabs.widget(0).findChild(QtWidgets.QSlider).isEnabled()  # enabled_if: wireless
    assert tabs.widget(0).findChild(QtWidgets.QGroupBox) is None  # the section is the frame

    [audio] = [b for b in tabs.findChildren(QtWidgets.QToolButton) if b.toolTip() == "Audio"]
    audio.click()
    assert tabs.currentIndex() == 1
    client.notification.emit("device.changed", summary("usb:1", "Acme", status="connecting"))
    client.notification.emit("device.changed", summary("usb:1", "Acme"))  # page rebuilt
    assert win.panel.tabs is not tabs and win.panel.tabs.currentIndex() == 1


def test_an_unavailable_device_fades_hides_battery_and_disables_settings(app):
    battery = {"level": 60, "charging": False}
    client = FakeClient([summary("usb:1", "Acme", battery=battery)])
    win = MainWindow(client)
    client.connected.emit()
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    panel = win.panel

    def opacity():  # alpha at the picture's centre
        image = panel.picture_label.pixmap().toImage()
        return image.pixelColor(image.width() // 2, image.height() // 2).alpha()

    bright = opacity()
    assert panel.battery.isVisibleTo(panel) and panel.settings_host.isEnabled()

    off = summary("usb:1", "Acme", battery=battery, available=False, status_text="Headset off")
    client.notification.emit("device.changed", off)
    assert win.panel is panel  # updated in place
    assert not panel.battery.isVisibleTo(panel)
    assert not panel.settings_host.isEnabled() and panel.settings_host.graphicsEffect() is not None
    assert opacity() < bright
    assert win.grid.item(0).data(BATTERY_ROLE) is None  # no battery on the home card either

    client.notification.emit("device.changed", summary("usb:1", "Acme", battery=battery))
    assert panel.settings_host.isEnabled() and panel.battery.isVisibleTo(panel)
    assert panel.settings_host.graphicsEffect() is None


def test_a_single_click_selects_and_the_edit_button_opens(app):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest

    client = FakeClient([summary("usb:1", "Acme")])
    win = MainWindow(client)
    client.connected.emit()
    win.show()
    app.processEvents()
    rect = win.grid.visualRect(win.grid.model().index(0, 0))
    QTest.mouseClick(win.grid.viewport(), Qt.MouseButton.LeftButton, pos=rect.topLeft() + QPoint(40, 40))
    assert win.pages.currentIndex() == 0  # selecting is not opening

    option = QtWidgets.QStyleOptionViewItem()
    option.rect = rect
    edit = win.grid.itemDelegate().edit_rect(option).center()
    QTest.mouseClick(win.grid.viewport(), Qt.MouseButton.LeftButton, pos=edit)
    assert win.pages.currentIndex() == 1

    win._go_home()
    win.grid.setCurrentRow(0)
    QTest.keyClick(win.grid, Qt.Key.Key_Return)
    assert win.pages.currentIndex() == 1
    win.close()


def test_the_edit_button_has_its_own_row_under_the_picture(app):
    from PyQt6.QtCore import QRect

    from borochid.gui.main_window import TileDelegate

    delegate = TileDelegate()
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = QRect(0, 0, TileDelegate.CARD.width(), TileDelegate.CARD.height())
    edit, picture = delegate.edit_rect(option), delegate.picture_rect(option)
    assert edit.top() > picture.bottom()  # never overlaps the picture
    assert edit.right() == option.rect.right() - TileDelegate.PAD
    assert picture.height() == TileDelegate.PICTURE  # the picture keeps its full size


def test_profile_switcher_shows_only_with_a_device_that_supports_profiles(app):
    client = FakeClient([summary("usb:1", "Acme Macropad", profiles=False)])
    win = MainWindow(client)
    win.show()
    client.connected.emit()
    assert not win.profile_selector.isVisible()

    mouse = summary("usb:2", "Zeta Mouse", profiles=True)
    client.devices[mouse["uid"]] = mouse
    client.notification.emit("device.added", mouse)
    assert win.profile_selector.isVisible()
    combo = win.profile_selector.combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["Default", "Games"]

    combo.activated.emit(1)
    assert ("profiles.select", {"id": "p1"}) in client.calls and combo.currentText() == "Games"

    # Another client switched: every window follows.
    client.notification.emit("profiles.changed", {**client.profiles, "active": "default"})
    assert combo.currentText() == "Default"

    win._open("usb:2")  # the same switcher stays at the top of the device page
    assert win.profile_selector.isVisible() and win.back.isVisible()
    assert not any(b.isVisible() for b in win.home_actions)

    client.notification.emit("device.removed", {"uid": "usb:2"})
    assert not win.profile_selector.isVisible()


def test_profile_switcher_hides_on_pages_of_devices_without_profiles(app):
    client = FakeClient([summary("usb:1", "Corsair Virtuoso SE", profiles=False), summary("usb:2", "Zeta Mouse", profiles=True)])
    win = MainWindow(client)
    win.show()
    client.connected.emit()
    assert win.profile_selector.isVisible()  # home: the mouse has profiles

    win._open("usb:1")
    assert not win.profile_selector.isVisible()  # the headset isn't affected
    win._go_home()
    win._open("usb:2")
    assert win.profile_selector.isVisible()
    win._go_home()
    assert win.profile_selector.isVisible()


def test_keyboards_get_a_card_two_columns_wide(app):
    from PyQt6.QtCore import QRect

    from borochid.gui.main_window import WIDE_ROLE, TileDelegate

    client = FakeClient([summary("usb:1", "A Mouse", category="mouse"), summary("usb:2", "B Keyboard", category="keyboard"),
                         summary("usb:3", "C Mouse", category="mouse"), summary("usb:4", "D Mouse", category="mouse")])
    win = MainWindow(client)
    client.connected.emit()
    win.resize(1200, 760)
    win.show()
    app.processEvents()
    rects = [win.grid.visualItemRect(win.grid.item(i)) for i in range(win.grid.count())]
    assert win.grid.item(1).data(WIDE_ROLE) and not win.grid.item(0).data(WIDE_ROLE)
    assert rects[1].width() == TileDelegate.WIDE_CARD.width() and rects[0].width() == TileDelegate.CARD.width()
    # The wide card covers exactly two columns: the cards under it line up with its edges.
    below = [r for r in rects if r.top() > rects[1].bottom()]
    if below:
        assert rects[1].left() in {r.left() for r in rects}
    assert all(r.height() == TileDelegate.CARD.height() for r in rects)

    option = QtWidgets.QStyleOptionViewItem()
    option.rect = QRect(0, 0, TileDelegate.WIDE_CARD.width(), TileDelegate.WIDE_CARD.height())
    picture = TileDelegate().picture_rect(option)
    assert picture.width() > 2 * picture.height()  # a wide picture keeps its aspect ratio


def test_a_wide_picture_fills_a_wide_card(app, tmp_path, png):
    from borochid.common import images
    from borochid.gui.main_window import TileDelegate

    digest = images.store(tmp_path, png(700, 200, (0, 255, 0, 255)))
    client = FakeClient([summary("usb:1", "Keyboard", category="keyboard", image=digest)])
    client.image_store = str(tmp_path)
    win = MainWindow(client)
    client.connected.emit()
    size = win.grid.item(0).icon().availableSizes()[0]
    assert size.width() == TileDelegate.picture_box(True).width() and size.height() < TileDelegate.PICTURE


@pytest.mark.parametrize(("wide", "columns", "used"), [
    ([False, False, True], 3, 2),  # the keyboard wraps: two columns in use
    ([False, False, True], 4, 4),
    ([False, False, False], 2, 2),  # a short last row doesn't narrow the grid
    ([True, False], 5, 3),
    ([True], 1, 1),
])
def test_grid_centres_the_columns_its_cards_use(app, wide, columns, used):
    grid = DeviceGrid()
    for w in wide:
        item = QtWidgets.QListWidgetItem("x")
        item.setData(WIDE_ROLE, w)
        grid.addItem(item)
    assert grid._used_columns(columns) == used


def test_a_device_on_two_connections_gets_one_card_with_its_connection(app):
    client = FakeClient([
        summary("usb:1-3", "Keyboard", category="keyboard", connection="cable", device_id="9454DCB7", shadowed=False),
        summary("usb:1-4", "Keyboard", category="keyboard", connection="wireless", device_id="9454DCB7", shadowed=True),
    ])
    win = MainWindow(client)
    client.connected.emit()
    assert win.grid.count() == 1 and win.grid.item(0).data(CONNECTION_ROLE) == "cable"

    # The cable is pulled: the receiver's connection is the one in use now.
    client.notification.emit("device.changed", summary("usb:1-4", "Keyboard", category="keyboard",
                                                       connection="wireless", device_id="9454DCB7", shadowed=False))
    client.notification.emit("device.removed", {"uid": "usb:1-3"})
    assert win.grid.count() == 1 and win.grid.item(0).data(Qt.ItemDataRole.UserRole) == "usb:1-4"
    win.grid.itemDoubleClicked.emit(win.grid.item(0))
    assert win.panel.connection.toolTip() == "Wireless"  # (no icon theme in tests, so no icon to show)
