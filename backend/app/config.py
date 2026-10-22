from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    database: str
    auth_secret: str = field(repr=False)
    fill_secret: str = field(repr=False)
    participant_secret: str = field(repr=False)
    access_seconds: int = 43200
    fill_seconds: int = 7200
    transaction_seconds: int = 20
    testing: bool = False

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        secrets = [os.getenv(name, "") for name in
                   ("AUTH_SECRET", "FILL_SECRET", "PARTICIPANT_SECRET")]
        if any(len(value) < 32 or "CHANGE_ME" in value for value in secrets):
            raise RuntimeError("请先运行 scripts/setup.py 生成项目独立配置，或配置三个至少32字符的密钥。")
        return cls(
            mongo_uri=os.getenv("MONGO_URI", "mongodb://127.0.0.1:27117/?replicaSet=rs0"),
            database=os.getenv("MONGO_DATABASE", "survey_phase1"),
            auth_secret=secrets[0], fill_secret=secrets[1], participant_secret=secrets[2],
            access_seconds=int(os.getenv("ACCESS_SECONDS", "43200")),
            fill_seconds=int(os.getenv("FILL_SECONDS", "7200")),
        )
