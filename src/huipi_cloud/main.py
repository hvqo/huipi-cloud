"""FastAPI application entry point."""

from fastapi import FastAPI

from huipi_cloud.api.v1.router import api_router
from huipi_cloud.core.config import settings
from huipi_cloud.core.logging import configure_logging

configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(api_router, prefix=settings.api_v1_prefix)
