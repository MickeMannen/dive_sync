import sys

from desktop.paths import configure_environment

# Must happen before desktop.app (or anything under src.core) is imported -
# src.core.config computes its file paths from DATA_DIR once, at import time.
configure_environment()

from desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
