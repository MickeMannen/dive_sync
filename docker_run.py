#!/usr/bin/env python3
import os
import sys
import logging
import uvicorn

def main():
    # Setup basic logging to stdout
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger = logging.getLogger("dive_sync.docker_entrypoint")
    logger.info("Starting Dive Sync Docker Engine entrypoint...")

    # Print version and check for updates
    try:
        from src.core.version import get_version_info
        v_info = get_version_info()
        logger.info("Dive Sync Version: %s", v_info["current_version"])
        if v_info["update_available"]:
            logger.warning("==========================================================")
            logger.warning("  A new version is available: %s", v_info["latest_version"])
            logger.warning("  Release notes and download: %s", v_info["release_url"])
            logger.warning("==========================================================")
    except Exception as version_err:
        logger.debug("Failed to determine version/update info: %s", version_err)

    # Load configuration from environment variables or use sensible production defaults
    host = os.getenv("DIVE_SYNC_HOST", "0.0.0.0")
    port = int(os.getenv("DIVE_SYNC_PORT", "8000"))

    # Configure and instantiate uvicorn Server explicitly to control graceful exit
    config = uvicorn.Config("src.web.app:app", host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    import signal
    def handle_signal(sig, frame):
        logger.info("Received signal %d. Shutting down server gracefully...", sig)
        server.should_exit = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Launching FastAPI Web Dashboard server on http://%s:%d", host, port)
    try:
        server.run()
    except Exception as e:
        logger.error("Web server failed to start: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
