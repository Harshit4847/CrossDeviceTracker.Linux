"""Tests for :mod:`cdt.window` and :mod:`cdt.idle`."""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

def fake_process(stdout: str = "", returncode: int = 0):
    """A stand-in for ``subprocess.CompletedProcess``."""
    return type("CompletedProcess", (), {"stdout": stdout, "returncode": returncode})()


from cdt.idle import (
    MutterIdleSource,
    NullIdleSource,
    ScriptedIdleSource,
    XPrintIdleSource,
    open_idle_source,
)
from cdt.models import WindowInfo
from cdt.window import (
    AtspiWindowSource,
    ExternalWindowSource,
    NullWindowSource,
    ScriptedWindowSource,
    WindowSource,
    describe_environment,
    open_window_source,
)

from .fake_dbus import (
    FakeDBusDaemon,
    STATE_ACTIVE,
    STATE_FOCUSED,
)


class ScriptedSourceTest(unittest.TestCase):
    def test_replays_and_repeats_the_last_entry(self):
        source = ScriptedWindowSource(
            [WindowInfo("a", "A"), WindowInfo("b", "B")]
        )
        self.assertEqual(source.get_active_window().app_id, "a")
        self.assertEqual(source.get_active_window().app_id, "b")
        self.assertEqual(source.get_active_window().app_id, "b")

    def test_empty_script_returns_none(self):
        self.assertIsNone(ScriptedWindowSource([]).get_active_window())

    def test_is_a_window_source(self):
        self.assertIsInstance(ScriptedWindowSource([]), WindowSource)


class NullSourceTest(unittest.TestCase):
    def test_returns_none(self):
        self.assertIsNone(NullWindowSource().get_active_window())
        self.assertTrue(NullWindowSource().is_available())


class FactoryTest(unittest.TestCase):
    def test_none_is_selectable(self):
        with open_window_source("none") as source:
            self.assertIsInstance(source, NullWindowSource)

    def test_unknown_source_raises_with_options(self):
        with self.assertRaises(OSError) as caught:
            open_window_source("bogus")
        self.assertIn("bogus", str(caught.exception))
        self.assertIn("external", str(caught.exception))

    def test_auto_falls_back_to_something_usable_or_explains_why(self):
        # In a headless sandbox there is no display, so the factory must either
        # return a source or raise an actionable error - never crash.
        try:
            with open_window_source("auto") as source:
                self.assertTrue(source.is_available())
        except OSError as error:
            self.assertIn("window", str(error))

    def test_external_requires_a_command(self):
        with self.assertRaises(OSError):
            open_window_source("external", external_command="")

    def test_external_reads_json(self):
        source = ExternalWindowSource(
            "echo '{\"app_id\": \"code\", \"app_name\": \"VS Code\", \"title\": \"x\", \"pid\": 42}'"
        )
        window = source.get_active_window()
        self.assertEqual(window.app_id, "code")
        self.assertEqual(window.app_name, "VS Code")
        self.assertEqual(window.pid, 42)

    def test_external_reads_pipe_separated_output(self):
        source = ExternalWindowSource("echo 'code|VS Code|main.py|42'")
        window = source.get_active_window()
        self.assertEqual(window.app_id, "code")
        self.assertEqual(window.app_name, "VS Code")
        self.assertEqual(window.pid, 42)

    def test_external_ignores_empty_output(self):
        source = ExternalWindowSource("true")
        self.assertIsNone(source.get_active_window())


class X11SourceTest(unittest.TestCase):
    """These only run when libX11 is actually installed."""

    def setUp(self):
        try:
            from cdt.window import X11WindowSource
        except Exception:  # pragma: no cover
            self.skipTest("libX11 not available")
        self.X11WindowSource = X11WindowSource

    def test_without_a_display_it_fails_cleanly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            source = self.X11WindowSource.__new__(self.X11WindowSource)
            self.assertFalse(source.is_available())


