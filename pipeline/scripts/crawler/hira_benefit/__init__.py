"""HIRA insurance-criteria ingestion domain.

This package is intentionally separate from the news crawler. Deployment
manifests and Temporal schedules are added only after the news cleanup runtime
has passed its production validation gate.
"""

from .models import NoticeListItem, ParsedNotice, ParseStatus

__all__ = ["NoticeListItem", "ParseStatus", "ParsedNotice"]
