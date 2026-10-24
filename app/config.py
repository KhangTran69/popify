from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./commerceops.db"
    app_secret: str = "local-development-secret-change-me"
    shopify_webhook_secret: str = ""
    shopify_store_domain: str = ""
    shopify_admin_token: str = ""
    shopify_api_version: str = "2026-10"
    operator_username: str = "operator"
    operator_password: str = "demo-change-me"
    llm_mode: Literal["mock", "openai"] = "mock"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    n8n_base_url: str = "http://n8n:5678"
    n8n_webhook_secret: str = "local-demo-secret"
    order_delay_hours: int = Field(default=48, ge=1, le=8760)
    mock_action_failures: int = Field(default=0, ge=0, le=10)
    mock_action_ambiguous: bool = False
    max_action_attempts: int = Field(default=3, ge=1, le=10)
    max_delivery_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    mock_retry_after_seconds: int = Field(default=0, ge=0, le=86400)
    store_policies: dict[str, str] = {
        "refund": "Refund requests are reviewed by an operator; no refund is automatic.",
        "cancellation": "Cancellation requests are reviewed by an operator; no cancellation is automatic.",
        "damaged_item": "Ask for a photo and brief description; do not promise reimbursement.",
        "order_status": "Only share the recorded status; do not infer a delivery date.",
        "other": "A support operator will review this request.",
    }
    @model_validator(mode="after")
    def validate_store_policies(self):
        expected = {"refund", "cancellation", "damaged_item", "order_status", "other"}
        if set(self.store_policies) != expected:
            raise ValueError("STORE_POLICIES must define refund, cancellation, damaged_item, order_status, and other")
        if any(not value.strip() or len(value) > 2000 for value in self.store_policies.values()):
            raise ValueError("Each STORE_POLICIES entry must contain 1 to 2000 characters")
        return self


settings = Settings()
