"""Request schema of POST /v1/embeddings, without any web-framework import.

The API contract (plugboard.api_contract) documents this model, and the MCP
contract is exported from the MCP environment, which has pydantic but not
FastAPI: keep this module free of fastapi/starlette imports.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=256)
    input: Union[str, list[str]]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: Optional[int] = None
    user: Optional[str] = Field(default=None, max_length=256)
