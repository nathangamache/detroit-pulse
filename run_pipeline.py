import os
import threading
import time
import signal
import sys
import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

FEEDS_CONFIG = "ingest/feeds.yaml"


def load_enabled_feeds():
    import yaml
    with open(FEEDS_CONFIG) as f:
        config = yaml.safe_load(f)
    return [f for f in config["feeds"] if f.get("enabled", True)]


def load_all_models():
    """
    Load ALL ML models in the main thread before any workers start.
    PyTorch CUDA initialization is not thread-safe — loading models
    concurrently across threads causes segfaults.
    """
    import torch
    from faster_whisper import WhisperModel
    import os

    # ── VAD ──────────────────────────────────────────────────────────
    log.info("Loading Silero VAD model (CPU)")
    vad_model, vad_utils = torch.hub.load(
        repo_or_dir = "snakers4/silero-vad",
        model       = "silero_vad",
        force_reload = False,
        trust_repo  = True,
    )
    # Force VAD to CPU — it is lightweight and does not need GPU.
    # Running VAD on CUDA alongside Whisper causes segfaults from
    # concurrent CUDA context initialization across threads.
    vad_model = vad_model.cpu()
    log.info("Silero VAD model loaded (CPU)")

    # ── Whisper ───────────────────────────────────────────────────────
    whisper_model_name = os.getenv("WHISPER_MODEL", "large-v3")
    log.info("Loading Whisper model", model=whisper_model_name)
    whisper_model = WhisperModel(
        whisper_model_name,
        device       = "cuda",
        compute_type = "float16",
    )
    log.info("Whisper model loaded")

    return {
        "vad_model":     vad_model,
        "vad_utils":     vad_utils,
        "whisper_model": whisper_model,
    }


def run_feed_worker_thread(feed, vad_model, vad_utils):
    from ingest.feed_worker import run_feed_worker
    from api.broadcaster import publish_debug

    feed_id = feed["id"]
    log.info("Feed worker thread starting", feed_id=feed_id)

    while True:
        try:
            publish_debug("ingest", feed_id,
                status = "starting",
                name   = feed["name"])
            run_feed_worker(feed, vad_model, vad_utils)
        except Exception as e:
            log.error("Feed worker crashed — restarting in 10s",
                      feed_id=feed_id, error=str(e))
            time.sleep(10)


def run_whisper_thread(whisper_model):
    from transcribe.whisper_worker import run_whisper_worker
    log.info("Whisper worker thread starting")
    while True:
        try:
            run_whisper_worker(whisper_model=whisper_model)
        except Exception as e:
            log.error("Whisper worker crashed — restarting in 10s", error=str(e))
            time.sleep(10)


def run_pipeline_thread():
    from llm.pipeline_worker import run_pipeline_worker
    log.info("Pipeline worker thread starting")
    while True:
        try:
            run_pipeline_worker()
        except Exception as e:
            log.error("Pipeline worker crashed — restarting in 10s", error=str(e))
            time.sleep(10)


def run_resolver_thread():
    from correlation.resolver import run_resolver_loop
    log.info("Resolver thread starting")
    while True:
        try:
            run_resolver_loop(interval_seconds=300)
        except Exception as e:
            log.error("Resolver crashed — restarting in 30s", error=str(e))
            time.sleep(30)


def main():
    feeds = load_enabled_feeds()
    log.info("Detroit Pulse pipeline starting",
             feeds    = len(feeds),
             feed_ids = [f["id"] for f in feeds])

    # Mark restart in debug log
    from api.debug_logger import log_separator
    log_separator(f"PIPELINE STARTED — {len(feeds)} feeds")

    # Load ALL models in main thread before spawning workers
    log.info("Loading all ML models in main thread...")
    models = load_all_models()
    log.info("All models loaded — starting workers")

    threads = []

    for feed in feeds:
        t = threading.Thread(
            target = run_feed_worker_thread,
            args   = (feed, models["vad_model"], models["vad_utils"]),
            name   = f"feed-{feed['id']}",
            daemon = True,
        )
        threads.append(t)

    threads.append(threading.Thread(
        target = run_whisper_thread,
        args   = (models["whisper_model"],),
        name   = "whisper",
        daemon = True,
    ))

    num_pipeline_workers = int(os.getenv("NUM_PIPELINE_WORKERS", "4"))
    log.info("Starting pipeline workers", count=num_pipeline_workers)
    for i in range(num_pipeline_workers):
        threads.append(threading.Thread(
            target = run_pipeline_thread,
            name   = f"pipeline-{i}",
            daemon = True,
        ))

    threads.append(threading.Thread(
        target = run_resolver_thread,
        name   = "resolver",
        daemon = True,
    ))

    for t in threads:
        t.start()
        log.info("Thread started", name=t.name)
        # Stagger feed worker starts to avoid concurrent CUDA/torch
        # initialization which causes segfaults
        if t.name.startswith("feed-"):
            time.sleep(2)

    def handle_shutdown(sig, frame):
        log.info("Shutting down pipeline...")
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    log.info("All workers running — press Ctrl+C to stop")

    while True:
        time.sleep(60)
        alive = [t.name for t in threads if t.is_alive()]
        dead  = [t.name for t in threads if not t.is_alive()]
        log.info("Thread health",
                 alive = len(alive),
                 dead  = dead if dead else "none")


if __name__ == "__main__":
    main()