#!/usr/bin/env python3
"""Supported Ryu 4.34 launcher (Python 3.10); preserves command-line flags."""
import os
import sys


def prepare_runtime():
    os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")
    import eventlet
    eventlet.monkey_patch()
    # Ryu's optional WebSocket response imports this removed Eventlet sentinel.
    # The application uses ordinary WSGI responses, never that WebSocket path.
    import eventlet.wsgi
    if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
        eventlet.wsgi.ALREADY_HANDLED = object()


def main():
    prepare_runtime()
    from ryu.cmd.manager import main as manager
    args = sys.argv[1:]
    if "--observe-links" in args:
        raise SystemExit("This controller owns discovery; omit --observe-links.")
    sys.argv = ["ryu-manager",
                "--ofp-tcp-listen-port", os.getenv("OF_PORT", "6633"),
                "--wsapi-port", os.getenv("API_PORT", "5000"),
                *args, "ryu_controller.sdn_controller"]
    manager()


if __name__ == "__main__":
    main()
