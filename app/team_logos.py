"""Stable LINE-safe team badge renderer.

All LINE cards use this service's fixed HTTPS PNG endpoint. Official/team
images are fetched from known public sports image hosts. When the source is
missing, broken, blocked or unsupported, a clear team-identity badge is drawn
locally instead of sending LINE a broken external image URL.

These fallback badges are NOT presented as official club logos.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
import unicodedata
from urllib.parse import urlencode, urlparse, quote

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

BASE_URL = "https://line-sports-ai-bot.onrender.com/team-logo"
LOGO_CACHE: dict[tuple[str, str, str], tuple[float, bytes, bool]] = {}
_FETCH_LIMIT = asyncio.Semaphore(6)

# Exact names appearing in Taiwan Sports Lottery daily screenshot rows.
SOCCER_ALIASES = {
    "拉賈斯坦聯": "Rajasthan United",
    "薩馬萊斯瓦里": "Samaleswari",
    "休達": "AD Ceuta FC",
    "特內里費": "CD Tenerife",
    "艾維利斯": "Real Aviles",
    "薩莫拉 CF": "Zamora CF",
    "薩莫拉CF": "Zamora CF",
    "亞美尼亞": "Armenia",
    "拉脫維亞": "Latvia",
    "蒙特內哥羅": "Montenegro",
    "賽普勒斯": "Cyprus",
    "科英布拉大學": "Academica de Coimbra",
    "波蒂莫內斯": "Portimonense",
}
# Identifiable club badge assets used only for the exact named club.
KNOWN_BADGES = {
    "拉賈斯坦聯": "https://images.mykhel.com/webp/common_dynamic/images/common/desk/football_flags/mk-2103.png?v=1",
    "休達": "https://assets.football-logos.cc/logos/spain/700x700/ceuta.a195364b.png",
    "特內里費": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/CD_Tenerife_logo.png/320px-CD_Tenerife_logo.png",
    "艾維利斯": "https://images.seeklogo.com/logo-png/11/1/real-aviles-industrial-logo-png_seeklogo-116357.png",
    "薩莫拉 CF": "https://assets.footylogos.com/previews/zamora-cf-logo-footylogos-1200.webp",
    "薩莫拉CF": "https://assets.footylogos.com/previews/zamora-cf-logo-footylogos-1200.webp",
    "科英布拉大學": "https://images.seeklogo.com/logo-png/30/1/academica-coimbra-logo-png_seeklogo-304394.png",
    "波蒂莫內斯": "https://images.seeklogo.com/logo-png/55/1/portimonense-s-c-logo-png_seeklogo-555856.png",
    "蒙特內哥羅": "https://images.seeklogo.com/logo-png/5/1/football-association-of-montenegro-logo-png_seeklogo-56490.png",
}
SPORTSDB_IDS = {
    "拉賈斯坦聯": "144706",
    "薩馬萊斯瓦里": "157489",
    "科英布拉大學": "134118",
}
# Never use arbitrary image URLs from a user-controlled query to access intranet
# or non-image services. Unrecognized hosts simply get a local badge.
SAFE_IMAGE_HOSTS = {
    "a.espncdn.com", "cdn.nba.com", "www.mlbstatic.com",
    "r2.thesportsdb.com", "www.thesportsdb.com",
    "api.thesportsdb.com", "upload.wikimedia.org",
    "assets.football-logos.cc", "images.mykhel.com",
    "shoplineimg.com", "assets.fiba.basketball",
    "www.volters.jp", "pleagueofficial.com", "imgs.tsna.com",
    "www.lala-peace.tw", "www.google.com",
    "wsrv.nl", "www.wsrv.nl",
    "assets.footylogos.com", "images.seeklogo.com", "i.imgur.com",
}
# Some known club assets are hosted at publisher-specific CDN origins. An
# unsupported source never becomes an unchecked third-party image request.


def _key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value or "").casefold()
                   if c.isalnum() and not unicodedata.combining(c))


def _allowed_image_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 900:
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname in SAFE_IMAGE_HOSTS
                and parsed.port in (None, 443)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


def team_logo_url(name: str, source: str = "", sport: str = "") -> str:
    """Stable LINE Flex image URL (PNG bytes are guaranteed by our endpoint)."""
    label = str(name or "隊伍").strip()[:90]
    src = str(source or "").strip()
    if not _allowed_image_url(src):
        src = ""
    query = urlencode({"name": label, "src": src, "sport": str(sport or "")[:24],
                       "v": "2"})
    return BASE_URL + "?" + query


def _badge(name: str, sport: str) -> bytes:
    seed = hashlib.sha256(name.encode("utf-8")).digest()
    # High-contrast dark-teal/blue tones and bright border for the HUD theme.
    palette = [(12, 94, 116), (22, 75, 129), (67, 55, 135),
               (17, 100, 91), (66, 70, 119)]
    primary = palette[seed[0] % len(palette)]
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Shield-shaped, consistent per-team identity glyph; it is a fallback,
    # not an imitation of an actual team's official crest.
    shield = [(64, 6), (114, 23), (108, 80), (93, 108),
              (64, 123), (35, 108), (20, 80), (14, 23)]
    draw.polygon(shield, fill=(*primary, 255), outline=(55, 228, 246, 255), width=4)
    draw.line([(28, 41), (100, 41)], fill=(113, 239, 251, 200), width=2)
    translated = SOCCER_ALIASES.get(name.strip(), name)
    words = re.findall(r"[A-Za-z]+", translated)
    if len(words) > 1:
        label = "".join(w[0] for w in words)[:3].upper()
    elif words:
        label = words[0][:2].upper()
    else:
        code = "FC" if sport in ("足球", "soccer") else (
            "BB" if sport in ("籃球", "basketball") else
            "BL" if sport in ("棒球", "baseball") else "TM")
        label = code
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 37 if len(label) <= 2 else 29)
    except OSError:
        font = ImageFont.load_default()
    bounds = draw.textbbox((0, 0), label, font=font)
    draw.text(((128 - (bounds[2] - bounds[0])) / 2 - bounds[0],
               (128 - (bounds[3] - bounds[1])) / 2 - bounds[1] + 8),
              label, fill=(255, 255, 255, 255), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def _fetch_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    if not _allowed_image_url(url):
        return None
    try:
        parsed = urlparse(url)
        # The LINE image component does not support SVG. Convert only already
        # allowlisted SVG sources using a public image-format service.
        if parsed.path.lower().endswith(".svg"):
            url = ("https://wsrv.nl/?url=" + quote(url, safe="")
                   + "&w=128&h=128&fit=contain&output=png")
        response = await client.get(url, timeout=3.2, follow_redirects=False)
        if response.status_code != 200:
            return None
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        if not (content_type.startswith("image/") or
                content_type == "application/octet-stream"):
            return None
        if len(response.content) > 1_800_000 or len(response.content) < 60:
            return None
        with Image.open(io.BytesIO(response.content)) as picture:
            if picture.width > 4096 or picture.height > 4096:
                return None
            picture.load()
            # Valid, small 128x128 PNG on our own stable HTTPS origin.
            picture = ImageOps.contain(picture.convert("RGBA"), (116, 116),
                                       Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            canvas.alpha_composite(picture, ((128 - picture.width) // 2,
                                             (128 - picture.height) // 2))
            buf = io.BytesIO()
            canvas.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except (httpx.HTTPError, ValueError, OSError, UnidentifiedImageError):
        return None


async def _sportsdb_logo(client: httpx.AsyncClient, name: str) -> str:
    alias = SOCCER_ALIASES.get(name.strip())
    if not alias:
        return ""
    try:
        if name.strip() in SPORTSDB_IDS:
            url = "https://www.thesportsdb.com/api/v1/json/3/lookupteam.php"
            params = {"id": SPORTSDB_IDS[name.strip()]}
        else:
            url = "https://www.thesportsdb.com/api/v1/json/3/searchteams.php"
            params = {"t": alias}
        response = await client.get(url, params=params, timeout=2.5,
                                    follow_redirects=False)
        if response.status_code != 200:
            return ""
        raw = response.json()
        teams = raw.get("teams", []) if isinstance(raw, dict) else []
        for team in teams or []:
            if not isinstance(team, dict) or team.get("strSport") != "Soccer":
                continue
            if name.strip() not in SPORTSDB_IDS:
                known = _key(str(team.get("strTeam") or ""))
                if known != _key(alias):
                    continue
            badge = team.get("strBadge") or team.get("strTeamBadge") or ""
            if _allowed_image_url(badge):
                return badge
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return ""


async def rendered_team_logo(client: httpx.AsyncClient, name: str,
                             src: str = "", sport: str = "") -> bytes:
    name = (name or "隊伍").strip()[:90]
    sport = (sport or "")[:24]
    src = src if _allowed_image_url(src) else ""
    key = (name, src, sport)
    previous = LOGO_CACHE.get(key)
    now = time.monotonic()
    if previous and previous[0] > now:
        return previous[1]
    async with _FETCH_LIMIT:
        previous = LOGO_CACHE.get(key)
        if previous and previous[0] > time.monotonic():
            return previous[1]
        urls = []
        if src:
            urls.append(src)
        known = KNOWN_BADGES.get(name)
        if known and known not in urls:
            urls.append(known)
        async def resolve_image():
            for url in urls:
                candidate = await _fetch_image(client, url)
                if candidate:
                    return candidate
            if sport in ("足球", "soccer") or name in SOCCER_ALIASES:
                discovered = await _sportsdb_logo(client, name)
                if discovered:
                    return await _fetch_image(client, discovered)
            return None

        # Never let several slow outside image hosts make LINE time out.
        try:
            result = await asyncio.wait_for(resolve_image(), timeout=3.3)
        except (TimeoutError, asyncio.TimeoutError):
            result = None
        valid = result is not None
        if not valid:
            result = _badge(name, sport)
        if len(LOGO_CACHE) > 360:
            LOGO_CACHE.pop(next(iter(LOGO_CACHE)))
        LOGO_CACHE[key] = (time.monotonic() + (86400 if valid else 600),
                           result, valid)
        return result
