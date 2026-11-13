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
SPORT_IDS = {"MLB": (1,), "NPB": (31,), "KBO": (32,), "MiLB": (11, 12, 13, 14, 16)}
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



CPBL_TEAMS = {
    "AAA":"味全龍", "AEO":"富邦悍將", "AKP":"台鋼雄鷹",
    "ACN":"中信兄弟", "AJL":"樂天桃猿", "ADD":"統一7-ELEVEn獅",
}


async def verified_cpbl_analysis(client, home, away, translate=None):
    """Use the dated CPBL 2026 snapshot. Historical only; never call it live."""
    try:
        response = await client.get(
            "https://raw.githubusercontent.com/pochih/cpbl2026/main/cpbl_data.json",
            timeout=3.5)
        response.raise_for_status()
        document=response.json()
        if not isinstance(document,dict):
            return UNAVAILABLE
        lookup=document.get("teams") or {}
        def match_code(wanted):
            scored=[]
            for code,record in lookup.items():
                if not isinstance(record,dict):continue
                aliases=[record.get("name",""),record.get("abbr",""),CPBL_TEAMS.get(code,"")]
                points=max((similarity(n,wanted,translate) for n in aliases),default=0)
                scored.append((points,code))
            return max(scored,default=(0,None))
        h_score,h_code=match_code(home)
        a_score,a_code=match_code(away)
        if min(h_score,a_score)<0.72 or not h_code or not a_code or h_code==a_code:
            return UNAVAILABLE
        groups=[]
        for code in (h_code,a_code):
            played=[]
            for game in document.get("games",[]):
                if not isinstance(game,dict):continue
                if str(game.get("status","")).lower() not in ("final","finished"):
                    continue
                if code not in (game.get("home"),game.get("away")):continue
                try:
                    hs,ass=int(game["hs"]),int(game["as"])
                    if hs<0 or ass<0:continue
                except (ValueError,TypeError,KeyError):
                    continue
                home_side=code==game.get("home")
                own,other=(hs,ass) if home_side else (ass,hs)
                outcome="W" if own>other else "L" if own<other else "D"
                played.append((str(game.get("date") or ""),outcome,float(own),float(other)))
            played.sort(key=lambda x:x[0],reverse=True)
            groups.append(played[:5])
        if any(len(group)<5 for group in groups):
            return UNAVAILABLE
        records=[(sum(g[1]=="W" for g in group),
                  sum(g[1]=="D" for g in group),
                  sum(g[1]=="L" for g in group)) for group in groups]
        rates=[x[0]/5 for x in records]
        net=[sum(x[2]-x[3] for x in group)/5 for group in groups]
        if rates[0]==rates[1] and net[0]==net[1]:
            return ("兩隊歷史最近五場勝率和淨得失分相同，暫不推薦。",
                    "球員排行可查詢，但本場暫無主力推薦。",None,None)
        index=0 if (rates[0],net[0])>(rates[1],net[1]) else 1
        team=(home,away)[index]
        code=(h_code,a_code)[index]
        stamp=str(document.get("updated") or "未提供")
        def show_record(name, record):
            wins, draws, losses = record
            return f"{name}：近5場 {wins}勝{draws}和{losses}敗｜勝率 {wins*20:.0f}%"
        history=(show_record(home,records[0])+"\n"+
                 show_record(away,records[1])+"\n"+
                 f"歷史戰績較佳：{team}｜資料截至 {stamp[:16]}")
        weights=[5,4,3,2,1]
        attack=[sum(w*g[2] for w,g in zip(weights,group))/15 for group in groups]
        defense=[sum(w*g[3] for w,g in zip(weights,group))/15 for group in groups]
        expected_home=(attack[0]+defense[1])/2
        expected_away=(attack[1]+defense[0])/2
        total=expected_home+expected_away
        margin=expected_home-expected_away
        baseline=sum(x[2]+x[3] for group in groups for x in group)/10
        model_line=math.floor(baseline)+0.5
        tendency=("大" if total>model_line+0.15 else "小" if total<model_line-0.15 else "接近參考線")
        favored=home if margin>=0 else away
        metrics={"total":f"{tendency} {model_line:.1f} 分（歷史模型參考）｜預估 {total:.1f} 分",
                 "handicap":f"{favored} -1.5 分（模型"+("支持" if abs(margin)>1.8 else "差距不足")+"）",
                 "expected_total":total,"expected_margin":margin}
        named=[]
        for group in (document.get("leaders") or {}).values():
            if not isinstance(group,dict):continue
            for category,leaders in group.items():
                if not isinstance(leaders,list):continue
                for item in leaders:
                    if not isinstance(item,list) or len(item)<2:continue
                    if item[1]==code and item[0] not in named:
                        named.append(str(item[0]))
        players=(f"{team}｜2026 賽季數據榜上球員（非確定先發）："+ "、".join(named[:5])
                 if named else f"{team}｜沒有可核實的主力球員資料。")
        return history,players,team,metrics
    except (httpx.HTTPError,ValueError,TypeError,KeyError,IndexError) as exc:
        logger.warning("cpbl_history_unavailable kind=%s",type(exc).__name__)
        return UNAVAILABLE

