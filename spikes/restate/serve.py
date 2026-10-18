"""Serve the workflow endpoint in this process: `python serve.py 127.0.0.1:9080`.

One process, no worker children, so a SIGKILL to this pid is a real crash of
the whole service.
"""

import asyncio
import sys

from hypercorn.asyncio import serve
from hypercorn.config import Config

from ci_cycle.app import app

if __name__ == "__main__":
    config = Config()
    config.bind = [sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:9080"]
    asyncio.run(serve(app, config))
