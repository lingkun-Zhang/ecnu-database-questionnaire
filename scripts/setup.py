"""Generate stable local secrets once. This script never resets business data."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
env = ROOT / ".env"
if env.exists():
    print("项目配置已存在，保留原有密钥和数据库设置。")
else:
    lines = ["MONGO_URI=mongodb://127.0.0.1:27117/?replicaSet=rs0", "MONGO_DATABASE=survey_phase1"]
    lines += [f"{name}={secrets.token_urlsafe(48)}" for name in
              ["AUTH_SECRET", "FILL_SECRET", "PARTICIPANT_SECRET"]]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已生成项目独立配置。密钥未输出；重启时保留此文件。")
