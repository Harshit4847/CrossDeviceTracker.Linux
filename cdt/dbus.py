"""A minimal D-Bus client built on the standard library.

The client talks to two D-Bus services: the Mutter idle monitor (through
the ``gdbus`` command) and - for GNOME on Wayland - the AT-SPI
accessibility bus, which is the only reliable way to learn the focused
window on a session that does not expose it through X11.  Instead of
pulling in a third-party D-Bus package, this module implements the
small part of the wire protocol the client needs:

* connect to an existing bus (the session bus or the accessibility bus),
* make method calls and decode the reply,
* subscribe to signals with ``AddMatch`` and forward them to a callback.

Everything else raises :class:`DBusError`.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "DBusConnection",
    "DBusError",
    "marshal_body",
    "unmarshal_body",
    "build_message",
    "read_message",
]

# message types -------------------------------------------------------------
METHOD_CALL = 1
METHOD_RETURN = 2
ERROR_MESSAGE = 3
SIGNAL_MESSAGE = 4

# header field codes --------------------------------------------------------
FIELD_PATH = 1
FIELD_INTERFACE = 2
FIELD_MEMBER = 3
FIELD_ERROR_NAME = 4
FIELD_REPLY_SERIAL = 5
FIELD_DESTINATION = 6
FIELD_SENDER = 7
FIELD_SIGNATURE = 8

# alignment of the basic types (variant and signature align to 1) -----------
_BASIC_ALIGNMENT = {
    "y": 1,
    "b": 1,
    "g": 1,
    "v": 1,
    "n": 2,
    "q": 2,
    "i": 4,
    "u": 4,
    "s": 4,
    "o": 4,
    "h": 4,
    "x": 8,
    "t": 8,
    "d": 8,
}


class DBusError(Exception):
    """A D-Bus error reply or a protocol level failure."""

    def __init__(self, name: str, message: str = "") -> None:
        super().__init__(message or name)
        self.name = name
        self.message = message


# --------------------------------------------------------------------------- #
# signature parsing
# --------------------------------------------------------------------------- #

#: A parsed type: a basic type code (``"s"``) or a container tuple
#: (``("array", element)``, ``("struct", [fields])``, ``("dict", key, value)``).
TypeTree = Any


def _parse_type(signature: str, position: int = 0) -> Tuple[TypeTree, int]:
    """Parse one complete type, returning ``(type tree, next position)``."""
    if position >= len(signature):
        raise DBusError("invalid signature", "unexpected end of signature")
    token = signature[position]
    if token in _BASIC_ALIGNMENT:
        return token, position + 1
    if token == "a":
        element, position = _parse_type(signature, position + 1)
        return ("array", element), position
    if token == "(":
        fields: List[TypeTree] = []
        position += 1
        while position < len(signature) and signature[position] != ")":
            field, position = _parse_type(signature, position)
            fields.append(field)
        if position >= len(signature):
            raise DBusError("invalid signature", "missing ')'")
        return ("struct", fields), position + 1
    if token == "{":
        key, position = _parse_type(signature, position + 1)
        value, position = _parse_type(signature, position)
        if position >= len(signature) or signature[position] != "}":
            raise DBusError("invalid signature", "missing '}'")
        return ("dict", key, value), position + 1
    raise DBusError("invalid signature", f"unknown type code {token!r}")


def parse_signature(signature: str) -> List[TypeTree]:
    """Split a complete signature into its top-level types."""
    trees: List[TypeTree] = []
    position = 0
    while position < len(signature):
        tree, position = _parse_type(signature, position)
        trees.append(tree)
    return trees


def _element_token(tree: TypeTree) -> str:
    """The D-Bus type code of a type tree's root."""
    if isinstance(tree, str):
        return tree
    kind = tree[0]
    if kind == "array":
        return "a"
    return "(" if kind == "struct" else "{"


def _alignment(tree: TypeTree) -> int:
    if isinstance(tree, str):
        return _BASIC_ALIGNMENT[tree]
    if tree[0] == "array":
        return _alignment(tree[1])
    # structs and dict entries are aligned to 8 bytes
    return 8


# --------------------------------------------------------------------------- #
# marshalling
# --------------------------------------------------------------------------- #


