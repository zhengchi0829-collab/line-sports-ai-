"""Verified bilingual display names for sports fixture cards.

Static Chinese names first. Unknown teams retain provider spelling rather than
inventing a Chinese club identity; fixture actions always use the original name.
"""
from __future__ import annotations

import re
import unicodedata

SOCCER_ZH = {
    # Premier League and common variants
    "Arsenal": "兵工廠", "Arsenal FC": "兵工廠",
    "Aston Villa": "阿斯頓維拉", "Aston Villa FC": "阿斯頓維拉",
    "AFC Bournemouth": "伯恩茅斯", "Bournemouth": "伯恩茅斯",
    "Brentford": "布倫特福德", "Brentford FC": "布倫特福德",
    "Brighton & Hove Albion": "布萊頓", "Brighton and Hove Albion": "布萊頓",
    "Brighton": "布萊頓", "Burnley": "伯恩利", "Chelsea": "切爾西",
    "Chelsea FC": "切爾西", "Crystal Palace": "水晶宮",
    "Everton": "艾佛頓", "Everton FC": "艾佛頓",
    "Fulham": "富勒姆", "Leeds United": "里茲聯",
    "Leicester City": "萊斯特城", "Liverpool": "利物浦",
    "Liverpool FC": "利物浦", "Manchester City": "曼徹斯特城",
    "Man City": "曼城", "Manchester United": "曼徹斯特聯",
    "Man United": "曼聯", "Newcastle United": "紐卡索聯",
    "Nottingham Forest": "諾丁漢森林",
    "Sunderland": "桑德蘭", "Tottenham Hotspur": "托特納姆熱刺",
    "Tottenham": "熱刺", "West Ham United": "西漢姆聯",
    "Wolverhampton Wanderers": "狼隊", "Wolves": "狼隊",
    "Ipswich Town": "伊普斯維奇", "Southampton": "南安普頓",
    "Sheffield United": "雪菲爾聯",
    # La Liga
    "Real Madrid": "皇家馬德里", "Real Madrid CF": "皇家馬德里",
    "Barcelona": "巴塞隆納", "FC Barcelona": "巴塞隆納",
    "Atlético Madrid": "馬德里競技", "Atletico Madrid": "馬德里競技",
    "Athletic Club": "畢爾包競技", "Athletic Bilbao": "畢爾包競技",
    "Real Sociedad": "皇家社會", "Real Betis": "皇家貝提斯",
    "Real Betis Balompié": "皇家貝提斯", "Villarreal": "比利亞雷亞爾",
    "Villarreal CF": "比利亞雷亞爾", "Valencia": "瓦倫西亞",
    "Valencia CF": "瓦倫西亞", "Sevilla": "塞維利亞",
    "Sevilla FC": "塞維利亞", "Getafe": "赫塔費",
    "Getafe CF": "赫塔費", "Rayo Vallecano": "巴列卡諾",
    "Osasuna": "奧薩蘇納", "CA Osasuna": "奧薩蘇納",
    "Celta Vigo": "塞爾塔", "RC Celta": "塞爾塔",
    "Mallorca": "馬約卡", "RCD Mallorca": "馬約卡",
    "Girona": "赫羅納", "Girona FC": "赫羅納",
    "Espanyol": "西班牙人", "RCD Espanyol": "西班牙人",
    "Alavés": "阿拉維斯", "Alaves": "阿拉維斯",
    "Deportivo Alavés": "阿拉維斯", "Leganés": "雷加利斯",
    "Leganes": "雷加利斯", "Elche": "埃爾切",
    "Elche CF": "埃爾切", "Real Oviedo": "皇家奧維耶多",
    "Levante": "萊萬特", "Levante UD": "萊萬特",
    "Las Palmas": "拉斯帕爾馬斯", "UD Las Palmas": "拉斯帕爾馬斯",
    "Valladolid": "巴亞多利德", "Real Valladolid": "巴亞多利德",
    "Granada": "格拉納達", "Cádiz": "加的斯", "Cadiz": "加的斯",
    # Bundesliga and variants
    "Bayern München": "拜仁慕尼黑", "Bayern Munich": "拜仁慕尼黑",
    "Borussia Dortmund": "多特蒙德", "Dortmund": "多特蒙德",
    "Bayer Leverkusen": "勒沃庫森", "Bayer 04 Leverkusen": "勒沃庫森",
    "RB Leipzig": "萊比錫", "Eintracht Frankfurt": "法蘭克福",
    "VfB Stuttgart": "斯圖加特", "VfL Wolfsburg": "沃夫斯堡",
    "SC Freiburg": "弗萊堡", "Union Berlin": "柏林聯合",
    "1. FC Union Berlin": "柏林聯合", "Mainz": "美因茨",
    "Mainz 05": "美因茨", "Borussia Mönchengladbach": "門興格拉德巴赫",
    "Borussia Monchengladbach": "門興格拉德巴赫",
    "Werder Bremen": "雲達不萊梅", "Augsburg": "奧格斯堡",
    "FC Augsburg": "奧格斯堡", "TSG Hoffenheim": "霍芬海姆",
    "Hoffenheim": "霍芬海姆", "Heidenheim": "海登海姆",
    "FC Heidenheim": "海登海姆", "St. Pauli": "聖保利",
    "Hamburger SV": "漢堡", "Hamburg SV": "漢堡",
    "FC Köln": "科隆", "FC Cologne": "科隆",
    "Holstein Kiel": "荷爾斯泰因基爾", "VfL Bochum": "波鴻",
    "Schalke 04": "沙爾克04", "Hertha Berlin": "柏林赫塔",
    "SV Darmstadt 98": "達姆施塔特", "Fortuna Düsseldorf": "杜塞道夫",
    # Serie A
    "Inter Milan": "國際米蘭", "Internazionale": "國際米蘭",
    "Internazionale Milano": "國際米蘭", "FC Internazionale Milano": "國際米蘭",
    "AC Milan": "AC米蘭", "Milan": "AC米蘭",
    "Juventus": "尤文圖斯", "Juventus FC": "尤文圖斯",
    "Napoli": "拿坡里", "SSC Napoli": "拿坡里",
    "Roma": "羅馬", "AS Roma": "羅馬", "Lazio": "拉齊奧",
    "SS Lazio": "拉齊奧", "Atalanta": "亞特蘭大",
    "Atalanta BC": "亞特蘭大", "Fiorentina": "佛羅倫斯",
    "ACF Fiorentina": "佛羅倫斯", "Bologna": "波隆那",
    "Bologna FC": "波隆那", "Torino": "杜里諾",
    "Torino FC": "杜里諾", "Udinese": "烏迪內斯",
    "Udinese Calcio": "烏迪內斯", "Genoa": "熱那亞",
    "Genoa CFC": "熱那亞", "Cagliari": "卡利亞里",
    "Cagliari Calcio": "卡利亞里", "Lecce": "萊切",
    "US Lecce": "萊切", "Sassuolo": "薩索羅",
    "US Sassuolo": "薩索羅", "Parma": "帕爾馬",
    "Parma Calcio": "帕爾馬", "Como": "科莫",
    "Como 1907": "科莫", "Pisa": "比薩",
    "Pisa SC": "比薩", "Cremonese": "克雷莫納",
    "US Cremonese": "克雷莫納", "Verona": "維羅納",
    "Hellas Verona": "維羅納", "Empoli": "恩波利",
    "Venezia": "威尼斯", "Monza": "蒙札",
    "Sampdoria": "桑普多利亞", "Palermo": "巴勒摩",
    # Ligue 1
    "Paris Saint-Germain": "巴黎聖日耳曼", "Paris Saint Germain": "巴黎聖日耳曼",
    "PSG": "巴黎聖日耳曼", "Marseille": "馬賽",
    "Olympique de Marseille": "馬賽", "AS Monaco": "摩納哥",
    "Monaco": "摩納哥", "Lyon": "里昂",
    "Olympique Lyonnais": "里昂", "Lille": "里爾",
    "Lille OSC": "里爾", "Nice": "尼斯", "OGC Nice": "尼斯",
    "Lens": "朗斯", "RC Lens": "朗斯", "Rennes": "雷恩",
    "Stade Rennais": "雷恩", "Strasbourg": "史特拉斯堡",
    "RC Strasbourg Alsace": "史特拉斯堡",
    "Toulouse": "土魯斯", "Toulouse FC": "土魯斯",
    "Nantes": "南特", "FC Nantes": "南特",
    "Brest": "布雷斯特", "Stade Brestois 29": "布雷斯特",
    "Montpellier": "蒙彼利埃", "Montpellier HSC": "蒙彼利埃",
    "Auxerre": "歐塞爾", "AJ Auxerre": "歐塞爾",
    "Angers": "昂熱", "Angers SCO": "昂熱",
    "Le Havre": "勒阿弗爾", "Le Havre AC": "勒阿弗爾",
    "Metz": "梅斯", "FC Metz": "梅斯",
    "Lorient": "洛里昂", "FC Lorient": "洛里昂",
    "Paris FC": "巴黎FC", "Saint-Étienne": "聖艾蒂安",
    "Saint Etienne": "聖艾蒂安", "Reims": "蘭斯",
    "Stade de Reims": "蘭斯", "Clermont Foot": "克萊蒙",
    # Other Champions League clubs
    "Benfica": "本菲卡", "SL Benfica": "本菲卡",
    "FC Porto": "波圖", "Porto": "波圖",
    "Sporting CP": "葡萄牙體育", "Sporting Lisbon": "葡萄牙體育",
    "Braga": "布拉加", "SC Braga": "布拉加",
    "Ajax": "阿賈克斯", "AFC Ajax": "阿賈克斯",
    "PSV Eindhoven": "恩荷芬", "PSV": "恩荷芬",
    "Feyenoord": "飛燕諾", "AZ Alkmaar": "阿爾克馬爾",
    "Club Brugge": "布魯日", "Club Brugge KV": "布魯日",
    "Union Saint-Gilloise": "聖吉羅斯聯",
    "RSC Anderlecht": "安德萊赫特",
    "Celtic": "塞爾提克", "Celtic FC": "塞爾提克",
    "Rangers": "格拉斯哥流浪者", "Rangers FC": "格拉斯哥流浪者",
    "Galatasaray": "加拉塔薩雷", "Fenerbahçe": "費內巴切",
    "Fenerbahce": "費內巴切", "Beşiktaş": "貝西克塔斯",
    "Besiktas": "貝西克塔斯",
    "Olympiacos": "奧林匹亞科斯", "Olympiacos FC": "奧林匹亞科斯",
    "Panathinaikos": "帕納辛奈科斯", "AEK Athens": "雅典AEK",
    "Shakhtar Donetsk": "頓內次克礦工",
    "Dynamo Kyiv": "基輔迪納摩",
    "Red Star Belgrade": "貝爾格勒紅星",
    "Crvena Zvezda": "貝爾格勒紅星",
    "Dinamo Zagreb": "札格瑞布迪納摩",
    "RB Salzburg": "薩爾斯堡紅牛", "Red Bull Salzburg": "薩爾斯堡紅牛",
    "FC Copenhagen": "哥本哈根", "FC København": "哥本哈根",
    "Bodø/Glimt": "博德閃耀", "Bodo/Glimt": "博德閃耀",
    "Molde": "莫爾德", "FC Midtjylland": "中日德蘭",
    "Slavia Prague": "布拉格斯拉維亞",
    "Sparta Prague": "布拉格斯巴達", "Viktoria Plzen": "比爾森勝利",
    "Basel": "巴塞爾", "FC Basel": "巴塞爾",
    "Young Boys": "伯恩年輕人", "BSC Young Boys": "伯恩年輕人",
    "FC Zürich": "蘇黎世", "Ludogorets": "盧多戈雷茨",
    "Qarabag": "卡拉巴赫", "Qarabağ": "卡拉巴赫",
    "Ferencváros": "費倫茨瓦羅斯",
    "Maccabi Tel Aviv": "特拉維夫馬卡比",
    "Maccabi Haifa": "海法馬卡比",
    "Pafos FC": "帕福斯", "Kairat Almaty": "阿拉木圖凱拉特",
    "Napoli FC": "拿坡里",
}

