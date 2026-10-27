"""Confirmed native controls for the owned stock application."""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import json
import math
import os
import sys
import time

_READBACK_SECONDS = 1.0
_DISCOVERY_SECONDS = 5.0
_CONTROL_NAMES = {
    'auto_sequence': ('check box', 'Auto Seq', ''),
    'tx_even': ('check box', 'Tx even/1st', ''),
    'tx_enabled': ('check box', 'Enable Tx', ''),
    'tx_df_hz': ('spin button', '', 'Audio Tx frequency'),
    'rx_df_hz': ('spin button', '', 'Audio Rx frequency'),
}
_CONTROL_LABELS = {
    'auto_sequence': 'Auto Seq',
    'tx_even': 'Tx even/1st',
    'tx_enabled': 'Enable Tx',
    'tx_df_hz': 'Audio Tx frequency',
    'rx_df_hz': 'Audio Rx frequency',
}

class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in ('x', 'y', 'width', 'height')]

@functools.lru_cache(maxsize=1)
def available() -> bool:
    from .display import available as display_available
    return display_available() and all(ctypes.util.find_library(name) for name in ('atspi', 'gobject-2.0', 'glib-2.0'))

class _NativeApi:
    def __init__(self):
        pointer = ctypes.c_void_p
        self.api = ctypes.CDLL(ctypes.util.find_library('atspi'))
        self.objects = ctypes.CDLL(ctypes.util.find_library('gobject-2.0'))
        self.memory = ctypes.CDLL(ctypes.util.find_library('glib-2.0'))
        bindings = {
            'atspi_init': ([], ctypes.c_int),
            'atspi_get_desktop': ([ctypes.c_int], pointer),
            'atspi_accessible_get_child_count': ([pointer, pointer], ctypes.c_int),
            'atspi_accessible_get_child_at_index': ([pointer, ctypes.c_int, pointer], pointer),
            'atspi_accessible_get_process_id': ([pointer, pointer], ctypes.c_uint),
            'atspi_accessible_get_state_set': ([pointer], pointer),
            'atspi_state_set_contains': ([pointer, ctypes.c_int], ctypes.c_int),
            'atspi_accessible_get_action_iface': ([pointer], pointer),
            'atspi_action_get_n_actions': ([pointer, pointer], ctypes.c_int),
            'atspi_action_get_action_name': ([pointer, ctypes.c_int, pointer], pointer),
            'atspi_action_do_action': ([pointer, ctypes.c_int, pointer], ctypes.c_int),
            'atspi_accessible_get_value_iface': ([pointer], pointer),
            'atspi_accessible_get_component_iface': ([pointer], pointer),
            'atspi_component_get_extents': ([pointer, ctypes.c_int, pointer], ctypes.POINTER(_Rect)),
            'atspi_value_set_current_value': ([pointer, ctypes.c_double, pointer], ctypes.c_int),
            'atspi_accessible_clear_cache': ([pointer], None),
        }
        for name in ('name', 'role_name', 'description'):
            bindings['atspi_accessible_get_' + name] = ([pointer, pointer], pointer)
        for name in ('current', 'minimum', 'maximum'):
            bindings['atspi_value_get_' + name + '_value'] = ([pointer, pointer], ctypes.c_double)
        for name, (arguments, result) in bindings.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.objects.g_object_unref.argtypes = [pointer]
        self.objects.g_object_ref.argtypes = [pointer]
        self.objects.g_object_ref.restype = pointer
        self.memory.g_free.argtypes = [pointer]
        if self.api.atspi_init() != 0:
            raise RuntimeError('Linux accessibility service is unavailable')
        self.desktop = self.api.atspi_get_desktop(0)
        if not self.desktop:
            raise RuntimeError('Linux accessibility desktop is unavailable')

    def unref(self, node):
        self.objects.g_object_unref(node)

    def ref(self, node):
        return self.objects.g_object_ref(node)

    def clear_cache(self, node):
        self.api.atspi_accessible_clear_cache(node)

    def _text(self, function, node, *arguments):
        value = getattr(self.api, function)(node, *arguments, None)
        if not value:
            return ''
        try:
            return ctypes.string_at(value).decode('utf-8', errors='replace')
        finally:
            self.memory.g_free(value)

    def info(self, node):
        return {key: self._text('atspi_accessible_get_' + suffix, node)
                for key, suffix in (('name', 'name'), ('role', 'role_name'), ('description', 'description'))}

    def children(self, node):
        count = self.api.atspi_accessible_get_child_count(node, None)
        if not 0 <= count <= 1000:
            raise RuntimeError('Managed accessibility tree is unavailable or too large')
        return [child for index in range(count)
                if (child := self.api.atspi_accessible_get_child_at_index(node, index, None))]

    def process_id(self, node):
        return self.api.atspi_accessible_get_process_id(node, None)

    def _state(self, node, flag):
        self.clear_cache(node)
        states = self.api.atspi_accessible_get_state_set(node)
        if not states:
            raise RuntimeError('Stock control state is unavailable')
        try:
            return bool(self.api.atspi_state_set_contains(states, flag))
        finally:
            self.unref(states)

    def enabled(self, node):
        return self._state(node, 8)

    def checked(self, node):
        return self._state(node, 4)

    def toggle(self, node):
        action = self.api.atspi_accessible_get_action_iface(node)
        if not action:
            raise RuntimeError('Stock checkbox has no accessible action')
        try:
            actions = [index for index in range(self.api.atspi_action_get_n_actions(action, None))
                       if self._text('atspi_action_get_action_name', action, index).lower() == 'toggle']
            if len(actions) != 1:
                raise RuntimeError('Stock checkbox has no unique toggle action')
            return bool(self.api.atspi_action_do_action(action, actions[0], None))
        finally:
            self.unref(action)

    def _bounds(self, node):
        if not self._state(node, 25) or not self._state(node, 30):
            raise RuntimeError('Stock transmit control is not visible')
        interface = self.api.atspi_accessible_get_component_iface(node)
        if not interface:
            raise RuntimeError('Stock transmit control has no component interface')
        rectangle = None
        try:
            rectangle = self.api.atspi_component_get_extents(interface, 0, None)
            if not rectangle:
                raise RuntimeError('Stock transmit control geometry is unavailable')
            return tuple(getattr(rectangle.contents, key) for key in ('x', 'y', 'width', 'height'))
        finally:
            if rectangle:
                self.memory.g_free(rectangle)
            self.unref(interface)

    def activate(self, node, window, pid):
        from .display import press_owned_button
        # A checkable push button's Toggle action need not run its click handler.
        # Activate the verified button, rather than changing only its appearance.
        return press_owned_button(os.environ.get('DISPLAY', ''), pid,
                                  self._bounds(window), self._bounds(node))

    def _value(self, node, operation, value=None):
        interface = self.api.atspi_accessible_get_value_iface(node)
        if not interface:
            raise RuntimeError('Stock frequency control has no numeric value interface')
        try:
            function = getattr(self.api, 'atspi_value_' + operation)
            return function(interface, None) if value is None else function(interface, value, None)
        finally:
            self.unref(interface)

    def value(self, node):
        self.clear_cache(node)
        return self._value(node, 'get_current_value')

    def value_range(self, node):
        return self._value(node, 'get_minimum_value'), self._value(node, 'get_maximum_value')

    def set_value(self, node, value):
        return bool(self._value(node, 'set_current_value', value))

