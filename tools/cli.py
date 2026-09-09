from __future__ import annotations

import argparse
import os
import sys


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--backend-path")
known, _ = parser.parse_known_args()
if known.backend_path:
    os.environ["V4_DEBUG_BACKEND_PATH"] = known.backend_path

from interactive_quote_debug import main  # noqa: E402


if __name__ == "__main__":
    main()