BASKETBALL_ZH = {
    # Australian NBL
    "Adelaide 36ers": "阿德雷德36人",
    "Brisbane Bullets": "布里斯本子彈",
    "Cairns Taipans": "凱恩斯太攀蛇",
    "Illawarra Hawks": "伊拉瓦拉老鷹",
    "Melbourne United": "墨爾本聯",
    "New Zealand Breakers": "紐西蘭破壞者",
    "Perth Wildcats": "伯斯野貓",
    "South East Melbourne Phoenix": "東南墨爾本鳳凰",
    "Sydney Kings": "雪梨國王",
    "Tasmania JackJumpers": "塔斯馬尼亞跳蟻",
    # TPBL
    "New Taipei Kings": "新北國王", "新北國王": "新北國王",
    "New Taipei CTBC DEA": "新北中信特攻", "新北中信特攻": "新北中信特攻",
    "Formosa Dreamers": "福爾摩沙夢想家", "福爾摩沙夢想家": "福爾摩沙夢想家",
    "Taipei Taishin Mars": "臺北台新戰神", "臺北台新戰神": "臺北台新戰神",
    "Taipei Fubon Braves": "臺北富邦勇士", "臺北富邦勇士": "臺北富邦勇士",
    "Kaohsiung Aquas": "高雄全家海神", "高雄全家海神": "高雄全家海神",
    "Taoyuan Taiwan Beer Leopards": "桃園台啤永豐雲豹",
    "Taoyuan Leopards": "桃園台啤永豐雲豹", "桃園台啤永豐雲豹": "桃園台啤永豐雲豹",
    "Hsinchu Toplus Lioneers": "新竹御嵿攻城獅", "新竹御嵿攻城獅": "新竹御嵿攻城獅",
    # NCAA major programs
    "Duke Blue Devils": "杜克藍魔", "North Carolina Tar Heels": "北卡羅來納焦油腳跟",
    "Kentucky Wildcats": "肯塔基野貓", "Kansas Jayhawks": "堪薩斯松鴉鷹",
    "UConn Huskies": "康乃狄克哈士奇", "Connecticut Huskies": "康乃狄克哈士奇",
    "Gonzaga Bulldogs": "岡薩加鬥牛犬", "Houston Cougars": "休士頓美洲獅",
    "Purdue Boilermakers": "普渡鍋爐工", "Tennessee Volunteers": "田納西志願者",
    "Auburn Tigers": "奧本老虎", "Alabama Crimson Tide": "阿拉巴馬紅潮",
    "Florida Gators": "佛羅里達鱷魚", "Arkansas Razorbacks": "阿肯色剃刀背野豬",
    "Arizona Wildcats": "亞利桑那野貓", "Arizona State Sun Devils": "亞利桑那州立太陽魔",
    "Baylor Bears": "貝勒熊", "Texas Longhorns": "德州長角牛",
    "Texas Tech Red Raiders": "德州理工紅色突擊者",
    "Texas A&M Aggies": "德州農工農人",
    "Iowa State Cyclones": "愛荷華州立旋風",
    "Iowa Hawkeyes": "愛荷華鷹眼",
    "Michigan Wolverines": "密西根狼獾", "Michigan State Spartans": "密西根州立斯巴達",
    "Ohio State Buckeyes": "俄亥俄州立七葉樹",
    "Indiana Hoosiers": "印第安納山地人",
    "Illinois Fighting Illini": "伊利諾戰士",
    "Wisconsin Badgers": "威斯康辛獾",
    "Nebraska Cornhuskers": "內布拉斯加玉米收割者",
    "Minnesota Golden Gophers": "明尼蘇達金地鼠",
    "Maryland Terrapins": "馬里蘭水龜",
    "UCLA Bruins": "加州大學洛杉磯分校棕熊",
    "USC Trojans": "南加州大學特洛伊人",
    "Oregon Ducks": "奧勒岡鴨", "Oregon State Beavers": "奧勒岡州立海狸",
    "Washington Huskies": "華盛頓哈士奇",
    "Washington State Cougars": "華盛頓州立美洲獅",
    "Stanford Cardinal": "史丹佛紅衣主教",
    "California Golden Bears": "加州金熊",
    "Colorado Buffaloes": "科羅拉多水牛",
    "Colorado State Rams": "科羅拉多州立公羊",
    "Utah Utes": "猶他猶特人",
    "BYU Cougars": "楊百翰美洲獅",
    "San Diego State Aztecs": "聖地牙哥州立阿茲特克",
    "New Mexico Lobos": "新墨西哥狼",
    "UNLV Rebels": "內華達大學拉斯維加斯分校叛軍",
    "Creighton Bluejays": "克瑞頓藍松鴉",
    "Marquette Golden Eagles": "馬奎特金鷹",
    "St. John's Red Storm": "聖約翰紅色風暴",
    "Villanova Wildcats": "維拉諾瓦野貓",
    "Georgetown Hoyas": "喬治城霍亞",
    "Xavier Musketeers": "澤維爾火槍手",
    "Seton Hall Pirates": "西頓霍爾海盜",
    "Butler Bulldogs": "巴特勒鬥牛犬",
    "Providence Friars": "普羅維登斯修士",
    "Cincinnati Bearcats": "辛辛那提熊貓",
    "Memphis Tigers": "曼菲斯老虎",
    "Louisville Cardinals": "路易維爾紅雀",
    "Virginia Cavaliers": "維吉尼亞騎士",
    "Virginia Tech Hokies": "維吉尼亞理工霍奇鳥",
    "NC State Wolfpack": "北卡州立狼群",
    "Wake Forest Demon Deacons": "維克森林惡魔執事",
    "Clemson Tigers": "克萊姆森老虎",
    "Florida State Seminoles": "佛羅里達州立塞米諾爾",
    "Miami Hurricanes": "邁阿密颶風",
    "Notre Dame Fighting Irish": "聖母大學愛爾蘭鬥士",
    "Syracuse Orange": "雪城橘人",
    "Pittsburgh Panthers": "匹茲堡黑豹",
    "Georgia Bulldogs": "喬治亞鬥牛犬",
    "Georgia Tech Yellow Jackets": "喬治亞理工黃夾克",
    "LSU Tigers": "路易斯安那州立老虎",
    "Ole Miss Rebels": "密西西比叛軍",
    "Mississippi State Bulldogs": "密西西比州立鬥牛犬",
    "Oklahoma Sooners": "奧克拉荷馬捷足者",
    "Oklahoma State Cowboys": "奧克拉荷馬州立牛仔",
    "TCU Horned Frogs": "德州基督教角蛙",
    "West Virginia Mountaineers": "西維吉尼亞登山者",
    "Wichita State Shockers": "威奇塔州立震撼者",
    "Saint Mary's Gaels": "聖瑪麗蓋爾人",
    "San Francisco Dons": "舊金山唐斯",
    "Dayton Flyers": "戴頓飛人",
    "VCU Rams": "維吉尼亞聯邦公羊",
}

