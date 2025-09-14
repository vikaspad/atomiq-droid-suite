from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional

class BuildOptions(BaseModel):
    """Single source of truth for pipeline options."""
    job_id: str
    github_url: str

    # Optional prompt/LLM settings
    prompt: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    api_key: Optional[str] = None

    # NEW: absolute path (on server) to the uploaded requirement/spec file, if any
    requirement_path: Optional[str] = Field(
        default=None, alias="requirementPath"
    )

    # Canonical flags with legacy UI aliases for backward-compat
    generate_unit: bool = Field(False, alias="generateUnitTests")
    generate_bdd: bool = Field(False, alias="createBDDFramework")

    # Pydantic v2 config
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

class JobState(BaseModel):
    job_id: str
    status: str
    message: str = ""
    progress: int = 0
    artifact_path: Optional[str] = None
    traceback: Optional[str] = None