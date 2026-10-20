"""Tests for :mod:`cdt.dbus` - the minimal D-Bus client."""

from __future__ import annotations

import os
import unittest

from cdt.dbus import (
    DBusConnection,
    DBusError,
    build_message,
    marshal_body,
    parse_message,
    unmarshal_body,
)

from .fake_dbus import FakeDBusDaemon


class MarshallingTest(unittest.TestCase):
    """Encode/decode round trips for every type the client uses."""

    def test_basic_types(self):
        cases = [
            ("y", [255]),
            ("b", [True]),
            ("b", [False]),
            ("i", [-42]),
            ("u", [7]),
            ("x", [-2**40]),
            ("t", [2**40]),
            ("s", ["hello"]),
            ("o", ["/org/a11y/root"]),
            ("g", ["a{sv}"]),
            ("as", [["firefox", "code"]]),
            ("au", [[1, 12, 25]]),
            ("aay", [[b"ab", b"cd"]]),
            ("(so)", [(":1.42", "/org/a11y/x")]),
            ("a(so)", [[(":1.42", "/a"), (":1.43", "/b")]]),
            ("a{ss}", [{"key": "value"}]),
            ("(ss)", [("a", "b")]),
            ("a(si)", [[("x", 1), ("y", 2)]]),
            ("", []),
        ]
        for signature, values in cases:
            with self.subTest(signature=signature):
                data = marshal_body(signature, values)
                self.assertEqual(unmarshal_body(signature, data), tuple(values))

    def test_variants_decode_to_their_content(self):
        # a variant is transparent: the decoded body carries the
        # inner value, not the (signature, value) pair
        data = marshal_body("a{sv}", [{"a": ("s", "x"), "b": ("i", 2)}])
        self.assertEqual(unmarshal_body("a{sv}", data), ({"a": "x", "b": 2},))
        data = marshal_body("(soa{sv})", [(":1.42", "/x", {"k": ("s", "v")})])
        self.assertEqual(
            unmarshal_body("(soa{sv})", data), ((":1.42", "/x", {"k": "v"}),)
        )

    def test_empty_variant_decodes_to_none(self):
        body = marshal_body("siiva{sv}", ["focused", 1, 0, None, {}])
        self.assertEqual(
            unmarshal_body("siiva{sv}", body),
            ("focused", 1, 0, None, {}),
        )

    def test_malformed_signature_is_an_error(self):
        with self.assertRaises(DBusError):
            marshal_body("(", ())


try:  # dbus-next is only used as an independent oracle, never at runtime
    from dbus_next import Variant
    from dbus_next._private.marshaller import Marshaller

    class _Variant:
        """Marks a value as a D-Bus variant for the oracle."""

        def __init__(self, signature: str, value) -> None:
            self.signature = signature
            self.value = value

    def _oracle_value(value):
        if isinstance(value, _Variant):
            return Variant(value.signature, _oracle_value(value.value))
        if isinstance(value, tuple):
            return [_oracle_value(item) for item in value]
        if isinstance(value, list):
            return [_oracle_value(item) for item in value]
        if isinstance(value, dict):
            return {k: _oracle_value(v) for k, v in value.items()}
        return value

    def _my_value(value):
        if isinstance(value, _Variant):
            return (value.signature, value.value)
        if isinstance(value, tuple):
            return tuple(_my_value(item) for item in value)
        if isinstance(value, list):
            return [_my_value(item) for item in value]
        if isinstance(value, dict):
            return {k: _my_value(v) for k, v in value.items()}
        return value

    HAVE_DBUS_NEXT = True
except ImportError:  # pragma: no cover - oracle is optional
    HAVE_DBUS_NEXT = False


