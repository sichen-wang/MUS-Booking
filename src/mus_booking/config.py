"""运行配置。全部来自环境变量，默认值适合在开发机上直接运行。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    key_file: Path
    public_url: str
    """对外的完整地址，例如 https://13.140.76.154/booking，用于命令行打印链接。"""

    @property
    def database(self) -> Path:
        return self.data_dir / "mus-booking.db"

    @classmethod
    def from_env(cls) -> Settings:
        data = Path(os.environ.get("MUS_BOOKING_DATA", "data")).resolve()
        key = Path(os.environ.get("MUS_BOOKING_KEY", str(data / "secret.key"))).resolve()
        url = os.environ.get("MUS_BOOKING_URL", "http://127.0.0.1:8000").rstrip("/")
        return cls(data, key, url)
