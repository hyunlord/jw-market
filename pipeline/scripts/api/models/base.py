from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiBaseModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
