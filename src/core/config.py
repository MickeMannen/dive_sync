import os
import json
from typing import List, Optional
from pydantic import BaseModel, Field

SETTINGS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "settings.json")
CREDENTIALS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "credentials.json")

class SyncFilters(BaseModel):
    date_from: Optional[str] = Field(None, description="Sync start date, format YYYY-MM-DD")
    date_to: Optional[str] = Field(None, description="Sync end date, format YYYY-MM-DD")
    only_new: bool = Field(True, description="Sync only new dives since last run")
    sync_gases: bool = Field(True, description="Sync detailed gas mixtures")
    sync_fit: bool = Field(False, description="Sync FIT files")

class SyncScheduleSlot(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)

class SettingsModel(BaseModel):
    directionality: str = Field("bidirectional", description="bidirectional, to_divelogs, to_garmin")
    sync_filters: SyncFilters = Field(default_factory=SyncFilters)
    grace_window_minutes: int = Field(15, description="Matching grace window in minutes")
    api_cooldown_seconds: float = Field(1.0, description="Cool-down delay in seconds between API requests")
    schedule: List[SyncScheduleSlot] = Field(default_factory=list, description="Cron-like multi-slot schedule")

class GarminCredentials(BaseModel):
    username: str = ""
    password: str = ""
    token_dir: str = "tokens/garmin"

class DivelogsCredentials(BaseModel):
    username: str = ""
    password: str = ""

from typing import Union

class CredentialsModel(BaseModel):
    garmin: Union[List[GarminCredentials], GarminCredentials] = Field(default_factory=GarminCredentials)
    divelogs: Union[List[DivelogsCredentials], DivelogsCredentials] = Field(default_factory=DivelogsCredentials)

    def get_garmin_accounts(self) -> List[GarminCredentials]:
        if isinstance(self.garmin, list):
            return self.garmin
        if getattr(self.garmin, "username", None):
            return [self.garmin]
        return []

    def get_divelogs_accounts(self) -> List[DivelogsCredentials]:
        if isinstance(self.divelogs, list):
            return self.divelogs
        if getattr(self.divelogs, "username", None):
            return [self.divelogs]
        return []

class ConfigManager:
    @staticmethod
    def load_settings(path: str = SETTINGS_FILE) -> SettingsModel:
        if not os.path.exists(path):
            defaults = SettingsModel()
            ConfigManager.save_settings(defaults, path)
            return defaults
        with open(path, "r") as f:
            try:
                data = json.load(f)
                return SettingsModel.model_validate(data)
            except Exception:
                return SettingsModel()

    @staticmethod
    def save_settings(settings: SettingsModel, path: str = SETTINGS_FILE) -> None:
        with open(path, "w") as f:
            json.dump(settings.model_dump(), f, indent=2)

    @staticmethod
    def load_credentials(path: str = CREDENTIALS_FILE) -> CredentialsModel:
        if not os.path.exists(path):
            defaults = CredentialsModel()
            ConfigManager.save_credentials(defaults, path)
            return defaults
        with open(path, "r") as f:
            try:
                data = json.load(f)
                return CredentialsModel.model_validate(data)
            except Exception:
                return CredentialsModel()

    @staticmethod
    def save_credentials(credentials: CredentialsModel, path: str = CREDENTIALS_FILE) -> None:
        with open(path, "w") as f:
            json.dump(credentials.model_dump(), f, indent=2)
