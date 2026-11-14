"""`broadcastify_legacy` source: REMOVED. Kept only as a loud, disabled placeholder.

The original pipeline pulled https://broadcastify.cdnstream1.com/<feed> with
ffmpeg every ~31 s and transcribed it. Broadcastify's Terms of Service, section 9
(revision 2026-07-22), forbid automated capture of feeds and AI/automated
transcription of their audio, including for Premium subscribers, and Broadcastify
is not issuing developer licenses at present. So:

  * the capture code has been removed from this repository (it is in git history
    before the godseye-rework branch, for reference only);
  * this source refuses to run unless BOTH BROADCASTIFY_LEGACY_ENABLED=true and
    BROADCASTIFY_TOS_ACKNOWLEDGED=true are set, and even then it only logs this
    warning and exits: re-adding capture requires written permission from
    Broadcastify (a developer/data license), which this project does not have;
  * the unauthenticated /audio/stream relay that re-served Broadcastify audio is gone.

Use your own receivers instead (`sdr_files` source, see docs/sdr-ingest.md).
"""
from __future__ import annotations

import threading

import structlog

from core.settings import get_settings
from ingest.sources.base import Source

log = structlog.get_logger()

TOS_WARNING = (
    "Broadcastify Terms §9 (rev. 2026-07-22) forbid automated capture and AI transcription of "
    "Broadcastify audio, even with Premium. The legacy capture code was removed; this source does "
    "nothing. Use the sdr_files source with your own receivers."
)


class BroadcastifyLegacySource(Source):
    name = "broadcastify_legacy"

    def __init__(self):
        s = get_settings()
        self.enabled = s.broadcastify_legacy_enabled and s.broadcastify_tos_acknowledged

    def poll_once(self) -> int:
        return 0

    def run(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        log.error("broadcastify_legacy source requested but disabled", warning=TOS_WARNING,
                  flags_set=self.enabled)

    def status(self) -> dict:
        return {"source": self.name, "enabled": False, "warning": TOS_WARNING}