class IdleSourceTest(unittest.TestCase):
    def test_scripted_sequence(self):
        source = ScriptedIdleSource([100, 200, 300])
        self.assertEqual(source.get_idle_ms(), 100)
        self.assertEqual(source.get_idle_ms(), 200)
        self.assertEqual(source.get_idle_ms(), 300)
        self.assertEqual(source.get_idle_ms(), 300)  # repeats the last value

    def test_null_source(self):
        self.assertEqual(NullIdleSource().get_idle_ms(), 0)

    def test_auto_never_fails(self):
        for preference in ("auto", "none", "xss", "mutter", "xprintidle"):
            with self.subTest(preference=preference):
                source = open_idle_source(preference)
                self.assertGreaterEqual(source.get_idle_ms(), 0)
                source.close()

    def test_unknown_idle_source_raises(self):
        with self.assertRaises(OSError):
            open_idle_source("bogus")

    def test_xprintidle_parses_milliseconds(self):
        source = XPrintIdleSource.__new__(XPrintIdleSource)
        with mock.patch("subprocess.run", return_value=fake_process(" 4200\n")):
            self.assertEqual(source.get_idle_ms(), 4200)

    def test_xprintidle_handles_failures(self):
        source = XPrintIdleSource.__new__(XPrintIdleSource)
        with mock.patch("subprocess.run", return_value=fake_process("", returncode=1)):
            self.assertEqual(source.get_idle_ms(), 0)

    def test_mutter_parses_gdbus_output(self):
        source = MutterIdleSource.__new__(MutterIdleSource)
        with mock.patch("subprocess.run", return_value=fake_process("(uint64 9876,)\n")):
            self.assertEqual(source.get_idle_ms(), 9876)


class EnvironmentDescriptionTest(unittest.TestCase):
    def test_describe_environment_returns_a_diagnostic_payload(self):
        described = describe_environment()
        for key in ("session", "display", "waylandDisplay", "availableSources", "tools", "notes"):
            self.assertIn(key, described)
        self.assertIsInstance(described["availableSources"], list)
        self.assertIn("systemctl", described["tools"])


