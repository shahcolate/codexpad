"""python -m codexpad — the one command that starts everything.

Launches the daemon if one isn't already running, then serves the control
panel at http://127.0.0.1:8378.
"""
from .app import main

main()
