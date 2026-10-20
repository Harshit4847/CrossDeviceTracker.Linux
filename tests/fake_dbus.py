"""An in-process D-Bus daemon used by the tests.

The daemon speaks the real D-Bus wire protocol (it reuses the
marshaller from :mod:`cdt.dbus`, which is validated against
dbus-next in :mod:`tests.test_dbus`) and hosts a small fake AT-SPI
world: a registry with a desktop root, applications with windows
and widgets.  Tests can flip focus/active states and the daemon
emits the same ``StateChanged`` signals GNOME emits, so the
AT-SPI window source is exercised exactly as it runs on a real
GNOME Wayland session.
"""

from __future__ import annotations

import itertools
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cdt.dbus import (
    ERROR_MESSAGE,
    FIELD_DESTINATION,
    FIELD_ERROR_NAME,
    FIELD_INTERFACE,
    FIELD_MEMBER,
    FIELD_PATH,
    FIELD_REPLY_SERIAL,
    FIELD_SENDER,
    FIELD_SIGNATURE,
    METHOD_CALL,
    METHOD_RETURN,
    SIGNAL_MESSAGE,
    DBusError,
    build_message,
    read_message,
)

# AT-SPI roles and states (see atspi-constants.h)
ROLE_ALERT = 2
ROLE_DIALOG = 16
ROLE_DESKTOP_FRAME = 14
ROLE_FRAME = 23
ROLE_WINDOW = 69
ROLE_APPLICATION = 75
ROLE_TEXT = 60

STATE_ACTIVE = 1
STATE_FOCUSED = 12

ACCESSIBLE_IFACE = "org.a11y.atspi.Accessible"
APPLICATION_IFACE = "org.a11y.atspi.Application"
REGISTRY_IFACE = "org.a11y.atspi.Registry"
REGISTRY_SERVICE = "org.a11y.atspi.Registry"
REGISTRY_PATH = "/org/a11y/atspi/registry"
ROOT_PATH = "/org/a11y/atspi/accessible/root"
BUS_SERVICE = "org.a11y.Bus"
BUS_PATH = "/org/a11y/bus"
SESSION_BUS = "org.freedesktop.DBus"
SESSION_PATH = "/org/freedesktop/DBus"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
NULL_PATH = "/org/a11y/atspi/null"


class FakeAccessible:
    """One node of the fake accessibility tree."""

    def __init__(
        self,
        path: str,
        name: str = "",
        role: int = 0,
        states: Sequence[int] = (),
    ) -> None:
        self.path = path
        self.name = name
        self.role = role
        self.states = list(states)
        self.children: List[Tuple[str, str]] = []
        self.parent: Tuple[str, str] = ("", NULL_PATH)
        self.application: Tuple[str, str] = ("", NULL_PATH)
        self.attributes: Dict[str, str] = {}


class FakeApp:
    """A fake application living on the accessibility bus."""

    def __init__(self, daemon: "FakeDBusDaemon", unique_name: str,
                 pid: int, app_name: str) -> None:
        self.daemon = daemon
        self.unique_name = unique_name
        self.pid = pid
        self.app_name = app_name
        self._counter = itertools.count()
        self.root = self._add_object(
            f"/org/a11y/atspi/accessible/app{next(self._counter)}",
            app_name, ROLE_APPLICATION,
        )
        self.root.application = (unique_name, self.root.path)
        daemon.set_pid(unique_name, pid)

    def _add_object(self, path: str, name: str, role: int,
                    states: Sequence[int] = ()) -> FakeAccessible:
        accessible = FakeAccessible(path, name=name, role=role, states=states)
        self.daemon.add_object(self.unique_name, path, accessible)
        return accessible

    def add_window(self, title: str,
                   states: Sequence[int] = ()) -> FakeAccessible:
        window = self._add_object(
            f"/org/a11y/atspi/accessible/app{next(self._counter)}",
            title, ROLE_FRAME, states,
        )
        window.parent = (self.unique_name, self.root.path)
        window.application = (self.unique_name, self.root.path)
        self.root.children.append((self.unique_name, window.path))
        return window

    def add_widget(self, window: FakeAccessible, name: str,
                   states: Sequence[int] = ()) -> FakeAccessible:
        widget = self._add_object(
            f"/org/a11y/atspi/accessible/app{next(self._counter)}",
            name, ROLE_TEXT, states,
        )
        widget.parent = (self.unique_name, window.path)
        widget.application = (self.unique_name, self.root.path)
        window.children.append((self.unique_name, widget.path))
        return widget


