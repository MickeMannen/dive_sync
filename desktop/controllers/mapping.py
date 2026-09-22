"""Mapping board backend (rework.md C7): the same catalogue, defaults,
validation, preview and Test mapping the status page uses, exposed to QML as
plain lists and maps. The board state lives here; QML only draws it."""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials
from desktop.jobs import Worker
from src.core.config import ConfigManager, SettingsModel, SyncPairModel
from src.core.fields import FieldLink, build_catalog
from src.core.pairs import default_links_for, parse_service_spec, service_id_of
from src.core.templates import preview as preview_link, validate_links

STRUCTURAL = ("tanks", "samples")
TEMPLATE_TYPES = ("text", "number", "datetime", "list")


def _adapter_class(service_id: str):
    if service_id in ("subsurface", "subsurface-cloud"):
        from src.core.services.subsurface import SubsurfaceAdapter
        return SubsurfaceAdapter
    if service_id == "uddf":
        from src.core.services.uddf import UddfAdapter
        return UddfAdapter
    if service_id == "submersion":
        from src.core.services.submersion.adapter import SubmersionAdapter
        return SubmersionAdapter
    if service_id == "garmin":
        from src.core.services.garmin import GarminAdapter
        return GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter
    return DivelogsAdapter


def pairs_info(settings: SettingsModel) -> List[Dict[str, Any]]:
    specs = [("default", "garmin", "divelogs")] + [(p.id, p.source, p.target) for p in settings.sync_pairs]
    out = []
    for pair_id, source_spec, target_spec in specs:
        try:
            source = _adapter_class(parse_service_spec(source_spec)[0])
            target = _adapter_class(parse_service_spec(target_spec)[0])
        except (ValueError, KeyError):
            continue
        out.append({
            "id": pair_id,
            "source": source.service_id, "target": target.service_id,
            "source_name": source.display_name, "target_name": target.display_name,
            "default_links": [l.model_dump() for l in default_links_for(source.service_id, target.service_id)],
            "fields": {source.service_id: [f.model_dump() for f in source.field_catalog()],
                       target.service_id: [f.model_dump() for f in target.field_catalog()]},
        })
    return out


