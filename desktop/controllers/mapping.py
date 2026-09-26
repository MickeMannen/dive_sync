"""Mapping board backend (rework.md C7, rebuilt around receiver rules in G6):
the same catalogue, defaults, validation, preview and Test mapping the status
page uses, exposed to QML as plain lists and maps. The board state - one rule
list per receiving service plus the pair's ordered match keys - lives here;
QML only draws it."""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import accounts, credentials
from desktop.jobs import Worker
from src.core.config import DEFAULT_PAIR_ID, ConfigManager, SettingsModel, SyncPairModel
from src.core.fields import FieldLink, SyncRule, build_catalog, links_to_rules, rules_to_links
from src.core.pairs import board_pairs, default_links_for, parse_service_spec, service_id_of
from src.core.templates import AUTO_REVERSE, preview as preview_link, validate_links

STRUCTURAL = ("tanks", "samples")
TEMPLATE_TYPES = ("text", "number", "datetime", "list")
MATCH_KEY_TYPES = ("number", "datetime")
POLICIES = ("manual", "prefer_non_empty", "prefer_source", "source_wins", "target_wins")


def _split_active(rule: Dict[str, Any]) -> bool:
    """A templated rule whose reverse pattern actually runs (see SyncEngine.active_rules)."""
    return bool(rule.get("template") and rule.get("reverse")
                and (rule.get("reverse_conflict") or rule["conflict"]) != "target_wins")


def _split_policy(rule: Dict[str, Any]) -> str:
    return rule.get("reverse_conflict") or rule["conflict"]


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


