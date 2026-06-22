from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime
from src.core.models import UnifiedDive

class BaseDiveAdapter(ABC):
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
