"""Configuration via environment variables using pydantic-settings."""

import socket
import uuid

from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/scraper"
    pool_size: int = 10

    model_config = SettingsConfigDict(env_prefix="DB_")


class WorkerSettings(BaseSettings):
    """Worker process settings."""

    id: str = ""
    poll_interval: float = 5.0
    heartbeat_interval: float = 30.0
    task_timeout: float = 300.0
    stale_task_minutes: int = 5

    model_config = SettingsConfigDict(env_prefix="WORKER_")

    def get_worker_id(self) -> str:
        if self.id:
            return self.id
        hostname = socket.gethostname()
        short_uuid = uuid.uuid4().hex[:8]
        return f"worker-{hostname}-{short_uuid}"


class BrowserSettings(BaseSettings):
    """Browser settings.

    Two modes:
    - External browser (default): connect to a user's real Chrome via CDP.
      Set CHROME_DEBUG_HOST/CHROME_DEBUG_PORT to point to the browser.
    - Browser pool: Worker launches its own headless Chromium instances.
      Set BROWSER_POOL=true to enable.
    """

    # External browser (connect to existing Chrome)
    chrome_debug_host: str = "127.0.0.1"
    chrome_debug_port: int = 9222

    # Browser pool (self-managed headless Chromium)
    pool: bool = False
    size: int = 1
    max_tasks_per_browser: int = 50
    headless: bool = True

    model_config = SettingsConfigDict(env_prefix="BROWSER_")


class S3Settings(BaseSettings):
    """S3 upload settings."""

    bucket: str = ""
    prefix: str = ""
    delete_local_after_upload: bool = False

    model_config = SettingsConfigDict(env_prefix="S3_")


class FeishuSettings(BaseSettings):
    """Feishu (Lark) bot notification settings."""

    app_id: str = ""
    app_secret: str = ""
    # Accepts chat_id (oc_xxx) for group chats or open_id (ou_xxx) for P2P.
    receive_id: str = ""
    poll_interval: float = 5.0
    api_base: str = "https://open.feishu.cn"

    model_config = SettingsConfigDict(env_prefix="FEISHU_")

    @property
    def enabled(self) -> bool:
        """Return True if app credentials are configured.

        receive_id is optional — FeishuClient will auto-resolve it from
        the bot's contact scope if not set.
        """
        return bool(self.app_id and self.app_secret)

    @property
    def receive_id_type(self) -> str:
        """Infer receive_id_type from the ID prefix."""
        if self.receive_id.startswith("oc_"):
            return "chat_id"
        if self.receive_id.startswith("ou_"):
            return "open_id"
        if self.receive_id.startswith("on_"):
            return "union_id"
        return "chat_id"


class AutoScaleSettings(BaseSettings):
    """Auto-scaling settings for ASG-based deployment.

    When enabled, the API periodically checks the pending task count
    and directly adjusts the ASG desired capacity via boto3.
    Min/max are synced to the ASG on startup so .env is the single
    source of truth — no need to redeploy CDK to change limits.
    """

    enabled: bool = False
    asg_name: str = ""
    min_instances: int = 0
    max_instances: int = 5
    tasks_per_worker: int = 1
    check_interval: float = 60.0  # seconds between scaling checks
    scale_down_cooldown: int = 5  # consecutive low-task cycles before scale-down

    model_config = SettingsConfigDict(env_prefix="AUTOSCALE_")


class ServiceSettings(BaseSettings):
    """Top-level service configuration."""

    db: DatabaseSettings = DatabaseSettings()
    worker: WorkerSettings = WorkerSettings()
    browser: BrowserSettings = BrowserSettings()
    s3: S3Settings = S3Settings()
    feishu: FeishuSettings = FeishuSettings()
    autoscale: AutoScaleSettings = AutoScaleSettings()
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    model_config = SettingsConfigDict(env_prefix="SCRAPER_")
