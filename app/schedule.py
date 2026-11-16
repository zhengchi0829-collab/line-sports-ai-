"""Fixed Taiwan-time refresh slots; no paid dependency or server clock assumptions."""
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
ANALYSIS_TIMES = ((9, 0), (15, 0), (20, 0))
PLATFORM_START = dtime(0, 30)
PLATFORM_CATCHUP_END = dtime(3, 0)


def latest_analysis_slot(now: datetime) -> str:
    """Most recent planned analysis refresh slot, including 20:00 yesterday."""
    local = now.astimezone(TZ)
    slots = [local.replace(hour=hour, minute=minute, second=0, microsecond=0)
             for hour, minute in ANALYSIS_TIMES]
    ready = [slot for slot in slots if slot <= local]
    if ready:
        return ready[-1].isoformat()
    return (slots[-1] - timedelta(days=1)).isoformat()


def platform_refresh_window(now: datetime) -> bool:
    """Only 00:30-03:00 Taiwan: allow short delayed wake-ups, not afternoon spend."""
    local = now.astimezone(TZ)
    return PLATFORM_START <= local.time().replace(tzinfo=None) < PLATFORM_CATCHUP_END


def platform_day(now: datetime) -> str:
    return now.astimezone(TZ).date().isoformat()
