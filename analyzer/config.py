from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    analyzer_api_key: SecretStr = SecretStr("")
    database_url: str = "sqlite:///./analysis.db"
    source_base_url: str = "http://10.33.65.152:8099"
    source_api_token: SecretStr = SecretStr("")
    source_auth_header: str = "Authorization"
    source_db_dsn: SecretStr = SecretStr("")
    source_timeout_seconds: float = Field(default=30, gt=0, le=300)
    source_max_response_bytes: int = Field(default=20_000_000, ge=1000)
    source_notices_path: str = "/api/v1/admin/notices"
    source_notice_path: str = "/api/v1/admin/notices/{notice_id}"
    source_attachment_path: str = "/api/v1/admin/attachments/{att_id}/text"
    source_keywords_path: str = "/api/v1/kb/keywords"
    source_page_param: str = "page"
    source_page_size_param: str = "page_size"
    source_body_field: str = ""

    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_json_mode: bool = True
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)
    llm_rpm: int = Field(default=5, ge=1)
    llm_tpm: int = Field(default=0, ge=0)
    llm_max_output_tokens: int = Field(default=3000, ge=256, le=32000)
    llm_max_input_chars: int = Field(default=28000, ge=2000)
    candidate_limit: int = Field(default=8, ge=1, le=54)
    evidence_max_chars: int = Field(default=12000, ge=500)
    evidence_chunk_chars: int = Field(default=700, ge=100, le=2000)
    knowledge_max_chars_per_line: int = Field(default=1800, ge=200)
    material_max_chars: int = Field(default=2_000_000, ge=1000)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_retry_seconds: float = Field(default=30, ge=0)
    worker_poll_seconds: float = Field(default=1, gt=0)
    worker_enabled: bool = True

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.llm_enabled and (not self.llm_base_url or not self.llm_model):
            raise ValueError("LLM_ENABLED=true 时必须配置 LLM_BASE_URL 和 LLM_MODEL")
        if not self.source_base_url.startswith(("http://", "https://")):
            raise ValueError("SOURCE_BASE_URL 必须是 HTTP(S) URL")
        if self.llm_base_url and not self.llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL 必须是 HTTP(S) URL")
        return self