class MappingController(QObject):
    pairsChanged = Signal()
    boardChanged = Signal()
    selectedChanged = Signal()
    messageChanged = Signal()
    previewChanged = Signal()
    testResultChanged = Signal()
    busyChanged = Signal()
    askApplyToAll = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = ConfigManager.load_settings()
        self._pairs = pairs_info(self._settings)
        self._pair_id = "default"
        self._links: List[Dict[str, Any]] = []
        self._saved: List[Dict[str, Any]] = []
        self._selected: Optional[str] = None
        self._message = ""
        self._preview = ""
        self._preview_problems = ""
        self._test_result: Dict[str, Any] = {}
        self._busy = False
        self._worker = None
        self.selectPair("default")

    # -- properties -------------------------------------------------------

    @Property("QVariantList", notify=pairsChanged)
    def pairs(self):
        return [{"id": p["id"], "label": p["source_name"] + " ↔ " + p["target_name"] if p["id"] == "default"
                 else f"{p['id']} ({p['source']} → {p['target']})"} for p in self._pairs]

    @Property(str, notify=boardChanged)
    def pairId(self) -> str:
        return self._pair_id

    @Property(str, notify=boardChanged)
    def sourceName(self) -> str:
        return self._pair()["source_name"] if self._pair() else ""

    @Property(str, notify=boardChanged)
    def targetName(self) -> str:
        return self._pair()["target_name"] if self._pair() else ""

    @Property("QVariantList", notify=boardChanged)
    def sourceFields(self):
        p = self._pair()
        return self._annotate(p["fields"][p["source"]]) if p else []

    @Property("QVariantList", notify=boardChanged)
    def targetFields(self):
        p = self._pair()
        return self._annotate(p["fields"][p["target"]]) if p else []

    @Property("QVariantList", notify=boardChanged)
    def links(self):
        return [dict(l, source_labels=[self._label(k) for k in l["source"]], target_label=self._label(l["target"]))
                for l in self._links]

    @Property("QVariantList", notify=boardChanged)
    def directions(self):
        p = self._pair()
        if not p:
            return []
        return [{"value": "bidirectional", "label": "Bidirectional"},
                {"value": f"to_{p['target']}", "label": f"To {p['target']}"},
                {"value": f"to_{p['source']}", "label": f"To {p['source']}"}]

    @Property(str, notify=boardChanged)
    def pairDirection(self) -> str:
        if self._pair_id == "default":
            return self._settings.directionality
        pair = self._settings_pair()
        return pair.directionality if pair else "bidirectional"

    @Property(int, notify=boardChanged)
    def pairGrace(self) -> int:
        if self._pair_id == "default":
            return self._settings.grace_window_minutes
        pair = self._settings_pair()
        return pair.grace_window_minutes if pair and pair.grace_window_minutes is not None else self._settings.grace_window_minutes

    @Property(bool, notify=boardChanged)
    def pairPropagateDeletes(self) -> bool:
        if self._pair_id == "default":
            return self._settings.propagate_deletes
        pair = self._settings_pair()
        return pair.propagate_deletes if pair and pair.propagate_deletes is not None else self._settings.propagate_deletes

    @Property(bool, notify=boardChanged)
    def pairCreateOnGarmin(self) -> bool:
        if self._pair_id == "default":
            return self._settings.create_on_garmin
        pair = self._settings_pair()
        return pair.create_on_garmin if pair and pair.create_on_garmin is not None else self._settings.create_on_garmin

    @Property(bool, notify=boardChanged)
    def dirty(self) -> bool:
        return self._links != self._saved

    @Property(str, notify=boardChanged)
    def matchKeys(self) -> str:
        keys = sorted((l for l in self._links if l.get("match_order") is not None), key=lambda l: l["match_order"])
        return "Match keys: " + (" → ".join(l["id"] for l in keys) + " (then start time)" if keys else "none (start time only)")

    @Property(str, notify=selectedChanged)
    def selectedId(self) -> str:
        return self._selected or ""

    @Property("QVariantMap", notify=selectedChanged)
    def selectedLink(self):
        link = self._find(self._selected) if self._selected else None
        return dict(link) if link else {}

    @Property("QVariantList", notify=selectedChanged)
    def allowedDirections(self):
        link = self._find(self._selected) if self._selected else None
        if not link:
            return []
        p = self._pair()
        catalog = self._catalog()
        target, source = catalog.get(link["target"]), catalog.get(link["source"][0])
        composite = len(link["source"]) > 1 or bool(link.get("template"))
        reversible = composite and bool(link.get("reverse"))
        structural = (target and target.type in STRUCTURAL) or (source and source.type in STRUCTURAL)
        out = []
        if (not composite or reversible) and not structural and target and target.writable and source and source.writable:
            out.append({"value": "bidirectional", "label": "Both ways"})
        if target and target.writable:
            out.append({"value": "to_target", "label": "Source → target"})
        if (not composite or reversible) and source and source.writable:
            out.append({"value": "to_source", "label": "Target → source"})
        out.append({"value": "off", "label": "Off (not synced)"})
        return out

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(str, notify=previewChanged)
    def preview(self) -> str:
        return self._preview

    @Property(str, notify=previewChanged)
    def previewProblems(self) -> str:
        return self._preview_problems

    @Property("QVariantMap", notify=testResultChanged)
    def testResult(self):
        return dict(self._test_result)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    # -- helpers ----------------------------------------------------------

    def _pair(self) -> Optional[Dict[str, Any]]:
        return next((p for p in self._pairs if p["id"] == self._pair_id), None)

    def _settings_pair(self) -> Optional[SyncPairModel]:
        return next((p for p in self._settings.sync_pairs if p.id == self._pair_id), None)

    def _catalog(self):
        p = self._pair()
        if not p:
            return {}
        from src.core.fields import FieldSpec
        return build_catalog([FieldSpec(**f) for f in p["fields"][p["source"]]], [FieldSpec(**f) for f in p["fields"][p["target"]]])

    def _annotate(self, fields):
        linked = set(l["target"] for l in self._links) | set(k for l in self._links for k in l["source"])
        return [dict(f, linked=f["key"] in linked) for f in fields]

    def _label(self, key: str) -> str:
        spec = self._catalog().get(key)
        return spec.label if spec else key

    def _find(self, link_id):
        return next((l for l in self._links if l["id"] == link_id), None)

    def _saved_links_for(self, pair_id: str) -> List[Dict[str, Any]]:
        if pair_id == "default":
            return [l.model_dump() for l in self._settings.field_links]
        pair = self._settings_pair()
        info = self._pair()
        if pair and pair.field_links is not None:
            return [l.model_dump() for l in pair.field_links]
        return copy.deepcopy(info["default_links"]) if info else []

    def _set_message(self, text: str) -> None:
        self._message = text
        self.messageChanged.emit()

    # -- board actions ----------------------------------------------------

    @Slot()
    def reload(self) -> None:
        self._settings = ConfigManager.load_settings()
        self._pairs = pairs_info(self._settings)
        self.pairsChanged.emit()
        self.selectPair(self._pair_id if self._pair() else "default")

    @Slot(str)
    def selectPair(self, pair_id: str) -> None:
        self._pair_id = pair_id if any(p["id"] == pair_id for p in self._pairs) else "default"
        self._saved = copy.deepcopy(self._saved_links_for(self._pair_id))
        self._links = copy.deepcopy(self._saved)
        self._selected = None
        self._test_result = {}
        self.boardChanged.emit()
        self.selectedChanged.emit()
        self.testResultChanged.emit()

    @Slot(str, str, result=str)
    def canLink(self, from_key: str, to_key: str) -> str:
        """Empty string when the two fields may be linked, else the reason."""
        catalog = self._catalog()
        src, dst = catalog.get(from_key), catalog.get(to_key)
        if not src or not dst:
            return "Unknown field."
        if from_key.split(".")[0] == to_key.split(".")[0]:
            return "Link a field with one on the other side."
        existing = next((l for l in self._links if l["target"] == to_key), None)
        if existing:
            if src.type not in TEMPLATE_TYPES:
                return "Only text, number, date and list fields can be combined into a composite."
            if dst.type != "text":
                return "A composite can only target a text field."
            if not dst.writable:
                return "This field is locked (cannot be written by its service)."
            return ""
        compatible = src.type == dst.type or (src.type in ("list", "text") and dst.type in ("list", "text"))
        if not compatible:
            return f"Cannot link {src.type} to {dst.type}."
        if not dst.writable and not src.writable:
            return "Both fields are locked."
        return ""

    @Slot(str, str, result=bool)
    def createLink(self, from_key: str, to_key: str) -> bool:
        reason = self.canLink(from_key, to_key)
        if reason:
            self._set_message(reason)
            return False
        catalog = self._catalog()
        src, dst = catalog[from_key], catalog[to_key]
        existing = next((l for l in self._links if l["target"] == to_key), None)
        if existing:
            existing["source"].append(from_key)
            existing["direction"] = "to_target"
            base = existing.get("template") or " ".join(f"{{{k}}}" for k in existing["source"][:-1])
            existing["template"] = f"{base} {{{from_key}}}"
            existing["match_order"] = None
            self.boardChanged.emit()
            self.selectLink(existing["id"])
            return True
        direction = "bidirectional"
        if not dst.writable:
            direction = "to_source"
        elif not src.writable or src.type in STRUCTURAL:
            direction = "to_target"
        base_id = from_key.split(".", 1)[1]
        link_id, n = base_id, 2
        while self._find(link_id):
            link_id = f"{base_id}_{n}"
            n += 1
        self._links.append({"id": link_id, "source": [from_key], "target": to_key, "direction": direction,
                            "conflict": "prefer_non_empty", "template": None, "reverse": None, "match_order": None,
                            "separator": ", ", "when": None})
        self._set_message("")
        self.boardChanged.emit()
        self.selectLink(link_id)
        return True

    @Slot(str)
    def selectLink(self, link_id: str) -> None:
        self._selected = link_id if self._find(link_id) else None
        self.selectedChanged.emit()
        self.updatePreview()

    @Slot("QVariantMap", result=str)
    def updateLink(self, values) -> str:
        """Apply editor values to the selected link; returns a problem or ''."""
        link = self._find(self._selected) if self._selected else None
        if not link:
            return "No link selected."
        values = dict(values)
        new_id = str(values.get("id") or link["id"]).strip()
        if new_id != link["id"] and self._find(new_id):
            return f"A link named {new_id} already exists."
        match = values.get("match_order")
        link.update({
            "id": new_id,
            "direction": str(values.get("direction") or link["direction"]),
            "conflict": str(values.get("conflict") or link["conflict"]),
            "separator": str(values.get("separator") or ", "),
            "template": (str(values.get("template")).strip() or None) if values.get("template") is not None else link.get("template"),
            "reverse": (str(values.get("reverse")).strip() or None) if values.get("reverse") is not None else link.get("reverse"),
            "match_order": None if match in (None, "", 0) else int(match),
        })
        self._selected = new_id
        self.boardChanged.emit()
        self.selectedChanged.emit()
        self.updatePreview()
        return ""

    @Slot(str)
    def deleteLink(self, link_id: str) -> None:
        self._links = [l for l in self._links if l["id"] != link_id]
        if self._selected == link_id:
            self._selected = None
        self.boardChanged.emit()
        self.selectedChanged.emit()

    @Slot(str)
    def previewTemplate(self, template: str) -> None:
        """Live preview while typing: renders the selected link with ``template``."""
        link = self._find(self._selected) if self._selected else None
        if not link:
            return
        candidate = dict(link, template=template.strip() or None)
        self._render_preview(candidate)

    @Slot(str)
    def previewReverse(self, reverse: str) -> None:
        """Live preview while typing: checks ``reverse`` splits the rendered text back apart."""
        link = self._find(self._selected) if self._selected else None
        if not link:
            return
        candidate = dict(link, reverse=reverse.strip() or None)
        self._render_preview(candidate)

    @Slot()
    def updatePreview(self) -> None:
        link = self._find(self._selected) if self._selected else None
        if link:
            self._render_preview(link)

    def _render_preview(self, link: Dict[str, Any]) -> None:
        if not link.get("template") and len(link["source"]) == 1:
            self._preview, self._preview_problems = "(plain copy)", ""
        else:
            try:
                out = preview_link(FieldLink(**link), self._catalog())
                self._preview = (out["text"] or "(empty)") if out["ok"] else "–"
                self._preview_problems = "\n".join(out["problems"] + out["warnings"])
                if out.get("ok") and "reverse_sample" in out:
                    sample = out["reverse_sample"]
                    self._preview += "  |  reverse -> " + (str(sample) if sample else "(pattern does not match its own template output)")
            except Exception as e:
                self._preview, self._preview_problems = "–", str(e)
        self.previewChanged.emit()

    @Slot(result=str)
    def save(self) -> str:
        p = self._pair()
        if not p:
            return "No pair selected."
        try:
            links = [FieldLink(**l) for l in self._links]
        except Exception as e:
            self._set_message(f"Invalid link: {e}")
            return self._message
        problems = validate_links(links, self._catalog())
        if problems:
            self._set_message("\n".join(problems))
            return self._message
        changed = self.dirty
        settings = ConfigManager.load_settings()
        if self._pair_id == "default":
            settings.field_links = links
        else:
            for pair in settings.sync_pairs:
                if pair.id == self._pair_id:
                    pair.field_links = links
        ConfigManager.save_settings(settings)
        self._settings = settings
        self._saved = copy.deepcopy(self._links)
        self.boardChanged.emit()
        self._set_message("Saved.")
        if changed:
            self.askApplyToAll.emit()
        return ""

    @Slot(str, int, bool, bool)
    def savePairOptions(self, direction: str, grace: int, propagate_deletes: bool, create_on_garmin: bool) -> None:
        settings = ConfigManager.load_settings()
        if self._pair_id == "default":
            settings.directionality = direction
            settings.grace_window_minutes = grace
            settings.propagate_deletes = propagate_deletes
            settings.create_on_garmin = create_on_garmin
        else:
            for pair in settings.sync_pairs:
                if pair.id == self._pair_id:
                    pair.directionality = direction
                    pair.grace_window_minutes = grace
                    pair.propagate_deletes = propagate_deletes
                    pair.create_on_garmin = create_on_garmin
        ConfigManager.save_settings(settings)
        self._settings = settings
        self.boardChanged.emit()

    @Slot()
    def applyToAll(self) -> None:
        from src.core.pairs import engine_for_pair, find_pair
        from src.core.sync_engine import SyncEngine
        try:
            if self._pair_id == "default":
                engine = SyncEngine()
            else:
                engine = engine_for_pair(find_pair(self._settings, self._pair_id))
            engine.request_full_compare(True)
            self._set_message("Saved. The next run will compare every matched dive.")
        except Exception as e:
            self._set_message(f"Saved, but the full-compare flag could not be set: {e}")

    @Slot()
    def cancel(self) -> None:
        self._links = copy.deepcopy(self._saved)
        self._selected = None
        self.boardChanged.emit()
        self.selectedChanged.emit()
        self._set_message("Changes discarded.")

    @Slot()
    def resetToDefaults(self) -> None:
        p = self._pair()
        if p:
            self._links = copy.deepcopy(p["default_links"])
            self._selected = None
            self.boardChanged.emit()
            self.selectedChanged.emit()

    @Slot()
    def testMapping(self) -> None:
        if self._busy:
            return
        try:
            links = [FieldLink(**l) for l in self._links]
        except Exception as e:
            self._set_message(f"Invalid link: {e}")
            return
        self._busy = True
        self.busyChanged.emit()
        self._set_message("Fetching the newest dives from both services… this takes a while.")
        pair_id = self._pair_id

        def work():
            from src.core.pairs import engine_for_pair, find_pair
            from src.core.sync_engine import SyncEngine
            credentials.begin_operation()
            try:
                engine = SyncEngine() if pair_id == "default" else engine_for_pair(find_pair(ConfigManager.load_settings(), pair_id))
                return engine.test_mapping(links, limit=10)
            finally:
                credentials.end_operation()

        def done(result):
            self._busy = False
            self.busyChanged.emit()
            self._test_result = result or {}
            self.testResultChanged.emit()
            self._set_message("" if result.get("ok") else "The board is not valid: " + "; ".join(result.get("problems", [])))

        def fail(message):
            self._busy = False
            self.busyChanged.emit()
            self._set_message(f"Test mapping failed: {message}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()
