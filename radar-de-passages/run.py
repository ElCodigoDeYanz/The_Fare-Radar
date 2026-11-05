"""Local/cloud entry point. The ASGI app is built by a factory so importing the package has no side effects."""

import argparse
import logging
import os
import uvicorn
from radar.security import install_log_redaction

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("RADAR_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    install_log_redaction([os.environ.get("RADAR_TELEGRAM_TOKEN", ""), os.environ.get("RADAR_ADMIN_PASSWORD", "")])
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RADAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8003")))
    args = parser.parse_args()
    uvicorn.run(
        "radar.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
