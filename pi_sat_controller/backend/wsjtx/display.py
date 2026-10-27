"""A verified pointer action on the managed application's private display."""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import re


@functools.lru_cache(maxsize=1)
def available() -> bool:
    return all(ctypes.util.find_library(name) for name in ('X11', 'Xtst'))


def _rectangle(value: tuple[int, int, int, int]) -> bool:
    return (isinstance(value, tuple) and len(value) == 4
            and all(type(part) is int for part in value)
            and value[0] >= 0 and value[1] >= 0
            and value[2] > 0 and value[3] > 0)


class _DisplayApi:
    def __init__(self) -> None:
        pointer, window = ctypes.c_void_p, ctypes.c_ulong
        integer, unsigned = ctypes.c_int, ctypes.c_uint
        ulong_pointer = ctypes.POINTER(window)
        self.x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
        self.test = ctypes.CDLL(ctypes.util.find_library('Xtst'))
        bindings = {
            'XOpenDisplay': ([ctypes.c_char_p], pointer),
            'XCloseDisplay': ([pointer], integer),
            'XDefaultScreen': ([pointer], integer),
            'XRootWindow': ([pointer, integer], window),
            'XDisplayWidth': ([pointer, integer], integer),
            'XDisplayHeight': ([pointer, integer], integer),
            'XInternAtom': ([pointer, ctypes.c_char_p, integer], window),
            'XQueryTree': ([pointer, window, ulong_pointer, ulong_pointer,
                            ctypes.POINTER(ulong_pointer), ctypes.POINTER(unsigned)], integer),
            'XGetWindowProperty': ([pointer, window, window, ctypes.c_long, ctypes.c_long,
                                   integer, window, ulong_pointer, ctypes.POINTER(integer),
                                   ulong_pointer, ulong_pointer, ctypes.POINTER(pointer)], integer),
            'XGetGeometry': ([pointer, window, ulong_pointer, ctypes.POINTER(integer),
                              ctypes.POINTER(integer), ctypes.POINTER(unsigned),
                              ctypes.POINTER(unsigned), ctypes.POINTER(unsigned),
                              ctypes.POINTER(unsigned)], integer),
            'XTranslateCoordinates': ([pointer, window, window, integer, integer,
                                       ctypes.POINTER(integer), ctypes.POINTER(integer),
                                       ulong_pointer], integer),
            'XQueryPointer': ([pointer, window, ulong_pointer, ulong_pointer,
                               ctypes.POINTER(integer), ctypes.POINTER(integer),
                               ctypes.POINTER(integer), ctypes.POINTER(integer),
                               ctypes.POINTER(unsigned)], integer),
            'XRaiseWindow': ([pointer, window], integer),
            'XChangeProperty': ([pointer, window, window, window, integer, integer,
                                 ctypes.c_char_p, integer], integer),
            'XSync': ([pointer, integer], integer),
            'XFree': ([pointer], integer),
            'XSetErrorHandler': ([pointer], pointer),
        }
        for name, (arguments, result) in bindings.items():
            function = getattr(self.x11, name)
            function.argtypes, function.restype = arguments, result
        self.test.XTestFakeMotionEvent.argtypes = [pointer, integer, integer, integer, window]
        self.test.XTestFakeMotionEvent.restype = integer
        self.test.XTestFakeButtonEvent.argtypes = [pointer, unsigned, integer, window]
        self.test.XTestFakeButtonEvent.restype = integer

    def owned(self, display, candidate, pid_atom, cardinal_atom, pid: int) -> bool:
        actual_type, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        actual_format, data = ctypes.c_int(), ctypes.c_void_p()
        try:
            status = self.x11.XGetWindowProperty(
                display, candidate, pid_atom, 0, 1, 0, cardinal_atom,
                ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(count),
                ctypes.byref(remaining), ctypes.byref(data))
            if (status != 0 or actual_type.value != cardinal_atom or actual_format.value != 32
                    or count.value != 1 or remaining.value != 0 or not data):
                return False
            # Xlib returns format-32 property items in native unsigned-long storage.
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0] == pid
        finally:
            if data:
                self.x11.XFree(data)

    def geometry(self, display, candidate, root) -> tuple[int, int, int, int] | None:
        returned_root, child = ctypes.c_ulong(), ctypes.c_ulong()
        local_x, local_y, screen_x, screen_y = (ctypes.c_int() for _ in range(4))
        width, height, border, depth = (ctypes.c_uint() for _ in range(4))
        if not self.x11.XGetGeometry(
                display, candidate, ctypes.byref(returned_root), ctypes.byref(local_x),
                ctypes.byref(local_y), ctypes.byref(width), ctypes.byref(height),
                ctypes.byref(border), ctypes.byref(depth)) or returned_root.value != root:
            return None
        if not self.x11.XTranslateCoordinates(
                display, candidate, root, 0, 0, ctypes.byref(screen_x),
                ctypes.byref(screen_y), ctypes.byref(child)):
            return None
        return screen_x.value, screen_y.value, width.value, height.value

    def property(self, display, window, atom) -> bytes | None:
        actual_type, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        actual_format, data = ctypes.c_int(), ctypes.c_void_p()
        try:
            status = self.x11.XGetWindowProperty(
                display, window, atom, 0, 1024, 0, 0,
                ctypes.byref(actual_type), ctypes.byref(actual_format),
                ctypes.byref(count), ctypes.byref(remaining), ctypes.byref(data),
            )
            if (status != 0 or actual_format.value != 8 or remaining.value != 0
                    or count.value > 1024 or (count.value and not data)):
                return None
            return ctypes.string_at(data, count.value) if data else b''
        finally:
            if data:
                self.x11.XFree(data)


