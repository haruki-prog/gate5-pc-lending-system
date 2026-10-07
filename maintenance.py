"""Explicit, audited hold maintenance; no arbitrary device status rewrite."""
import argparse
import json
from pathlib import Path

from app import Store, ROOT, stamp, encode


def change_hold(store, device_id, action, operator, reason):
    if action not in {"hold", "release"} or not operator.strip() or not 1 <= len(reason.strip()) <= 200:
        raise ValueError("担当者と1～200文字の根拠を指定してください。")
    with store.transaction() as db:
        device = store.device(db, device_id)
        before = db.execute("SELECT * FROM device_holds WHERE device_id=?", (device_id,)).fetchone()
        now = stamp(store.clock())
        if action == "release":
            expected = 1 if device["status"] == "LENT" else 0
            if device["open_count"] != expected:
                raise ValueError("未返却件数と状態が一致しません。調査・訂正を完了するまで解除できません。")
            if not before or before["released_at"] is not None:
                raise ValueError("有効な利用保留がありません。")
            db.execute("UPDATE device_holds SET released_at=? WHERE device_id=?", (now, device_id))
        else:
            db.execute("INSERT INTO device_holds VALUES(?,?,?,NULL) ON CONFLICT(device_id) DO UPDATE SET reason=excluded.reason,held_at=excluded.held_at,released_at=NULL",
                       (device_id, reason.strip(), now))
        after = dict(db.execute("SELECT * FROM device_holds WHERE device_id=?", (device_id,)).fetchone())
        db.execute("INSERT INTO maintenance_audit(device_id,operator,reason,before_json,after_json,changed_at) VALUES(?,?,?,?,?,?)",
                   (device_id, operator.strip(), reason.strip(), encode(dict(before) if before else None), encode(after), now))
        return after


def main():
    parser = argparse.ArgumentParser(description="調査根拠を記録して端末の利用保留を設定・解除します。")
    parser.add_argument("action", choices=["hold", "release"])
    parser.add_argument("device_id", type=int)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True, help="実機・本人・台帳を照合した根拠")
    parser.add_argument("--db", type=Path, default=ROOT/"gate5.db")
    args = parser.parse_args()
    if not args.db.exists():
        parser.error("既存のDBを指定してください。")
    print(json.dumps(change_hold(Store(args.db), args.device_id, args.action, args.operator, args.reason), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
