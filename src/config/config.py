"""
A shared resource to set inpainting configuration and to provide default values.

Main features
-------------
    - Save and load values from a JSON file.
    - Allow access to those values through config.get()
    - Allow typesafe changes to those values through config.set()
    - Subscribe to specific value changes through config.connect()
"""
import json
import logging
import os
import tempfile
import threading
from inspect import signature
from threading import Lock
from typing import Optional, Any, Callable

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QKeySequence, QColor
from PySide6.QtWidgets import QApplication

from src.config.config_entry import ConfigEntry, DefinitionKey, DefinitionType
from src.ui.input_fields.check_box import CheckBox
from src.ui.input_fields.combo_box import ComboBox
from src.util.parameter import ParamType, DynamicFieldWidget, ParamTypeList, get_parameter_type
from src.util.signals_blocked import signals_blocked

logger = logging.getLogger(__name__)

# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'config.config'


def _tr(key: str, disambiguation: Optional[str] = None, n: int = -1) -> str:
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, key, disambiguation, n)


MISSING_DEF_ERROR = _tr('Config definition file not found at {definition_path}')
INVALID_CONFIG_TYPE_ERROR = _tr('Config value definition for {key} had invalid data type {value_type}')
INVALID_KEY_ERROR = _tr('Loading {key} failed: {err}')
INVALID_JSON_DEFINITION_ERROR = _tr('Reading JSON config definitions failed: {err}')
INVALID_JSON_ERROR = _tr('Reading JSON config values failed: {err}')
UNKNOWN_KEY_ERROR = _tr('Tried to access unknown config value "{key}"')
INVALID_OPTION_KEY_ERROR = _tr('Tried to track fixed options for key "{key}", which does not have a fixed list of'
                               ' options.')
INVALID_KEYCODE_ERROR = _tr('Tried to get key code "{key}", found "{code_string}"')
DUPLICATE_KEY_ERROR = _tr('Tried to add duplicate config entry for key "{key}"')


