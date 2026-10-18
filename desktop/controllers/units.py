"""The unit system every page shows its numbers in (rework.md Track J,
plans/units.md J2): one object, registered as ``units`` in
``desktop/app.py``, that every controller and QML page reads the current
system from and reacts to through ``unitsChanged``.

The choice is the desktop preference ``"units"`` in ``desktop_prefs.json``
(``desktop/preferences.py``); all conversions, labels and rounding come from
``src/core/units.py``, nothing is duplicated here. Stored data stays SI:
only what is shown converts.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import preferences
from src.core import units as core_units


class UnitsController(QObject):
    unitsChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._units = preferences.get_units()

    # -- the setting --------------------------------------------------------

    @Property(str, notify=unitsChanged)
    def units(self) -> str:
        """``"metric"`` or ``"imperial"``."""
        return self._units

    @Property(bool, notify=unitsChanged)
    def isImperial(self) -> bool:
        return self._units == core_units.IMPERIAL

    @Slot(str)
    def setUnits(self, system: str) -> None:
        """Switch the shown unit system: normalised (an unknown value is
        metric), saved to the desktop preferences and announced through
        ``unitsChanged`` only when it actually changes."""
        system = core_units.normalize(system)
        if system == self._units:
            return
        self._units = system
        preferences.set_units(system)
        self.unitsChanged.emit()

    @Property("QVariantList", constant=True)
    def systems(self):
        """The two choices for the Settings card: ``{key, label}`` per
        system, in ``UNIT_SYSTEMS`` order, with ``UNIT_LABELS``' texts."""
        return [{"key": key, "label": core_units.UNIT_LABELS[key]} for key in core_units.UNIT_SYSTEMS]

    @Property("QVariantMap", notify=unitsChanged)
    def labels(self):
        """The unit label of every kind in the current system, keyed by kind
        (``{"depth": "ft", "pressure": "psi", ...}``): a property rather than
        the ``label`` slot for QML bindings that must re-evaluate when the
        system changes (``"Max depth (" + units.labels.depth + ")"``)."""
        return {kind: core_units.unit_label(kind, self._units) for kind in core_units.KINDS}

    @Property("QVariantMap", notify=unitsChanged)
    def scales(self):
        """What a QML component that draws SI numbers itself needs per kind,
        keyed by kind: ``{"factor": <shown unit per SI unit>, "label":
        <unit label>, "decimals": <the display rule's>}`` (``factor`` is
        ``convert(kind, 1)``: 1 in metric, 3.28... for depth in imperial).
        ProfileChart.qml labels its depth axis from ``scales.depth`` (J4);
        a temperature is affine, so its factor alone would be wrong - the
        chart does not plot one."""
        return {kind: {"factor": core_units.convert(kind, 1.0, self._units),
                       "label": core_units.unit_label(kind, self._units),
                       "decimals": core_units.decimals(kind, self._units)}
                for kind in core_units.KINDS if kind != "temperature"}

    # -- conversions for the current system (src/core/units.py by kind) -----

    @Slot(str, result=str)
    def label(self, kind: str) -> str:
        """The unit label of ``kind`` (depth, pressure, temperature, volume,
        weight, sac) in the current system; ``""`` for an unknown kind."""
        return core_units.unit_label(kind, self._units)

    @Slot(str, result=int)
    def decimals(self, kind: str) -> int:
        """How many decimals ``kind`` is shown with in the current system."""
        return core_units.decimals(kind, self._units)

    @Slot(str, "QVariant", result="QVariant")
    def convert(self, kind: str, value):
        """SI in, the current system's unit out, exact; ``None`` stays ``None``."""
        return core_units.convert(kind, value, self._units)

    @Slot(str, "QVariant", result="QVariant")
    def convertBack(self, kind: str, value):
        """The current system's unit in, SI out at full precision (what a
        typed value goes through before it is stored); ``None`` stays ``None``."""
        return core_units.convert_back(kind, value, self._units)

    @Slot(str, "QVariant", result=str)
    def format(self, kind: str, value) -> str:
        """``value`` (SI) in the current system, rounded per the display rule,
        without the label; ``""`` for ``None``."""
        return core_units.format_value(kind, value, self._units)

    @Slot(str, "QVariant", result=str)
    def formatWithLabel(self, kind: str, value) -> str:
        """``format`` followed by the unit label (``"33 ft"``); ``""`` for ``None``."""
        return core_units.format_with_label(kind, value, self._units)