@unittest.skipUnless(HAVE_DBUS_NEXT, "dbus-next is only a test oracle")
class OracleTest(unittest.TestCase):
    """Byte-for-byte comparison against the dbus-next implementation."""

    def test_bytes_match_dbus_next(self):
        cases = [
            ("s", ["hello"]),
            ("so", ["x", "/org/a11y/root"]),
            ("i", [-42]),
            ("u", [7]),
            ("t", [2**40]),
            ("au", [[1, 12, 25]]),
            ("as", [["firefox", "code"]]),
            ("a(so)", [[(":1.42", "/org/a11y/atspi/accessible/app")]]),
            ("a{sv}", [{"toolkit": _Variant("s", "gtk4"),
                        "n": _Variant("i", 3)}]),
            ("(soa{sv})", [(":1.42", "/org/x",
                            {"a": _Variant("s", "b")})]),
            ("a{ss}", [{"key": "value"}]),
            ("a(si)", [[("x", 1), ("y", 2)]]),
            ("(ss)", [("a", "b")]),
            ("(ia{sv})", [(5, {"k": _Variant("s", "v")})]),
            ("(st)", [("x", 2**40)]),
            ("b", [True]),
            ("y", [255]),
            ("x", [-2**40]),
        ]
        for signature, values in cases:
            with self.subTest(signature=signature):
                mine = marshal_body(signature, _my_value(values))
                theirs = bytes(
                    Marshaller(signature, _oracle_value(values)).marshall()
                )
                self.assertEqual(mine, theirs)


class MessageTest(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        message = build_message(
            1,  # METHOD_CALL
            {
                6: "org.a11y.atspi.Registry",
                1: "/org/a11y/atspi/registry",
                2: "org.a11y.atspi.Registry",
                3: "GetRegisteredEvents",
            },
            "",
            (),
            7,
        )
        parsed = parse_message(message)
        self.assertEqual(parsed["type"], 1)
        self.assertEqual(parsed["serial"], 7)
        self.assertEqual(parsed["fields"][3], "GetRegisteredEvents")
        self.assertEqual(parsed["body"], ())

    def test_signal_with_body(self):
        message = build_message(
            4,  # SIGNAL
            {
                1: "/org/a11y/x",
                2: "org.a11y.atspi.Event.Object",
                3: "StateChanged",
                7: ":1.42",
            },
            "siiva{sv}",
            ("focused", 1, 0, None, {}),
            8,
        )
        parsed = parse_message(message)
        self.assertEqual(parsed["type"], 4)
        self.assertEqual(parsed["fields"][7], ":1.42")
        self.assertEqual(parsed["body"], ("focused", 1, 0, None, {}))


class ConnectionTest(unittest.TestCase):
    def setUp(self):
        self.daemon = FakeDBusDaemon()
        self._old_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.daemon.address
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_address is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self._old_address
        self.daemon.close()

    def test_session_connects_and_hello_works(self):
        connection = DBusConnection.session()
        self.addCleanup(connection.close)
        (name,) = connection.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "Hello",
            "",
            (),
        )
        self.assertTrue(name.startswith(":1."))

    def test_method_call_round_trip(self):
        connection = DBusConnection.session()
        self.addCleanup(connection.close)
        (address,) = connection.call(
            "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress"
        )
        self.assertEqual(address, self.daemon.address)

    def test_error_reply_raises_dbus_error(self):
        connection = DBusConnection.session()
        self.addCleanup(connection.close)
        with self.assertRaises(DBusError) as caught:
            connection.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                "s",
                (":1.999",),
            )
        self.assertEqual(caught.exception.name,
                         "org.freedesktop.DBus.Error.NameHasNoOwner")

    def test_signals_are_delivered(self):
        connection = DBusConnection.session()
        self.addCleanup(connection.close)
        bus = DBusConnection.connect(self.daemon.address)
        self.addCleanup(bus.close)
        self.daemon.add_app(":1.42", 4321, "Firefox")

        received = []
        bus.add_signal_handler(
            lambda interface, member, path, sender, body: received.append(
                (interface, member, path, sender, body)
            )
        )
        bus.add_match(
            "type='signal',interface='org.a11y.atspi.Event.Object',"
            "member='StateChanged'"
        )
        self.daemon.emit_state_changed(":1.42", "/org/a11y/x", "focused", True)

        import time

        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(received)
        interface, member, path, sender, body = received[0]
        self.assertEqual(interface, "org.a11y.atspi.Event.Object")
        self.assertEqual(member, "StateChanged")
        self.assertEqual(sender, ":1.42")
        self.assertEqual(body, ("focused", 1, 0, None, {}))

    def test_missing_bus_address_raises(self):
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        with self.assertRaises(DBusError):
            DBusConnection.session()


if __name__ == "__main__":
    unittest.main()
