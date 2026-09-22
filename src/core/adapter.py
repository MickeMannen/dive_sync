from abc import ABC, abstractmethod
from typing import ClassVar, List, Optional
from datetime import datetime
from src.core.models import UnifiedDive
from src.core.fields import FieldSpec

class BaseDiveAdapter(ABC):
    """Interface every dive service implements.

    ``service_id`` names the service in ``UnifiedDive.external_ids``, in
    catalogue keys (``<service_id>.<field>``), in cache directory names and in
    sync-result keys (``uploaded_to_<service_id>``). ``field_catalog()`` is the
    declared list of what this adapter reads into a ``UnifiedDive`` and what
    its ``update_dive`` can push back; the mapping board is built from it, so
    it must be callable without logging in (a classmethod)."""

    service_id: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    # True when update_dive can persist the other service's id on the dive
    # (Subsurface extradata, Submersion importId). Garmin and Divelogs have no
    # such field, so their pairs are remembered in the local sync state instead.
    stores_external_ids: ClassVar[bool] = False

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        """Fields this service can read and/or write. Default: none."""
        return []

    @abstractmethod
    def login(self) -> bool:
        """Authenticate with the service."""
        pass

    @abstractmethod
    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        """Fetch dives from the service, optionally filtered by date range."""
        pass

    def fetch_recent_dives(self, limit: int = 10) -> List[UnifiedDive]:
        """The newest ``limit`` dives. Default fetches everything and slices;
        adapters whose fetch is expensive per dive override this."""
        dives = sorted(self.fetch_dives(), key=lambda d: d.date_time, reverse=True)
        return dives[:limit]

    @abstractmethod
    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        """Add a new dive to the service. Returns the new external ID if successful."""
        pass

    @abstractmethod
    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        """Update an existing dive on the service."""
        pass

    @abstractmethod
    def delete_dive(self, external_id: str) -> bool:
        """Delete an existing dive from the service."""
        pass

    def finish(self) -> None:
        """Called once after a run that may have written (never after a dry
        run). Adapters that batch their writes (a git repository, a file
        store) commit and publish here; the default does nothing."""
        return None
