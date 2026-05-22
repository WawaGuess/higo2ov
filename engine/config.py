"""OpenViking configuration loaded from .env file."""

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load .env from project root
load_dotenv()


class OpenVikingConfig(BaseModel):
    """Configuration for OpenViking connection and behavior."""

    base_url: str = Field(default="http://127.0.0.1:1933")
    api_key: str = Field(default="")
    agent_id: str = Field(default="default")
    account_id: str = Field(default="")
    user_id: str = Field(default="")
    timeout_ms: int = Field(default=30000)
    commit_token_threshold: int = Field(default=8000)
    recall_limit: int = Field(default=10)
    recall_score_threshold: float = Field(default=0.1)
    isolate_user_scope_by_agent: bool = Field(default=False)
    isolate_agent_scope_by_user: bool = Field(default=True)

    @classmethod
    def from_env(cls) -> "OpenVikingConfig":
        """Load configuration from .env file."""
        return cls(
            base_url=os.getenv("OPENVIKING_BASE_URL", "http://127.0.0.1:1933"),
            api_key=os.getenv("OPENVIKING_API_KEY", ""),
            agent_id=os.getenv("OPENVIKING_AGENT_ID", "default"),
            account_id=os.getenv("OPENVIKING_ACCOUNT_ID", ""),
            user_id=os.getenv("OPENVIKING_USER_ID", ""),
            timeout_ms=int(os.getenv("OPENVIKING_TIMEOUT_MS", "30000")),
            commit_token_threshold=int(
                os.getenv("OPENVIKING_COMMIT_TOKEN_THRESHOLD", "8000")
            ),
            recall_limit=int(os.getenv("OPENVIKING_RECALL_LIMIT", "10")),
            recall_score_threshold=float(
                os.getenv("OPENVIKING_RECALL_SCORE_THRESHOLD", "0.1")
            ),
            isolate_user_scope_by_agent=os.getenv(
                "OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT", "false"
            ).lower()
            == "true",
            isolate_agent_scope_by_user=os.getenv(
                "OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER", "true"
            ).lower()
            == "true",
        )