def _pad(buffer: bytearray, alignment: int) -> None:
    remainder = len(buffer) % alignment
    if remainder:
        buffer.extend(b"\x00" * (alignment - remainder))


def _encode(buffer: bytearray, tree: TypeTree, value: Any) -> None:
    if isinstance(tree, str):
        _encode_basic(buffer, tree, value)
    elif tree[0] == "array":
        _encode_array(buffer, tree, value)
    elif tree[0] == "struct":
        _pad(buffer, 8)
        for field_tree, field_value in zip(tree[1], value):
            _encode(buffer, field_tree, field_value)
    elif tree[0] == "dict":
        _pad(buffer, 8)
        _encode(buffer, tree[1], value[0])
        _encode(buffer, tree[2], value[1])


def _encode_basic(buffer: bytearray, token: str, value: Any) -> None:
    if token == "y":
        buffer.append(int(value) & 0xFF)
    elif token == "b":
        _pad(buffer, 4)
        buffer.extend(struct.pack("<i", 1 if value else 0))
    elif token == "n":
        _pad(buffer, 2)
        buffer.extend(struct.pack("<h", value))
    elif token == "q":
        _pad(buffer, 2)
        buffer.extend(struct.pack("<H", value))
    elif token == "i":
        _pad(buffer, 4)
        buffer.extend(struct.pack("<i", value))
    elif token in ("u", "h"):
        _pad(buffer, 4)
        buffer.extend(struct.pack("<I", value))
    elif token == "x":
        _pad(buffer, 8)
        buffer.extend(struct.pack("<q", value))
    elif token == "t":
        _pad(buffer, 8)
        buffer.extend(struct.pack("<Q", value))
    elif token in ("s", "o"):
        raw = value.encode("utf-8")
        _pad(buffer, 4)
        buffer.extend(struct.pack("<I", len(raw)))
        buffer.extend(raw)
        buffer.append(0)
    elif token == "g":
        raw = value.encode("utf-8")
        buffer.append(len(raw))
        buffer.extend(raw)
        buffer.append(0)
    elif token == "v":
        if value is None:
            buffer.append(0)
            return
        signature, variant_value = value
        _encode_basic(buffer, "g", signature)
        if not signature:
            return  # an empty variant carries no data
        tree, _ = _parse_type(signature)
        _encode(buffer, tree, variant_value)
    else:
        raise DBusError("invalid signature", f"cannot encode type {token!r}")


def _encode_array(buffer: bytearray, tree: TypeTree, value: Any) -> None:
    element = tree[1]
    _pad(buffer, 4)
    length_offset = len(buffer)
    buffer.extend(b"\x00\x00\x00\x00")
    if _element_token(element) in ("x", "t", "d", "{", "("):
        _pad(buffer, 8)
    start = len(buffer)
    if element == "y":
        buffer.extend(bytes(value))
    elif isinstance(element, tuple) and element[0] == "dict":
        for key, item in (value.items() if isinstance(value, dict) else value):
            _encode(buffer, element, (key, item))
    else:
        for item in value:
            _encode(buffer, element, item)
    buffer[length_offset : length_offset + 4] = struct.pack("<I", len(buffer) - start)


def marshal_body(signature: str, values: Sequence[Any]) -> bytes:
    """Encode ``values`` according to ``signature``."""
    buffer = bytearray()
    for tree, value in zip(parse_signature(signature), values):
        _encode(buffer, tree, value)
    return bytes(buffer)


# --------------------------------------------------------------------------- #
# unmarshalling
# --------------------------------------------------------------------------- #


