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
