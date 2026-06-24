from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class GasMixture(BaseModel):
    oxygen: float = Field(21.0, description="Oxygen percentage (0-100)")
    helium: float = Field(0.0, description="Helium percentage (0-100)")
    start_pressure: Optional[float] = Field(None, description="Starting pressure in bar")
    end_pressure: Optional[float] = Field(None, description="Ending pressure in bar")
    tank_volume: Optional[float] = Field(None, description="Tank volume in liters")

class UnifiedDive(BaseModel):
    date_time: datetime = Field(..., description="Local start date and time of the dive (timezone-naive)")
    duration: int = Field(..., description="Duration of the dive in seconds")
    max_depth: float = Field(..., description="Maximum depth in meters")
    avg_depth: Optional[float] = Field(None, description="Average depth in meters")
    temp_min: Optional[float] = Field(None, description="Minimum temperature in Celsius")
    temp_max: Optional[float] = Field(None, description="Maximum temperature in Celsius")
    temp_avg: Optional[float] = Field(None, description="Average temperature in Celsius")
    external_ids: Dict[str, str] = Field(default_factory=dict, description="Service-specific primary keys, e.g., {'garmin': '12345', 'divelogs': '67890'}")
    gas_mixtures: List[GasMixture] = Field(default_factory=list, description="Gas mixtures used per dive")
    fit_file: Optional[str] = Field(None, description="Raw binary/base64 encoded .fit file payload (optional)")
    location: Optional[str] = Field(None, description="Location/Dive site name")
    notes: Optional[str] = Field(None, description="Notes/Description of the dive")
    dive_number: Optional[int] = Field(None, description="Dive number sequence")
    weight: Optional[float] = Field(None, description="Lead weight value")
    weight_unit: Optional[str] = Field(None, description="Weight unit, e.g. 'kilogram' or 'pound'")
    visibility: Optional[float] = Field(None, description="Visibility value")
    visibility_unit: Optional[str] = Field(None, description="Visibility unit, e.g. 'meter' or 'foot'")