def press_owned_button(display_name: str, pid: int,
                       frame_rect: tuple[int, int, int, int],
                       button_rect: tuple[int, int, int, int]) -> bool:
    """Click once only when process, live geometry and pointer destination agree."""
    if (not isinstance(display_name, str) or not re.fullmatch(r':\d+(?:\.\d+)?', display_name)
            or type(pid) is not int or pid <= 0 or not _rectangle(frame_rect)
            or not _rectangle(button_rect) or not available()):
        return False
    fx, fy, fw, fh = frame_rect
    bx, by, bw, bh = button_rect
    if bx < fx or by < fy or bx + bw > fx + fw or by + bh > fy + fh:
        return False
    api = _DisplayApi()
    display = api.x11.XOpenDisplay(display_name.encode('ascii'))
    if not display:
        return False
    native_error = False

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def record_error(_display, _event):
        nonlocal native_error
        native_error = True
        return 0

    previous_handler = api.x11.XSetErrorHandler(ctypes.cast(record_error, ctypes.c_void_p))
    pressed = False
    try:
        screen = api.x11.XDefaultScreen(display)
        root = api.x11.XRootWindow(display, screen)
        if (fx + fw > api.x11.XDisplayWidth(display, screen)
                or fy + fh > api.x11.XDisplayHeight(display, screen)):
            return False
        pid_atom = api.x11.XInternAtom(display, b'_NET_WM_PID', 1)
        cardinal_atom = api.x11.XInternAtom(display, b'CARDINAL', 1)
        if not pid_atom or not cardinal_atom:
            return False
        returned_root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        try:
            if not api.x11.XQueryTree(display, root, ctypes.byref(returned_root),
                                     ctypes.byref(parent), ctypes.byref(children),
                                     ctypes.byref(count)) or count.value > 1024:
                return False
            matches = [children[index] for index in range(count.value)
                       if api.owned(display, children[index], pid_atom, cardinal_atom, pid)
                       and api.geometry(display, children[index], root) == frame_rect]
        finally:
            if children:
                api.x11.XFree(children)
        api.x11.XSync(display, 0)
        if native_error or len(matches) != 1:
            return False
        target = matches[0]
        api.x11.XRaiseWindow(display, target)
        if not api.test.XTestFakeMotionEvent(display, screen, bx + bw // 2, by + bh // 2, 0):
            return False
        api.x11.XSync(display, 0)
        pointer_root, pointer_child = ctypes.c_ulong(), ctypes.c_ulong()
        coordinates = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        if (not api.x11.XQueryPointer(display, root, ctypes.byref(pointer_root),
                                     ctypes.byref(pointer_child),
                                     *(ctypes.byref(value) for value in coordinates),
                                     ctypes.byref(mask))
                or pointer_root.value != root or pointer_child.value != target):
            return False
        api.x11.XSync(display, 0)
        if native_error or api.geometry(display, target, root) != frame_rect:
            return False
        # A failed request may already have reached the server; always release.
        pressed = True
        if not api.test.XTestFakeButtonEvent(display, 1, 1, 0):
            return False
        released = bool(api.test.XTestFakeButtonEvent(display, 1, 0, 0))
        pressed = not released
        api.x11.XSync(display, 0)
        return released and not native_error
    finally:
        if pressed:
            api.test.XTestFakeButtonEvent(display, 1, 0, 0)
        api.x11.XSync(display, 0)
        api.x11.XSetErrorHandler(previous_handler)
        api.x11.XCloseDisplay(display)


def set_root_property(display_name: str, name: str, value: str) -> bool:
    """Publish one bounded STRING property on the managed display root."""
    if (not isinstance(display_name, str) or not re.fullmatch(r':\d+(?:\.\d+)?', display_name)
            or not isinstance(name, str) or not name.isascii() or not name.isprintable() or not name
            or not isinstance(value, str) or not value.isascii() or not value.isprintable() or not value
            or '\x00' in value or len(value.encode('ascii')) > 1024
            or not available()):
        return False
    api = _DisplayApi()
    display = api.x11.XOpenDisplay(display_name.encode('ascii'))
    if not display:
        return False
    native_error = False

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def record_error(_display, _event):
        nonlocal native_error
        native_error = True
        return 0

    previous_handler = api.x11.XSetErrorHandler(ctypes.cast(record_error, ctypes.c_void_p))
    try:
        screen = api.x11.XDefaultScreen(display)
        root = api.x11.XRootWindow(display, screen)
        property_atom = api.x11.XInternAtom(display, name.encode('ascii'), 0)
        string_atom = api.x11.XInternAtom(display, b'STRING', 0)
        data = value.encode('ascii')
        if not property_atom or not string_atom:
            return False
        changed = api.x11.XChangeProperty(
            display, root, property_atom, string_atom, 8, 0, data, len(data),
        )
        api.x11.XSync(display, 0)
        return (bool(changed) and not native_error
                and api.property(display, root, property_atom) == data)
    finally:
        api.x11.XSync(display, 0)
        api.x11.XSetErrorHandler(previous_handler)
        api.x11.XCloseDisplay(display)
