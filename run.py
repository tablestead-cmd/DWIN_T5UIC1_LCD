#!/usr/bin/env python3
# Start the display service: python3 run.py [--config FILE] [--log-level debug] [--no-gpio] [--mock]
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dwinlcd.app import main  # noqa: E402

if __name__ == "__main__":
	sys.exit(main())