class AtspiFixture(unittest.TestCase):
    """Base class that serves a fake AT-SPI world over a real
    D-Bus wire protocol."""

    def setUp(self):
        self.daemon = FakeDBusDaemon()
        self._old_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        self._old_wayland = os.environ.get("WAYLAND_DISPLAY")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.daemon.address
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        self.firefox = self.daemon.add_app(":1.42", 4321, "Firefox")
        self.firefox_window = self.firefox.add_window(
            "Inbox - Gmail", states=[STATE_ACTIVE]
        )
        self.firefox_entry = self.firefox.add_widget(
            self.firefox_window, "url-entry", states=[STATE_FOCUSED]
        )
        self.terminal = self.daemon.add_app(":1.43", 5332, "org.gnome.Terminal")
        self.terminal_window = self.terminal.add_window("bash - 80x24")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_address is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self._old_address
        if self._old_wayland is None:
            os.environ.pop("WAYLAND_DISPLAY", None)
        else:
            os.environ["WAYLAND_DISPLAY"] = self._old_wayland
        self.daemon.close()

    def settle(self, timeout: float = 5.0) -> None:
        """Give the source's reader thread time to see the signals."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self.source._lock:
                if self.source._dirty:
                    return


class AtspiWindowSourceTest(AtspiFixture):
    def setUp(self):
        super().setUp()
        self.source = AtspiWindowSource()
        self.addCleanup(self.source.close)

    def test_the_initial_tree_walk_finds_the_active_window(self):
        window = self.source.get_active_window()
        self.assertIsNotNone(window)
        self.assertEqual(window.app_id, "firefox")
        self.assertEqual(window.app_name, "Firefox")
        self.assertEqual(window.title, "Inbox - Gmail")
        self.assertEqual(window.pid, 4321)
        self.assertEqual(window.source, "atspi")

    def test_an_active_state_event_switches_the_window(self):
        self.daemon.emit_state_changed(
            ":1.42", self.firefox_window.path, "active", False
        )
        self.daemon.emit_state_changed(
            ":1.43", self.terminal_window.path, "active", True
        )
        self.settle()
        window = self.source.get_active_window()
        self.assertEqual(window.app_id, "org.gnome.terminal")
        self.assertEqual(window.title, "bash - 80x24")
        self.assertEqual(window.pid, 5332)

    def test_a_focus_event_on_a_widget_climbs_to_its_window(self):
        entry = self.terminal.add_widget(
            self.terminal_window, "vte-42", states=[STATE_FOCUSED]
        )
        self.daemon.emit_state_changed(":1.43", entry.path, "focused", True)
        self.settle()
        window = self.source.get_active_window()
        self.assertEqual(window.app_id, "org.gnome.terminal")
        self.assertEqual(window.title, "bash - 80x24")

    def test_the_title_is_refreshed_without_a_focus_change(self):
        first = self.source.get_active_window()
        self.assertEqual(first.title, "Inbox - Gmail")
        self.firefox_window.name = "Inbox - Gmail (2 unread)"
        refreshed = self.source.get_active_window()
        self.assertEqual(refreshed.title, "Inbox - Gmail (2 unread)")

    def test_a_focus_event_returns_to_the_previous_window(self):
        self.daemon.emit_state_changed(
            ":1.42", self.firefox_window.path, "active", False
        )
        self.daemon.emit_state_changed(
            ":1.43", self.terminal_window.path, "active", True
        )
        self.settle()
        self.daemon.emit_state_changed(
            ":1.42", self.firefox_entry.path, "focused", True
        )
        self.settle()
        window = self.source.get_active_window()
        self.assertEqual(window.app_id, "firefox")

    def test_a_vanished_window_falls_back_to_the_tree_walk(self):
        # focus a widget, then make it disappear: the source must
        # recover through the tree walk instead of returning garbage
        self.daemon.emit_state_changed(
            ":1.43", self.terminal_window.path, "active", True
        )
        self.settle()
        self.daemon._objects.pop(
            (":1.43", self.terminal_window.path), None
        )
        with self.source._lock:
            self.source._dirty = True
        window = self.source.get_active_window()
        self.assertEqual(window.app_id, "firefox")

    def test_is_available(self):
        self.assertTrue(self.source.is_available())

    def test_an_empty_desktop_yields_nothing(self):
        # remove every window: the source must report None,
        # not crash, and must not re-walk on every poll
        self.daemon._objects.pop(
            (":1.42", self.firefox_window.path), None
        )
        with self.source._lock:
            self.source._dirty = True
        self.assertIsNone(self.source.get_active_window())
        self.assertIsNone(self.source.get_active_window())
        self.assertGreater(self.source._last_empty_resolve, 0.0)

    def test_a_window_appearing_after_an_empty_walk_is_picked_up(self):
        # nothing is focused anywhere: the walk finds nothing
        self.daemon._objects.pop(
            (":1.42", self.firefox_window.path), None
        )
        with self.source._lock:
            self.source._dirty = True
        self.assertIsNone(self.source.get_active_window())
        # a focus event must bypass the empty-desktop throttle
        entry = self.terminal.add_widget(
            self.terminal_window, "vte", states=[STATE_FOCUSED]
        )
        self.daemon.emit_state_changed(":1.43", entry.path, "focused", True)
        self.settle()
        window = self.source.get_active_window()
        self.assertEqual(window.app_id, "org.gnome.terminal")
        self.assertEqual(window.title, "bash - 80x24")


class AtspiAvailabilityTest(AtspiFixture):
    def test_a_missing_accessibility_bus_raises_a_clean_error(self):
        # the bus runs, but no AT-SPI services are registered
        daemon = FakeDBusDaemon(serve_atspi=False)
        old = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = daemon.address
        try:
            with self.assertRaises(OSError):
                AtspiWindowSource()
        finally:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = old
            daemon.close()

    def test_no_session_bus_at_all_raises_a_clean_error(self):
        old = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        try:
            with self.assertRaises(OSError):
                AtspiWindowSource()
        finally:
            if old is not None:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = old


class AtspiAutoSelectionTest(AtspiFixture):
    def test_auto_prefers_atspi_on_wayland(self):
        with open_window_source("auto") as source:
            self.assertIsInstance(source, AtspiWindowSource)

    def test_atspi_is_selectable_by_name(self):
        with open_window_source("atspi") as source:
            self.assertIsInstance(source, AtspiWindowSource)

    def test_doctor_reports_atspi_as_available(self):
        described = describe_environment()
        self.assertIn("atspi", described["availableSources"])
        self.assertEqual(described["session"], "Wayland")
        self.assertEqual(described["notes"], [])


if __name__ == "__main__":
    unittest.main()