class FakeDBusDaemon:
    """A minimal D-Bus daemon over a unix socket.

    With ``serve_atspi=False`` the bus runs but hosts no AT-SPI
    services, which mimics a session without the accessibility
    stack installed.
    """

    def __init__(self, serve_atspi: bool = True) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="cdt-fake-dbus-"))
        self._socket_path = str(self._tmp / "bus")
        self.address = f"unix:path={self._socket_path}"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        self._server.listen(8)
        self._lock = threading.Lock()
        self._clients: List[socket.socket] = []
        self._names: Dict[socket.socket, str] = {}
        self._counter = itertools.count(1)
        self._serial = itertools.count(1)
        self._objects: Dict[Tuple[str, str], FakeAccessible] = {}
        self._pids: Dict[str, int] = {}
        self.serve_atspi = serve_atspi
        self._thread = threading.Thread(
            target=self._accept_loop, name="fake-dbus-accept", daemon=True
        )
        self._thread.start()

        if serve_atspi:
            # the registry daemon itself serves the desktop root
            self._root = FakeAccessible(ROOT_PATH, name="desktop",
                                         role=ROLE_DESKTOP_FRAME)
            self.add_object(REGISTRY_SERVICE, ROOT_PATH, self._root)

    # -- setup ------------------------------------------------------------
    def add_object(self, bus_name: str, path: str,
                   accessible: FakeAccessible) -> None:
        self._objects[(bus_name, path)] = accessible

    def set_pid(self, bus_name: str, pid: int) -> None:
        self._pids[bus_name] = pid

    def add_app(self, unique_name: str, pid: int,
                app_name: str) -> FakeApp:
        app = FakeApp(self, unique_name, pid, app_name)
        self._root.children.append((unique_name, app.root.path))
        return app

    def emit_state_changed(self, sender: str, path: str,
                           state: str, enabled: bool) -> None:
        """Emit the signal atk-bridge sends when a state changes."""
        self.broadcast_signal(
            sender, path, "org.a11y.atspi.Event.Object", "StateChanged",
            "siiva{sv}", (state, 1 if enabled else 0, 0, None, {}),
        )

    def broadcast_signal(self, sender: str, path: str, interface: str,
                         member: str, signature: str,
                         values: Sequence[Any]) -> None:
        message = build_message(
            SIGNAL_MESSAGE,
            {
                FIELD_PATH: path,
                FIELD_INTERFACE: interface,
                FIELD_MEMBER: member,
                FIELD_SENDER: sender,
            },
            signature, values, next(self._serial),
        )
        with self._lock:
            for client in list(self._clients):
                try:
                    client.sendall(message)
                except OSError:
                    pass

    # -- server loop ------------------------------------------------------
    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            with self._lock:
                self._clients.append(client)
            thread = threading.Thread(
                target=self._serve_client, args=(client,),
                name="fake-dbus-client", daemon=True,
            )
            thread.start()

    def _serve_client(self, client: socket.socket) -> None:
        try:
            self._authenticate(client)
            buffer = bytearray()
            while True:
                message = read_message(client, buffer)
                self._handle(client, message)
        except (EOFError, OSError):
            pass
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            client.close()

    @staticmethod
    def _authenticate(client: socket.socket) -> None:
        # the client may lead with a NUL byte, then sends AUTH EXTERNAL
        line = bytearray()
        while not line.endswith(b"\r\n"):
            chunk = client.recv(1)
            if not chunk:
                raise EOFError
            line.extend(chunk)
        command = bytes(line).lstrip(b"\x00")
        if not command.startswith(b"AUTH"):
            raise DBusError("org.freedesktop.DBus.Error.AccessDenied",
                            "expected AUTH")
        client.sendall(b"OK fake-guid\r\n")
        begin = bytearray()
        while not begin.endswith(b"\r\n"):
            chunk = client.recv(1)
            if not chunk:
                raise EOFError
            begin.extend(chunk)
        if not bytes(begin).startswith(b"BEGIN"):
            raise DBusError("org.freedesktop.DBus.Error.AccessDenied",
                            "expected BEGIN")

    # -- dispatch ---------------------------------------------------------
    def _handle(self, client: socket.socket, message: Dict[str, Any]) -> None:
        if message["type"] != METHOD_CALL:
            return
        fields = message["fields"]
        destination = fields.get(FIELD_DESTINATION, "")
        try:
            signature, values = self._dispatch(
                destination,
                fields.get(FIELD_PATH, ""),
                fields.get(FIELD_INTERFACE, ""),
                fields.get(FIELD_MEMBER, ""),
                message["body"],
                client,
            )
            reply = build_message(
                METHOD_RETURN,
                {FIELD_REPLY_SERIAL: message["serial"]},
                signature, values, next(self._serial),
            )
        except DBusError as error:
            reply = build_message(
                ERROR_MESSAGE,
                {
                    FIELD_REPLY_SERIAL: message["serial"],
                    FIELD_ERROR_NAME: error.name,
                },
                "s", (error.message,), next(self._serial),
            )
        with self._lock:
            try:
                client.sendall(reply)
            except OSError:
                pass

    def _dispatch(self, destination: str, path: str, interface: str,
                  member: str, body: Tuple[Any, ...],
                  client: socket.socket) -> Tuple[str, Tuple[Any, ...]]:
        if member == "Hello":
            return ("s", (self._hello(client),))
        if member == "AddMatch":
            return ("", ())

        if destination == SESSION_BUS and path == SESSION_PATH:
            if member == "GetConnectionUnixProcessID":
                bus_name = body[0]
                pid = self._pids.get(bus_name)
                if pid is None:
                    raise DBusError(
                        "org.freedesktop.DBus.Error.NameHasNoOwner",
                        f"no such name {bus_name}",
                    )
                return ("u", (pid,))

        if destination == BUS_SERVICE and path == BUS_PATH:
            if not self.serve_atspi:
                raise DBusError(
                    "org.freedesktop.DBus.Error.ServiceUnknown",
                    "org.a11y.Bus is not running",
                )
            if member == "GetAddress":
                return ("s", (self.address,))
            raise DBusError("org.freedesktop.DBus.Error.UnknownMethod",
                            f"no method {member}")

        if (self.serve_atspi and destination == REGISTRY_SERVICE
                and path == REGISTRY_PATH
                and interface == REGISTRY_IFACE):
            if member == "GetRegisteredEvents":
                return ("a(ss)", ([],))
            if member in ("RegisterEvent", "DeregisterEvent"):
                return ("", ())
            raise DBusError("org.freedesktop.DBus.Error.UnknownMethod",
                            f"no method {member}")

        accessible = self._objects.get((destination, path))
        if accessible is None:
            raise DBusError(
                "org.freedesktop.DBus.Error.UnknownObject",
                f"no object {path} for {destination}",
            )

        if interface == PROPERTIES_IFACE and member == "Get":
            prop_interface, prop_name = body
            return self._get_property(accessible, prop_interface, prop_name)
        if interface == ACCESSIBLE_IFACE:
            return self._accessible_call(accessible, member, body)
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod",
                        f"no method {interface}.{member}")

    def _hello(self, client: socket.socket) -> str:
        with self._lock:
            name = self._names.get(client)
            if name is None:
                name = f":1.{next(self._counter)}"
                self._names[client] = name
            return name

    def _get_property(self, accessible: FakeAccessible,
                      prop_interface: str, prop_name: str) -> Tuple[str, Tuple]:
        # Properties.Get returns the value wrapped in a variant
        if prop_interface == ACCESSIBLE_IFACE:
            if prop_name == "Name":
                return ("v", (("s", accessible.name),))
            if prop_name == "ChildCount":
                return ("v", (("i", len(accessible.children)),))
            if prop_name == "Parent":
                return ("v", (("(so)", accessible.parent),))
        if prop_interface == APPLICATION_IFACE:
            if prop_name == "ToolkitName":
                return ("v", (("s", "fake-toolkit"),))
        raise DBusError("org.freedesktop.DBus.Error.UnknownProperty",
                        f"no property {prop_name}")

    def _accessible_call(self, accessible: FakeAccessible, member: str,
                         body: Tuple[Any, ...]) -> Tuple[str, Tuple]:
        if member == "GetChildren":
            return ("a(so)", (accessible.children,))
        if member == "GetChildAtIndex":
            index = body[0]
            if 0 <= index < len(accessible.children):
                return ("(so)", (accessible.children[index],))
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs",
                            "index out of range")
        if member == "GetRole":
            return ("u", (accessible.role,))
        if member == "GetRoleName":
            return ("s", (f"role-{accessible.role}",))
        if member == "GetState":
            # The real bus packs state flags into 32-bit words: state N
            # is set when word N//32 has bit N%32.  Tests keep using
            # human-readable state IDs in ``accessible.states``; pack
            # them here so the wire format matches at-spi2-core.
            words = [0] * (max(accessible.states, default=0) // 32 + 1)
            for state in accessible.states:
                words[state // 32] |= 1 << (state % 32)
            return ("au", (words,))
        if member == "GetApplication":
            return ("(so)", (accessible.application,))
        if member == "GetAttributes":
            return ("a{ss}", (accessible.attributes,))
        if member == "GetIndexInParent":
            return ("i", (-1,))
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod",
                        f"no method {member}")

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._server.close()
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()