class Config:
    """A shared resource to set inpainting configuration and to provide default values.

    Common Exceptions Raised
    ------------------------
    KeyError
        When any function with the `key` parameter is called with an unknown key.
    TypeError
        When a function with the optional `inner_key` parameter is used with a non-empty `inner_key` and a `key`
        that doesn't contain a dict value.
    RuntimeError
        If a function that interacts with lists of accepted value options is called on a value that doesn't have
        a fixed list of acceptable options.
    """

    def __init__(self, definition_path: str, saved_value_path: Optional[str], child_class: type) -> None:
        """Load existing config, or initialize from defaults.

        Parameters
        ----------
        definition_path: str
            Path to a file defining accepted config values.
        saved_value_path: str, optional
            Path where config values will be saved and read. If the file does not exist, it will be created with
            default values. Any expected keys not found in the file will be added with default values. Any unexpected
            values will be removed. If not provided, the Config object won't allow file IO.
        child_class: class
            Child class where definition keys should be written as properties when first initialized.
        """
        self._entries: dict[str, ConfigEntry] = {}
        self._connected: dict[str, dict[Any, Callable[..., None]]] = {}
        self._option_connected: dict[str, dict[Any, Callable[[ParamTypeList], None]]] = {}
        self._json_path = saved_value_path
        self._lock = Lock()
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)

        if not os.path.isfile(definition_path):
            raise RuntimeError(MISSING_DEF_ERROR.format(definition_path=definition_path))
        try:
            with open(definition_path, encoding='utf-8') as file:
                config_text_key = definition_path.replace('_definitions.json', '')

                def _tr_cfg(text: str) -> str:
                    return QApplication.translate(config_text_key, text)

                json_data = json.load(file)
                for key, definition in json_data.items():
                    assert isinstance(definition, dict)
                    init_attr_name = key.upper()
                    if not hasattr(child_class, init_attr_name):
                        setattr(child_class, init_attr_name, key)
                    try:
                        if DefinitionKey.DEFAULT in definition:
                            initial_value = definition[DefinitionKey.DEFAULT]
                            match definition[DefinitionKey.TYPE]:
                                case DefinitionType.QSIZE:
                                    width, height = (int(n) for n in initial_value.split('x'))
                                    initial_value = QSize(width, height)
                                case DefinitionType.INT:
                                    initial_value = int(initial_value)
                                case DefinitionType.FLOAT:
                                    initial_value = float(initial_value)
                                case DefinitionType.STR:
                                    initial_value = str(initial_value)
                                case DefinitionType.BOOL:
                                    initial_value = bool(initial_value)
                                case DefinitionType.LIST:
                                    initial_value = list(initial_value)
                                case DefinitionType.DICT:
                                    initial_value = dict(initial_value)
                                case _:
                                    raise RuntimeError(INVALID_CONFIG_TYPE_ERROR.format(key=key,
                                                                                        value_type=definition[
                                                                                            DefinitionKey.TYPE]))
                        else:  # If no default is provided, use the closest equivalent to an empty value:
                            match definition[DefinitionKey.TYPE]:
                                case DefinitionType.QSIZE:
                                    initial_value = QSize()
                                case DefinitionType.INT:
                                    initial_value = 0
                                case DefinitionType.FLOAT:
                                    initial_value = 0.0
                                case DefinitionType.STR:
                                    initial_value = ''
                                case DefinitionType.BOOL:
                                    initial_value = False
                                case DefinitionType.LIST:
                                    initial_value = []
                                case DefinitionType.DICT:
                                    initial_value = {}
                                case _:
                                    raise RuntimeError(INVALID_CONFIG_TYPE_ERROR.format(key=key,
                                                                                        value_type=definition[
                                                                                            DefinitionKey.TYPE]))
                    except KeyError as err:
                        raise RuntimeError(INVALID_KEY_ERROR.format(key=key, err=err)) from err

                    label = _tr_cfg(definition[DefinitionKey.LABEL])
                    category = _tr_cfg(definition[DefinitionKey.CATEGORY])
                    subcategory = None if DefinitionKey.SUBCATEGORY not in definition \
                        else _tr_cfg(definition[DefinitionKey.SUBCATEGORY])
                    tooltip = _tr_cfg(definition[DefinitionKey.TOOLTIP])
                    options = None if DefinitionKey.OPTIONS not in definition \
                        else list(definition[DefinitionKey.OPTIONS])
                    range_options = None if DefinitionKey.RANGE not in definition \
                        else dict(definition[DefinitionKey.RANGE])
                    if DefinitionKey.SAVED in definition:
                        save_json = definition[DefinitionKey.SAVED]
                    else:
                        save_json = False
                    self._add_entry(key, initial_value, label, category, subcategory, tooltip, options, range_options,
                                    save_json)

        except json.JSONDecodeError as err:
            raise RuntimeError(INVALID_JSON_DEFINITION_ERROR.format(err=err)) from err

        self._adjust_defaults()
        if self._json_path is not None:
            if os.path.isfile(self._json_path):
                self._read_from_json()
            else:
                self._write_to_json()

    # noinspection PyProtectedMember
    def _reset(self) -> None:
        """Discard all changes and connections, and reload from JSON. For testing use only."""
        with self._lock:
            self._connected = {}
            for key, entry in self._entries.items():
                self._connected[key] = {}
                entry._value = entry.default_value
                if entry._options is not None and len(entry._options) > 0 and entry.default_value is not None:
                    entry._options = [entry.default_value]

    def _adjust_defaults(self) -> None:
        """Override this to perform any adjustments to default values needed before file IO, e.g. loading list options
           from an external source."""

    @property
    def json_path(self) -> Optional[str]:
        """Returns the path where this config object saves changes. If None, the config object does not save data to
         disk."""
        return self._json_path

    def get(self, key: str, inner_key: Optional[str] = None) -> Any:
        """Returns a value from config.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        inner_key : str, optional
            If not None, assume the value at `key` is a dict and attempt to return the value within it at `inner_key`.
            If the value is a dict but does not contain `inner_key`, instead return None

        Returns
        -------
        int or float or str or bool or list or dict or QSize or None
            Type varies based on key. Each key is guaranteed to always return the same type, but inner_key values
            are not type-checked.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        with self._lock:
            return self._entries[key].get_value(inner_key)

    def get_data_type(self, key: str) -> str:
        """Gets the data type associated with a config key, raising KeyError if the key isn't found."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].type_name

    def get_category(self, key: str) -> str:
        """Returns a config value's category, raising KeyError if the value does not exist."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].category

    def get_subcategory(self, key: str) -> Optional[str]:
        """Returns a config value's subcategory, raising KeyError if the value does not exist."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].subcategory

    def get_color(self, key: str, default_color: QColor | Qt.GlobalColor) -> QColor:
        """Returns a color value from config.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        default_color : QColor | Qt.GlobalColor
            Color value to return if the key doesn't map to a valid color string.

        Returns
        -------
        The configured color, or the default if the color isn't valid."""
        color_str = self.get(key)
        if not QColor(color_str).isValid():
            if isinstance(default_color, Qt.GlobalColor):
                default_color = QColor(default_color)
            return default_color
        return QColor(color_str)

    def get_control_widget(self, key: str, connect_to_config: bool = True, multi_line=False) -> DynamicFieldWidget:
        """Returns a QWidget capable of adjusting the chosen config value. Unless connect_to_config is false, changes
        will immediately propagate to the underlying config file."""
        with self._lock:
            entry = self._entries[key]
            control_widget = entry.get_input_widget(multi_line, False)
            control_widget.setValue(entry.get_value())
            if isinstance(control_widget, CheckBox):
                control_widget.setText(entry.name)
            if connect_to_config:
                config_key = key

                def _update_config(new_value: Any) -> None:
                    self.set(config_key, new_value)

                assert hasattr(control_widget, 'valueChanged')
                control_widget.valueChanged.connect(_update_config)

                def _update_control(new_value: Any) -> None:
                    if control_widget.value() != new_value:
                        if isinstance(control_widget, ComboBox):
                            current_options = self.get_options(config_key)
                            widget_options = [control_widget.itemText(i) for i in range(control_widget.count())]
                            if widget_options != current_options:
                                with signals_blocked(control_widget):
                                    while control_widget.count() > 0:
                                        control_widget.removeItem(0)
                                    for new_option in current_options:
                                        control_widget.addItem(str(new_option), userData=new_option)
                        control_widget.setValue(new_value)

                self.connect(control_widget, key, _update_control)

                if isinstance(control_widget, ComboBox):
                    combobox = control_widget

                    def _update_options(new_options: ParamTypeList) -> None:
                        last_selected_text = combobox.currentText()
                        with signals_blocked(combobox):
                            while combobox.count() > 0:
                                combobox.removeItem(0)
                            for option in new_options:
                                combobox.addItem(str(option), userData=option)
                            new_index = combobox.findText(last_selected_text)
                            if new_index >= 0:
                                combobox.setCurrentIndex(new_index)

                    self.connect_to_option_changes(combobox, key, _update_options)
            return control_widget

    def get_keycodes(self, key: str) -> QKeySequence:
        """Returns a config value as a key sequence, throws RuntimeError if the value isn't a keycode."""
        code_string = self.get(key)
        if not isinstance(code_string, str):
            raise RuntimeError(INVALID_KEYCODE_ERROR.format(key=key, code_string=code_string))
        sequence = QKeySequence(code_string)
        if Qt.Key.Key_unknown in sequence:
            raise RuntimeError(INVALID_KEYCODE_ERROR.format(key=key, code_string=code_string))
        return sequence

    def get_label(self, key: str) -> str:
        """Gets the label text assigned to a config value."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return _tr(self._entries[key].name)

    def get_tooltip(self, key: str) -> str:
        """Gets the tooltip text assigned to a config value."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return _tr(self._entries[key].description)

    def set(self,
            key: str,
            value: Any,
            save_change: bool = True,
            add_missing_options: bool = False,
            inner_key: Optional[str] = None) -> None:
        """Updates a saved value.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        value : int or float or str or bool or list or dict or QSize or None
            The new value to assign to the key. Unless inner_key is not None, this must have the same type as the
            previous value.
        save_change: bool, default=True
            If true, save the change to the underlying JSON file. Otherwise, the change will be saved the next time
            any value is set with save_change=True
        add_missing_options: bool, default=False
            If the key is associated with a list of valid options and this is true, value will be added to the list
            of options if not already present. Otherwise, RuntimeError is raised if value is not within the list
        inner_key: str, optional
            If not None, assume the value at `key` is a dict and attempt to set the value within it at `inner_key`. If
            the value is a dict but does not contain `inner_key`, instead return None
       
        Raises
        ------
        TypeError
            If `value` does not have the same type as the current value saved under `key`
        RuntimeError
            If `key` has a list of associated valid options, `value` is not one of those options, and
            `add_missing_options` is false.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        new_value = value
        # Update existing value:
        with self._lock:
            value_changed = self._entries[key].set_value(value, add_missing_options, inner_key)
        if not value_changed:
            return
        # Schedule save to JSON file:
        if save_change:
            with self._lock:
                if not self._save_timer.isActive():
                    if threading.current_thread() is not threading.main_thread():
                        self._write_to_json()  # Timers can't be started from other threads.
                    else:
                        def write_change() -> None:
                            """Copy changes to the file and disconnect the timer."""
                            self._write_to_json()
                         