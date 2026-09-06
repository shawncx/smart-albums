"""Reusable photography capabilities; ingestion works without a model service."""

from .config import Config
from .ingest import ingest
ingestion = ingest

__all__ = ["Config", "ingestion", "ingest"]