ARCHIVES = {
    "NPB": "https://raw.githubusercontent.com/dentearl/npb-data/main/data/npb/{year}.json",
    "KBO": "https://raw.githubusercontent.com/dentearl/kbo-data/main/data/kbo/{year}.json",
}

async def verified_asian_baseball_analysis(client, league, home, away, translate=None):
    """Compare actual completed games from the selected league's dated archive.

    The archive is a third-party mirror, not official live odds or lineup data.
    Never mix another league's data or pretend that listed players are confirmed starters.
    """
    year = datetime.now(timezone.utc).year
    try:
        responses = await asyncio.gather(*(
            client.get(ARCHIVES[league].format(year=y), timeout=4)
            for y in (year, year - 1)), return_exceptions=True)
        rows = []
        for response in responses:
            if isinstance(response, BaseException) or response.status_code != 200:
                continue
            value = response.json()
            if isinstance(value, list):
                rows.extend(value)
        teams = {}
        for game in rows:
            if not isinstance(game, dict):
                continue
            for side in ("home", "away"):
                team = ((game.get("teams") or {}).get(side) or {}).get("team") or {}
                if team.get("id") is not None and team.get("name"):
                    teams[str(team["id"])] = team["name"]
        selected = []
        for wanted in (home, away):
            ranked = sorted(
                ((similarity(name, wanted, translate), tid, name)
                 for tid, name in teams.items()), reverse=True)
            if not ranked or ranked[0][0] < 0.74:
                logger.warning("asian_baseball_unknown_team league=%s team=%s", league, wanted[:35])
                return UNAVAILABLE
            selected.append(ranked[0])
        if selected[0][1] == selected[1][1]:
            return UNAVAILABLE
        groups = []
        for _, tid, _ in selected:
            matches = []
            for game in rows:
                if not isinstance(game, dict):
                    continue
                status = game.get("status") or {}
                if str(status.get("abstractGameState", "")).lower() not in ("f", "final"):
                    continue
                sides = game.get("teams") or {}
                h, a = sides.get("home") or {}, sides.get("away") or {}
                home_id = str((h.get("team") or {}).get("id"))
                away_id = str((a.get("team") or {}).get("id"))
                if tid not in (home_id, away_id):
                    continue
                own, other = (h, a) if tid == home_id else (a, h)
                mine, theirs = verified_score(own), verified_score(other)
                if mine is None or theirs is None:
                    continue
                date = str(game.get("gameDate") or "")
                if date[:10] > datetime.now(timezone.utc).date().isoformat():
                    continue
                result = "W" if mine > theirs else "L" if mine < theirs else "D"
                matches.append((date, result, mine, theirs))
            matches.sort(key=lambda x: x[0], reverse=True)
            groups.append(matches[:5])
        if any(len(group) < 5 for group in groups):
            logger.warning("asian_baseball_insufficient_history league=%s", league)
            return UNAVAILABLE
        records = [(sum(g[1] == "W" for g in group),
                    sum(g[1] == "D" for g in group),
                    sum(g[1] == "L" for g in group)) for group in groups]
        diff = [sum(g[2] - g[3] for g in group) / 5 for group in groups]
        tied = (records[0][0], diff[0]) == (records[1][0], diff[1])
        fav_index = 0 if (records[0][0], diff[0]) > (records[1][0], diff[1]) else 1
        winner = None if tied else (home, away)[fav_index]
        def record_text(name, record, matches):
            wins, draws, losses = record
            return (f"{name}｜最近5場 {wins}勝{draws}和{losses}敗"
                    f"｜歷史勝率 {wins * 20:.0f}%｜最近一戰 {matches[0][0][:10]}")
        history = (record_text(home, records[0], groups[0]) + "\n" +
                   record_text(away, records[1], groups[1]) +
                   "\n近期戰績較佳：" + (winner or "雙方持平"))
        weights = (5, 4, 3, 2, 1)
        attack = [sum(w * g[2] for w, g in zip(weights, group)) / 15 for group in groups]
        defense = [sum(w * g[3] for w, g in zip(weights, group)) / 15 for group in groups]
        projected_home = (attack[0] + defense[1]) / 2
        projected_away = (attack[1] + defense[0]) / 2
        projected_total = projected_home + projected_away
        projected_margin = projected_home - projected_away
        average_total = sum(g[2] + g[3] for group in groups for g in group) / 10
        reference = math.floor(average_total) + 0.5
        tendency = ("偏大" if projected_total > reference + 0.15 else
                    "偏小" if projected_total < reference - 0.15 else "接近參考線")
        favored = home if projected_margin >= 0 else away
        metrics = {
            "total": f"{tendency} {reference:.1f} 分（歷史模型線）｜預估 {projected_total:.1f} 分",
            "handicap": f"{favored} -1.5 分（歷史模型，"
                        + ("差距足夠" if abs(projected_margin) > 1.8 else "差距不足") + "）",
            "expected_total": projected_total,
            "expected_margin": projected_margin,
        }
        players = (f"{league}｜該資料來源只核實賽程與比分，"
                   "尚無可核實的先發投手、登錄球員或傷停名單。")
        return history, players, winner, metrics
    except (httpx.HTTPError, TypeError, ValueError, KeyError, IndexError) as exc:
        logger.warning("asian_baseball_history_error league=%s kind=%s",
                       league, type(exc).__name__)
        return UNAVAILABLE


