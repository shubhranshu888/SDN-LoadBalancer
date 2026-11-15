"""Ryu manager launcher with Python 3.10 / eventlet>=0.31 compatibility shim."""
import sys

try:
    import eventlet.wsgi
    if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
        eventlet.wsgi.ALREADY_HANDLED = []
except ImportError:
    pass

from ryu.cmd.manager import main


if __name__ == "__main__":
    sys.exit(main())