BASEBALL_ZH = {
    # Alternate NPB/KBO feed spellings
    "Yokohama BayStars": "橫濱DeNA灣星",
    "DeNA BayStars": "橫濱DeNA灣星",
    "Yakult Swallows": "養樂多燕子",
    "Nippon Ham Fighters": "日本火腿鬥士",
    "Nippon-Ham Fighters": "日本火腿鬥士",
    "SoftBank Hawks": "軟銀鷹",
    "Rakuten Golden Eagles": "樂天金鷲",
    "Chiba Lotte Marines": "千葉羅德海洋",
    "Kia Tigers": "起亞虎", "KT Wiz": "KT巫師",
    "SSG Landers": "SSG登陸者",
    # MLB franchise variants
    "Oakland Athletics": "奧克蘭運動家",
    "Sacramento Athletics": "運動家",
    "Tampa Bay Devil Rays": "坦帕灣魔鬼魚",
    # Common MiLB clubs, current and older affiliates
    "Lehigh Valley IronPigs": "利哈伊谷鐵豬",
    "Scranton/Wilkes-Barre RailRiders": "斯克蘭頓／威爾克斯巴里鐵騎",
    "Buffalo Bisons": "水牛城野牛",
    "Rochester Red Wings": "羅徹斯特紅翼",
    "Syracuse Mets": "雪城大都會",
    "Worcester Red Sox": "伍斯特紅襪",
    "Charlotte Knights": "夏洛特騎士",
    "Durham Bulls": "德罕公牛",
    "Jacksonville Jumbo Shrimp": "傑克森維爾巨蝦",
    "Norfolk Tides": "諾福克潮汐",
    "Gwinnett Stripers": "格威內特條紋鱸",
    "Memphis Redbirds": "曼菲斯紅鳥",
    "Nashville Sounds": "納許維爾之聲",
    "Louisville Bats": "路易維爾蝙蝠",
    "Toledo Mud Hens": "托雷多泥雞",
    "Indianapolis Indians": "印第安納波利斯印第安人",
    "Columbus Clippers": "哥倫布快艇",
    "Omaha Storm Chasers": "奧馬哈追風者",
    "Iowa Cubs": "愛荷華小熊",
    "St. Paul Saints": "聖保羅聖徒",
    "Oklahoma City Comets": "奧克拉荷馬市彗星",
    "Sugar Land Space Cowboys": "糖地太空牛仔",
    "Round Rock Express": "圓石特快車",
    "El Paso Chihuahuas": "艾爾帕索吉娃娃",
    "Albuquerque Isotopes": "阿布奎基同位素",
    "Las Vegas Aviators": "拉斯維加斯飛行員",
    "Reno Aces": "雷諾王牌",
    "Salt Lake Bees": "鹽湖城蜜蜂",
    "Sacramento River Cats": "沙加緬度河貓",
    "Tacoma Rainiers": "塔科馬雷尼爾山",
    "Portland Sea Dogs": "波特蘭海狗",
    "Reading Fightin Phils": "雷丁費城人",
    "Bowie Baysox": "鮑伊灣襪",
    "Somerset Patriots": "薩默塞特愛國者",
    "New Hampshire Fisher Cats": "新罕布夏漁貓",
    "Hartford Yard Goats": "哈特福德庭院山羊",
    "Binghamton Rumble Ponies": "賓漢頓隆隆小馬",
    "Erie SeaWolves": "伊利海狼",
    "Akron RubberDucks": "阿克倫橡皮鴨",
    "Richmond Flying Squirrels": "里奇蒙飛鼠",
    "Harrisburg Senators": "哈里斯堡參議員",
    "Altoona Curve": "阿爾圖納曲球",
    "Birmingham Barons": "伯明罕男爵",
    "Chattanooga Lookouts": "查塔努加瞭望者",
    "Tennessee Smokies": "田納西煙山熊",
    "Rocket City Trash Pandas": "火箭城浣熊",
    "Montgomery Biscuits": "蒙哥馬利餅乾",
    "Pensacola Blue Wahoos": "彭薩科拉藍鰆",
    "Biloxi Shuckers": "比洛克西剝牡蠣者",
    "Mississippi Braves": "密西西比勇士",
    "Amarillo Sod Poodles": "阿馬里洛草原犬鼠",
    "Corpus Christi Hooks": "科珀斯克里斯蒂魚鉤",
    "Frisco RoughRiders": "弗里斯科粗騎兵",
    "Midland RockHounds": "米德蘭岩獵犬",
    "San Antonio Missions": "聖安東尼奧傳教士",
    "Springfield Cardinals": "春田紅雀",
    "Tulsa Drillers": "塔爾薩鑽井工",
    "Arkansas Travelers": "阿肯色旅行者",
    "Northwest Arkansas Naturals": "阿肯色西北自然隊",
    "Wichita Wind Surge": "威奇塔風潮",
    "San Jose Giants": "聖荷西巨人",
    "Inland Empire 66ers": "內陸帝國66人",
    "Lake Elsinore Storm": "埃爾西諾湖風暴",
    "Rancho Cucamonga Quakes": "蘭喬庫卡蒙加地震",
    "Modesto Nuts": "莫德斯托堅果",
    "Stockton Ports": "史塔克頓港口",
    "Fresno Grizzlies": "弗雷斯諾灰熊",
    "Visalia Rawhide": "維薩利亞生皮",
    "Eugene Emeralds": "尤金翡翠",
    "Hillsboro Hops": "希爾斯伯勒啤酒花",
    "Spokane Indians": "斯波坎印第安人",
    "Everett AquaSox": "艾佛瑞特水襪",
    "Vancouver Canadians": "溫哥華加拿大人",
    "Tri-City Dust Devils": "三城沙塵魔",
    "Great Lakes Loons": "大湖潛鳥",
    "Lansing Lugnuts": "蘭辛螺帽",
    "South Bend Cubs": "南本德小熊",
    "West Michigan Whitecaps": "西密西根白浪",
    "Fort Wayne TinCaps": "韋恩堡鐵帽",
    "Dayton Dragons": "戴頓龍",
    "Lake County Captains": "湖郡船長",
    "Cedar Rapids Kernels": "錫達拉皮茲玉米粒",
    "Beloit Sky Carp": "貝洛伊特天鯉",
    "Wisconsin Timber Rattlers": "威斯康辛木紋響尾蛇",
    "Peoria Chiefs": "皮奧里亞酋長",
    "Quad Cities River Bandits": "四城河盜",
    "Brooklyn Cyclones": "布魯克林旋風",
    "Jersey Shore BlueClaws": "澤西海岸藍爪",
    "Hudson Valley Renegades": "哈德遜谷叛軍",
    "Wilmington Blue Rocks": "威明頓藍岩",
    "Aberdeen IronBirds": "亞伯丁鐵鳥",
    "Greensboro Grasshoppers": "格林斯伯勒蚱蜢",
    "Hickory Crawdads": "希科里小龍蝦",
    "Winston-Salem Dash": "溫斯頓塞勒姆衝刺",
    "Asheville Tourists": "阿什維爾旅人",
    "Bowling Green Hot Rods": "鮑靈格林改裝車",
    "Rome Emperors": "羅馬皇帝",
    "Augusta GreenJackets": "奧古斯塔綠夾克",
    "Columbia Fireflies": "哥倫比亞螢火蟲",
    "Charleston RiverDogs": "查爾斯頓河狗",
    "Myrtle Beach Pelicans": "默特爾海灘鵜鶘",
    "Carolina Mudcats": "卡羅來納泥鯰",
    "Fayetteville Woodpeckers": "費耶特維爾啄木鳥",
    "Salem Red Sox": "塞勒姆紅襪",
    "Lynchburg Hillcats": "林奇堡山貓",
    "Fredericksburg Nationals": "弗雷德里克斯堡國民",
    "Delmarva Shorebirds": "德爾馬瓦濱鳥",
    "Kannapolis Cannon Ballers": "坎納波利斯砲彈手",
    "Down East Wood Ducks": "東部林鴨",
    "Dunedin Blue Jays": "達尼丁藍鳥",
    "Tampa Tarpons": "坦帕大海鰱",
    "Bradenton Marauders": "布雷登頓掠奪者",
    "Clearwater Threshers": "清水長尾鯊",
    "Fort Myers Mighty Mussels": "邁爾斯堡強力貽貝",
    "Lakeland Flying Tigers": "萊克蘭飛虎",
    "St. Lucie Mets": "聖露西大都會",
    "Daytona Tortugas": "戴通納海龜",
    "Jupiter Hammerheads": "朱比特雙髻鯊",
    "Palm Beach Cardinals": "棕櫚灘紅雀",
}