async def verified_baseball_analysis(client, league, home, away, translate=None, team_ids=None, sport_id=None):
    """Return (form, roster, winner, metrics) with no fake observed statistics."""
    if league == "CPBL":
        return await verified_cpbl_analysis(client,home,away,translate)
    if league in ARCHIVES:
        return await verified_asian_baseball_analysis(client, league, home, away, translate)
    sport_ids = SPORT_IDS.get(league)
    if sport_ids is None:
        return UNAVAILABLE
    base = "https://statsapi.mlb.com/api/v1"
    try:
        # When a user selects an official MiLB fixture, use its exact team IDs.
        # MiLB roster/team searches can omit upcoming-season teams.
        direct_ids = (list(team_ids) if team_ids and len(team_ids) == 2
                      and all(str(x).isdigit() for x in team_ids) else None)
        # MiLB covers five levels; avoid treating aggregate sportId 21 as a team league.
        async def load_teams(sid):
            response = await client.get(base + "/teams",
                                        params={"sportId": sid}, timeout=3)
            response.raise_for_status()
            return [(team, sid) for team in response.json().get("teams", [])]
        matches = []
        if direct_ids:
            level = int(sport_id) if sport_id in sport_ids else None
            matches = [({"id": int(tid)}, level) for tid in direct_ids]
        else:
            team_responses = await asyncio.gather(
                *(load_teams(sid) for sid in sport_ids), return_exceptions=True)
            teams = [entry for response in team_responses if isinstance(response, list)
                     for entry in response]
            if not teams:
                return UNAVAILABLE
            for wanted in (home, away):
                def match_score(team):
                    aliases = [team.get(k,"") for k in
                               ("name", "teamName", "clubName", "abbreviation", "shortName")]
                    aliases.append((team.get("locationName") or "") + " " + (team.get("teamName") or ""))
                    return max((similarity(v, wanted, translate) for v in aliases), default=0.0)
                chosen, selected_sid = max(teams, key=lambda entry: match_score(entry[0]))
                if match_score(chosen) < 0.74 or not chosen.get("id"):
                    logger.info("baseball_form_unknown_team league=%s name=%s best=%s",
                                league, wanted, chosen.get("name"))
                    return UNAVAILABLE
                matches.append((chosen, selected_sid))
        if matches[0][0]["id"] == matches[1][0]["id"]:
            return UNAVAILABLE
        now=datetime.now(timezone.utc)
        start=(now-timedelta(days=230)).date().isoformat()
        end=now.date().isoformat()
        async def schedule(entry):
            team, sid = entry
            params = {"teamId": team["id"], "startDate": start, "endDate": end}
            if sid is not None:
                params["sportId"] = sid
            resp=await client.get(base + "/schedule",params=params,timeout=4)
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
        tied = rates[0] == rates[1] and net[0] == net[1]
        index = 0 if (rates[0],net[0]) > (rates[1],net[1]) else 1
        winner = None if tied else (home, away)[index]
        # Show both verified teams; showing only the selected side hid the comparison.
        def form_line(team_name, record, group):
            wins, draws, losses = record
            last_date = group[0][0][:10] if group else "未知"
            return (f"{team_name}：近5場 {wins}勝{draws}和{losses}敗"
                    f"｜勝率 {wins * 20:.0f}%｜最近一戰 {last_date}")
        form = ("已核實歷史賽果（非勝率預測）\n" +
                form_line(home, records[0], games[0]) + "\n" +
                form_line(away, records[1], games[1]) +
                "\n近期戰績較佳：" + (winner or "雙方相同，暫不推薦"))
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
        async def fetch_roster(entry):
            try:
                team, _ = entry
                response = await client.get(base + f"/teams/{team['id']}/roster",
                                            params={"rosterType": "active"}, timeout=3)
                response.raise_for_status()
                names = []
                for item in response.json().get("roster", []):
                    if isinstance(item, dict):
                        name = (item.get("person") or {}).get("fullName")
                        if name and name not in names:
                            names.append(name)
                return names[:4]
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                return []
        rosters = await asyncio.gather(*(fetch_roster(entry) for entry in matches))
        players = "\n".join(
            f"{name}｜登錄球員（非確定先發）：{', '.join(roster)}" if roster
            else f"{name}｜暫無可核實登錄名單"
            for name, roster in zip((home, away), rosters))
        return form,players,winner,metrics
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        logger.warning("baseball_form_error league=%s kind=%s",league,type(exc).__name__)
        return UNAVAILABLE
