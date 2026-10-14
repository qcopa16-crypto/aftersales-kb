"""Uvicorn entry: `uvicorn main:app` or `python main.py`."""

from core.config import get_settings
from app.main import app

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run("app.main:app", host=cfg.host, port=cfg.port, reload=False)