EXTRA_TEAMS_ZH = dict(
    line.split(" = ", 1) for line in """SC Paderborn 07 = 帕德博恩07
SV 07 Elversberg = 艾禾斯堡
SV Elversberg = 艾禾斯堡
Coventry City = 考文垂城
Hull City = 赫爾城
Deportivo = 拉科魯尼亞
Málaga = 馬拉加
Racing Santander = 桑坦德競技
Frosinone = 弗羅西諾內
Le Mans = 勒芒
Troyes = 特魯瓦
Feyenoord Rotterdam = 鹿特丹飛燕諾
LASK Linz = 林茨LASK
Sabah FK = 薩巴赫
Slovan Bratislava = 布拉提斯拉瓦斯洛萬
Viking FK = 維京
Chesapeake Baysox = 切薩皮克灣襪
Columbus Clingstones = 哥倫布黏核桃
Knoxville Smokies = 諾克斯維爾煙山熊
Ozarks Springbirds = 歐扎克春鳥
Scranton/Wilkes-Barre Highlanders = 斯克蘭頓／威爾克斯巴里高地人
Air Force Falcons = 空軍獵鷹
Akron Zips = 阿克倫拉鍊
Alcorn State Braves = 奧爾康州立勇士
App State Mountaineers = 阿帕拉契州立登山者
Arkansas State Red Wolves = 阿肯色州立紅狼
Arkansas-Pine Bluff Golden Lions = 阿肯色大學松崖分校金獅
Army Black Knights = 陸軍黑騎士
Austin Peay Governors = 奧斯汀皮伊州立總督
Ball State Cardinals = 波爾州立紅雀
Bellarmine Knights = 貝拉明騎士
Belmont Bruins = 貝爾蒙特棕熊
Binghamton Bearcats = 賓漢頓熊貓
Bradley Braves = 布萊德利勇士
Brown Bears = 布朗熊
Bryant Bulldogs = 布萊恩特鬥牛犬
Bucknell Bison = 巴克內爾野牛
Cal Poly Mustangs = 加州理工州立野馬
California Baptist Lancers = 加州浸信會槍騎兵
Campbell Fighting Camels = 坎貝爾戰駱駝
Central Arkansas Bears = 中阿肯色熊
Central Michigan Chippewas = 中密西根奇佩瓦人
Champion Christian Tigers = 冠軍基督教老虎
Charleston Southern Buccaneers = 南查爾斯頓海盜
Charlotte 49ers = 夏洛特49人
Chattanooga Mocs = 查塔努加莫克斯
Cheyney (PA) Wolves = 賓州切尼狼
Chicago State Cougars = 芝加哥州立美洲獅
Cleveland State Vikings = 克里夫蘭州立維京人
Coastal Georgia Mariner = 喬治亞海岸水手
College of Biblical Studies Ambassadors = 聖經研究學院大使
Columbia Lions = 哥倫比亞大學獅
Coppin State Eagles = 科平州立老鷹
Corban Warriors = 科爾班勇士
Cornell Big Red = 康乃爾大紅
Davidson Wildcats = 戴維森野貓
DePaul Blue Demons = 德保羅藍魔
Delaware State Hornets = 德拉瓦州立黃蜂
Denver Pioneers = 丹佛先鋒
Detroit Mercy Titans = 底特律慈悲泰坦
Dillard Blue Devils = 迪拉德藍魔
Drake Bulldogs = 德雷克鬥牛犬
Drexel Dragons = 卓克索龍
Duquesne Dukes = 迪尤肯公爵
Earlham Quakers = 厄勒姆貴格會
East Carolina Pirates = 東卡羅來納海盜
East Texas A&M Lions = 東德州農工獅
Eastern Kentucky Colonels = 東肯塔基上校
Eastern Washington Eagles = 東華盛頓老鷹
Elon Phoenix = 伊隆鳳凰
Evansville Purple Aces = 伊凡斯維爾紫色王牌
Fairfield Stags = 費爾菲爾德雄鹿
Fairleigh Dickinson Knights = 費爾利狄金森騎士
Florida Atlantic Owls = 佛羅里達大西洋貓頭鷹
Florida Gulf Coast Eagles = 佛羅里達灣岸老鷹
Florida International Panthers = 佛羅里達國際黑豹
Fordham Rams = 福坦莫公羊
Fresno State Bulldogs = 弗雷斯諾州立鬥牛犬
Furman Paladins = 傅爾曼聖騎士
Gardner-Webb Runnin' Bulldogs = 加德納韋伯奔跑鬥牛犬
George Mason Patriots = 喬治梅森愛國者
George Washington Revolutionaries = 喬治華盛頓革命者
Georgia Southern Eagles = 南喬治亞老鷹
Grand Canyon Lopes = 大峽谷羚羊
Green Bay Phoenix = 綠灣鳳凰
Hampton Pirates = 漢普頓海盜
Harvard Crimson = 哈佛深紅
Hofstra Pride = 霍夫斯特拉獅群
Houston Christian Huskies = 休士頓基督教哈士奇
Howard Bison = 霍華德野牛
IU Indianapolis Jaguars = 印第安納大學印第安納波利斯分校美洲豹
Idaho State Bengals = 愛達荷州立孟加拉虎
Idaho Vandals = 愛達荷汪達爾人
Illinois State Redbirds = 伊利諾州立紅鳥
Indiana State Sycamores = 印第安納州立梧桐
Iona Gaels = 艾歐納蓋爾人
Jackson State Tigers = 傑克森州立老虎
Jacksonville State Gamecocks = 傑克森維爾州立鬥雞
Kentucky State Thorobreds = 肯塔基州立純種馬
LSU New Orleans Privateers = 路易斯安那州立紐奧良海盜
Lafayette Leopards = 拉法葉美洲豹
Le Moyne Dolphins = 勒莫恩海豚
Lehigh Mountain Hawks = 利哈伊山鷹
Liberty Flames = 自由大學火焰
Lincoln (CA) Oaklanders = 加州林肯奧克蘭人
Lindenwood Lions = 林登伍德獅
Lipscomb Bisons = 利普斯科姆野牛
Little Rock Trojans = 小岩城特洛伊人
Long Island University Sharks = 長島大學鯊魚
Longwood Lancers = 朗伍德槍騎兵
Louisiana Tech Bulldogs = 路易斯安那理工鬥牛犬
Loyola Chicago Ramblers = 芝加哥羅耀拉漫遊者
Loyola Maryland Greyhounds = 馬里蘭羅耀拉靈緹
Maine Black Bears = 緬因黑熊
Manhattan Jaspers = 曼哈頓碧玉
Marist Red Foxes = 馬里斯特紅狐
Marshall Thundering Herd = 馬歇爾雷霆群
Mary Baldwin Fighting Squirrels = 瑪麗鮑德溫戰鬥松鼠
Merrimack Warriors = 梅里馬克勇士
Miami (OH) RedHawks = 俄亥俄邁阿密紅鷹
Middle Tennessee Blue Raiders = 中田納西藍色突擊者
Milwaukee Panthers = 密爾瓦基黑豹
Mississippi Valley State Delta Devils = 密西西比河谷州立三角洲惡魔
Montana Grizzlies = 蒙大拿灰熊
Montreat Cavaliers = 蒙特里特騎士
Morehead State Eagles = 莫爾黑德州立老鷹
Morgan State Bears = 摩根州立熊
Mount St. Mary's Mountaineers = 聖瑪麗山登山者
Murray State Racers = 莫瑞州立賽馬
NJIT Highlanders = 紐澤西理工高地人
Navy Midshipmen = 海軍官校生
Nevada Wolf Pack = 內華達狼群
Nicholls Colonels = 尼可斯上校
North Alabama Lions = 北阿拉巴馬獅
North Carolina A&T Aggies = 北卡農工農人
North Carolina Central Eagles = 北卡中央老鷹
North Dakota Fighting Hawks = 北達科他戰鷹
North Dakota State Bison = 北達科他州立野牛
North Florida Ospreys = 北佛羅里達魚鷹
North Texas Mean Green = 北德州兇綠
Northeastern Huskies = 東北大學哈士奇
Northern Arizona Lumberjacks = 北亞利桑那伐木工
Northern Colorado Bears = 北科羅拉多熊
Northern Illinois Huskies = 北伊利諾哈士奇
Northern Iowa Panthers = 北愛荷華黑豹
Northern Kentucky Norse = 北肯塔基北歐人
Northwest University Eagles = 西北大學老鷹
Northwestern State Demons = 西北州立惡魔
Northwestern Wildcats = 西北大學野貓
Oakland Golden Grizzlies = 奧克蘭金灰熊
Ohio Bobcats = 俄亥俄山貓
Oklahoma City Okies = 奧克拉荷馬城奧基人
Old Dominion Monarchs = 老道明君主
Omaha Mavericks = 奧馬哈獨行俠
Oral Roberts Golden Eagles = 歐洛羅伯茨金鷹
Pacific Tigers = 太平洋大學老虎
Pennsylvania Quakers = 賓夕法尼亞貴格會
Plattsburgh Cardinals = 普拉茨堡紅雀
Portland Pilots = 波特蘭領航員
Portland State Vikings = 波特蘭州立維京人
Prairie View A&M Panthers = 草原景農工黑豹
Queens University Royals = 皇后大學皇家
Radford Highlanders = 拉德福德高地人
Rice Owls = 萊斯貓頭鷹
Rider Broncs = 萊德野馬
Rutgers Scarlet Knights = 羅格斯緋紅騎士
SIU Edwardsville Cougars = 南伊利諾大學愛德華茲維爾分校美洲獅
SMU Mustangs = 南美以美大學野馬
Sacramento State Hornets = 沙加緬度州立黃蜂
Saint Joseph's Hawks = 聖約瑟夫老鷹
Sam Houston Bearkats = 山姆休士頓熊貓
Samford Bulldogs = 桑佛德鬥牛犬
San Diego Toreros = 聖地牙哥鬥牛士
Santa Clara Broncos = 聖塔克拉拉野馬
Seattle U Redhawks = 西雅圖大學紅鷹
Shenandoah Hornets = 雪蘭多黃蜂
Siena Saints = 錫耶納聖徒
South Alabama Jaguars = 南阿拉巴馬美洲豹
South Carolina Gamecocks = 南卡羅來納鬥雞
South Carolina Upstate Spartans = 南卡上州斯巴達
South Dakota Coyotes = 南達科他郊狼
South Dakota State Jackrabbits = 南達科他州立長耳兔
South Florida Bulls = 南佛羅里達公牛
Southeast Missouri State Redhawks = 東南密蘇里州立紅鷹
Southern Miss Golden Eagles = 南密西西比金鷹
Southern New Orleans Knights = 南紐奧良騎士
Southern Utah Thunderbirds = 南猶他雷鳥
St. Bonaventure Bonnies = 聖文德邦尼
St. Thomas Tommies = 聖托瑪斯湯米
Stephen F. Austin Lumberjacks = 史蒂芬奧斯汀伐木工
Stonehill Skyhawks = 斯通希爾天鷹
Tarleton State Texans = 塔爾頓州立德州人
Tennessee State Tigers = 田納西州立老虎
Texas A&M-Corpus Christi Islanders = 德州農工科珀斯克里斯蒂島民
Texas Southern Tigers = 南德州老虎
Texas State Bobcats = 德州州立山貓
The Citadel Bulldogs = 城堡軍校鬥牛犬
Toledo Rockets = 托雷多火箭
Towson Tigers = 陶森老虎
Troy Trojans = 特洛伊特洛伊人
Tulane Green Wave = 杜蘭綠浪
UAB Blazers = 阿拉巴馬大學伯明罕分校拓荒者
UC Davis Aggies = 加州大學戴維斯分校農人
UC Riverside Highlanders = 加州大學河濱分校高地人
UC San Diego Tritons = 加州大學聖地牙哥分校海神
UCF Knights = 中佛羅里達騎士
UIC Flames = 伊利諾大學芝加哥分校火焰
UL Monroe Warhawks = 路易斯安那大學門羅分校戰鷹
UMBC Retrievers = 馬里蘭大學巴爾的摩郡分校尋回犬
UNC Asheville Bulldogs = 北卡大學阿什維爾分校鬥牛犬
UNC Greensboro Spartans = 北卡大學格林斯伯勒分校斯巴達
UNC Wilmington Seahawks = 北卡大學威明頓分校海鷹
UT Arlington Mavericks = 德州大學阿靈頓分校獨行俠
UT Martin Skyhawks = 田納西大學馬丁分校天鷹
UT Rio Grande Valley Vaqueros = 德州大學格蘭德河谷分校牛仔
Utah State Aggies = 猶他州立農人
Utah Tech Trailblazers = 猶他理工開拓者
Utah Valley Wolverines = 猶他谷狼獾
VMI Keydets = 維吉尼亞軍事學院學員
Vanderbilt Commodores = 范德堡海軍準將
Vermont Catamounts = 佛蒙特山貓
Viterbo V-Hawks = 維特博V鷹
Weber State Wildcats = 韋伯州立野貓
West Florida Argonauts = 西佛羅里達阿爾戈英雄
West Georgia Wolves = 西喬治亞狼
Western Carolina Catamounts = 西卡羅來納山貓
Winthrop Eagles = 溫斯羅普老鷹
Wofford Terriers = 沃福德梗犬
Wright State Raiders = 萊特州立突擊者
Wyoming Cowboys = 懷俄明牛仔
Yale Bulldogs = 耶魯鬥牛犬
Youngstown State Penguins = 揚斯敦州立企鵝
Frederick Keys = 弗雷德里克鑰匙
Greenville Drive = 格林維爾衝勁
Hub City Spartanburgers = 哈布城斯帕坦堡漢堡""".splitlines()
    if " = " in line
)

_ALL = {**SOCCER_ZH, **BASKETBALL_ZH, **BASEBALL_ZH, **EXTRA_TEAMS_ZH}

def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", value.casefold())

_NORMALIZED = {_key(k): v for k, v in _ALL.items()}

def chinese_name(value: str, existing: dict | None = None) -> str:
    """Resolve translated name without guessing if provider changed the team."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    if existing and raw in existing:
        return existing[raw]
    result = _NORMALIZED.get(_key(raw))
    if result:
        return result
    # Normalize only well-defined provider club prefixes and suffixes.
    trimmed = re.sub(r"^(?:FC|AC|AFC|SC)\s+", "", raw, flags=re.I)
    trimmed = re.sub(r"\s+(?:FC|CF|SC)$", "", trimmed, flags=re.I)
    result = _NORMALIZED.get(_key(trimmed))
    return result or raw

def bilingual_name(value: str, existing: dict | None = None) -> str:
    original = str(value or "").strip()
    translated = chinese_name(original, existing)
    if translated == original:
        return original
    return f"{translated}（{original}）"
