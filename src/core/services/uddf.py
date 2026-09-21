"""UDDF 3.2 file adapter (rework.md F2).

UDDF (Universal Dive Data Format) is the one format both Subsurface and
Submersion import and export. This adapter reads and writes a single
``.uddf`` file with the standard library XML parser. Units in UDDF are SI:
metres, seconds, kelvin, pascal, cubic metres and kilograms; ``UnifiedDive``
uses Celsius, bar and litres, so the adapter converts.

Mapping (UDDF element -> UnifiedDive):
    profiledata/repetitiongroup/dive
        @id                                       external_ids["uddf"]
        informationbeforedive/datetime            date_time (local, naive)
        informationbeforedive/divenumber          dive_number
        informationbeforedive/link@ref -> divesite/site   location, lat, lng
        informationbeforedive/link@ref -> diver/buddy     buddy (joined with ", ")
        informationafterdive/greatestdepth        max_depth
        informationafterdive/averagedepth         avg_depth
        informationafterdive/diveduration         duration
        informationafterdive/lowesttemperature    temp_min
        informationafterdive/notes/para           notes
        informationafterdive/rating/ratingvalue   service_fields["rating"]
        informationafterdive/visibility           visibility (m)
        tankdata (link@ref -> gasdefinitions/mix) gas_mixtures
        samples/waypoint                          samples

UDDF has no slot for another service's id. dive_sync writes its own ids in
``<dive id="dive_sync-<garmin id>">`` when it creates a dive and otherwise
uses whatever id the writing application put there; the engine's local
link table keeps pairs across runs.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.core.adapter import BaseDiveAdapter
from src.core.fields import FieldSpec
from src.core.models import GasMixture, UnifiedDive, UnifiedSample

logger = logging.getLogger("dive_sync.uddf")

SERVICE_ID = "uddf"
NS = "http://www.streit.cc/uddf/3.2/"
GENERATOR = "dive_sync"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _strip_ns(tree: ET.Element) -> None:
    for el in tree.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def _text(el: Optional[ET.Element], path: str) -> Optional[str]:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _float(el: Optional[ET.Element], path: str) -> Optional[float]:
    t = _text(el, path)
    try:
        return float(t) if t not in (None, "") else None
    except ValueError:
        return None


def kelvin_to_c(k: Optional[float]) -> Optional[float]:
    return None if k is None else round(k - 273.15, 2)


def c_to_kelvin(c: Optional[float]) -> Optional[float]:
    return None if c is None else round(c + 273.15, 2)


def pa_to_bar(pa: Optional[float]) -> Optional[float]:
    return None if pa is None else round(pa / 100000.0, 2)


def bar_to_pa(bar: Optional[float]) -> Optional[float]:
    return None if bar is None else round(bar * 100000.0)


def parse_uddf_datetime(text: str) -> Optional[datetime]:
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_uddf(path: str) -> Tuple[ET.Element, List[UnifiedDive]]:
    """Parse a UDDF file. Returns the (namespace-stripped) root and the dives."""
    tree = ET.parse(path)
    root = tree.getroot()
    _strip_ns(root)
    sites: Dict[str, Tuple[str, Optional[float], Optional[float]]] = {}
    for site in root.findall("divesite/site"):
        lat = _float(site, "geography/latitude")
        lng = _float(site, "geography/longitude")
        sites[site.get("id", "")] = (_text(site, "name") or "", lat, lng)
    buddies: Dict[str, str] = {}
    for buddy in root.findall("diver/buddy"):
        name = " ".join(x for x in (_text(buddy, "personal/firstname"), _text(buddy, "personal/lastname")) if x)
        buddies[buddy.get("id", "")] = name or (_text(buddy, "personal/nickname") or "")
    mixes: Dict[str, Tuple[float, float]] = {}
    for mix in root.findall("gasdefinitions/mix"):
        o2 = _float(mix, "o2")
        he = _float(mix, "he")
        mixes[mix.get("id", "")] = ((o2 or 0.21) * 100.0, (he or 0.0) * 100.0)

    dives: List[UnifiedDive] = []
    for group in root.findall("profiledata/repetitiongroup"):
        for dive in group.findall("dive"):
            before = dive.find("informationbeforedive")
            after = dive.find("informationafterdive")
            when_text = _text(before, "datetime")
            when = parse_uddf_datetime(when_text) if when_text else None
            if when is None:
                logger.warning("UDDF dive %s has no usable datetime; skipped", dive.get("id"))
                continue
            location = lat = lng = None
            buddy_names: List[str] = []
            if before is not None:
                for link in before.findall("link"):
                    ref = link.get("ref", "")
                    if ref in sites:
                        location, lat, lng = sites[ref]
                    elif ref in buddies:
                        buddy_names.append(buddies[ref])
            tanks: List[GasMixture] = []
            tank_ids: List[str] = []
            for tank in dive.findall("tankdata"):
                o2, he = 21.0, 0.0
                link = tank.find("link")
                if link is not None and link.get("ref", "") in mixes:
                    o2, he = mixes[link.get("ref", "")]
                vol = _float(tank, "tankvolume")
                tanks.append(GasMixture(
                    oxygen=round(o2, 1), helium=round(he, 1),
                    start_pressure=pa_to_bar(_float(tank, "tankpressurebegin")),
                    end_pressure=pa_to_bar(_float(tank, "tankpressureend")),
                    tank_volume=None if vol is None else round(vol * 1000.0, 3),
                    tank_name=tank.get("id") or None,
                ))
                tank_ids.append(tank.get("id", ""))
            samples: List[UnifiedSample] = []
            for wp in dive.findall("samples/waypoint"):
                depth = _float(wp, "depth")
                if depth is None:
                    continue
                t = _float(wp, "divetime")
                samples.append(UnifiedSample(depth=depth, temp=kelvin_to_c(_float(wp, "temperature")),
                                             time=None if t is None else int(round(t))))
            duration = _float(after, "diveduration")
            if duration is None and samples and samples[-1].time is not None:
                duration = samples[-1].time
            max_depth = _float(after, "greatestdepth")
            if max_depth is None and samples:
                max_depth = max(s.depth for s in samples)
            notes = "\n".join(p.text.strip() for p in (after.findall("notes/para") if after is not None else []) if p.text)
            number_text = _text(before, "divenumber")
            visibility = _float(after, "visibility")
            rating = _float(after, "rating/ratingvalue")
            dives.append(UnifiedDive(
                date_time=when,
                duration=int(round(duration or 0)),
                max_depth=max_depth or 0.0,
                avg_depth=_float(after, "averagedepth"),
                temp_min=kelvin_to_c(_float(after, "lowesttemperature")),
                external_ids={SERVICE_ID: dive.get("id") or f"uddf-{when.strftime('%Y%m%dT%H%M%S')}"},
                gas_mixtures=tanks,
                location=location or None,
                notes=notes or None,
                dive_number=int(number_text) if number_text and number_text.isdigit() else None,
                visibility=visibility,
                visibility_unit="meter" if visibility is not None else None,
                buddy=", ".join(b for b in buddy_names if b) or None,
                lat=lat,
                lng=lng,
                samples=samples,
                service_fields={"rating": int(rating) if rating is not None else None},
            ))
    return root, dives


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _sub(parent: ET.Element, tag: str, text: Optional[object] = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = str(text)
    return el


def _fmt(value: float) -> str:
    return f"{value:.6g}" if isinstance(value, float) else str(value)


class UddfDocument:
    """In-memory UDDF document that keeps ids stable across writes."""

    def __init__(self, root: Optional[ET.Element] = None):
        if root is None:
            root = ET.Element("uddf", {"version": "3.2.0"})
            gen = _sub(root, "generator")
            _sub(gen, "name", GENERATOR)
            _sub(gen, "type", "logbook")
            _sub(root, "diver")
            _sub(root, "divesite")
            _sub(root, "gasdefinitions")
            _sub(_sub(root, "profiledata"), "repetitiongroup", id="rg-dive_sync")
        self.root = root
        for tag in ("diver", "divesite", "gasdefinitions", "profiledata"):
            if root.find(tag) is None:
                _sub(root, tag)
        if root.find("profiledata/repetitiongroup") is None:
            _sub(root.find("profiledata"), "repetitiongroup", id="rg-dive_sync")

    # -- lookups ----------------------------------------------------------

    def _mix_id(self, o2: float, he: float) -> str:
        gases = self.root.find("gasdefinitions")
        for mix in gases.findall("mix"):
            if abs((_float(mix, "o2") or 0) * 100 - o2) < 0.05 and abs((_float(mix, "he") or 0) * 100 - he) < 0.05:
                return mix.get("id", "")
        mix_id = f"mix-{int(round(o2))}-{int(round(he))}"
        mix = _sub(gases, "mix", id=mix_id)
        _sub(mix, "name", f"{'Air' if abs(o2 - 21) < 0.5 and he == 0 else f'EAN{int(round(o2))}' if he == 0 else f'TX{int(round(o2))}/{int(round(he))}'}")
        _sub(mix, "o2", _fmt(round(o2 / 100.0, 4)))
        _sub(mix, "he", _fmt(round(he / 100.0, 4)))
        return mix_id

    def _site_id(self, name: Optional[str], lat: Optional[float], lng: Optional[float]) -> Optional[str]:
        if not name and lat is None:
            return None
        from src.core.site_matcher import find_site
        sites = self.root.find("divesite")
        existing = list(sites.findall("site"))
        hit = find_site(existing, name, lat, lng,
                        get_name=lambda s: _text(s, "name"),
                        get_gps=lambda s: (_float(s, "geography/latitude"), _float(s, "geography/longitude")))
        if hit is not None:
            return hit.get("id")
        site_id = f"site-{len(existing) + 1}"
        while sites.find(f"site[@id='{site_id}']") is not None:
            site_id += "x"
        site = _sub(sites, "site", id=site_id)
        _sub(site, "name", name or f"{lat:.6f}, {lng:.6f}")
        if lat is not None and lng is not None:
            geo = _sub(site, "geography")
            _sub(geo, "latitude", _fmt(float(lat)))
            _sub(geo, "longitude", _fmt(float(lng)))
        return site_id

    def _buddy_ids(self, buddy: Optional[str]) -> List[str]:
        if not buddy:
            return []
        diver = self.root.find("diver")
        ids = []
        for name in [b.strip() for b in buddy.split(",") if b.strip()]:
            hit = None
            for el in diver.findall("buddy"):
                full = " ".join(x for x in (_text(el, "personal/firstname"), _text(el, "personal/lastname")) if x)
                if full.lower() == name.lower():
                    hit = el
                    break
            if hit is None:
                hit = _sub(diver, "buddy", id=f"buddy-{len(diver.findall('buddy')) + 1}")
                personal = _sub(hit, "personal")
                first, _, last = name.partition(" ")
                _sub(personal, "firstname", first)
                if last:
                    _sub(personal, "lastname", last)
            ids.append(hit.get("id"))
        return ids

    # -- dives ------------------------------------------------------------

    def find_dive(self, dive_id: str) -> Optional[ET.Element]:
        for dive in self.root.findall("profiledata/repetitiongroup/dive"):
            if dive.get("id") == dive_id:
                return dive
        return None

    def write_dive(self, unified: UnifiedDive, dive_id: Optional[str] = None) -> str:
        """Create or replace a ``<dive>``; returns its id."""
        group = self.root.find("profiledata/repetitiongroup")
        existing = self.find_dive(dive_id) if dive_id else None
        if dive_id is None:
            source = next((f"{k}-{v}" for k, v in unified.external_ids.items() if k != SERVICE_ID and v), None)
            dive_id = f"{GENERATOR}-{source}" if source else f"{GENERATOR}-{unified.date_time.strftime('%Y%m%dT%H%M%S')}"
            while self.find_dive(dive_id) is not None:
                dive_id += "x"
        if existing is not None:
            index = list(group).index(existing)
            group.remove(existing)
        else:
            index = len(group)
        dive = ET.Element("dive", {"id": dive_id})
        group.insert(index, dive)

        before = _sub(dive, "informationbeforedive")
        site_id = self._site_id(unified.location, unified.lat, unified.lng)
        if site_id:
            _sub(before, "link", ref=site_id)
        for bid in self._buddy_ids(unified.buddy):
            _sub(before, "link", ref=bid)
        if unified.dive_number is not None:
            _sub(before, "divenumber", unified.dive_number)
        _sub(before, "datetime", unified.date_time.strftime("%Y-%m-%dT%H:%M:%S"))

        for idx, gas in enumerate(unified.gas_mixtures):
            tank = _sub(dive, "tankdata", id=f"{dive_id}-tank{idx}")
            _sub(tank, "link", ref=self._mix_id(gas.oxygen or 21.0, gas.helium or 0.0))
            if gas.tank_volume is not None:
                _sub(tank, "tankvolume", _fmt(round(gas.tank_volume / 1000.0, 6)))
            if gas.start_pressure is not None:
                _sub(tank, "tankpressurebegin", _fmt(bar_to_pa(gas.start_pressure)))
            if gas.end_pressure is not None:
                _sub(tank, "tankpressureend", _fmt(bar_to_pa(gas.end_pressure)))

        if unified.samples:
            samples = _sub(dive, "samples")
            for s in unified.samples:
                wp = _sub(samples, "waypoint")
                _sub(wp, "depth", _fmt(round(s.depth, 3)))
                if s.time is not None:
                    _sub(wp, "divetime", _fmt(float(s.time)))
                if s.temp is not None:
                    _sub(wp, "temperature", _fmt(c_to_kelvin(s.temp)))

        after = _sub(dive, "informationafterdive")
        _sub(after, "greatestdepth", _fmt(round(unified.max_depth, 3)))
        if unified.avg_depth is not None:
            _sub(after, "averagedepth", _fmt(round(unified.avg_depth, 3)))
        _sub(after, "diveduration", _fmt(float(unified.duration)))
        if unified.temp_min is not None:
            _sub(after, "lowesttemperature", _fmt(c_to_kelvin(unified.temp_min)))
        if unified.visibility is not None:
            vis = unified.visibility / 3.28084 if (unified.visibility_unit or "").lower().startswith("f") else unified.visibility
            _sub(after, "visibility", _fmt(round(vis, 2)))
        rating = unified.service_fields.get("rating")
        if rating is not None:
            _sub(_sub(after, "rating"), "ratingvalue", int(rating))
        if unified.notes:
            notes = _sub(after, "notes")
            for para in str(unified.notes).split("\n"):
                _sub(notes, "para", para)
        return dive_id

    def remove_dive(self, dive_id: str) -> bool:
        group = self.root.find("profiledata/repetitiongroup")
        dive = self.find_dive(dive_id)
        if dive is None:
            return False
        group.remove(dive)
        return True

    def save(self, path: str) -> None:
        root = ET.Element(self.root.tag, self.root.attrib)
        root.extend(list(self.root))
        _restore_ns(root)
        ET.indent(root, space="  ")
        ET.register_namespace("", NS)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _restore_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and not el.tag.startswith("{"):
            el.tag = _q(el.tag)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class UddfAdapter(BaseDiveAdapter):
    """One ``.uddf`` file as a dive service. A missing file is created on the
    first write; reading a missing file yields no dives."""

    service_id = SERVICE_ID
    display_name = "UDDF file"
    stores_external_ids = False

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        return [
            FieldSpec(key="uddf.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="uddf.duration", label="Duration", type="number", unified="duration", unit="s"),
            FieldSpec(key="uddf.max_depth", label="Max depth", type="number", unified="max_depth", unit="m"),
            FieldSpec(key="uddf.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m"),
            FieldSpec(key="uddf.temp_min", label="Lowest temperature", type="number", unified="temp_min", unit="°C"),
            FieldSpec(key="uddf.dive_number", label="Dive number", type="number", unified="dive_number"),
            FieldSpec(key="uddf.location", label="Dive site", type="text", unified="location"),
            FieldSpec(key="uddf.notes", label="Notes", type="text", unified="notes"),
            FieldSpec(key="uddf.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="uddf.visibility", label="Visibility", type="number", unified="visibility"),
            FieldSpec(key="uddf.gps", label="Site position", type="gps", unified="gps"),
            FieldSpec(key="uddf.tanks", label="Tanks", type="tanks", unified="tanks"),
            FieldSpec(key="uddf.samples", label="Dive profile", type="samples", unified="samples"),
            FieldSpec(key="uddf.rating", label="Rating", type="number"),
        ]

    def __init__(self, path: str):
        self.path = path
        self.document: Optional[UddfDocument] = None

    def login(self) -> bool:
        try:
            if os.path.exists(self.path):
                root, _ = read_uddf(self.path)
                self.document = UddfDocument(root)
            else:
                self.document = UddfDocument()
            return True
        except ET.ParseError as e:
            logger.error("UDDF file %s is not valid XML: %s", self.path, e)
            return False

    def _doc(self) -> UddfDocument:
        if self.document is None and not self.login():
            raise RuntimeError(f"UDDF file not readable: {self.path}")
        return self.document

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        if not os.path.exists(self.path):
            self._doc()
            return []
        _root, dives = read_uddf(self.path)
        self.document = UddfDocument(_root)
        out = []
        for d in dives:
            if date_from and d.date_time < date_from:
                continue
            if date_to and d.date_time > date_to:
                continue
            out.append(d)
        return out

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        doc = self._doc()
        dive_id = doc.write_dive(dive)
        doc.save(self.path)
        logger.info("UDDF: added dive %s to %s", dive_id, self.path)
        return dive_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        doc = self._doc()
        if doc.find_dive(external_id) is None:
            logger.error("UDDF: no dive with id %s in %s", external_id, self.path)
            return False
        doc.write_dive(dive, dive_id=external_id)
        doc.save(self.path)
        return True

    def delete_dive(self, external_id: str) -> bool:
        doc = self._doc()
        if not doc.remove_dive(external_id):
            return False
        doc.save(self.path)
        return True
