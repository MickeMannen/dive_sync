from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List, Optional
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
    # (Subsurface extradata). Garmin and Divelogs have no
    # such field, so their pairs are remembered in the local sync state instead.
    stores_external_ids: ClassVar[bool] = False
    # False when the service never takes a dive from elsewhere (Shearwater
    # Cloud: only the app's own computer download adds one). The engine then
    # leaves the dives the service is missing alone, says so once per run,
    # and still syncs the metadata of the dives it does have.
    accepts_new_dives: ClassVar[bool] = True
    # False while the service can take new dives but not changes to the
    # dives it already has (MySSI until its update call is tested live,
    # plans/myssi-sync.md). The engine then says once per matched dive that
    # the changes were not sent, writes nothing there and queues no conflict
    # for it (a resolution would be an update it cannot take).
    accepts_updates: ClassVar[bool] = True
    # False when no sync run may ever delete on this service - not a Mirror
    # run, not a pair with propagate_deletes on (MySSI: a delete is permanent
    # and the dive number may not come back; Shearwater: the app's database
    # is never deleted from). The engine logs each dive it would have deleted,
    # counts it under skipped and leaves it; only a page of the app that says
    # so deletes there. The name is kept from when only Mirror deleted.
    accepts_mirror_deletes: ClassVar[bool] = True

    # Set by ``add_dive`` when it returned None because it left the dive out
    # on purpose (the service already has it: Divelogs' "already exists",
    # MySSI's duplicate check) - a short reason; None when the add was tried
    # and failed, or never set. The engine clears it before every add_dive
    # call and counts a None result with a reason as skipped
    # (``receiver_already_has_dive``), without one as failed (check.md B4).
    add_skipped_reason: Optional[str] = None

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        """Fields this service can read and/or write. Default: none."""
        return []

    def cannot_add_reason(self, dive: UnifiedDive) -> Optional[str]:
        """Why ``add_dive`` would refuse this particular dive, as a short
        reason ("it has 2 tanks; ... not supported yet"), or None when it can
        be added. The engine asks before every upload so a refused dive is
        counted as skipped (with the reason) instead of tried and dropped;
        ``add_dive`` applies the same check for callers that go straight to
        it. Default: every dive can be added."""
        return None

    def upload_details(self, dive: UnifiedDive) -> Dict[str, Any]:
        """How ``add_dive`` would send this dive when that is worth saying,
        for the run's log and its results entry: an optional ``"note"`` (one
        log line, said in a dry run too) plus any keys the entry under
        ``uploaded_to_<service>`` should carry (Garmin's .fit upload, R13:
        ``upload_as`` and ``fit_drops``). Called on the dive as it will be
        uploaded, never makes a request. Default: nothing to say."""
        return {}

    @classmethod
    def as_stored(cls, field_type: str, value: Any) -> Any:
        """``value`` (of a catalogue ``field_type``) as this service would
        store and read it back. The engine compares a sender's value in this
        form, so what the service cannot hold (MySSI: a tank's name, a profile
        off its own time grid) is not a difference on every run. Default: the
        value unchanged."""
        return value

    @classmethod
    def canonical_id(cls, external_id) -> str:
        """The part of a dive id that stays the same for the dive's lifetime,
        which is what links are compared on. Default: the id itself; a
        service whose id carries something editable (Subsurface's Dive-N
        file name holds the dive number) reduces it."""
        return str(external_id).strip()

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

    def fetch_dive(self, external_id: str) -> Optional[UnifiedDive]:
        """One dive by its id, for a write that needs the current record
        (resolving a conflict). None when the service no longer has it.
        Default: not supported, which makes the engine fall back to a dated
        ``fetch_dives`` window; an adapter whose listing is expensive
        (Garmin: a page-through plus three calls per dive) overrides this."""
        raise NotImplementedError

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
