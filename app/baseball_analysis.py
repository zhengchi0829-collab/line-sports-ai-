"""Historical five-game baseball analysis for supported league sources.

Only finished games with verified scores qualify. No invented sportsbook markets.
"""
import asyncio
import difflib
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("sports_bot")
SPORT_IDS = {"MLB": 1, "NPB": 31, "KBO": 32, "MiLB": 21}
UNAVAILABLE = ("尚無可核實的該聯盟歷史最近五場戰績，暫不顯示勝率。",
               "目前沒有可核實的該聯盟球員資料。", None, None)


def normalize(value):
    return re.sub(r"[^\w]", "", str(value or "")).casefold()


def similarity(actual, wanted, translate=None):
    actual = str(actual or "")
    wanted = str(wanted or "")
    aa, bb = normalize(actual), normalize(wanted)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    options = [actual]
    if translate:
        options.append(translate(actual))
    return max(difflib.SequenceMatcher(None, normalize(name), bb).ratio()
               for name in options if normalize(name))


def verified_score(obj):
    try:
        score = obj.get("score")
        if score is None:
            return None
        value = float(score)
        return value if value >= 0 and value.is_integer() else None
    except (ValueError, TypeError, AttributeError):
        return None


def finished_matches(payload, team_id):
    results = []
    if not isinstance(payload, dict):
        return results
    for day in payload.get("dates", []):
        for game in day.get("games", []):
            status = game.get("status") or {}
            code = str(status.get("codedGameState", ""))
            abstract = str(status.get("abstractGameState", ""))
            detailed = str(status.get("detailedState", "")).lower()
            if code != "F" and abstract != "Final" and "final" not in detailed:
                continue
            sides = game.get("teams") or {}
            home, away = sides.get("home") or {}, sides.get("away") or {}
            my_home = str((home.get("team") or {}).get("id")) == str(team_id)
            my_away = str((away.get("team") or {}).get("id")) == str(team_id)
            if not my_home and not my_away:
                continue
            my_score = verified_score(home if my_home else away)
            opp_score = verified_score(away if my_home else home)
            if my_score is None or opp_score is None:
                continue
            outcome = "W" if my_score > opp_score else "L" if my_score < opp_score else "D"
            results.append((str(game.get("gameDate") or ""), outcome, my_score, opp_score))
    return sorted(results, key=lambda row: row[0], reverse=True)[:5]


async def verified_baseball_analysis(client, league, home, away, translate=None):
    """Return (form, roster, winner, metrics) with no fake observed statistics."""
    sid = SPORT_IDS.get(league)
    if sid is None:
        # CPBL must use its own verified scores; MLB Stats API is not a CPBL feed.
        return UNAVAILABLE
    base = "https://statsapi.mlb.com/api/v1"
    try:
        response = await client.get(base + "/teams", params={"sportId": sid}, timeout=3)
        response.raise_for_status()
        teams = response.json().get("teams", [])
        if not teams:
            return UNAVAILABLE
        matches = []
        for wanted in (home, away):
            def match_score(team):
                aliases = [team.get(k,"") for k in
                           ("name", "teamName", "clubName", "abbreviation", "shortName")]
                aliases.append((team.get("locationName") or "") + " " + (team.get("teamName") or ""))
                return max((similarity(v, wanted, translate) for v in aliases), default=0.0)
            chosen = max(teams, key=match_score)
            if match_score(chosen) < 0.74 or not chosen.get("id"):
                logger.info("baseball_form_unknown_team league=%s",league)
                return UNAVAILABLE
            matches.append(chosen)
        if matches[0]["id"] == matches[1]["id"]:
            return UNAVAILABLE
        now=datetime.now(timezone.utc)
        start=(now-timedelta(days=230)).date().isoformat()
        end=now.date().isoformat()
        async def schedule(team):
            resp=await client.get(base + "/schedule",params={
                "sportId":sid,"teamId":team["id"],"startDate":start,"endDate":end},
                timeout=4)
            resp.raise_for_status()
            return finished_matches(resp.json(), team["id"])
        games = await asyncio.gather(*(schedule(t) for t in matches),return_exceptions=True)
        if any(isinstance(g,BaseException) for g in games):
            logger.warning("baseball_form_schedule_unavailable league=%s",league)
            return UNAVAILABLE
        if any(len(g)<5 for g in games):
            return UNAVAILABLE
        records = [(sum(row[1]=="W" for row in g),sum(row[1]=="D" for row in g),
                    sum(row[1]=="L" for row in g)) for g in games]
        rates = [record[0]/5 for record in records]
        net = [sum(x-y for _,_,x,y in group)/5 for group in games]
        if rates[0] == rates[1] and net[0] == net[1]:
            return ("兩隊歷史最近五場勝率相同，且淨得失分相同；暫無獨贏推薦。",
                    "球員名單依資料來源提供。",None,None)
        index = 0 if (rates[0],net[0]) > (rates[1],net[1]) else 1
        winner = (home, away)[index]
        wins, draws, losses = records[index]
        form=f"{winner}｜歷史最近五場：{wins}勝、{draws}和、{losses}敗｜勝率 {wins*20:.1f}%"
        # Expected scores use recent-game weighted runs for/against (5 most recent first).
        weights=[5,4,3,2,1]
        points_for=[sum(w*g[2] for w,g in zip(weights,group))/15 for group in games]
        points_against=[sum(w*g[3] for w,g in zip(weights,group))/15 for group in games]
        expected_home=(points_for[0]+points_against[1])/2
        expected_away=(points_for[1]+points_against[0])/2
        expected_total=expected_home+expected_away
        expected_margin=expected_home-expected_away
        unweighted=sum(g[2]+g[3] for group in games for g in group)/10
        model_line=math.floor(unweighted)+0.5
        tendency=("大" if expected_total>model_line+0.15 else
                  "小" if expected_total<model_line-0.15 else "接近參考線")
        favored=home if expected_margin>=0 else away
        spread=1.5
        coverage=("支持" if abs(expected_margin)>spread+0.3 else "差距不足")
        metrics={"total":f"{tendency} {model_line:.1f} 分（歷史模型參考）｜預估 {expected_total:.1f} 分",
                 "handicap":f"{favored} -{spread:.1f} 分（模型 {coverage}）",
                 "expected_total":expected_total,"expected_margin":expected_margin}
        names=[]
        try:
            roster=await client.get(base+f"/teams/{matches[index]['id']}/roster",
                                    params={"rosterType":"active"},timeout=3)
            if roster.status_code==200:
                for item in roster.json().get("roster",[]):
                    if isinstance(item,dict):
                        name=(item.get("person") or {}).get("fullName")
                        if name and name not in names:
                            names.append(name)
        except httpx.HTTPError:
            pass
        players=(f"{winner}｜球隊登錄球員（非確定先發）："+ "、".join(names[:5])
                 if names else f"{winner}｜目前沒有可核實的登錄球員名單。")
        return form,players,winner,metrics
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        logger.warning("baseball_form_error league=%s kind=%s",league,type(exc).__name__)
        return UNAVAILABLE
