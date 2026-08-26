from __future__ import annotations

import os
import sys
from urllib.parse import urlencode

import database


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print('用法: python create_beta_user.py "显示名称"')
        return 2
    database.init_db()
    user_id = database.create_user(sys.argv[1])
    base_url = os.getenv("BETA_BASE_URL", "http://localhost:8501/").rstrip("/") + "/"
    print(f"用户: {sys.argv[1].strip()}")
    print(f"UUID: {user_id}")
    print(f"邀请链接: {base_url}?{urlencode({'user_id': user_id})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
