from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    analyzer_api_key: SecretStr = SecretStr("")
    mcp_api_key: SecretStr = SecretStr("")
    analyzer_bind_host: str = "0.0.0.0"
    analyzer_port: int = Field(default=8100, ge=1, le=65535)
    database_url: str = "sqlite:///./analysis.db"
    analysis_schema: str = ""
    source_mode: Literal['snapshot','shared'] = 'snapshot'
    sales_secret_key_file: str = "./runtime/settings.key"
    sales_engine_default: bool = True
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
    source_file_root: str = ""
    knowledge_files_dir: str = ""
    sync_interval_seconds: float = Field(default=300, ge=5)
    sync_batch_size: int = Field(default=100, ge=1, le=500)
    incremental_enabled: bool = False
    incremental_lookback_seconds: int = Field(default=86400, ge=60)
    compensation_interval_seconds: int = Field(default=21600, ge=60)
    http_trust_env: bool = False

    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_json_mode: bool = True
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)
    llm_rpm: int = Field(default=5, ge=1)
    llm_concurrency: int = Field(default=3, ge=1, le=32)
    model_no_hit_sample_percent: int = Field(default=5, ge=0, le=100)
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
        import re
        if self.mcp_api_key.get_secret_value() and self.mcp_api_key.get_secret_value()==self.analyzer_api_key.get_secret_value():
            raise ValueError('MCP_API_KEY 必须独立于管理接口 ANALYZER_API_KEY')
        if self.analysis_schema and (self.analysis_schema == "public" or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.analysis_schema)):
            raise ValueError("ANALYSIS_SCHEMA 必须是非 public 的简单 schema 名称")
        target = make_url(self.database_url)
        if self.source_mode == 'shared' and (not target.drivername.startswith('postgres') or not self.analysis_schema):
            raise ValueError('同库模式必须使用 PostgreSQL 并设置独立 ANALYSIS_SCHEMA')
        if target.drivername.startswith("postgres") and self.source_db_dsn.get_secret_value():
            try:
                source = conninfo_to_dict(self.source_db_dsn.get_secret_value())
            except ProgrammingError:
                source = {}
            if (source.get("dbname") and target.database == source["dbname"]
                    and (target.host or "localhost").casefold() == source.get("host", "localhost").casefold()
                    and (target.port or 5432) == int(source.get("port", "5432"))):
                if not self.analysis_schema:
                    raise ValueError("共享来源数据库必须设置独立 ANALYSIS_SCHEMA，禁止写入 public")
        if self.llm_enabled and (not self.llm_base_url or not self.llm_model):
            raise ValueError("LLM_ENABLED=true 时必须配置 LLM_BASE_URL 和 LLM_MODEL")
        if not self.source_base_url.startswith(("http://", "https://")):
            raise ValueError("SOURCE_BASE_URL 必须是 HTTP(S) URL")
        if self.llm_base_url and not self.llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL 必须是 HTTP(S) URL")
        return self
