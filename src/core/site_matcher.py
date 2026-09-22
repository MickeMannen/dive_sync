"""Dive-site resolution shared by the Subsurface and Submersion adapters
(rework.md F14): exact name first, then the nearest existing site within
``radius_m`` of the coordinates, otherwise none (the caller creates one).
"""
from __future__ import annotations

import math
from typing import Callable, Iterable, Optional, Tuple, TypeVar

T = TypeVar("T")

DEFAULT_SITE_MATCH_RADIUS_M = 200.0


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_site(sites: Iterable[T], name: Optional[str], lat: Optional[float], lng: Optional[float],
              get_name: Callable[[T], Optional[str]], get_gps: Callable[[T], Tuple[Optional[float], Optional[float]]],
              radius_m: float = DEFAULT_SITE_MATCH_RADIUS_M) -> Optional[T]:
    sites = list(sites)
    wanted = (name or "").strip().lower()
    if wanted:
        for site in sites:
            if (get_name(site) or "").strip().lower() == wanted:
                return site
    if lat is None or lng is None:
        return None
    best, best_d = None, None
    for site in sites:
        slat, slng = get_gps(site)
        if slat is None or slng is None:
            continue
        d = distance_m(lat, lng, slat, slng)
        if d <= radius_m and (best_d is None or d < best_d):
            best, best_d = site, d
    return best