class _Cursor:
    """A read cursor over a byte buffer."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def pad(self, alignment: int) -> None:
        remainder = self.offset % alignment
        if remainder:
            self.offset += alignment - remainder

    def take(self, count: int) -> bytes:
        chunk = self.data[self.offset : self.offset + count]
        if len(chunk) != count:
            raise DBusError("invalid message", "message is truncated")
        self.offset += count
        return chunk


def _decode(tree: TypeTree, cursor: _Cursor) -> Any:
    if isinstance(tree, str):
        return _decode_basic(tree, cursor)
    if tree[0] == "array":
        return _decode_array(tree, cursor)
    if tree[0] == "struct":
        cursor.pad(8)
        return tuple(_decode(field, cursor) for field in tree[1])
    return _decode_dict(tree, cursor)


def _decode_basic(token: str, cursor: _Cursor) -> Any:
    if token == "y":
        return cursor.take(1)[0]
    if token == "b":
        cursor.pad(4)
        return bool(struct.unpack("<i", cursor.take(4))[0])
    if token == "n":
        cursor.pad(2)
        return struct.unpack("<h", cursor.take(2))[0]
    if token == "q":
        cursor.pad(2)
        return struct.unpack("<H", cursor.take(2))[0]
    if token == "i":
        cursor.pad(4)
        return struct.unpack("<i", cursor.take(4))[0]
    if token in ("u", "h"):
        cursor.pad(4)
        return struct.unpack("<I", cursor.take(4))[0]
    if token == "x":
        cursor.pad(8)
        return struct.unpack("<q", cursor.take(8))[0]
    if token == "t":
        cursor.pad(8)
        return struct.unpack("<Q", cursor.take(8))[0]
    if token in ("s", "o"):
        cursor.pad(4)
        length = struct.unpack("<I", cursor.take(4))[0]
        raw = cursor.take(length + 1)
        return raw[:length].decode("utf-8", "replace")
    if token == "g":
        length = cursor.take(1)[0]
        return cursor.take(length + 1)[:length].decode("utf-8", "replace")
    if token == "v":
        signature = _decode_basic("g", cursor)
        if not signature:
            return None
        tree, _ = _parse_type(signature)
        return _decode(tree, cursor)
    raise DBusError("invalid signature", f"cannot decode type {token!r}")


def _decode_array(tree: TypeTree, cursor: _Cursor) -> Any:
    element = tree[1]
    cursor.pad(4)
    length = struct.unpack("<I", cursor.take(4))[0]
    if _element_token(element) in ("x", "t", "d", "{", "("):
        cursor.pad(8)
    start = cursor.offset
    limit = start + length
    if element == "y":
        raw = cursor.take(length)
        return bytes(raw)
    if isinstance(element, tuple) and element[0] == "dict":
        result: Dict[str, Any] = {}
        while cursor.offset < limit:
            key, value = _decode(element, cursor)
            result[key] = value
        return result
    values = []
    while cursor.offset < limit:
        values.append(_decode(element, cursor))
    return values


def _decode_dict(tree: TypeTree, cursor: _Cursor) -> Tuple[Any, Any]:
    cursor.pad(8)
    key = _decode(tree[1], cursor)
    value = _decode(tree[2], cursor)
    return key, value


def unmarshal_body(signature: str, data: bytes) -> Tuple[Any, ...]:
    """Decode a message body according to ``signature``."""
    cursor = _Cursor(data)
    return tuple(_decode(tree, cursor) for tree in parse_signature(signature))


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #

_FIELD_TOKEN = {
    FIELD_PATH: "o",
    FIELD_INTERFACE: "s",
    FIELD_MEMBER: "s",
    FIELD_ERROR_NAME: "s",
    FIELD_REPLY_SERIAL: "u",
    FIELD_DESTINATION: "s",
    FIELD_SENDER: "s",
    FIELD_SIGNATURE: "g",
}


def build_message(
    message_type: int,
    fields: Dict[int, Any],
    signature: str,
    values: Sequence[Any],
    serial: int,
    flags: int = 0,
) -> bytes:
    """Serialise a whole D-Bus message (header plus body)."""
    body = marshal_body(signature, values) if signature else b""
    if signature:
        fields = {**fields, FIELD_SIGNATURE: signature}

    header_fields = bytearray()
    for code in sorted(fields):
        token = _FIELD_TOKEN[code]
        _pad(header_fields, 8)
        header_fields.append(code)
        _encode_basic(header_fields, "g", token)
        _encode(header_fields, token, fields[code])

    buffer = bytearray()
    buffer.append(ord("l"))  # little endian
    buffer.append(message_type)
    buffer.append(flags)
    buffer.append(1)  # protocol version
    buffer.extend(struct.pack("<I", len(body)))
    buffer.extend(struct.pack("<I", serial))
    buffer.extend(struct.pack("<I", len(header_fields)))
    _pad(buffer, 8)
    buffer.extend(header_fields)
    _pad(buffer, 8)
    buffer.extend(body)
    return bytes(buffer)


def parse_message(data: bytes) -> Dict[str, Any]:
    """Split a raw message into type, header fields and body."""
    if len(data) < 16:
        raise DBusError("invalid message", "message shorter than the fixed header")
    fmt = "<" if data[0:1] == b"l" else ">"
    message_type = data[1]
    body_length = struct.unpack_from(fmt + "I", data, 4)[0]
    serial = struct.unpack_from(fmt + "I", data, 8)[0]
    fields_length = struct.unpack_from(fmt + "I", data, 12)[0]

    cursor = _Cursor(data)
    cursor.offset = 16
    fields: Dict[int, Any] = {}
    end = 16 + fields_length
    while cursor.offset < end:
        # every header field is a struct, aligned to 8 bytes
        cursor.pad(8)
        code = cursor.take(1)[0]
        signature = _decode_basic("g", cursor)
        fields[code] = _decode(_parse_type(signature)[0], cursor)

    cursor.pad(8)
    signature = fields.get(FIELD_SIGNATURE, "")
    body = unmarshal_body(signature, data[cursor.offset : cursor.offset + body_length])
    return {
        "type": message_type,
        "serial": serial,
        "fields": fields,
        "body": body,
    }


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #


def read_message(sock: socket.socket, buffer: bytearray) -> Dict[str, Any]:
    """Read one whole message from ``sock`` (blocks until it arrives)."""
    while len(buffer) < 16:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError
        buffer.extend(chunk)
    fmt = "<" if buffer[0:1] == b"l" else ">"
    body_length = struct.unpack_from(fmt + "I", buffer, 4)[0]
    fields_length = struct.unpack_from(fmt + "I", buffer, 12)[0]
    total = 16 + fields_length
    total += (-total) % 8
    total += body_length
    while len(buffer) < total:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError
        buffer.extend(chunk)
    raw = bytes(buffer[:total])
    del buffer[:total]
    return parse_message(raw)


class _PendingCall:
    """A method call waiting for its reply."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.body: Tuple[Any, ...] = ()
        self.error: Optional[DBusError] = None

    def complete(self, message: Dict[str, Any]) -> None:
        if message["type"] == ERROR_MESSAGE:
            name = message["fields"].get(FIELD_ERROR_NAME, "org.freedesktop.DBus.Error.Failed")
            text = message["body"][0] if message["body"] else ""
            self.error = DBusError(name, str(text))
        else:
            self.body = message["body"]
        self.event.set()