class _OwnedControls:
    def __init__(self, api, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError('Invalid managed application process')
        self.api, self.pid = api, pid
        self.controls = {}
        self.window = None

    def _release(self, controls):
        for nodes in controls.values():
            for node in nodes:
                self.api.unref(node)

    def _scan(self, window):
        found = {key: [] for key in _CONTROL_NAMES}
        queue = self.api.children(window)
        visited = 0
        try:
            while queue:
                node = queue.pop()
                try:
                    visited += 1
                    if visited > 1000:
                        raise RuntimeError('Managed accessibility tree exceeded its bound')
                    info = self.api.info(node)
                    match = next((key for key, (role, name, description) in _CONTROL_NAMES.items()
                                  if info['role'] == role and
                                  (info['description'].strip() == description if description else info['name'] == name)), None)
                    if match:
                        found[match].append(node)
                        node = None
                    else:
                        queue.extend(self.api.children(node))
                finally:
                    if node:
                        self.api.unref(node)
            return found
        except Exception:
            self._release(found)
            raise
        finally:
            for node in queue:
                self.api.unref(node)

    def __enter__(self):
        deadline = time.monotonic() + _DISCOVERY_SECONDS
        observed = set()
        saw_application = saw_owned_application = saw_owned_window = False
        while True:
            candidates = []
            self.api.clear_cache(self.api.desktop)
            applications = self.api.children(self.api.desktop)
            saw_application = saw_application or bool(applications)
            try:
                for application in applications:
                    if self.api.process_id(application) != self.pid:
                        continue
                    saw_owned_application = True
                    windows = self.api.children(application)
                    try:
                        for window in windows:
                            if self.api.info(window)['role'] not in ('frame', 'dialog'):
                                continue
                            saw_owned_window = True
                            found = self._scan(window)
                            observed.update(key for key, nodes in found.items() if nodes)
                            if all(found.values()):
                                candidates.append((self.api.ref(window), found))
                            else:
                                self._release(found)
                    finally:
                        for window in windows:
                            self.api.unref(window)
                if candidates:
                    if len(candidates) != 1 or any(len(nodes) != 1 for nodes in candidates[0][1].values()):
                        raise RuntimeError('Owned WSJT-X transmit controls are ambiguous')
                    self.window, selected = candidates.pop()
                    self.controls = {key: nodes[0] for key, nodes in selected.items()}
                    self._validate()
                    return self
            except Exception:
                self.__exit__(None, None, None)
                raise
            finally:
                for application in applications:
                    self.api.unref(application)
                for window, candidate in candidates:
                    self.api.unref(window)
                    self._release(candidate)
            if time.monotonic() >= deadline:
                if not saw_application:
                    raise RuntimeError('Linux accessibility has no registered applications')
                if not saw_owned_application:
                    raise RuntimeError(
                        f'No accessible application matched owned WSJT-X PID {self.pid}'
                    )
                if not saw_owned_window:
                    raise RuntimeError(
                        f'Owned WSJT-X PID {self.pid} exposed no accessible window'
                    )
                missing = [_CONTROL_LABELS[key] for key in _CONTROL_NAMES if key not in observed]
                if missing:
                    raise RuntimeError('Owned WSJT-X is missing required transmit controls: '
                                       + ', '.join(missing))
                raise RuntimeError('Owned WSJT-X transmit controls were not available in one window')
            time.sleep(0.05)

    def __exit__(self, *_):
        for node in self.controls.values():
            self.api.unref(node)
        self.controls.clear()
        if self.window is not None:
            self.api.unref(self.window)
            self.window = None

    def _validate(self):
        for key, node in self.controls.items():
            if not self.api.enabled(node):
                raise RuntimeError('Stock ' + key + ' control is disabled')
        for key in ('tx_df_hz', 'rx_df_hz'):
            node = self.controls[key]
            low, high = self.api.value_range(node)
            current = self.api.value(node)
            if (not all(math.isfinite(value) for value in (low, high, current))
                    or not low <= current <= high or low > 200 or high < 200 or current != int(current)):
                raise RuntimeError('Stock ' + key + ' frequency interface is incompatible')

    def inspect(self):
        return {key: int(self.api.value(node)) if key.endswith('_hz') else self.api.checked(node)
                for key, node in self.controls.items()}

    def _confirm(self, predicate, key):
        deadline = time.monotonic() + _READBACK_SECONDS
        while not predicate():
            if time.monotonic() >= deadline:
                raise RuntimeError('Stock ' + key + ' control did not confirm its state')
            time.sleep(0.02)

    def _set_checked(self, key, expected):
        node = self.controls[key]
        if not self.api.enabled(node):
            raise RuntimeError('Stock ' + key + ' control is disabled')
        if self.api.checked(node) == expected:
            return
        accepted = self.api.activate(node, self.window, self.pid) if key == 'tx_enabled' else self.api.toggle(node)
        if not accepted:
            raise RuntimeError('Stock ' + key + ' control refused the requested change')
        self._confirm(lambda: self.api.checked(node) == expected, key)

    def set_options(self, tx_even):
        if type(tx_even) is not bool:
            raise ValueError('Invalid managed transmit slot')
        self._set_checked('auto_sequence', False)
        self._set_checked('tx_even', tx_even)
        return self.inspect()

    def set_offset(self, hz):
        node = self.controls['tx_df_hz']
        if not self.api.enabled(node):
            raise RuntimeError('Stock tx_df_hz control is disabled')
        low, high = self.api.value_range(node)
        if type(hz) is not int or not 200 <= hz <= 5000 or not low <= hz <= high:
            raise ValueError('Invalid managed transmit audio offset')
        if self.api.value(node) != hz:
            if not self.api.set_value(node, float(hz)):
                raise RuntimeError('Stock transmit frequency refused the requested change')
            self._confirm(lambda: self.api.value(node) == hz, 'tx_df_hz')
        return self.inspect()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Invalid managed transmit enable state')
        if enabled and self.api.checked(self.controls['auto_sequence']):
            raise RuntimeError('Stock automatic sequencing must be disabled before enabling transmit')
        self._set_checked('tx_enabled', enabled)
        return self.inspect()

def run_control(pid, operation, value=None):
    if operation not in ('inspect', 'options', 'offset', 'enabled'):
        raise ValueError('Invalid managed control operation')
    if not os.environ.get('DBUS_SESSION_BUS_ADDRESS', '').startswith(('unix:path=', 'unix:abstract=')):
        raise RuntimeError('The managed private session bus is unavailable')
    api = _NativeApi()
    try:
        with _OwnedControls(api, pid) as controls:
            if operation == 'inspect':
                return controls.inspect()
            return {'options': controls.set_options, 'offset': controls.set_offset,
                    'enabled': controls.set_enabled}[operation](value)
    finally:
        api.unref(api.desktop)

if __name__ == '__main__':
    try:
        if len(sys.argv) not in (3, 4):
            raise ValueError('Invalid managed accessibility arguments')
        operation = sys.argv[2]
        value = None
        if operation != 'inspect':
            if len(sys.argv) != 4:
                raise ValueError('Missing managed control value')
            if operation in ('options', 'enabled'):
                if sys.argv[3] not in ('0', '1'):
                    raise ValueError('Invalid managed control state')
                value = sys.argv[3] == '1'
            else:
                value = int(sys.argv[3])
        print(json.dumps(run_control(int(sys.argv[1]), operation, value)))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
