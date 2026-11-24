"""学校密码和登录凭据的加密。密钥放在数据库之外的单独文件里，只有服务进程能读。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class MissingKey(Exception):
    pass


class Vault:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def load(cls, path: Path) -> Vault:
        if not path.is_file():
            raise MissingKey(f"找不到密钥文件 {path}。请先运行 mus-booking init 生成它。")
        return cls(path.read_bytes().strip())

    @staticmethod
    def create(path: Path) -> bool:
        """生成密钥文件；已经存在时不覆盖，返回是否新建。"""
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key() + b"\n")
        return True

    def seal(self, value: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def open(self, token: bytes) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(token))
        except InvalidToken as error:
            raise MissingKey("密钥与数据库不匹配，无法解开保存的密码") from error
