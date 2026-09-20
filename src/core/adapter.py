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
