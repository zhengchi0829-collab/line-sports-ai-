"""Fixed Taiwan-time refresh slots; no paid dependency or server clock assumptions."""
from calendar import monthrange
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


def platform_credit_budget(remaining: int, now: datetime, reserve: int = 3) -> int:
    """Conserve a shared 500-credit monthly quota through the remaining days.

    Charge estimates apply to one US region, one credit per market/league.
    A price request may use fewer credits if a league has no listed events.
    """
    local = now.astimezone(TZ)
    days_remaining = monthrange(local.year, local.month)[1] - local.day + 1
    if not isinstance(remaining, int) or remaining < 1:
        return 0
    return max(0, (remaining - reserve) // days_remaining)


def platform_market_plan(supported, budget: int, now: datetime):
    """Pick 1X2/h2h across sports first, enrich only if budget allows.

    Returned (league, API-key, market CSV) never spends more than budget
    even if every supported league has actual odds in the API response.
    """
    if not supported or budget < 1:
        return []
    # Rotate non-core leagues by date so the same minor market is not always
    # omitted when free credits are scarce; prioritize major NBA/EPL.
    priority = ("NBA", "英超", "MLB", "NCAA", "歐冠", "西甲", "德甲", "義甲", "法甲")
    index = {label: key for label, key in supported}
    core = [label for label in ("NBA", "英超") if label in index]
    others = [label for label in priority if label in index and label not in core]
    others += [label for label in index if label not in core and label not in others]
    shift = (now.astimezone(TZ).timetuple().tm_yday - 1) % len(others) if others else 0
    ordered = core + others[shift:] + others[:shift]
    selected = ordered[:budget]
    remaining = budget - len(selected)
    markets = {league: ["h2h"] for league in selected}
    # Only request spreads/totals where the remaining free allowance permits.
    for league in selected:
        if remaining >= 2:
            markets[league].extend(("spreads", "totals"))
            remaining -= 2
        elif remaining == 1:
            markets[league].append("totals")
            remaining -= 1
    return [(league, index[league], ",".join(markets[league]))
            for league in selected]