def _socket_path(address: str) -> str:
    """Extract the unix socket location from a D-Bus address."""
    transport, _, parameters = address.partition(":")
    if transport != "unix":
        raise DBusError("org.freedesktop.DBus.Error.AddressNotFound",
                        f"unsupported D-Bus address: {address!r}")
    for part in parameters.split(","):
        key, _, value = part.partition("=")
        if key == "path":
            return value
        if key == "abstract":
            return "\x00" + value
    raise DBusError("org.freedesktop.DBus.Error.AddressNotFound",
                    f"no socket path in D-Bus address: {address!r}")


class DBusConnection:
    """A connection to a D-Bus daemon (session bus or accessibility bus)."""

    def __init__(self, address: str) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(_socket_path(address))
        self._lock = threading.Lock()
        self._serial = 0
        self._pending: Dict[int, _PendingCall] = {}
        self._signal_handlers: List[Callable[..., None]] = []
        self._closed = False
        self._authenticate()
        self._reader = threading.Thread(
            target=self._serve, name="cdt-dbus-reader", daemon=True
        )
        self._reader.start()
        # Hello is mandatory and yields our unique bus name
        self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "Hello",
            "",
            (),
        )

    @classmethod
    def session(cls) -> "DBusConnection":
        """Connect to the user's session bus."""
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if not address:
            raise DBusError(
                "org.freedesktop.DBus.Error.NotSupported",
                "no session bus (DBUS_SESSION_BUS_ADDRESS is not set)",
            )
        return cls(address)

    @classmethod
    def connect(cls, address: str) -> "DBusConnection":
        """Connect to the bus at ``address`` (e.g. the AT-SPI bus)."""
        return cls(address)

    # -- low level ----------------------------------------------------------
    def _authenticate(self) -> None:
        # SASL EXTERNAL: the kernel credentials prove the uid
        self._socket.sendall(b"\x00")
        self._socket.sendall(f"AUTH EXTERNAL {os.getuid():x}\r\n".encode())
        line = self._read_line()
        if line.startswith("REJECTED"):
            self._socket.sendall(b"AUTH ANONYMOUS\r\n")
            line = self._read_line()
        if not line.startswith("OK"):
            raise DBusError(
                "org.freedesktop.DBus.Error.AccessDenied",
                f"D-Bus SASL handshake failed: {line!r}",
            )
        self._socket.sendall(b"BEGIN\r\n")

    def _read_line(self) -> str:
        data = bytearray()
        while not data.endswith(b"\r\n"):
            chunk = self._socket.recv(1)
            if not chunk:
                raise DBusError("org.freedesktop.DBus.Error.Disconnected",
                                "the D-Bus connection closed")
            data.extend(chunk)
        return bytes(data[:-2]).decode("utf-8", "replace")

    def _read_message(self, buffer: bytearray) -> Dict[str, Any]:
        return read_message(self._socket, buffer)

    def _serve(self) -> None:
        buffer = bytearray()
        while True:
            try:
                message = self._read_message(buffer)
            except (EOFError, OSError):
                break
            except DBusError:  # pragma: no cover - defensive
                continue
            try:
                self._dispatch(message)
            except Exception:  # pragma: no cover - never kill the reader
                pass

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if message["type"] in (METHOD_RETURN, ERROR_MESSAGE):
            pending = self._pending.pop(message["fields"].get(FIELD_REPLY_SERIAL), None)
            if pending is not None:
                pending.complete(message)
        elif message["type"] == SIGNAL_MESSAGE:
            fields = message["fields"]
            for handler in list(self._signal_handlers):
                handler(
                    fields.get(FIELD_INTERFACE, ""),
                    fields.get(FIELD_MEMBER, ""),
                    fields.get(FIELD_PATH, ""),
                    fields.get(FIELD_SENDER, ""),
                    message["body"],
                )
        # incoming method calls are ignored: the client exposes no objects

    # -- public API ---------------------------------------------------------
    def call(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        args: Sequence[Any] = (),
        timeout: float = 25.0,
    ) -> Tuple[Any, ...]:
        """Call a method and return the decoded reply body."""
        if self._closed:
            raise DBusError("org.freedesktop.DBus.Error.Disconnected",
                            "the D-Bus connection is closed")
        fields = {
            FIELD_DESTINATION: destination,
            FIELD_PATH: path,
            FIELD_INTERFACE: interface,
            FIELD_MEMBER: member,
        }
        if signature:
            fields[FIELD_SIGNATURE] = signature
        pending = _PendingCall()
        with self._lock:
            # register before sending: on a fast local bus the reply can
            # beat the send call back to the reader thread
            self._serial += 1
            serial = self._serial
            self._pending[serial] = pending
            self._socket.sendall(
                build_message(METHOD_CALL, fields, signature, args, serial)
            )
        if not pending.event.wait(timeout):
            self._pending.pop(serial, None)
            raise DBusError(
                "org.freedesktop.DBus.Error.NoReply",
                f"no reply to D-Bus call {interface}.{member}",
            )
        if pending.error is not None:
            raise pending.error
        return pending.body

    def add_match(self, rule: str) -> None:
        """Subscribe to signals matching ``rule`` (an AddMatch string)."""
        self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "AddMatch",
            "s",
            (rule,),
        )

    def add_signal_handler(self, handler: Callable[..., None]) -> None:
        """Register ``handler(interface, member, path, sender, body)``."""
        self._signal_handlers.append(handler)

    def close(self) -> None:
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:  # pragma: no cover - already closed
            pass
        self._socket.close()