def pairs_info(settings: SettingsModel, configured: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The boards the page offers (pairs.board_pairs): the saved pairs - the
    Garmin <-> Divelogs one is sync_pairs[0], rework.md G1 - then each
    combination of the ``configured`` services not saved yet."""
    specs = [(p["id"], p["source"], p["target"], p["saved"]) for p in board_pairs(settings, configured or [])]
    out = []
    for pair_id, source_spec, target_spec, saved in specs:
        try:
            source = _adapter_class(parse_service_spec(source_spec)[0])
            target = _adapter_class(parse_service_spec(target_spec)[0])
        except (ValueError, KeyError):
            continue
        default_links = default_links_for(source.service_id, target.service_id)
        default_rules, default_keys = links_to_rules(default_links, source.service_id, target.service_id)
        out.append({
            "id": pair_id,
            "source": source.service_id, "target": target.service_id,
            "source_spec": source_spec, "target_spec": target_spec, "saved": saved,
            "source_name": source.display_name, "target_name": target.display_name,
            "default_rules": {receiver: [r.model_dump() for r in items] for receiver, items in default_rules.items()},
            "default_match_keys": default_keys,
            "fields": {source.service_id: [f.model_dump() for f in source.field_catalog()],
                       target.service_id: [f.model_dump() for f in target.field_catalog()]},
        })
    return out


def _new_rule(rule_id: str, target: str, source: List[str]) -> Dict[str, Any]:
    return {"id": rule_id, "target": target, "source": list(source), "conflict": "prefer_non_empty",
            "template": None, "reverse": None, "reverse_conflict": None, "separator": ", ", "when": None}


class MappingController(QObject):
    pairsChanged = Signal()
    boardChanged = Signal()
    selectedChanged = Signal()
    messageChanged = Signal()
    previewChanged = Signal()
    testResultChanged = Signal()
    busyChanged = Signal()
    askApplyToAll = Signal()
    askSplit = Signal(str, str, str, str)   # question, sender key, receiver, receiver key

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = ConfigManager.load_settings()
        self._pairs = pairs_info(self._settings, self._configured())
        self._pair_id = DEFAULT_PAIR_ID
        self._rules: Dict[str, List[Dict[str, Any]]] = {}
        self._match_keys: List[List[str]] = []
        self._saved: Dict[str, Any] = {"rules": {}, "match_keys": []}
        self._selected: Optional[Dict[str, str]] = None      # {"receiver": ..., "id": ...}
        # Click-to-connect (2026-09-23): a sender field can be "armed" with a
        # click and the receiver field clicked second, instead of dragged onto.
        # With 36 Submersion fields a drag often spans more than the viewport,
        # and neither board auto-scrolls while a drag is in flight.
        self._armed: Optional[Dict[str, str]] = None         # {"receiver": ..., "key": ...}
        self._view_direction: str = ""                         # the direction picked in the header, unsaved
        self._message = ""
        self._preview = ""
        self._preview_problems = ""
        self._test_result: Dict[str, Any] = {}
        self._busy = False
        self._worker = None
        self.selectPair(DEFAULT_PAIR_ID)

    # -- properties -------------------------------------------------------

    @Property("QVariantList", notify=pairsChanged)
    def pairs(self):
        return [{"id": p["id"], "label": p["source_name"] + " ↔ " + p["target_name"] if p["id"] == DEFAULT_PAIR_ID
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
    def receivers(self):
        """One panel per receiving side, the run's receiver first: the
        receiver's fields (each with its rule, if any) and the sender's
        readable fields to drag from."""
        p = self._pair()
        if not p:
            return []
        active = self._active_receiver()
        names = {p["source"]: p["source_name"], p["target"]: p["target_name"]}
        order = [p["target"], p["source"]]
        if active == p["source"]:
            order.reverse()
        panels = []
        for receiver in order:
            sender = p["source"] if receiver == p["target"] else p["target"]
            rules = self._rules_of(receiver)
            splits = self._splits_into(receiver)
            used = {k for r in rules for k in r["source"]} | {r["target"] for r in splits}
            fields = []
            for f in p["fields"][receiver]:
                rule = next((r for r in rules if r["target"] == f["key"]), None)
                split = next((r for r in splits if f["key"] in r["source"]), None)
                fields.append(dict(f, linked=rule is not None or split is not None, rule_id=rule["id"] if rule else "",
                                   rule_summary=self._summary(rule) if rule else "",
                                   rule_noop=bool(rule and rule["conflict"] == "target_wins"),
                                   split_id=split["id"] if split else "",
                                   split_summary=(f"⇠ split of {self._label(split['target'])} [{_split_policy(split)}]"
                                                  if split else ""),
                                   selected=bool((rule and self._is_selected(receiver, rule["id"]))
                                                 or (split and self._is_selected(sender, split["id"])))))
            sender_fields = [dict(f, linked=f["key"] in used,
                                  armed=bool(self._armed and self._armed["receiver"] == receiver
                                             and self._armed["key"] == f["key"]))
                             for f in p["fields"][sender] if f.get("readable", True)]
            panels.append({"receiver": receiver, "receiver_name": names[receiver], "sender": sender,
                           "sender_name": names[sender], "active": receiver == active,
                           "scheduled": self.savedDirection == f"to_{receiver}",
                           "fields": fields, "sender_fields": sender_fields,
                           "rules": [dict(r) for r in rules],
                           "splits": [dict(r, selected=self._is_selected(sender, r["id"])) for r in splits]})
        return panels

    # -- source / target view ----------------------------------------------
    # The header picks a Source and a Target; the board then shows the one
    # panel that matters for that direction: what the target takes from the
    # source. The pair behind the two is found either way round.

    @Property("QVariantList", notify=pairsChanged)
    def endpoints(self):
        """Every service that is one end of a pair with a board, as {id, label}."""
        out, seen = [], set()
        for p in self._pairs:
            for sid, name in ((p["source"], p["source_name"]), (p["target"], p["target_name"])):
                if sid not in seen:
                    seen.add(sid)
                    out.append({"id": sid, "label": name})
        return out

    @Slot(str, result="QVariantList")
    def targetsFor(self, source: str):
        """The services a board joins ``source`` to."""
        linked = {p["target"] if p["source"] == source else p["source"]
                  for p in self._pairs if source in (p["source"], p["target"])}
        return [e for e in self.endpoints if e["id"] in linked]

    @Property(str, notify=boardChanged)
    def viewSource(self) -> str:
        receiver = self._active_receiver()
        return self._other(receiver) if receiver else ""

    @Property(str, notify=boardChanged)
    def viewTarget(self) -> str:
        return self._active_receiver() or ""

    @Slot(str, str, result=bool)
    def selectView(self, source: str, target: str) -> bool:
        """Show the board of the pair joining ``source`` and ``target``, at
        the panel for writing ``target``. False when no board joins them."""
        pair = next((p for p in self._pairs if {p["source"], p["target"]} == {source, target}), None)
        if not pair or source == target:
            return False
        if pair["id"] != self._pair_id:
            self.selectPair(pair["id"])
        self._view_direction = f"to_{target}"
        self._armed = None
        self.boardChanged.emit()
        return True

    @Property("QVariantList", notify=boardChanged)
    def panels(self):
        """The panel of the direction on view (see receivers for both)."""
        return [p for p in self.receivers if p["active"]][:1]

    @Property("QVariantList", notify=boardChanged)
    def directions(self):
        """The pair's saved direction choices (what scheduled runs write)."""
        p = self._pair()
        if not p:
            return []
        # rework.md G0: one receiver per run - no "both ways" entry.
        return [{"value": f"to_{p['target']}", "label": p["target_name"]},
                {"value": f"to_{p['source']}", "label": p["source_name"]}]

    @Property(str, notify=boardChanged)
    def pairDirection(self) -> str:
        if self._pair_id == DEFAULT_PAIR_ID:
            return self._settings.directionality
        pair = self._settings_pair()
        return pair.directionality if pair else "to_target"

    @Property(str, notify=boardChanged)
    def savedDirection(self) -> str:
        """pairDirection spelled to_<service id>, as ``directions`` offers it."""
        p = self._pair()
        direction = self.pairDirection
        if p and direction == "to_target":
            return f"to_{p['target']}"
        if p and direction == "to_source":
            return f"to_{p['source']}"
        return direction

    @Property(int, notify=boardChanged)
    def pairGrace(self) -> int:
        if self._pair_id == DEFAULT_PAIR_ID:
            return self._settings.grace_window_minutes
        pair = self._settings_pair()
        return pair.grace_window_minutes if pair and pair.grace_window_minutes is not None else self._settings.grace_window_minutes

    @Property(bool, notify=boardChanged)
    def pairPropagateDeletes(self) -> bool:
        if self._pair_id == DEFAULT_PAIR_ID:
            return self._settings.propagate_deletes
        pair = self._settings_pair()
        return pair.propagate_deletes if pair and pair.propagate_deletes is not None else self._settings.propagate_deletes

    @Property(bool, notify=boardChanged)
    def pairCreateOnGarmin(self) -> bool:
        if self._pair_id == DEFAULT_PAIR_ID:
            return self._settings.create_on_garmin
        pair = self._settings_pair()
        return pair.create_on_garmin if pair and pair.create_on_garmin is not None else self._settings.create_on_garmin

    @Property(bool, notify=boardChanged)
    def dirty(self) -> bool:
        return {"rules": self._rules, "match_keys": self._match_keys} != self._saved

    @Property("QVariantList", notify=boardChanged)
    def matchKeys(self):
        return [list(k) for k in self._match_keys]

    @Property("QVariantList", notify=boardChanged)
    def matchKeyRows(self):
        """The match keys as the page shows them: field labels with their services."""
        p = self._pair()
        names = {p["source"]: p["source_name"], p["target"]: p["target_name"]} if p else {}
        return [f"{names.get(a.split('.')[0], a.split('.')[0])} {self._label(a)} = "
                f"{names.get(b.split('.')[0], b.split('.')[0])} {self._label(b)}" for a, b in self._match_keys]

    @Property(bool, notify=boardChanged)
    def pairHasGarmin(self) -> bool:
        p = self._pair()
        return bool(p and "garmin" in (p["source"], p["target"]))

    @Property(str, notify=boardChanged)
    def pairLabel(self) -> str:
        p = self._pair()
        return f"{p['source_name']} ↔ {p['target_name']}" if p else ""

    @Property(str, notify=boardChanged)
    def matchKeyLabel(self) -> str:
        if not self._match_keys:
            return "Match keys: none (start time only)"
        return "Match keys (before start time): " + ", ".join(f"{a} = {b}" for a, b in self._match_keys)

    @Property("QVariantList", notify=boardChanged)
    def matchKeyFieldsSource(self):
        return self._match_key_fields(self._pair()["source"]) if self._pair() else []

    @Property("QVariantList", notify=boardChanged)
    def matchKeyFieldsTarget(self):
        return self._match_key_fields(self._pair()["target"]) if self._pair() else []

    @Property(str, notify=selectedChanged)
    def selectedId(self) -> str:
        return self._selected["id"] if self._selected else ""

    @Property(str, notify=selectedChanged)
    def selectedReceiver(self) -> str:
        return self._selected["receiver"] if self._selected else ""

    @Property("QVariantMap", notify=selectedChanged)
    def selectedRule(self):
        rule = self._selected_rule()
        if not rule:
            return {}
        out = dict(rule)
        out["receiver"] = self._selected["receiver"]
        out["target_label"] = self._label(rule["target"])
        out["source_labels"] = [self._label(k) for k in rule["source"]]
        out["composite"] = len(rule["source"]) > 1 or bool(rule.get("template"))
        catalog = self._catalog()
        # the separator only means something where a list meets text
        out["list_rule"] = any(catalog.get(k) is not None and catalog[k].type == "list" for k in rule["source"] + [rule["target"]])
        spec = catalog.get(rule["target"])
        out["text_target"] = bool(spec and spec.type == "text")
        receiver = self._selected["receiver"]
        view = self._selected.get("view") or receiver
        sources = " + ".join(out["source_labels"])
        out["title"] = (f"{out['target_label']} → {sources}  (split, on runs to {self._service_name(view)})"
                        if view != receiver else f"{sources} → {out['target_label']}  (written to {self._service_name(receiver)})")
        out["split_active"] = _split_active(rule)
        out["split_label"] = (f"Split {out['target_label']} back into {sources} "
                              f"on runs to {self._service_name(self._other(receiver))}")
        reverse = rule.get("reverse") or ""
        out["reverse_custom"] = "" if reverse.strip().lower() == AUTO_REVERSE else reverse
        out["reverse_conflict"] = "" if rule.get("reverse_conflict") == "target_wins" else (rule.get("reverse_conflict") or "")
        return out

    @Property("QVariantList", constant=True)
    def policies(self):
        return [{"value": "manual", "label": "Fill blanks, ask about real differences (manual)"},
                {"value": "prefer_non_empty", "label": "Only fill me when blank (prefer_non_empty)"},
                {"value": "prefer_source", "label": "Take the sender's value whenever it has one (prefer_source)"},
                {"value": "source_wins", "label": "Always take the sender's value, even blank (source_wins)"},
                {"value": "target_wins", "label": "Never overwrite me (target_wins)"}]

    @Property("QVariantList", constant=True)
    def splitPolicies(self):
        return [{"value": "", "label": "Same as above"},
                {"value": "manual", "label": "Ask me when they differ (manual)"},
                {"value": "prefer_non_empty", "label": "Only fill blank fields (prefer_non_empty)"},
                {"value": "prefer_source", "label": "Take the split values whenever there are some (prefer_source)"},
                {"value": "source_wins", "label": "Always take the split values, even blank (source_wins)"}]

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

    @staticmethod
    def _configured() -> List[str]:
        try:
            return credentials.load_credentials_model().configured_services()
        except Exception:
            return []

    def _stored_pair(self, settings: SettingsModel) -> SyncPairModel:
        """The pair on view in ``settings``, added first when it is a
        combination of configured services that was never saved."""
        pair = next((sp for sp in settings.sync_pairs if sp.id == self._pair_id), None)
        if pair is None:
            p = self._pair()
            pair = SyncPairModel(id=p["id"], source=p["source_spec"], target=p["target_spec"],
                                 directionality=f"to_{p['target']}")
            settings.sync_pairs.append(pair)
            for entry in self._pairs:
                if entry["id"] == p["id"]:
                    entry["saved"] = True
        return pair

    def _settings_pair(self) -> Optional[SyncPairModel]:
        return next((p for p in self._settings.sync_pairs if p.id == self._pair_id), None)

    def _catalog(self):
        p = self._pair()
        if not p:
            return {}
        from src.core.fields import FieldSpec
        return build_catalog([FieldSpec(**f) for f in p["fields"][p["source"]]], [FieldSpec(**f) for f in p["fields"][p["target"]]])

    def _label(self, key: str) -> str:
        spec = self._catalog().get(key)
        return spec.label if spec else key

    def _rules_of(self, receiver: str) -> List[Dict[str, Any]]:
        return self._rules.setdefault(receiver, [])

    def _find(self, receiver: str, rule_id: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._rules_of(receiver) if r["id"] == rule_id), None)

    def _selected_rule(self) -> Optional[Dict[str, Any]]:
        return self._find(self._selected["receiver"], self._selected["id"]) if self._selected else None

    def _is_selected(self, receiver: str, rule_id: str) -> bool:
        return bool(self._selected and self._selected["receiver"] == receiver and self._selected["id"] == rule_id)

    def _other(self, receiver: str) -> str:
        p = self._pair()
        return p["source"] if receiver == p["target"] else p["target"]

    def _service_name(self, service_id: str) -> str:
        p = self._pair()
        if not p:
            return service_id
        return {p["source"]: p["source_name"], p["target"]: p["target_name"]}.get(service_id, service_id)

    def _splits_into(self, receiver: str) -> List[Dict[str, Any]]:
        """The other receiver's templated rules with a live reverse pattern:
        on runs towards ``receiver`` they take the sender's field (the rule's
        target) apart into this receiver's fields (its sources), so the board
        shows them in this receiver's panel."""
        return [r for r in self._rules_of(self._other(receiver)) if _split_active(r)]

    def _split_proposal(self, from_key: str, receiver: str, to_key: str):
        """``(question, fields, replaced rules)`` when dropping ``from_key`` on
        ``to_key`` should offer a split: the sender text field already feeds
        another text field here (by a plain rule or a split). Else ``None``."""
        catalog = self._catalog()
        is_text = lambda k: bool(catalog.get(k) and catalog[k].type == "text")  # noqa: E731
        if not is_text(from_key) or not is_text(to_key) or not catalog[to_key].writable:
            return None
        other = self._other(receiver)
        composite = next((r for r in self._rules_of(other) if r["target"] == from_key), None)
        replaced: List[Dict[str, Any]] = []
        if composite and _split_active(composite):
            if to_key in composite["source"]:
                return None
            fields = composite["source"] + [to_key]
        else:
            plain = next((r for r in self._rules_of(receiver) if r["target"] != to_key and not r.get("template")
                          and r["source"] == [from_key] and is_text(r["target"])), None)
            if not plain:
                return None
            fields = [plain["target"], to_key]
            replaced.append(plain)
        own = next((r for r in self._rules_of(receiver) if r["target"] == to_key), None)
        if own:
            replaced.append(own)
        question = (f"Split {self._label(from_key)} into {' + '.join(self._label(k) for k in fields)}?\n\n"
                    f"On runs to {self._service_name(receiver)} the value is cut at the text between the fields "
                    f"(\", \" by default; change it in the template).")
        dropped = [r for r in replaced if not (r["target"] == fields[0] and r["source"] == [from_key])]
        if dropped:
            question += "\n\nReplaces the rule on " + ", ".join(
                f"{self._label(r['target'])} (← {' + '.join(self._label(k) for k in r['source'])})" for r in dropped) + "."
        question += "\n\nNo = add it as a normal rule instead."
        return question, fields, replaced

    def _unique_id(self, receiver: str, base: str) -> str:
        rule_id, n = base, 2
        while self._find(receiver, rule_id):
            rule_id = f"{base}_{n}"
            n += 1
        return rule_id

    def _active_receiver(self) -> Optional[str]:
        p = self._pair()
        if not p:
            return None
        direction = self._view_direction or self.pairDirection
        if direction in ("to_target", f"to_{p['target']}"):
            return p["target"]
        if direction in ("to_source", f"to_{p['source']}"):
            return p["source"]
        return None

    def _summary(self, rule: Dict[str, Any]) -> str:
        parts = " + ".join(self._label(k) for k in rule["source"])
        extras = [x for x in ("template" if rule.get("template") else "", "splits back" if rule.get("reverse") else "") if x]
        policy = "never overwrite" if rule["conflict"] == "target_wins" else rule["conflict"]
        return f"← {parts}" + (f" ({', '.join(extras)})" if extras else "") + f" [{policy}]"

    def _match_key_fields(self, service_id: str) -> List[Dict[str, Any]]:
        p = self._pair()
        return [{"value": f["key"], "label": f["label"]} for f in p["fields"][service_id] if f["type"] in MATCH_KEY_TYPES]

    def _saved_board_for(self, pair_id: str) -> Dict[str, Any]:
        pair = self._settings_pair()
        info = self._pair()
        if pair and pair.rules is not None:
            return {"rules": {receiver: [r.model_dump() for r in items] for receiver, items in pair.rules.items()},
                    "match_keys": [list(k) for k in pair.match_keys]}
        if info:
            return {"rules": copy.deepcopy(info["default_rules"]), "match_keys": copy.deepcopy(info["default_match_keys"])}
        return {"rules": {}, "match_keys": []}

    def _as_models(self) -> Dict[str, List[SyncRule]]:
        return {receiver: [SyncRule(**r) for r in items] for receiver, items in self._rules.items()}

    def _problems(self) -> List[str]:
        """Save-time validation runs on the link view (rule ids are link ids)."""
        p = self._pair()
        links = rules_to_links(self._as_models(), self._match_keys, p["source"], p["target"])
        return validate_links(links, self._catalog())

    def _set_message(self, text: str) -> None:
        self._message = text
        self.messageChanged.emit()

    def _emit_board(self) -> None:
        self.boardChanged.emit()
        self.selectedChanged.emit()

    # -- board actions ----------------------------------------------------

    @Slot()
    def reload(self) -> None:
        self._settings = ConfigManager.load_settings()
        self._pairs = pairs_info(self._settings, self._configured())
        self.pairsChanged.emit()
        self.selectPair(self._pair_id if self._pair() else DEFAULT_PAIR_ID)

    @Slot(str)
    def selectPair(self, pair_id: str) -> None:
        self._pair_id = pair_id if any(p["id"] == pair_id for p in self._pairs) else DEFAULT_PAIR_ID
        self._saved = copy.deepcopy(self._saved_board_for(self._pair_id))
        self._rules = copy.deepcopy(self._saved["rules"])
        self._match_keys = copy.deepcopy(self._saved["match_keys"])
        self._selected = None
        self._armed = None
        self._view_direction = ""
        self._test_result = {}
        self._emit_board()
        self.testResultChanged.emit()

    @Slot(str)
    def notify(self, text: str) -> None:
        """A message from the QML side (e.g. a drop that landed nowhere)."""
        self._set_message(text)

    @Slot(str)
    def setViewDirection(self, direction: str) -> None:
        """The header's direction picker: reorders the panels and the
        'this run writes here' badge before anything is saved."""
        self._view_direction = direction or ""
        self.boardChanged.emit()

    @Slot(str, str, str, result=str)
    def canDrop(self, from_key: str, receiver: str, to_key: str) -> str:
        """Empty string when ``from_key`` (a sender field) may feed
        ``to_key`` on ``receiver``, else the reason."""
        catalog = self._catalog()
        src, dst = catalog.get(from_key), catalog.get(to_key)
        if not src or not dst:
            return "Unknown field."
        if from_key.split(".")[0] != self._other(receiver) or to_key.split(".")[0] != receiver:
            return "Drag a field from the left-hand list onto a field on the right."
        if not dst.writable:
            return "This field is locked (cannot be written by its service)."
        if not src.readable:
            return "This field cannot be read."
        existing = next((r for r in self._rules_of(receiver) if r["target"] == to_key), None)
        if existing:
            if from_key in existing["source"]:
                return "Already part of this rule."
            if src.type not in TEMPLATE_TYPES:
                return "Only text, number, date and list fields can be combined into a composite."
            if dst.type != "text":
                return "A composite can only target a text field."
            return ""
        compatible = src.type == dst.type or (src.type in ("list", "text") and dst.type in ("list", "text"))
        if not compatible:
            return f"Cannot take a {src.type} value into a {dst.type} field."
        return ""

    @Slot(str, str, str, result=bool)
    def createRule(self, from_key: str, receiver: str, to_key: str) -> bool:
        """A drop or click-connect. When it could be a split the view is asked
        first (``askSplit``) and answers with ``makeSplit`` or ``createPlainRule``."""
        proposal = self._split_proposal(from_key, receiver, to_key)
        if proposal:
            self.askSplit.emit(proposal[0], from_key, receiver, to_key)
            return True
        return self.createPlainRule(from_key, receiver, to_key)

    @Slot(str, str, str, result=bool)
    def makeSplit(self, from_key: str, receiver: str, to_key: str) -> bool:
        """Store the split as a templated rule on the other receiver with
        reverse "auto", merged into an existing rule on ``from_key`` there."""
        proposal = self._split_proposal(from_key, receiver, to_key)
        if not proposal:
            return False
        _question, fields, replaced = proposal
        other = self._other(receiver)
        self._rules[receiver] = [r for r in self._rules_of(receiver) if not any(r is x for x in replaced)]
        rule = next((r for r in self._rules_of(other) if r["target"] == from_key), None)
        if rule and all(k.split(".")[0] == receiver for k in rule["source"]):
            if not rule.get("template"):
                rule["template"] = ", ".join(f"{{{k}}}" for k in rule["source"])
            for k in fields:
                if k not in rule["source"]:
                    rule["template"] += f", {{{k}}}"
                    rule["source"].append(k)
            rule["reverse"] = rule.get("reverse") or AUTO_REVERSE
            if not _split_active(rule) or not rule.get("reverse_conflict"):
                rule["reverse_conflict"] = "prefer_source"
        else:
            if rule:
                self._rules[other] = [r for r in self._rules_of(other) if r is not rule]
            rule = dict(_new_rule(self._unique_id(other, f"{from_key.split('.', 1)[1]}_split"), from_key, list(fields)),
                        conflict="target_wins", template=", ".join(f"{{{k}}}" for k in fields),
                        reverse=AUTO_REVERSE, reverse_conflict="prefer_source")
            self._rules_of(other).append(rule)
        self._set_message(f"{self._label(from_key)} is now split into "
                          f"{' + '.join(self._label(k) for k in rule['source'])} (not saved yet).")
        self._emit_board()
        self.selectRuleFrom(other, rule["id"], receiver)
        return True

    @Slot(str, str, str, result=bool)
    def createPlainRule(self, from_key: str, receiver: str, to_key: str) -> bool:
        reason = self.canDrop(from_key, receiver, to_key)
        if reason:
            self._set_message(reason)
            return False
        rules = self._rules_of(receiver)
        existing = next((r for r in rules if r["target"] == to_key), None)
        if existing:
            base = existing.get("template") or " ".join(f"{{{k}}}" for k in existing["source"])
            existing["template"] = f"{base} {{{from_key}}}"
            existing["source"].append(from_key)
            self._emit_board()
            self.selectRule(receiver, existing["id"])
            return True
        base_id = to_key.split(".", 1)[1]
        rule_id, n = base_id, 2
        while self._find(receiver, rule_id):
            rule_id = f"{base_id}_{n}"
            n += 1
        rules.append(_new_rule(rule_id, to_key, [from_key]))
        self._set_message("")
        self._emit_board()
        self.selectRule(receiver, rule_id)
        return True

    @Slot(str, str)
    def selectRule(self, receiver: str, rule_id: str) -> None:
        self.selectRuleFrom(receiver, rule_id, receiver)

    @Slot(str, str, str)
    def selectRuleFrom(self, receiver: str, rule_id: str, view: str) -> None:
        """``view``: the panel it was picked in - a split opened where it
        writes reads "Activity name → Location + Dive site"."""
        self._selected = {"receiver": receiver, "id": rule_id, "view": view} if self._find(receiver, rule_id) else None
        self._emit_board()
        self.updatePreview()

    @Property(str, notify=boardChanged)
    def armedKey(self) -> str:
        return self._armed["key"] if self._armed else ""

    @Property(str, notify=boardChanged)
    def armedLabel(self) -> str:
        return self._label(self._armed["key"]) if self._armed else ""

    @Slot(str, str)
    def armSource(self, receiver: str, key: str) -> None:
        """First half of a click-to-connect: remember the sender field.
        Clicking the same one again cancels."""
        if self._armed and self._armed == {"receiver": receiver, "key": key}:
            self._armed = None
            self._set_message("")
        else:
            self._armed = {"receiver": receiver, "key": key}
            self._set_message(f"{self._label(key)} armed - now click a {receiver} field to take it (Esc cancels).")
        self.boardChanged.emit()

    @Slot(result=bool)
    def clearArmed(self) -> bool:
        """True when something was actually disarmed, so a view can tell
        whether its Escape key had anything to do."""
        if not self._armed:
            return False
        self._armed = None
        self._set_message("")
        self.boardChanged.emit()
        return True

    @Slot(str, str, result=bool)
    def connectArmed(self, receiver: str, target_key: str) -> bool:
        """Second half: create the rule the armed field implies."""
        if not self._armed or self._armed["receiver"] != receiver:
            return False
        source_key = self._armed["key"]
        self._armed = None
        return self.createRule(source_key, receiver, target_key)

    @Slot()
    def clearSelection(self) -> None:
        self._selected = None
        self._emit_board()

    @Slot("QVariantMap", result=str)
    def updateRule(self, values) -> str:
        """Apply editor values to the selected rule; returns a problem or ''."""
        rule = self._selected_rule()
        if not rule:
            return "No rule selected."
        values = dict(values)
        receiver = self._selected["receiver"]
        new_id = str(values.get("id") or rule["id"]).strip()
        if new_id != rule["id"] and self._find(receiver, new_id):
            return f"A rule named {new_id} already exists on this side."
        template = (str(values.get("template")).strip() or None) if values.get("template") is not None else rule.get("template")
        if "split" in values:
            # the split checkbox: on = the custom pattern, or one derived from the template
            reverse = (str(values.get("reverse") or "").strip() or AUTO_REVERSE) if values.get("split") else None
        else:
            reverse = (str(values.get("reverse")).strip() or None) if values.get("reverse") is not None else rule.get("reverse")
        if reverse and not template:
            return "A split needs a template."
        split_policy = values.get("reverse_conflict")
        rule.update({
            "id": new_id,
            "conflict": str(values.get("conflict") or rule["conflict"]),
            "separator": str(values.get("separator") or ", "),
            "template": template,
            "reverse": reverse,
            "reverse_conflict": (str(split_policy) or None) if split_policy is not None else rule.get("reverse_conflict"),
        })
        if not rule["reverse"]:
            rule["reverse_conflict"] = None
        elif not rule["reverse_conflict"] and rule["conflict"] == "target_wins":
            rule["reverse_conflict"] = "prefer_source"     # "same as above" would switch the split off with it
        self._selected = dict(self._selected, id=new_id)
        self._emit_board()
        self.updatePreview()
        return ""

    @Slot(str, str)
    def deleteRule(self, receiver: str, rule_id: str) -> None:
        self._rules[receiver] = [r for r in self._rules_of(receiver) if r["id"] != rule_id]
        if self._is_selected(receiver, rule_id):
            self._selected = None
        self._emit_board()

    # -- match keys -------------------------------------------------------

    @Slot(str, str, result=str)
    def addMatchKey(self, a: str, b: str) -> str:
        catalog = self._catalog()
        sa, sb = catalog.get(a), catalog.get(b)
        if not sa or not sb:
            return "Unknown field."
        if sa.type not in MATCH_KEY_TYPES or sb.type not in MATCH_KEY_TYPES or sa.type != sb.type:
            return "A match key pairs two number or two date fields."
        if [a, b] in self._match_keys:
            return "Already a match key."
        self._match_keys.append([a, b])
        self.boardChanged.emit()
        return ""

    @Slot(int)
    def removeMatchKey(self, index: int) -> None:
        if 0 <= index < len(self._match_keys):
            del self._match_keys[index]
            self.boardChanged.emit()

    @Slot(int)
    def moveMatchKeyUp(self, index: int) -> None:
        if 0 < index < len(self._match_keys):
            self._match_keys[index - 1], self._match_keys[index] = self._match_keys[index], self._match_keys[index - 1]
            self.boardChanged.emit()

    # -- preview ----------------------------------------------------------

    @Slot(str)
    def previewTemplate(self, template: str) -> None:
        """Live preview while typing: renders the selected rule with ``template``."""
        rule = self._selected_rule()
        if rule:
            self._render_preview(dict(rule, template=template.strip() or None))

    @Slot(str)
    def previewReverse(self, reverse: str) -> None:
        """Live preview while typing: checks ``reverse`` splits the rendered text back apart."""
        rule = self._selected_rule()
        if rule:
            self._render_preview(dict(rule, reverse=reverse.strip() or None))

    @Slot()
    def updatePreview(self) -> None:
        rule = self._selected_rule()
        if rule:
            self._render_preview(rule)

    def _render_preview(self, rule: Dict[str, Any]) -> None:
        if not rule.get("template") and len(rule["source"]) == 1:
            self._preview, self._preview_problems = "(plain copy)", ""
        else:
            try:
                # the preview validates a link; a rule is one aimed at its target
                link = FieldLink(id=rule["id"], source=rule["source"], target=rule["target"], template=rule.get("template"),
                                 reverse=rule.get("reverse"), direction="bidirectional" if rule.get("reverse") else "to_target",
                                 conflict="manual", separator=rule.get("separator") or ", ")
                out = preview_link(link, self._catalog())
                self._preview = (out["text"] or "(empty)") if out["ok"] else "–"
                self._preview_problems = "\n".join(out["problems"] + out["warnings"])
                if out.get("ok") and "reverse_sample" in out:
                    sample = out["reverse_sample"]
                    self._preview += "  |  split back: " + (
                        " · ".join(f'{self._label(k)} = "{v}"' for k, v in sample.items()) if sample
                        else "(the split pattern does not match the template's own output)")
            except Exception as e:
                self._preview, self._preview_problems = "–", str(e)
        self.previewChanged.emit()

    # -- save / options / test --------------------------------------------

    @Slot(result=str)
    def save(self) -> str:
        p = self._pair()
        if not p:
            return "No pair selected."
        try:
            models = self._as_models()
        except Exception as e:
            self._set_message(f"Invalid rule: {e}")
            return self._message
        problems = self._problems()
        if problems:
            self._set_message("\n".join(problems))
            return self._message
        changed = self.dirty
        settings = ConfigManager.load_settings()
        pair = self._stored_pair(settings)
        pair.rules = models
        pair.match_keys = [list(k) for k in self._match_keys]
        ConfigManager.save_settings(settings)
        self._settings = settings
        self._saved = copy.deepcopy({"rules": self._rules, "match_keys": self._match_keys})
        self.boardChanged.emit()
        self._set_message("Saved.")
        if changed:
            self.askApplyToAll.emit()
        return ""

    @Slot(str, int, bool, bool)
    def savePairOptions(self, direction: str, grace: int, propagate_deletes: bool, create_on_garmin: bool) -> None:
        settings = ConfigManager.load_settings()
        if self._pair_id == DEFAULT_PAIR_ID:
            # the default pair's options are the global defaults
            settings.directionality = direction
            settings.grace_window_minutes = grace
            settings.propagate_deletes = propagate_deletes
            settings.create_on_garmin = create_on_garmin
        else:
            pair = self._stored_pair(settings)
            pair.directionality = direction
            pair.grace_window_minutes = grace
            pair.propagate_deletes = propagate_deletes
            pair.create_on_garmin = create_on_garmin
        ConfigManager.save_settings(settings)
        self._settings = settings
        # the view stays on the direction being looked at; only scheduled runs follow the saved one
        self.boardChanged.emit()

    @Slot()
    def applyToAll(self) -> None:
        from src.core.pairs import engine_for_pair, find_pair, service_id_of
        try:
            # The board is shared by every account combination (rework.md
            # E19), so each combination's next run compares every dive.
            pair = find_pair(self._settings, self._pair_id)
            model = credentials.load_credentials_model()
            for source_account in accounts.accounts_for_spec(pair.source, model):
                for target_account in accounts.accounts_for_spec(pair.target, model):
                    selection = {service_id_of(pair.source): source_account, service_id_of(pair.target): target_account}
                    engine = engine_for_pair(pair, **accounts.engine_kwargs({k: v for k, v in selection.items() if v}))
                    engine.request_full_compare(True)
            self._set_message("Saved. The next run will compare every matched dive.")
        except Exception as e:
            self._set_message(f"Saved, but the full-compare flag could not be set: {e}")

    @Slot()
    def cancel(self) -> None:
        self._rules = copy.deepcopy(self._saved["rules"])
        self._match_keys = copy.deepcopy(self._saved["match_keys"])
        self._selected = None
        self._emit_board()
        self._set_message("Changes discarded.")

    @Slot()
    def resetToDefaults(self) -> None:
        p = self._pair()
        if p:
            self._rules = copy.deepcopy(p["default_rules"])
            self._match_keys = copy.deepcopy(p["default_match_keys"])
            self._selected = None
            self._emit_board()

    @Slot()
    def testMapping(self) -> None:
        if self._busy:
            return
        try:
            rules = self._as_models()
        except Exception as e:
            self._set_message(f"Invalid rule: {e}")
            return
        match_keys = [list(k) for k in self._match_keys]
        self._busy = True
        self.busyChanged.emit()
        self._set_message("Fetching the newest dives from both services… this takes a while.")
        pair_id = self._pair_id

        def work():
            from src.core.pairs import engine_for_pair, find_pair
            credentials.begin_operation()
            try:
                engine = engine_for_pair(find_pair(ConfigManager.load_settings(), pair_id), **accounts.engine_kwargs())
                return engine.test_mapping(limit=10, rules=rules, match_keys=match_keys)
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
