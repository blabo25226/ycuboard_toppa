"""巡回時刻まわりの計算。"""
import random
from datetime import datetime, timedelta
from typing import List, Optional

from src.config import load_config
from src.state import load_state, update_state

JITTER_KEEP_DAYS = 3  # 保存しておく誤差の日数（古いものは捨てる）


def draw_offset(max_minutes: float, sigma_minutes: float, previous_seconds: Optional[int] = None) -> timedelta:
    """±max_minutes の範囲に切り詰めた正規分布（標準偏差 sigma_minutes）から誤差を引く。
    previous_seconds（同じ枠の前回の誤差）と同じ値にならないよう、重なったら引き直す。"""
    if max_minutes <= 0:
        return timedelta(0)
    sigma = max(float(sigma_minutes), 0.1)
    seconds = 0
    for _ in range(200):
        x = random.gauss(0, sigma)
        if abs(x) > max_minutes:
            continue  # 範囲外は捨てて引き直す（端に溜まらないよう、丸めずに棄却する）
        seconds = round(x * 60)
        if seconds != previous_seconds:
            break
    else:
        seconds = int(max(-max_minutes, min(max_minutes, random.gauss(0, sigma))) * 60)
    return timedelta(seconds=seconds)


def planned_for(nominal: datetime, create: bool = False, cfg: Optional[dict] = None) -> datetime:
    """設定時刻（nominal）に誤差を加えた、実際の実行予定時刻。

    誤差は予定ごとに一度だけ引いて data/state.json に保存する（ループのたびに変わらない／再起動しても同じ）。
    create=False では新しく引かず、未保存なら nominal を返す（診断など、状態を書き換えたくない用途）。
    誤差の設定がOFFのときは常に nominal。
    """
    cfg = cfg or load_config()
    if not cfg.get("jitter_enabled", True):
        return nominal
    state = load_state()
    planned = dict(state.get("planned", {}))
    key = nominal.isoformat(timespec="minutes")
    if key in planned:
        try:
            return datetime.fromisoformat(planned[key])
        except ValueError:
            pass
    if not create:
        return nominal

    hm = nominal.strftime("%H:%M")
    last = dict(state.get("jitter_last", {}))
    offset = draw_offset(cfg.get("jitter_max_minutes", 10), cfg.get("jitter_sigma_minutes", 4), last.get(hm))
    result = nominal + offset
    planned[key] = result.isoformat(timespec="seconds")
    last[hm] = int(offset.total_seconds())
    cutoff = nominal - timedelta(days=JITTER_KEEP_DAYS)
    planned = {k: v for k, v in planned.items() if _parse(k) is None or _parse(k) >= cutoff}
    update_state(planned=planned, jitter_last=last)
    return result


def _parse(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def last_due_slot(times: List[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """今（now）までに到来した、直近の巡回予定時刻（昨日・今日から探す）。誤差が決まっていればその分を含む。"""
    now = now or datetime.now()
    slots = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for hm in times:
            slot = planned_for(datetime.combine(day, datetime.strptime(hm, "%H:%M").time()))
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


def next_slot(times: List[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """これから最初に来る巡回予定時刻（今日・明日から探す）。"""
    now = now or datetime.now()
    slots = []
    for day in (now.date(), now.date() + timedelta(days=1)):
        for hm in times:
            slot = planned_for(datetime.combine(day, datetime.strptime(hm, "%H:%M").time()))
            if slot > now:
                slots.append(slot)
    return min(slots) if slots else None
