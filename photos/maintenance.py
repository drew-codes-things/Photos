import logging
import threading
import time

MAINTENANCE_INTERVAL_SECONDS = 10 * 60

log = logging.getLogger("photos.maintenance")


class Maintenance:
    def __init__(self, library, uploads, clock=time.monotonic, interval: float = MAINTENANCE_INTERVAL_SECONDS):
        self.library = library
        self.uploads = uploads
        self.clock = clock
        self.interval = interval
        self.last_run: float | None = None
        self.guard = threading.Lock()

    def run_now(self) -> dict:
        expired = self.library.purge_expired()
        stale = self.uploads.purge_stale()
        if expired or stale:
            log.info("cleared %d items past their time in the bin and %d unfinished uploads", expired, stale)
        return {"expired": expired, "staleUploads": stale}

    def run_if_due(self) -> dict | None:
        with self.guard:
            now = self.clock()
            if self.last_run is not None and now - self.last_run < self.interval:
                return None
            self.last_run = now
        return self.run_now()
