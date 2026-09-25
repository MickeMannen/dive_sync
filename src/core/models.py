from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def recorded_water_temp(value: Any) -> Optional[float]:
    """A water temperature as recorded, or None when there is none. 0 °C
    counts as none: Garmin stores 0 on a hand-logged dive whose temperature
    was never entered, and a real 0 °C dive is rare enough not to guess it."""
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value == 0.0 else value

class GasMixture(BaseModel):
    oxygen: float = Field(21.0, description="Oxygen percentage (0-100)")
    helium: float = Field(0.0, description="Helium percentage (0-100)")
    start_pressure: Optional[float] = Field(None, description="Starting pressure in bar")
    end_pressure: Optional[float] = Field(None, description="Ending pressure in bar")
    tank_volume: Optional[float] = Field(None, description="Tank volume in liters")
    tank_name: Optional[str] = Field(None, description="Custom name of the tank/cylinder (e.g. a transmitter name or user label)")
    tank_role: Optional[str] = Field(
        None,
        description=(
            "Role of this tank in a multi-tank setup, e.g. 'backGas', 'stage', 'deco', 'bailout', "
            "'sidemountLeft', 'sidemountRight', 'diluent', 'oxygen', 'not_used'. Services model this "
            "differently (or not at all); see each adapter's mapping. Order in gas_mixtures is the "
            "primary source of tank order across all services."
        ),
    )

class UnifiedSample(BaseModel):
    depth: float = Field(..., description="Depth in meters")
    temp: Optional[float] = Field(None, description="Temperature in Celsius")
    time: Optional[int] = Field(None, description="Time in seconds from start of dive")

class UnifiedDive(BaseModel):
    date_time: datetime = Field(..., description="Local start date and time of the dive (timezone-naive)")
    date_time_utc: Optional[datetime] = Field(None, description="Start instant in UTC (timezone-naive) when the service provides it; used for matching and the {date_time_utc} template key")
    timezone: Optional[str] = Field(None, description="IANA zone of the local start time when the service provides it, e.g. 'Asia/Kuala_Lumpur'")
    duration: int = Field(..., description="Duration of the dive in seconds")
    max_depth: float = Field(..., description="Maximum depth in meters")
    avg_depth: Optional[float] = Field(None, description="Average depth in meters")
    temp_min: Optional[float] = Field(None, description="Minimum temperature in Celsius")
    temp_max: Optional[float] = Field(None, description="Maximum temperature in Celsius")
    temp_avg: Optional[float] = Field(None, description="Average temperature in Celsius")
    external_ids: Dict[str, str] = Field(default_factory=dict, description="Service-specific primary keys, e.g., {'garmin': '12345', 'divelogs': '67890'}")
    gas_mixtures: List[GasMixture] = Field(default_factory=list, description="Gas mixtures used per dive")
    location: Optional[str] = Field(None, description="Location/Dive site name")
    notes: Optional[str] = Field(None, description="Notes/Description of the dive")
    dive_number: Optional[int] = Field(None, description="Dive number sequence")
    weight: Optional[float] = Field(None, description="Lead weight value")
    weight_unit: Optional[str] = Field(None, description="Weight unit, e.g. 'kilogram' or 'pound'")
    visibility: Optional[float] = Field(None, description="Visibility value")
    visibility_unit: Optional[str] = Field(None, description="Visibility unit, e.g. 'meter' or 'foot'")
    buddy: Optional[str] = Field(None, description="Dive buddy name")
    lat: Optional[float] = Field(None, description="Latitude coordinate")
    lng: Optional[float] = Field(None, description="Longitude coordinate")
    samples: List[UnifiedSample] = Field(default_factory=list, description="Time-series dive profile samples")
    device_logged: Optional[bool] = Field(
        None,
        description=(
            "True when a dive computer recorded this dive, False when it was entered by hand, None when the "
            "service does not say. Provenance rather than dive data: it decides whether a receiver that gets its "
            "computer data elsewhere (Submersion, whose profile comes from the diver's own .fit import) should "
            "have the dive created for it at all (rework.md F17)"
        ),
    )
    service_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Service-specific scalars that have no unified attribute, keyed by their native name "
            "(Garmin: activityName, locationName; Divelogs: location, divesite). Filled by the adapter's "
            "to_unified mapping, pushed back by its update_dive, addressed on the mapping board as "
            "<service_id>.<name>."
        ),
    )
