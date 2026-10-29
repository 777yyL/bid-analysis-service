"""Native single-instance entry point honoring the deployment .env file."""

import uvicorn

from analyzer.config import Settings

if __name__ == "__main__":
    cfg = Settings()
    uvicorn.run("analyzer.main:app", host=cfg.analyzer_bind_host, port=cfg.analyzer_port, workers=1)
