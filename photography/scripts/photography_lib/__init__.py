"""Reusable photography capabilities; ingestion works without a model service."""

from .config import Config
from .ingest import ingestion

__all__ = ["Config", "ingestion"]
