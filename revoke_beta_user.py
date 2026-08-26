from __future__ import annotations

import sys
import uuid

import database


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python revoke_beta_user.py <uuid>")
        return 2
    try:
        user_id = str(uuid.UUID(sys.argv[1].strip()))
    except ValueError:
        print("无效 UUID。")
        return 2
    database.init_db()
    if not database.revoke_user(user_id):
        print("用户不存在或已经撤销。")
        return 1
    print(f"已撤销用户: {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
