"""ASGI entry point: `hypercorn ci_cycle.app:app --bind 127.0.0.1:9080`."""

import restate

from .workflow import ci_cycle

app = restate.app(services=[ci_cycle])
