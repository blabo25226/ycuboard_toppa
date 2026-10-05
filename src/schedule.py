"""巡回時刻まわりの計算。"""
from datetime import datetime, timedelta
from typing import List, Optional

from src.config import load_config


def last_due_slot(times: List[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """今（now）までに到来した、直近の巡回予定時刻（昨日・今日から探す）。"""
    now = now or datetime.now()
    slots = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for hm in times:
            slot = datetime.combine(day, datetime.strptime(hm, "%H:%M").time())
            if slot <= now:
                slots.append(slot)
    return max(slots) if slots else None


def in_maintenance(when: datetime) -> bool:
    """when の時刻が YCU-Board の定期メンテナンス（既定: 毎週火曜 1:00〜6:00）に当たるか。"""
    mw = load_config()["maintenance_window"]
    if not mw.get("enabled", True):
        return False
    return when.weekday() == mw["weekday"] and mw["start_hour"] <= when.hour < mw["end_hour"]


def missed_slot(last_run_iso: Optional[str], times: List[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """最終実行より後に到来していたのに実行されていない予定時刻（メンテナンス中は除く）。無ければ None。"""
    due = last_due_slot(times, now)
    if due is None or not last_run_iso or in_maintenance(due):
        return None  # 一度も実行していない場合は取りこぼしとは見なさない
    try:
        last_run = datetime.fromisoformat(last_run_iso)
    except ValueError:
        return None
    return due if last_run < due else None
