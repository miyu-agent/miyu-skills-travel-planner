#!/usr/bin/env python3
# Display name: Flight prices
# 显示名称：机票比价
# Description: One-way flight prices from Google Flights, cheapest first, or scan a date window for the cheapest days. Never guess origin or dest.
# 描述：查单程机票价格（Google 航班），按价排序，带航司、航班号、时刻、经停；也能扫一段日期找哪天走最便宜。
# Timeout: 300
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "action": {"type": "string", "enum": ["search", "sweep", "trend", "airports", "doctor"], "description": "search (default): one date, add return for a round trip priced as two one-ways. sweep: scan window for the cheapest days; with days also the return and best combinations. trend: price history of a route (no origin/dest lists recorded routes). airports: look a place up. doctor: self-check."},
#   "origin": {"type": "string", "description": "City, English name or IATA code, e.g. 上海 or PVG; a city covers all its airports. Ask the user if unknown."},
#   "dest": {"type": "string", "description": "Same forms, e.g. 东京 or NRT."},
#   "depart": {"type": "string", "description": "+30, 10-01 or 2026-10-01. search: default +14. trend: only this departure date."},
#   "return": {"type": "string", "description": "search: return date; both legs are searched and a total given."},
#   "window": {"type": "string", "description": "sweep: 09-29..10-08, the span the user can travel in (return included). Default +14..+28."},
#   "days": {"type": "string", "description": "sweep: trip length, 5 or 5-7. Given, the return leg is scanned too and combinations ranked."},
#   "query": {"type": "string", "description": "airports: place name or IATA code."},
#   "seat": {"type": ["string", "array"], "items": {"type": "string", "enum": ["economy", "premium", "business", "first", "all"]}, "description": "Default economy. search takes an array or all to compare cabins."},
#   "adults": {"type": "integer", "description": "Default 1."},
#   "children": {"type": "integer", "description": "Aged 2-11, default 0."},
#   "max_stops": {"type": "integer", "description": "0 = direct only. Only when the user asks; the output already names the cheapest direct flight."},
#   "airline": {"type": "array", "items": {"type": "string"}, "description": "Chinese or English airline names. Read 'lowest per airline' first."},
#   "depart_time": {"type": "string", "description": "Departure window like 08:00-18:00."},
#   "sort": {"type": "string", "enum": ["price", "duration", "depart", "arrive", "stops"], "description": "Default price."},
#   "top": {"type": "integer", "description": "Rows: search 15, sweep 8 by default."},
#   "currency": {"type": "string", "description": "Default CNY."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."},
#   "no_record": {"type": "boolean", "description": "Don't add this result to the price history."}
# }}

"""机票比价：Google 航班把航线、日期、舱位、人数编在 tfs 参数里（base64url 过的
protobuf），自己拼得出来，所以查一次只要一条 HTTP 请求，不用浏览器。只用标准库。

出发地和目的地必须给，脚本不猜：猜错了输出看起来完全正常，比报错危险。
价格历史（trend 用）存在 MIYU_SCRIPT_STATE_DIR，没有就放 MIYU_SCRIPT_CACHE_DIR。
"""

import base64
import html as _html
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# 价格历史放在能留住的地方：有持久的状态目录用它，没有就放缓存目录（重启会清空）。
DATA_DIR = os.environ.get("MIYU_SCRIPT_STATE_DIR") or os.environ.get("MIYU_SCRIPT_CACHE_DIR") or ""
DB_PATH = os.path.join(DATA_DIR, "flight-history.db") if DATA_DIR else ""

SPARK = "▁▂▃▄▅▆▇█"
SYMBOL = {"CNY": "¥", "JPY": "JP¥", "USD": "$", "EUR": "€", "HKD": "HK$",
          "KRW": "₩", "GBP": "£", "TWD": "NT$", "SGD": "S$", "AUD": "A$"}

# Google 对不认识的币种不报错，直接按 gl 回退（gl=us 就是美元）照常出价。
# 脚本又拿用户写的字符串当前缀原样印，于是 `--currency RMB` 会印出「RMB 78」——
# 实际是 78 美元，看着像捡了个大漏。好在 aria-label 的第二个捕获组写着真实
# 币种名，对一次就能把这种静默换币拦下来。表里没收录的币种(Google 支持一百
# 多种)不能一概判错，只有「回退成美元」才是没被接受的确凿信号。
CURRENCY_NAMES = {
    "CNY": ("chinese yuan", "chinese yuan renminbi", "yuan"),
    "USD": ("us dollars", "us dollar"),
    "JPY": ("japanese yen", "yen"),
    "EUR": ("euros", "euro"),
    "HKD": ("hong kong dollars", "hong kong dollar"),
    "KRW": ("south korean won", "korean won", "won"),
    "GBP": ("british pounds", "british pound", "pounds sterling"),
    "TWD": ("new taiwan dollars", "new taiwan dollar"),
    "SGD": ("singapore dollars", "singapore dollar"),
    "AUD": ("australian dollars", "australian dollar"),
}

SEATS = {"economy": 1, "premium": 2, "business": 3, "first": 4}
SEAT_CN = {1: "经济舱", 2: "超级经济舱", 3: "商务舱", 4: "头等舱"}


# 航司一般只放到 11 个月后开售，再远 Google 就不出结果页了 —— 而那种页面跟
# 「这条航线连不上」长得一模一样，不单独判就会把人往核对三字码那条错路上引。
BOOKING_HORIZON_DAYS = 330


class Failed(RuntimeError):
    """带「下一步」建议的失败。"""

    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint


# ---------------------------------------------------------------- Google tfs 参数

def _varint(n):
    out = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        out += bytes([chunk | (0x80 if n else 0)])
        if not n:
            return out


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _uint(field, n):
    return _tag(field, 0) + _varint(n)


def _sub(field, body):
    return _tag(field, 2) + _varint(len(body)) + body


def _pstr(field, text):
    raw = text.encode()
    return _tag(field, 2) + _varint(len(raw)) + raw


def build_tfs(day, from_codes, to_codes, seat=1, adults=1, children=0,
              max_stops=None):
    """拼出 google.com/travel/flights 的 tfs 参数（单程）。

    实测(2026-09)结构::

        3 { 2:<yyyy-mm-dd>  5:<最大经停数>  13{2:<出发机场码>}… 14{2:<到达机场码>}… }
        9:<舱位 1经济 2超经 3商务 4头等>
        8:1 每位乘客一个(1成人 2儿童 3占座婴儿 4怀抱婴儿)
        19:2 单程

    往返是重复 field 3 给两段(去程+回程)再把 19 改成 1，本脚本不用 ——
    往返联程票返回的每行只是去程航班加一个总价下限，返程班次并不确定，
    而实测它相对两张单程也没有价格优势(平日甚至更贵)。要往返就查两次单程。

    最容易踩的坑：机场码在 Airport 消息的 **field 2**，不是 field 1。
    写成 field 1 时 Google 不报错，而是静默退化成「没指定航线」的落地页 ——
    HTTP 200、页面正常、结果 0 条。所以 google_search() 必须能分辨
    「落地页」和「真的没航班」，见那里的 _RESULTS_MARK。
    """
    leg = _pstr(2, day.isoformat() if hasattr(day, "isoformat") else str(day))
    if max_stops is not None:
        leg += _uint(5, max_stops)
    leg += b"".join(_sub(13, _pstr(2, c)) for c in from_codes)
    leg += b"".join(_sub(14, _pstr(2, c)) for c in to_codes)
    body = _sub(3, leg) + _uint(9, seat)
    body += b"".join(_uint(8, 1) for _ in range(adults))
    body += b"".join(_uint(8, 2) for _ in range(children))
    body += _uint(19, 2)
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


# ---------------------------------------------------------------- 机场表

# 一行一个机场：三字码|机场中文名|机场英文名|城市中文|城市英文|别名(逗号分隔)
#
# 这张表只求准，不求全 —— 硬凑数量必然掺进记错的码，而错码的后果是「查了另一
# 座城市的票，输出完全正常」。表里没有的地方，用户直接写三字码就能用(见
# resolve())，所以覆盖率不足最多是多打三个字母，不会给出错误答案。
#
# 全表 2026-09-01 逐条对着 Google 实测过一遍。当时揪出两个已退役的码：金边
# PNH(德崇新机场启用后停用)和暹粒 REP(吴哥新机场启用后关闭)，Google 完全不认
# 这两个码 —— 机场改码是这张表最主要的腐烂来源，隔一阵子值得重跑一次校验。
AIRPORT_TABLE = """
PEK|首都|Beijing Capital|北京|Beijing|京
PKX|大兴|Beijing Daxing|北京|Beijing|京
PVG|浦东|Shanghai Pudong|上海|Shanghai|沪
SHA|虹桥|Shanghai Hongqiao|上海|Shanghai|沪
CAN|白云|Guangzhou Baiyun|广州|Guangzhou|穗
SZX|宝安|Shenzhen Bao'an|深圳|Shenzhen|
CTU|双流|Chengdu Shuangliu|成都|Chengdu|蓉
TFU|天府|Chengdu Tianfu|成都|Chengdu|蓉
CKG|江北|Chongqing Jiangbei|重庆|Chongqing|渝
XIY|咸阳|Xi'an Xianyang|西安|Xian|
HGH|萧山|Hangzhou Xiaoshan|杭州|Hangzhou|
NKG|禄口|Nanjing Lukou|南京|Nanjing|
WUH|天河|Wuhan Tianhe|武汉|Wuhan|
CSX|黄花|Changsha Huanghua|长沙|Changsha|
TAO|胶东|Qingdao Jiaodong|青岛|Qingdao|
DLC|周水子|Dalian Zhoushuizi|大连|Dalian|
SHE|桃仙|Shenyang Taoxian|沈阳|Shenyang|
HRB|太平|Harbin Taiping|哈尔滨|Harbin|
CGQ|龙嘉|Changchun Longjia|长春|Changchun|
TSN|滨海|Tianjin Binhai|天津|Tianjin|
TNA|遥墙|Jinan Yaoqiang|济南|Jinan|
CGO|新郑|Zhengzhou Xinzheng|郑州|Zhengzhou|
KMG|长水|Kunming Changshui|昆明|Kunming|
KWE|龙洞堡|Guiyang Longdongbao|贵阳|Guiyang|
NNG|吴圩|Nanning Wuxu|南宁|Nanning|
HAK|美兰|Haikou Meilan|海口|Haikou|
SYX|凤凰|Sanya Phoenix|三亚|Sanya|
XMN|高崎|Xiamen Gaoqi|厦门|Xiamen|
FOC|长乐|Fuzhou Changle|福州|Fuzhou|
JJN|晋江|Quanzhou Jinjiang|泉州|Quanzhou|
WNZ|龙湾|Wenzhou Longwan|温州|Wenzhou|
NGB|栎社|Ningbo Lishe|宁波|Ningbo|
HFE|新桥|Hefei Xinqiao|合肥|Hefei|
KHN|昌北|Nanchang Changbei|南昌|Nanchang|
TYN|武宿|Taiyuan Wusu|太原|Taiyuan|
SJW|正定|Shijiazhuang Zhengding|石家庄|Shijiazhuang|
LHW|中川|Lanzhou Zhongchuan|兰州|Lanzhou|
INC|河东|Yinchuan Hedong|银川|Yinchuan|
XNN|曹家堡|Xining Caojiabao|西宁|Xining|
URC|地窝堡|Urumqi Diwopu|乌鲁木齐|Urumqi|
LXA|贡嘎|Lhasa Gonggar|拉萨|Lhasa|
HET|白塔|Hohhot Baita|呼和浩特|Hohhot|
ZUH|金湾|Zhuhai Jinwan|珠海|Zhuhai|
SWA|潮汕|Jieyang Chaoshan|汕头|Shantou|潮汕,揭阳
KWL|两江|Guilin Liangjiang|桂林|Guilin|
LJG|三义|Lijiang Sanyi|丽江|Lijiang|
DYG|荷花|Zhangjiajie Hehua|张家界|Zhangjiajie|
WUX|硕放|Wuxi Shuofang|无锡|Wuxi|
CZX|奔牛|Changzhou Benniu|常州|Changzhou|
YNT|蓬莱|Yantai Penglai|烟台|Yantai|
WEH|大水泊|Weihai Dashuibo|威海|Weihai|
XUZ|观音|Xuzhou Guanyin|徐州|Xuzhou|
LYA|北郊|Luoyang Beijiao|洛阳|Luoyang|
MIG|南郊|Mianyang Nanjiao|绵阳|Mianyang|
ZHA|吴川|Zhanjiang|湛江|Zhanjiang|
BHY|福成|Beihai Fucheng|北海|Beihai|
YNJ|朝阳川|Yanji Chaoyangchuan|延吉|Yanji|
KHG|喀什|Kashgar|喀什|Kashgar|
DNH|莫高|Dunhuang Mogao|敦煌|Dunhuang|
JZH|黄龙|Jiuzhaigou Huanglong|九寨沟|Jiuzhaigou|
HKG|香港国际|Hong Kong|香港|Hong Kong|港,hongkong
MFM|澳门国际|Macau|澳门|Macau|
TPE|桃园|Taipei Taoyuan|台北|Taipei|
TSA|松山|Taipei Songshan|台北|Taipei|
KHH|小港|Kaohsiung|高雄|Kaohsiung|
NRT|成田|Tokyo Narita|东京|Tokyo|东京都
HND|羽田|Tokyo Haneda|东京|Tokyo|东京都
KIX|关西|Osaka Kansai|大阪|Osaka|
ITM|伊丹|Osaka Itami|大阪|Osaka|
NGO|中部|Nagoya Chubu|名古屋|Nagoya|
FUK|福冈|Fukuoka|福冈|Fukuoka|
CTS|新千岁|Sapporo New Chitose|札幌|Sapporo|北海道
OKA|那霸|Okinawa Naha|冲绳|Okinawa|那霸
HIJ|广岛|Hiroshima|广岛|Hiroshima|
SDJ|仙台|Sendai|仙台|Sendai|
KOJ|鹿儿岛|Kagoshima|鹿儿岛|Kagoshima|
NGS|长崎|Nagasaki|长崎|Nagasaki|
KMJ|熊本|Kumamoto|熊本|Kumamoto|
TAK|高松|Takamatsu|高松|Takamatsu|
HKD|函馆|Hakodate|函馆|Hakodate|
ICN|仁川|Seoul Incheon|首尔|Seoul|汉城
GMP|金浦|Seoul Gimpo|首尔|Seoul|汉城
PUS|金海|Busan Gimhae|釜山|Busan|
CJU|济州|Jeju|济州|Jeju|济州岛
TAE|大邱|Daegu|大邱|Daegu|
BKK|素万那普|Bangkok Suvarnabhumi|曼谷|Bangkok|
DMK|廊曼|Bangkok Don Mueang|曼谷|Bangkok|
HKT|普吉|Phuket|普吉|Phuket|普吉岛
CNX|清迈|Chiang Mai|清迈|Chiang Mai|
KBV|甲米|Krabi|甲米|Krabi|
USM|苏梅|Koh Samui|苏梅岛|Koh Samui|
SIN|樟宜|Singapore Changi|新加坡|Singapore|坡
KUL|吉隆坡国际|Kuala Lumpur|吉隆坡|Kuala Lumpur|
PEN|槟城|Penang|槟城|Penang|
BKI|亚庇|Kota Kinabalu|亚庇|Kota Kinabalu|沙巴
CGK|苏加诺哈达|Jakarta Soekarno-Hatta|雅加达|Jakarta|
DPS|伍拉莱|Bali Denpasar|巴厘岛|Bali|巴厘
MNL|尼诺阿基诺|Manila|马尼拉|Manila|
CEB|麦克坦|Cebu|宿务|Cebu|
KLO|卡利博|Kalibo|长滩岛|Boracay|
HAN|内排|Hanoi Noi Bai|河内|Hanoi|
SGN|新山一|Ho Chi Minh City|胡志明市|Ho Chi Minh City|西贡
DAD|岘港|Da Nang|岘港|Da Nang|
CXR|金兰|Nha Trang Cam Ranh|芽庄|Nha Trang|
PQC|富国|Phu Quoc|富国岛|Phu Quoc|
KTI|德崇|Phnom Penh Techo|金边|Phnom Penh|
SAI|吴哥|Siem Reap Angkor|暹粒|Siem Reap|吴哥
VTE|瓦岱|Vientiane|万象|Vientiane|
RGN|仰光|Yangon|仰光|Yangon|
DEL|英迪拉甘地|Delhi|新德里|Delhi|德里
BOM|贾特拉帕蒂|Mumbai|孟买|Mumbai|
KTM|特里布万|Kathmandu|加德满都|Kathmandu|
CMB|班达拉奈克|Colombo|科伦坡|Colombo|
MLE|马累|Male|马尔代夫|Maldives|马累
DXB|迪拜国际|Dubai|迪拜|Dubai|
AUH|阿布扎比|Abu Dhabi|阿布扎比|Abu Dhabi|
DOH|哈马德|Doha|多哈|Doha|
IST|伊斯坦布尔|Istanbul|伊斯坦布尔|Istanbul|
TLV|本古里安|Tel Aviv|特拉维夫|Tel Aviv|
LHR|希思罗|London Heathrow|伦敦|London|
LGW|盖特威克|London Gatwick|伦敦|London|
STN|斯坦斯特德|London Stansted|伦敦|London|
MAN|曼彻斯特|Manchester|曼彻斯特|Manchester|
EDI|爱丁堡|Edinburgh|爱丁堡|Edinburgh|
CDG|戴高乐|Paris Charles de Gaulle|巴黎|Paris|
ORY|奥利|Paris Orly|巴黎|Paris|
NCE|蔚蓝海岸|Nice|尼斯|Nice|
FRA|法兰克福|Frankfurt|法兰克福|Frankfurt|
MUC|慕尼黑|Munich|慕尼黑|Munich|
BER|勃兰登堡|Berlin Brandenburg|柏林|Berlin|
AMS|史基浦|Amsterdam Schiphol|阿姆斯特丹|Amsterdam|
BRU|布鲁塞尔|Brussels|布鲁塞尔|Brussels|
FCO|菲乌米奇诺|Rome Fiumicino|罗马|Rome|
MXP|马尔彭萨|Milan Malpensa|米兰|Milan|
VCE|马可波罗|Venice|威尼斯|Venice|
BCN|埃尔普拉特|Barcelona|巴塞罗那|Barcelona|
MAD|巴拉哈斯|Madrid|马德里|Madrid|
LIS|波特拉|Lisbon|里斯本|Lisbon|
ZRH|苏黎世|Zurich|苏黎世|Zurich|
GVA|日内瓦|Geneva|日内瓦|Geneva|
VIE|维也纳|Vienna|维也纳|Vienna|
PRG|布拉格|Prague|布拉格|Prague|
BUD|布达佩斯|Budapest|布达佩斯|Budapest|
WAW|肖邦|Warsaw|华沙|Warsaw|
CPH|哥本哈根|Copenhagen|哥本哈根|Copenhagen|
ARN|阿兰达|Stockholm Arlanda|斯德哥尔摩|Stockholm|
OSL|加勒穆恩|Oslo|奥斯陆|Oslo|
HEL|万塔|Helsinki|赫尔辛基|Helsinki|
DUB|都柏林|Dublin|都柏林|Dublin|
ATH|雅典|Athens|雅典|Athens|
KEF|凯夫拉维克|Reykjavik Keflavik|雷克雅未克|Reykjavik|冰岛
SVO|谢列梅捷沃|Moscow Sheremetyevo|莫斯科|Moscow|
LED|普尔科沃|Saint Petersburg|圣彼得堡|Saint Petersburg|
JFK|肯尼迪|New York JFK|纽约|New York|
EWR|纽瓦克|Newark|纽约|New York|
LGA|拉瓜迪亚|New York LaGuardia|纽约|New York|
LAX|洛杉矶国际|Los Angeles|洛杉矶|Los Angeles|
SFO|旧金山国际|San Francisco|旧金山|San Francisco|三藩市
SEA|西雅图塔科马|Seattle Tacoma|西雅图|Seattle|
ORD|奥黑尔|Chicago O'Hare|芝加哥|Chicago|
BOS|洛根|Boston Logan|波士顿|Boston|
IAD|杜勒斯|Washington Dulles|华盛顿|Washington|
ATL|哈茨菲尔德|Atlanta|亚特兰大|Atlanta|
MIA|迈阿密国际|Miami|迈阿密|Miami|
DFW|达福|Dallas Fort Worth|达拉斯|Dallas|
IAH|布什|Houston Bush|休斯顿|Houston|
LAS|哈里里德|Las Vegas|拉斯维加斯|Las Vegas|
DEN|丹佛国际|Denver|丹佛|Denver|
HNL|丹尼尔井上|Honolulu|檀香山|Honolulu|夏威夷
YVR|温哥华国际|Vancouver|温哥华|Vancouver|
YYZ|皮尔逊|Toronto Pearson|多伦多|Toronto|
YUL|特鲁多|Montreal|蒙特利尔|Montreal|
YYC|卡尔加里|Calgary|卡尔加里|Calgary|
SYD|金斯福德|Sydney|悉尼|Sydney|
MEL|墨尔本|Melbourne|墨尔本|Melbourne|
BNE|布里斯班|Brisbane|布里斯班|Brisbane|
PER|珀斯|Perth|珀斯|Perth|
AKL|奥克兰|Auckland|奥克兰|Auckland|
CHC|基督城|Christchurch|基督城|Christchurch|
CAI|开罗国际|Cairo|开罗|Cairo|
JNB|坦博|Johannesburg|约翰内斯堡|Johannesburg|
NBO|肯雅塔|Nairobi|内罗毕|Nairobi|
GRU|瓜鲁柳斯|Sao Paulo|圣保罗|Sao Paulo|
EZE|埃塞萨|Buenos Aires|布宜诺斯艾利斯|Buenos Aires|
MEX|贝尼托华雷斯|Mexico City|墨西哥城|Mexico City|
LIM|钱恰约|Lima|利马|Lima|
"""


class Airport:
    __slots__ = ("code", "name_cn", "name_en", "city_cn", "city_en", "aliases")

    def __init__(self, code, name_cn, name_en, city_cn, city_en, aliases):
        self.code, self.name_cn, self.name_en = code, name_cn, name_en
        self.city_cn, self.city_en, self.aliases = city_cn, city_en, aliases

    def as_dict(self):
        return {"code": self.code, "airport": self.name_cn, "airport_en": self.name_en,
                "city": self.city_cn, "city_en": self.city_en}


def _load_airports():
    out = []
    for line in AIRPORT_TABLE.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        code, name_cn, name_en, city_cn, city_en, aliases = (line.split("|") + [""])[:6]
        out.append(Airport(code, name_cn, name_en, city_cn, city_en,
                           [a for a in aliases.split(",") if a]))
    return out


AIRPORTS = _load_airports()


def _key(s):
    """归一化到「能对得上」的形式：全角转半角、大小写不敏感、去掉分隔符。"""
    return re.sub(r"[\s\-_·・'’.]+", "", unicodedata.normalize("NFKC", s).casefold())


def _index():
    """别名 -> 机场列表。城市名一对多(上海 -> PVG+SHA)，机场名一对一。"""
    idx = {}
    for a in AIRPORTS:
        keys = [a.city_cn, a.city_en, *a.aliases]        # 城市级：聚合同城机场
        for k in keys:
            idx.setdefault(_key(k), []).append(a)
    for a in AIRPORTS:
        for k in (a.code, a.name_cn, a.name_en):         # 机场级：精确到单个航站
            idx.setdefault(_key(k), [a])
    return idx


AIRPORT_INDEX = _index()

_IATA = re.compile(r"^[A-Za-z]{3}$")


class Place:
    """一次解析的结果：用户写的那个词 + 它对应的机场码。"""

    def __init__(self, query, codes, label, airports=(), passthrough=False):
        self.query, self.codes, self.label = query, codes, label
        self.airports, self.passthrough = list(airports), passthrough

    def as_dict(self):
        return {"query": self.query, "label": self.label, "codes": self.codes,
                "airports": [a.as_dict() for a in self.airports]}


def resolve(text, what="地点"):
    """把「上海」「Tokyo」「PVG」解析成机场码列表。

    只做精确匹配。模糊匹配是这类脚本最毒的坑 —— hotel-deals 里搜「苏州金鸡湖」
    返回了 30 公里外昆山的酒店，价格是真的、排版正常、没有任何报错。机票同理：
    把「宁波」模糊到「南宁」，用户看不出来。所以匹配不上就报错并给候选，
    绝不猜。

    表里没有的地方也不该被挡住：任何三个字母都当成 IATA 码原样放行(passthrough)，
    真假交给 Google 判 —— 码是假的时它会退化成落地页，google_search() 会把那种
    情况报成硬错误。
    """
    raw = (text or "").strip()
    if not raw:
        raise Failed(f"没给{what}", "origin 和 dest 都必须写明，例如 origin=上海、dest=东京")

    hit = AIRPORT_INDEX.get(_key(raw))
    if hit:
        codes = [a.code for a in hit]
        if len(hit) == 1:
            a = hit[0]
            label = f"{a.city_cn} {a.code}" if _key(raw) == _key(a.code) else \
                    airport_label(a)
        else:
            label = f"{hit[0].city_cn} {'/'.join(codes)}"
        return Place(raw, codes, label, hit)

    if _IATA.match(raw):
        code = raw.upper()
        return Place(raw, [code], code, passthrough=True)

    near = candidates(raw)
    hint = ("直接写三字码也行，例如 dest=NRT；"
            f"或跑 flight-deals airports {raw} 查一下")
    if near:
        hint = "可能是：" + "、".join(near[:8]) + "。" + hint
    raise Failed(f"不认识的{what}「{raw}」", hint)


def airport_label(a):
    """城市名 + 机场名 + 三字码，拼之前先去掉重复的城市名。

    表里 39 个机场的名字就是城市名(福冈福冈)，另有 11 个以城市名打头
    (洛杉矶洛杉矶国际)，直接拼会把城市名念两遍。砍掉前缀剩下的正好是
    「国际」「塔科马」这种真正区分航站的部分；砍完是空的就只留城市名。
    """
    if a.name_cn.startswith(a.city_cn):
        tail = a.name_cn[len(a.city_cn):]
    else:
        tail = "" if _key(a.name_cn) == _key(a.city_cn) else a.name_cn
    return f"{a.city_cn}{tail} {a.code}"


def candidates(text, limit=12):
    """含子串的候选。只用来写进报错里帮人纠正，绝不参与自动选择。"""
    k = _key(text)
    out = []
    for a in AIRPORTS:
        fields = [a.code, a.name_cn, a.name_en, a.city_cn, a.city_en, *a.aliases]
        blob = _key("".join(fields))
        hit = k in blob or blob.startswith(k)
        if not hit:
            hit = any(len(f) >= 2 and _key(f) in k for f in fields if f)
        if k and hit:
            out.append(airport_label(a))
    return out[:limit]


def route_key(origin, dest):
    """历史库里认航线用的键。用码而不是用户写的词，"上海"和"PVG"才能并到一起。"""
    return f"{','.join(sorted(origin.codes))}>{','.join(sorted(dest.codes))}"


# ---------------------------------------------------------------- 筛选与排序

# Google 给的是英文航司名；国内用户想筛「南航」不该被迫去查它叫 China Southern。
# 只收常用的，匹配不上还能直接写英文名的任意片段。
AIRLINE_ALIAS = {
    "国航": "Air China", "中国国航": "Air China",
    "中国国际航空": "Air China", "ca": "Air China",
    "东航": "China Eastern", "东方航空": "China Eastern",
    "中国东方航空": "China Eastern", "mu": "China Eastern",
    "南航": "China Southern", "南方航空": "China Southern",
    "中国南方航空": "China Southern", "cz": "China Southern",
    "海航": "Hainan", "海南航空": "Hainan", "hu": "Hainan",
    "春秋": "Spring", "春秋航空": "Spring", "9c": "Spring",
    "吉祥": "Juneyao", "ho": "Juneyao",
    "厦航": "Xiamen", "mf": "Xiamen",
    "深航": "Shenzhen", "zh": "Shenzhen",
    "山航": "Shandong", "sc": "Shandong",
    "川航": "Sichuan", "3u": "Sichuan",
    "国泰": "Cathay", "cx": "Cathay",
    "长荣": "EVA", "br": "EVA",
    "华航": "China Airlines", "ci": "China Airlines",
    "全日空": "ANA", "全日本空输": "ANA", "nh": "ANA",
    "日航": "JAL", "日本航空": "JAL", "jl": "JAL",
    "乐桃": "Peach", "桃航": "Peach", "mm": "Peach",
    "捷星": "Jetstar", "gk": "Jetstar", "jq": "Jetstar",
    "星悦": "Star Flyer", "7g": "Star Flyer",
    "天马": "Skymark", "bc": "Skymark",
    "亚航": "AirAsia", "亚洲航空": "AirAsia", "ak": "AirAsia", "d7": "AirAsia",
    "大韩": "Korean", "大韩航空": "Korean", "ke": "Korean",
    "韩亚": "Asiana", "oz": "Asiana",
    "真航空": "Jin Air", "lj": "Jin Air",
    "济州航空": "Jeju", "7c": "Jeju",
    "釜山航空": "Air Busan", "bx": "Air Busan",
    "德威": "T'way", "tw": "T'way",
    "新航": "Singapore", "新加坡航空": "Singapore", "sq": "Singapore",
    "酷航": "Scoot", "tr": "Scoot",
    "泰航": "Thai", "泰国航空": "Thai", "tg": "Thai",
    "越捷": "VietJet", "vj": "VietJet",
    "越南航空": "Vietnam Airlines", "vn": "Vietnam Airlines",
    "阿联酋": "Emirates", "ek": "Emirates",
    "卡塔尔": "Qatar", "qr": "Qatar",
    "土耳其": "Turkish", "tk": "Turkish",
}


def airline_query(word):
    """把用户写的航司词变成用来做子串匹配的英文片段。"""
    w = word.strip()
    return AIRLINE_ALIAS.get(w.casefold(), w).casefold()


def pos_int(text):
    """必须 >= 1 的整数。不拦的话 top=-3 会印「显示 -3/32 条」再把末尾三条
    切掉，--adults 0 会一路跑到底再印「0 成人」。"""
    try:
        v = int(str(text).strip())
    except ValueError:
        raise Failed(f"要一个整数：{text}")
    if v < 1:
        raise Failed(f"至少得是 1：{text}")
    return v


def nonneg_int(text):
    try:
        v = int(str(text).strip())
    except ValueError:
        raise Failed(f"要一个整数：{text}")
    if v < 0:
        raise Failed(f"不能是负数：{text}")
    return v


def parse_time_range(text):
    """接受 08:00-18:00 或 8-18（只写小时）。返回 (起, 止) 两个 time。"""
    parts = re.split(r"[-~]", str(text).strip())
    if len(parts) != 2:
        raise Failed(
            f"看不懂的时间段：{text}（写成 08:00-18:00 或 8-18）")
    out = []
    for part in parts:
        part = part.strip()
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", part)
        if not m or int(m.group(1)) > 24:
            raise Failed(f"看不懂的时刻：{part}")
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if mm > 59:
            raise Failed(f"看不懂的时刻：{part}（分钟最大 59）")
        # 24 点当天末尾讲。写成 hh=min(hh,23) 会把 24:00 悄悄变成 23:00，
        # 静默吃掉最后一小时 —— 而 `-24` 是 23:59，同一个意思两种结果。
        if hh == 24:
            if mm:
                raise Failed(
                    f"看不懂的时刻：{part}（24 点之后没有了，写 24 或 23:59）")
            hh, mm = 23, 59
        out.append(dtime(hh, mm))
    if out[0] > out[1]:
        raise Failed(
            f"时间段头尾反了：{text}（跨零点的段没法表达，拆成两次查）")
    return tuple(out)


SORTS = {
    "price": lambda i: (i.price, i.duration_min or 1 << 30),
    "duration": lambda i: (i.duration_min or 1 << 30, i.price),
    "depart": lambda i: (i.dep_at or datetime.max, i.price),
    "arrive": lambda i: (i.arr_at or datetime.max, i.price),
    "stops": lambda i: (i.stops if i.stops is not None else 9, i.price),
}
SORT_CN = {"price": "价格", "duration": "时长", "depart": "起飞时间",
           "arrive": "到达时间", "stops": "经停次数"}


def apply_filters(itins, airlines=None, depart_window=None, max_stops=None):
    """按航司 / 起飞时间段 / 经停次数筛。返回 (剩下的, 被筛掉的条数)。

    起飞时间段是这里最有用的一个：同价同航司的航班往往有十几二十班，差别只在
    几点起飞 —— 与其把它们折叠起来藏掉，不如让人直接说「我要下午走的」。
    """
    keep = list(itins)
    if airlines:
        # 纯子串匹配会让 ANA 命中 Asiana(Asi-ana)、CI 命中 Pacific —— 实测踩到。
        # 前后不许再挨着字母，短名/两字代码才不会到处误伤；"China Southern"
        # 这种带空格的多词名照样能匹配。
        pat = re.compile("|".join(
            f"(?<![A-Za-z]){re.escape(airline_query(a))}(?![A-Za-z])"
            for a in airlines), re.I)
        keep = [i for i in keep if any(pat.search(n) for n in i.airlines)]
    if depart_window:
        lo, hi = depart_window
        keep = [i for i in keep if i.dep_at and lo <= i.dep_at.time() <= hi]
    if max_stops is not None:
        keep = [i for i in keep if i.stops is not None and i.stops <= max_stops]
    return keep, len(itins) - len(keep)


def seat_summary(itins, currency):
    """每个舱位的最低价。舱位之间往往差好几倍，摆在一起才看得出值不值。"""
    best = {}
    for i in itins:
        # 刷新最低价时必须把已有计数带上：写成 (price, 0) 会把前面数过的班次
        # 清零，于是只数了「最后一次刷新最低价之后的那几班」。按价排序时第一
        # 条就是最低价、之后不再刷新，所以只测默认排序发现不了。
        cur = best.get(i.seat)
        if cur is None or i.price < cur[0]:
            best[i.seat] = (i.price, cur[1] + 1 if cur else 1)
        else:
            best[i.seat] = (cur[0], cur[1] + 1)
    return " · ".join(f"{SEAT_CN[k]} {money(v[0], currency)}({v[1]}班)"
                      for k, v in sorted(best.items()))


def airline_summary(itins, currency, limit=12):
    """每家航司的最低价和班次数 —— 手机 app 的航司筛选面板就是这么给的，
    看完才知道 airline 该填谁。"""
    best = {}
    for i in itins:
        name = "/".join(i.airlines[:2]) or "?"
        cur = best.get(name)
        if cur is None or i.price < cur[0]:
            best[name] = (i.price, cur[1] + 1 if cur else 1)
        else:
            best[name] = (cur[0], cur[1] + 1)
    rows = sorted(best.items(), key=lambda kv: kv[1][0])
    out = [f"{n} {money(p, currency)}({c}班)" for n, (p, c) in rows[:limit]]
    if len(rows) > limit:
        out.append(f"等 {len(rows)} 家")
    return " · ".join(out)


# ---------------------------------------------------------------- 数据结构

class Itin:
    """一趟单程航班。price 是它的起价（各票种里最低的那个）。"""

    __slots__ = ("price", "currency", "stops", "airlines",
                 "dep_airport", "dep_at", "arr_airport", "arr_at",
                 "duration_min", "layovers", "url", "seat", "flights", "planes")

    def __init__(self, price, currency, stops, airlines,
                 dep_airport, dep_at, arr_airport, arr_at, duration_min,
                 layovers, url=None, seat=1):
        self.price, self.currency = price, currency
        self.stops, self.airlines = stops, airlines
        self.dep_airport, self.dep_at = dep_airport, dep_at
        self.arr_airport, self.arr_at = arr_airport, arr_at
        self.duration_min, self.layovers, self.url = duration_min, layovers, url
        self.seat = seat
        self.flights = []        # 航班号，多航段就是多个；抽不到时为空
        self.planes = []

    def as_dict(self):
        return {
            "price": self.price, "currency": self.currency,
            "seat": SEATS_INV[self.seat],
            "flight_numbers": self.flights, "aircraft": self.planes,
            "stops": self.stops, "airlines": self.airlines,
            "depart_airport": self.dep_airport,
            "depart_at": self.dep_at.isoformat() if self.dep_at else None,
            "arrive_airport": self.arr_airport,
            "arrive_at": self.arr_at.isoformat() if self.arr_at else None,
            "duration_min": self.duration_min,
            "layovers": [{"duration_min": d, "place": p, "change_airport": x}
                         for d, p, x in self.layovers]}


def fmt_dur(minutes):
    if minutes is None:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


# ---------------------------------------------------------------- 解析 aria-label

# 卡片把整段行程写在一个 aria-label 里，首屏 HTML 就有。实测(2026-09)样例：
#   From 5677 Chinese yuan. 1 stop flight with China Eastern and Air France.
#   Leaves Chengdu Shuangliu International Airport at 1:30 PM on Thursday,
#   October 1 and arrives at Aéroport de Paris-Charles de Gaulle at 5:50 AM on
#   Friday, October 2. Total duration 22 hr 20 min.  Layover (1 of 1) is a
#   5 hr layover at Shanghai Pudong International Airport in Shanghai.
# 往返时价格那句变成 "From 5764 Chinese yuan round trip total."
_CARD = re.compile(r'aria-label="(From [^"]+)"')
_L_PRICE = re.compile(r"^From ([\d,]+(?:\.\d+)?) ([A-Za-z][A-Za-z .]*?)\.")
_L_STOPS = re.compile(r"(?:^|\.\s*)(Nonstop|(\d+)\s+stops?) flight with ([^.]+)\.")
_L_LEG = re.compile(r"Leaves (.+?) at (\d{1,2}:\d{2}\s*[AP]M) on ([^,]+, \w+ \d{1,2})"
                    r" and arrives at (.+?) at (\d{1,2}:\d{2}\s*[AP]M)"
                    r" on ([^,]+, \w+ \d{1,2})\.")
_L_DUR = re.compile(r"Total duration ([^.]+?)\.")
# 中转有两种句式，实测都出现过：
#   …layover at Shanghai Pudong International Airport in Shanghai.
#   …layover in Taipei City. Transfer here from Taiwan Taoyuan to Taipei Songshan.
# 后一种是「中转要换机场」，对行程可行性影响极大(得跨城跑一趟)，必须显示出来。
# 只写 "at" 那版正则的话，换机场的中转会静默变成一个空白的中转格。
_L_LAYOVER = re.compile(r"Layovers? \(\d+ of \d+\) is an? (.+?) layover "
                        r"(?:at (?P<ap>[^.]+?)(?: in (?P<city1>[^.]+))?"
                        r"|in (?P<city2>[^.]+))\."
                        r"(?P<xfer>\s*Transfer here from [^.]+\.)?")
# 航班号和机型不在 aria-label 上，但整份航段明细就嵌在首屏的 AF_initDataCallback
# 数据块里。那份数据是一大坨下标定位的数组，逐层写死下标很脆，所以这里只按「形状」
# 认航段（第 3/6 位是三字码、第 4/5 位是机场全名），Google 挪动别的字段也不影响。
#
# 这条链路是**锦上添花**：抽不到就没有航班号，aria-label 那条主链路照常出结果。
# 反过来做（改用数据块当主源）风险大得多 —— 它没有任何自解释的字段名。
_AF_BLOB = re.compile(r"AF_initDataCallback\((\{.*?\})\);</script>", re.S)
_AF_DATA = re.compile(r"data:(\[.*\]), sideChannel", re.S)
_IATA3 = re.compile(r"[A-Z]{3}")
_FN_AIRLINE = re.compile(r"[A-Z0-9]{2}")
_FN_NUMBER = re.compile(r"\d{1,4}[A-Z]?")


def _is_segment(n):
    return (isinstance(n, list) and len(n) > 22
            and isinstance(n[3], str) and _IATA3.fullmatch(n[3])
            and isinstance(n[6], str) and _IATA3.fullmatch(n[6])
            and isinstance(n[4], str) and isinstance(n[5], str))


def _hhmm(t):
    if not isinstance(t, list) or not t or not isinstance(t[0], int):
        return None
    return f"{t[0]:02d}:{(t[1] if len(t) > 1 and isinstance(t[1], int) else 0):02d}"


def _segment_flight(seg):
    v = seg[22]
    if (isinstance(v, list) and len(v) >= 2 and isinstance(v[0], str)
            and isinstance(v[1], str)
            and _FN_AIRLINE.fullmatch(v[0]) and _FN_NUMBER.fullmatch(v[1])):
        return v[0] + v[1]
    return None


def _walk_segments(node, out):
    if not isinstance(node, list):
        return
    if node and all(_is_segment(x) for x in node):
        first, last = node[0], node[-1]
        key = (first[4], last[5], _hhmm(first[8]), _hhmm(last[10]), len(node))
        flights = [f for f in map(_segment_flight, node) if f]
        if flights and all(key[:4]):
            planes = [x[17] for x in node if isinstance(x[17], str) and x[17]]
            vias = [s[5] for s in node[:-1]]      # 中转机场，撞键时拿来消歧
            same = out.setdefault(key, [])
            # 同一份行程在页面里会重复出现（推荐区 + 全部结果），去个重，
            # 剩下的才是真·不同航班。
            if not any(c[0] == flights for c in same):
                same.append((flights, planes, vias))
        return
    for v in node:
        _walk_segments(v, out)


def flight_index(page):
    """(出发机场, 到达机场, 起飞时刻, 到达时刻, 段数) -> [(航班号, 机型, 中转机场)]。

    这五样 aria-label 那边全拿得到，才能对号。少一样都不够唯一：只用
    (出发机场, 起降时刻) 时，北京→洛杉矶一天里就有 6 组行程同起飞机场、同起降
    时刻而中转点不同；同城多机场查询（东京→上海 PVG/SHA）还会撞上落地机场不同
    的行程。所以值是一串候选而不是一个 —— 撞键时交给 match_flights 再筛一道，
    筛不出唯一的就整行不印，绝不先到先得地瞎猜一个。
    """
    out = {}
    for blob in _AF_BLOB.findall(page):
        m = _AF_DATA.search(blob)
        if not m:
            continue
        try:
            _walk_segments(json.loads(m.group(1)), out)
        except Exception:      # 数据块换了形状就当没有，绝不能带崩主链路
            continue
    return out


def _norm_place(s):
    """归一化地名，好让「Guangzhou」跟「Guangzhou Baiyun International Airport」
    能互相包含 —— aria-label 那边给的有时是城市名，有时是机场全名。"""
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s or "").casefold())


def match_flights(it, index):
    """给一条行程贴上航班号和机型。对不上就原样不动。

    宁可整行印「—」，也不能印错一个号 —— 人是真会照着这串号去买票的。
    """
    if not (it.dep_airport and it.arr_airport and it.dep_at and it.arr_at
            and it.stops is not None):
        return
    segs = it.stops + 1
    # 段数对不上的候选直接扔：某一段的号没抽出来时列表会变短，印出来就成了
    # 「2 转却只有两个号」，比不印更误导。
    pool = [c for c in index.get((it.dep_airport, it.arr_airport,
                                  it.dep_at.strftime("%H:%M"),
                                  it.arr_at.strftime("%H:%M"), segs), ())
            if len(c[0]) == segs]
    if len(pool) > 1:
        # 同起降机场、同起降时刻、同段数的行程真的存在（北京→洛杉矶一天就有六组，
        # 全是绕不同城市飞广州、再接同一班跨洋），拿中转地名再筛一道。
        want = [_norm_place(lay[1]) for lay in it.layovers]
        pool = [c for c in pool if len(c[2]) == len(want)
                and all(w and (w in _norm_place(v) or _norm_place(v) in w)
                        for w, v in zip(want, c[2]))]
    if len(pool) == 1:
        it.flights, it.planes = pool[0][0], pool[0][1]


# 只有真的出了结果列表的页面才有排序控件。tfs 拼错时 Google 不报错，而是返回
# 一张促销落地页 —— 两者都是 HTTP 200、都能解析出 0 条，靠这个标记区分。
_RESULTS_MARK = re.compile(r'aria-label="Sorted by [^"]*"')

_DUR = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?")


def parse_duration(text):
    m = _DUR.search(text.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def parse_when(daytext, timetext, ref):
    """把 "Thursday, October 1" + "8:05 PM" 还原成 datetime。

    页面上不带年份 —— 跨年行程(12/31 走 1/1 到)会被解析成往回退一年，所以年份
    必须靠出发日 ref 推：取「不早于 ref 前一天」的最近那个年份。
    """
    m = re.search(r"(\w+)\s+(\d{1,2})$", daytext.strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    month, day = _MONTHS[m.group(1)], int(m.group(2))
    # Google 在时刻和 AM/PM 之间用的是 U+202F 窄不换行空格，不是普通空格 ——
    # replace(" ", "") 清不掉它，strptime 会直接抛 ValueError 把整次查询带崩。
    t = datetime.strptime(re.sub(r"\s+", "", timetext).upper(), "%I:%M%p").time()
    # 1 月 1 日出发、跨日期变更线「12 月 31 日」到达的行程,年份得往前一年找,
    # 所以候选从 ref.year - 1 起;取「不早于 ref 前一天」的最近那个。
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= ref - timedelta(days=1):
            return datetime.combine(d, t)
    return None


def dedupe(itins):
    """同一趟航班在页面上会被写进不止一个 aria-label（列表一次、详情锚点一次），
    照单全收会让结果表里连着好几行完全相同 —— 看起来像脚本坏了。按「价格 + 起降
    时刻 + 航司 + 时长」认同一趟；这几项全同却是两个航班的情况(共享航班号)在
    aria-label 里本来就分辨不出来，留一行反而更诚实。"""
    seen, out = set(), []
    for it in itins:
        key = (it.price, it.dep_at, it.arr_at, tuple(it.airlines), it.stops,
               it.duration_min)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def page_currency(labels):
    """页面上真实的币种名（"Chinese yuan" / "US dollars"）。取不到返回 None。"""
    for raw in labels:
        m = _L_PRICE.match(_html.unescape(raw))
        if m:
            return m.group(2).strip().lower()
    return None


def currency_mismatch(labels, want):
    """请求的币种没生效就返回页面上的真实币种名，生效则返回 None。"""
    got = page_currency(labels)
    if got is None:
        return None
    names = CURRENCY_NAMES.get(want.upper())
    if names:
        return None if got in names else got
    return got if got in CURRENCY_NAMES["USD"] else None


def parse_card(label, currency, ref_day, url=None):
    """把一条 aria-label 解析成 Itin。解析不出价格就返回 None。"""
    m = _L_PRICE.match(label)
    if not m:
        return None
    price = float(m.group(1).replace(",", ""))

    stops, airlines = None, []
    if s := _L_STOPS.search(label):
        stops = 0 if s.group(1) == "Nonstop" else int(s.group(2))
        airlines = [x.strip() for x in re.split(r",| and ", s.group(3)) if x.strip()]

    dep_ap = dep_at = arr_ap = arr_at = None
    if g := _L_LEG.search(label):
        dep_ap, arr_ap = g.group(1).strip(), g.group(4).strip()
        dep_at = parse_when(g.group(3), g.group(2), ref_day)
        arr_at = parse_when(g.group(6), g.group(5), ref_day)

    duration = None
    if d := _L_DUR.search(label):
        duration = parse_duration(d.group(1))

    # 中转地优先用句尾那个城市名。机场全称("Hong Kong International Airport")
    # 在表格里放不下，靠取首个单词缩写会把「Hong Kong」截成「Hong」。
    layovers = []
    for m in _L_LAYOVER.finditer(label):
        place = (m.group("city1") or m.group("city2") or m.group("ap") or "").strip()
        layovers.append((parse_duration(m.group(1)), place, bool(m.group("xfer"))))

    return Itin(price, currency, stops, airlines, dep_ap, dep_at,
                arr_ap, arr_at, duration, layovers, url)


# ---------------------------------------------------------------- 数据源：Google

FLIGHTS_URL = "https://www.google.com/travel/flights"
# 结果页视图描述符。跟查询内容无关，是个常量，照抄浏览器发的那个就行。
TFU = "EgQIABABIgA"


def flights_url(origin, dest, depart, seat, adults, children, currency,
                max_stops, region="us"):
    tfs = build_tfs(depart, origin.codes, dest.codes, seat, adults, children,
                    max_stops)
    return FLIGHTS_URL + "?" + urllib.parse.urlencode({
        "tfs": tfs,
        # 固定英文站：卡片的 aria-label 只有英文站是这套句式，换 hl 会整套解析失效。
        "hl": "en", "gl": region, "curr": currency, "tfu": TFU})


def google_search(origin, dest, depart, seat=1, adults=1, children=0,
                  currency="CNY", max_stops=None, limit=60, region="us",
                  timeout=40, tries=3):
    """纯 HTTP 抓 Google 航班。行程信息都在卡片的 aria-label 上，首屏 HTML 就有。"""
    url = flights_url(origin, dest, depart, seat, adults, children,
                      currency, max_stops, region)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    # 并发扫窗口时 Google 偶尔会回一张没有结果列表的页面 —— 内容不全，但 HTTP
    # 200。不重试的话这种偶发会被下面诊断成「没认出这条航线」，而同一条航线
    # 其他日期明明查得好好的，等于把人往「核对三字码」这条错路上引。
    # tfs 真拼错是确定性的，重试同样失败，所以重试次数正好把两者分开。
    page, has_results, err = None, False, None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                page = r.read().decode("utf-8", "replace")
        except Exception as exc:
            err = exc
        if page is not None and _RESULTS_MARK.search(page):
            has_results = True
            break
        if attempt < tries - 1:
            time.sleep(0.7 * (attempt + 1))
    if page is None:
        raise Failed(f"Google 请求失败（重试 {tries} 次）：{err}", "检查网络后重试")

    labels = _CARD.findall(page)
    itins = []
    for raw in labels:
        it = parse_card(_html.unescape(raw), currency, depart, url)
        if it is not None:
            itins.append(it)

    # 币种没生效时必须硬停：数字照出、单位是别人的，比报错危险得多。
    if itins and (actual := currency_mismatch(labels, currency)):
        raise Failed(
            f"currency={currency} 没被 Google 接受，回来的价格其实是「{actual}」",
            "币种要写 ISO 三字码（人民币是 CNY，不是 RMB）；"
            "常用 CNY/JPY/USD/EUR/HKD/KRW/GBP/TWD/SGD/AUD")

    itins = dedupe(itins)

    if not itins:
        # 三种「零结果」必须分清楚，否则调用方会把「参数没生效」当成「没航班」。
        if not has_results:
            bad = [p.query for p in (origin, dest) if p.passthrough]
            # 三种可能，按实测中的常见程度排：Google 在「这条航线一班都连不上」
            # 时同样不渲染结果页，所以不能张口就说机场码有问题。
            raise Failed(
                f"重试 {tries} 次都没拿到结果列表"
                f"（{origin.label} → {dest.label} {depart}）",
                "多半是这条航线在这个日期一班都连不上（Google 这时不出结果页）"
                "，换个日期或改用更大的机场试试；"
                + (f"也核对下机场码 {'、'.join(bad)} 是否还有效"
                   "（有的机场改过码，如金边 PNH→KTI、暹粒 REP→SAI）；"
                   if bad else "")
                + "同一航线换日期能查通就只是偶发限流，稍后重试即可")
        return []                      # 页面正常但真的一班都没有
    if not has_results:
        raise Failed("页面结构变了：解析出了价格，却找不到结果列表标记",
                     "多半是 Google 改版，需要更新脚本里的 _RESULTS_MARK/_L_* 正则")

    index = flight_index(page)
    for it in itins:
        it.seat = seat
        match_flights(it, index)
    itins.sort(key=lambda i: i.price)
    return itins[:limit]


# ---------------------------------------------------------------- 价格历史

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL, route TEXT NOT NULL,
    origin TEXT NOT NULL, dest TEXT NOT NULL,
    depart TEXT NOT NULL,
    seat INTEGER NOT NULL, adults INTEGER NOT NULL,
    currency TEXT NOT NULL, price REAL NOT NULL,
    stops INTEGER, airlines TEXT, duration_min INTEGER);
CREATE INDEX IF NOT EXISTS idx_obs_route ON observations (route, depart, observed_at);
"""


# 写入时用到的列。改这里就要同时改 SCHEMA —— reshape() 靠两者对比判断老库该不该重建。
COLUMNS = ("observed_at", "route", "origin", "dest", "depart", "seat", "adults",
           "currency", "price", "stops", "airlines", "duration_min")
# 新表里 NOT NULL 的那些。老库缺其中任何一列就没法搬，只能从头攒。
REQUIRED = {"observed_at", "route", "origin", "dest", "depart", "seat", "adults",
            "currency", "price"}


def db():
    if not DB_PATH:
        raise Failed("没有 MIYU_SCRIPT_STATE_DIR 或 MIYU_SCRIPT_CACHE_DIR，价格历史存不住",
                     "在 Miyu 里调用；或者设 no_record=true")
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record(origin, dest, itins, adults):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = route_key(origin, dest)
    rows = []
    for it in itins:
        if it.price is None or it.dep_at is None:
            continue
        rows.append((now, key, origin.label, dest.label,
                     it.dep_at.date().isoformat(), it.seat, adults, it.currency,
                     it.price, it.stops, ",".join(it.airlines), it.duration_min))
    with db() as conn:
        conn.executemany(
            "INSERT INTO observations (observed_at, route, origin, dest, depart,"
            " seat, adults, currency, price, stops, airlines,"
            " duration_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def floor_history(route, depart=None, currency=None, seat=None, adults=None):
    """一条航线的历史底价。币种/舱位/人数必须一起卡死 —— 同一条线上混进一次
    JPY 的观测，图上就会出现「一夜跌了 95%」这种纯属换算单位的假信号，商务舱
    和多人票同理。"""
    sql = ("SELECT observed_at, MIN(price) AS floor, currency FROM observations"
           " WHERE route = ?")
    args = [route]
    for col, val in (("depart", depart.isoformat() if depart else None),
                     ("currency", currency), ("seat", seat), ("adults", adults)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " GROUP BY substr(observed_at, 1, 13) ORDER BY observed_at"
    with db() as conn:
        return [(r["observed_at"], r["floor"], r["currency"])
                for r in conn.execute(sql, args)]


def known_routes():
    with db() as conn:
        return list(conn.execute(
            "SELECT route, origin, dest, COUNT(*) n, MAX(observed_at) last"
            " FROM observations GROUP BY route ORDER BY last DESC"))


# ---------------------------------------------------------------- 扫日期窗口

def plan_combos(window, day_specs):
    """窗口里所有「玩 N 天」的出发/返程组合，以及为此需要扫哪些日期。

    关键在于组合是**算**出来的，不是**查**出来的：去程和回程各自独立定价，
    所以扫 N 个出发日 + M 个返程日（N+M 次请求）就能拼出全部 N×M 个组合，
    不必真去查 N×M 次。原先按组合逐个查往返票，12 个组合就要 12 次请求；
    现在同样的窗口 20 次请求能给出 100 个组合，而且每个价格都是确定的。

    返程日必须落在窗口内：窗口就是「这段时间我能出门」的意思。
    """
    start, end = window
    combos, day = [], start
    while day <= end:
        for n in day_specs:
            back = day + timedelta(days=n)
            if back <= end:
                combos.append((day, back))
        day += timedelta(days=1)
    out_days = sorted({d for d, _ in combos})
    back_days = sorted({r for _, r in combos})
    return combos, out_days, back_days


def scan_days(origin, dest, days, seat, adults, children, currency, max_stops,
              workers=6, progress=None, done_base=0, total=None):
    """并发扫一串出发日的**单程**最低价。实测 6 并发 ~0.4 秒/天，无限流。"""

    def one(day):
        try:
            itins = google_search(origin, dest, day, seat, adults,
                                  children, currency, max_stops, limit=60)
        except Failed as exc:               # 单天失败不该拖垮整张表
            return {"day": day, "floor": None, "error": str(exc)[:70]}
        if not itins:
            return {"day": day, "floor": None, "error": "无航班"}
        best = itins[0]                 # google_search 返回时已按价排好
        prices = [i.price for i in itins]
        direct = [i for i in itins if i.stops == 0]
        durs = [i.duration_min for i in itins if i.duration_min]
        return {"day": day, "floor": best.price,
                "median": statistics.median(prices), "count": len(itins),
                "direct_floor": direct[0].price if direct else None,
                "airlines": "/".join(best.airlines[:2]),
                "stops": best.stops, "duration_min": best.duration_min,
                # 这一天真正最快的班，跟最低价那班往往不是同一个。
                "fastest_min": min(durs) if durs else None,
                "error": None}

    out, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(one, days):
            out[res["day"]] = res
            done += 1
            if progress:
                progress(done_base + done, total or len(days))
    return out


def combine(combos, out_map, back_map):
    """把两个方向的扫描结果拼成完整行程。价格是两段单程之和 —— 全部确定值。"""
    rows = []
    for dep, ret in combos:
        o, b = out_map.get(dep), back_map.get(ret)
        total = None
        if o and b and o["floor"] is not None and b["floor"] is not None:
            total = o["floor"] + b["floor"]
        rows.append({"depart": dep, "ret": ret, "nights": (ret - dep).days,
                     "total": total, "out": o, "back": b})
    return rows



# ---------------------------------------------------------------- 输出

def wcwidth(s):
    """CJK 字符占两格，不算宽度表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, width, align="<"):
    s = str(s)
    gap = width - wcwidth(s)
    if gap < 0:
        out = ""
        for c in s:
            if wcwidth(out + c) > width - 1:
                break
            out += c
        return out + "…" + " " * (width - wcwidth(out) - 1)
    return (" " * gap + s) if align == ">" else (s + " " * gap)


def table(headers, rows, aligns=None, md=False):
    """对齐的纯文本表格；md=True 出 Markdown。"""
    if not rows:
        return ""
    aligns = aligns or ["<"] * len(headers)
    widths = [max(wcwidth(h), max(wcwidth(r[i]) for r in rows))
              for i, h in enumerate(headers)]
    if md:
        sep = ["---:" if a == ">" else "---" for a in aligns]
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
        lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        return "\n".join(lines)
    out = ["  ".join(pad(h, widths[i], aligns[i]) for i, h in enumerate(headers)).rstrip(),
           "  ".join("─" * widths[i] for i in range(len(headers)))]
    out += ["  ".join(pad(c, widths[i], aligns[i])
                      for i, c in enumerate(r)).rstrip() for r in rows]
    return "\n".join(out)


def money(v, currency):
    return "—" if v is None else f"{SYMBOL.get(currency, currency + ' ')}{v:,.0f}"


def week_cn(d):
    return "一二三四五六日"[d.weekday()]


def sparkline(values):
    real = [v for v in values if v is not None]
    if not real:
        return ""
    lo, hi = min(real), max(real)
    span = hi - lo
    return "".join(
        " " if v is None else
        SPARK[len(SPARK) // 2] if span == 0 else
        SPARK[int((v - lo) / span * (len(SPARK) - 1))]
        for v in values)


def price_note(adults, children, seats):
    who = f"{adults} 成人" + (f" + {children} 儿童" if children else "")
    cabins = seats if isinstance(seats, (list, tuple)) else [seats]
    return (f"价格为单程起价（{who}，"
            f"{'/'.join(SEAT_CN[s] for s in cabins)}），实际下单价可能更高")


def search_payload(itins, origin, dest, depart, seats, adults, children,
                   currency, top, urls, warnings=(), sort="price",
                   filtered_out=0, filters=()):
    """一次单程查询的机器可读形态。单程和往返两处 JSON 都拿它拼，往返才不会
    多出一套「跟单程长得不一样」的字段名。"""
    return {
        "query": {"origin": origin.as_dict(), "destination": dest.as_dict(),
                  "depart": depart.isoformat(),
                  "seats": [SEATS_INV[s] for s in seats],
                  "adults": adults, "children": children,
                  "currency": currency},
        "price_means": "one_way_total",
        "sorted_by": sort,
        "filters": list(filters),
        "filtered_out": filtered_out,
        "warnings": list(warnings),
        "returned": min(top, len(itins)), "total": len(itins),
        "urls": [{"seat": n, "url": u} for n, u in urls],
        "itineraries": [i.as_dict() for i in itins[:top]],
    }


def more_note(itins, top):
    """「还有 N 条没显示」。单程和往返都要出这句，别各写一份。"""
    if len(itins) <= top:
        return None
    return (f"还有 {len(itins) - top} 条没显示 —— 调大 top，或用 "
            "airline、depart_time、max_stops 缩小范围"
            "（同价同航司常有十几班，差别只在几点起飞）")


def render_search(itins, origin, dest, depart, seats, adults, children,
                  currency, top, fmt, urls, warnings=(), sort="price",
                  filtered_out=0, filters=(), lead=None, standalone=True):
    """出报表。

    lead 非空时这只是往返报表里的一段（「去程」/「回程」），注意栏和链接
    交给外面统一收拢 —— 两段各出一份注意栏，同样的话要读两遍。

    warnings 一定要进 stdout：程序调用时通常只收 stdout，写进 stderr 的提醒
    等于没写 —— 「机场码是猜的」和「机场码是查表来的」输出会长得一模一样。
    """
    if fmt == "json":
        return json.dumps(
            search_payload(itins, origin, dest, depart, seats, adults, children,
                           currency, top, urls, warnings, sort, filtered_out,
                           filters), ensure_ascii=False, indent=2)

    md = fmt == "md"
    multi_seat = len(seats) > 1
    # 航班号来自页面里的数据块，Google 换了结构就抽不到；那时整列不出现，
    # 而不是留一列破折号。判据是「这批结果一个号都没抽到」，不能只看要显示的
    # 那几条 —— 前几条恰好没号就整列消失，同一条命令换个 --top 表头都变了。
    has_flights = any(i.flights for i in itins)
    head = (f"{lead + ' ' if lead else ''}{origin.label} → {dest.label}"
            f"   {depart}（周{week_cn(depart)}）{'' if lead else '单程'}"
            f"   按{SORT_CN[sort]}排序 · 显示 {min(top, len(itins))}/{len(itins)} 条")

    rows = []
    for i, it in enumerate(itins[:top], 1):
        stop_txt = "直飞" if it.stops == 0 else (f"{it.stops} 转" if it.stops else "—")
        if it.layovers:
            stop_txt += " " + "/".join(
                f"{ap}{fmt_dur(d)}" + ("(换机场)" if x else "")
                for d, ap, x in it.layovers[:2])
        row = [str(i), money(it.price, currency)]
        if multi_seat:
            row.append(SEAT_CN[it.seat])
        if has_flights:
            row.append("/".join(it.flights) or "—")
        row += ["/".join(it.airlines[:2]) or "—",
                it.dep_at.strftime("%m-%d %H:%M") if it.dep_at else "—",
                it.arr_at.strftime("%m-%d %H:%M") if it.arr_at else "—",
                fmt_dur(it.duration_min), stop_txt]
        rows.append(row)
    headers = ["#", "价格"] + (["舱位"] if multi_seat else []) \
        + (["航班号"] if has_flights else []) \
        + ["航司", "起飞时间", "到达时间", "时长", "中转"]
    aligns = [">", ">"] + (["<"] if multi_seat else []) \
        + (["<"] if has_flights else []) + ["<", "<", "<", ">", "<"]
    body = table(headers, rows, aligns, md)

    parts = [("## " if lead else "# ") + head if md else head]
    if filters:
        parts.append("筛选：" + "，".join(filters)
                     + f"（滤掉 {filtered_out} 条）")
    parts += ["", body]
    if itins:
        # 汇总行永远按价格取最值，不跟着 --sort 走（否则换排序会报错数）
        cheapest = min(itins, key=SORTS["price"])
        fastest = min((i for i in itins if i.duration_min), default=None,
                      key=lambda i: i.duration_min)
        direct = [i for i in itins if i.stops == 0]
        summary = [f"最低 {money(cheapest.price, currency)}"
                   f"（{'/'.join(cheapest.airlines[:2]) or '?'}，{fmt_dur(cheapest.duration_min)}）"]
        if direct:
            summary.append(
                f"直飞最低 {money(min(direct, key=SORTS['price']).price, currency)}")
        if fastest and fastest is not cheapest:
            summary.append(f"最快 {fmt_dur(fastest.duration_min)}"
                           f" {money(fastest.price, currency)}")
        parts += ["", " · ".join(summary)]
        if multi_seat:
            parts += ["", ("## 各舱位最低价" if md else "各舱位最低价"), "",
                      seat_summary(itins, currency)]
        parts += ["", ("## 各航司最低价" if md else "各航司最低价"), "",
                  airline_summary(itins, currency)]

    if not standalone:              # 注意栏和链接由往返报表统一出
        return "\n".join(parts)
    notes = [price_note(adults, children, seats)]
    if more := more_note(itins, top):
        notes.append(more)
    notes += warnings
    parts += ["", "## 注意" if md else "注意", ""] + [f"- {n}" for n in notes]
    if urls:
        parts += [""] + [f"在 Google 航班里打开（{n}）：{u}" for n, u in urls]
    return "\n".join(parts)


def render_roundtrip(legs, seats, adults, children, currency, top, fmt,
                     sort="price", warnings=()):
    """往返报表：两段单程各自成表，开头先把合计摆出来。

    合计取的是各段在当前筛选下的最低价之和 —— 两段独立定价、独立可选，
    所以这是个确定的数，不是估算。但它跟航司自己卖的往返联程票是两回事，
    注意栏里写明，别让人以为这就是「往返票价」。

    一段查不到不拖垮另一段：那段位置上印出它自己的失败原因，另一段照常出表。
    """
    out, back = legs[0], legs[1]
    lo_o = min(out["itins"], key=SORTS["price"]) if out["itins"] else None
    lo_b = min(back["itins"], key=SORTS["price"]) if back["itins"] else None
    total = lo_o.price + lo_b.price if lo_o and lo_b else None
    nights = (back["depart"] - out["depart"]).days
    span = "当天往返" if nights == 0 else f"{nights} 天往返"

    if fmt == "json":
        return json.dumps({
            "roundtrip": {
                "depart": out["depart"].isoformat(),
                "return": back["depart"].isoformat(),
                "nights": nights, "total": total, "currency": currency,
                "outbound_price": lo_o.price if lo_o else None,
                "inbound_price": lo_b.price if lo_b else None,
            },
            "price_means": "sum_of_two_one_ways",
            "warnings": list(warnings),
            "legs": [dict(direction=("outbound" if i == 0 else "inbound"),
                          error=None if l["itins"] else str(l["error"]),
                          **search_payload(l["itins"], l["origin"], l["dest"],
                                           l["depart"], seats, adults, children,
                                           currency, top, l["urls"], (), sort,
                                           l["dropped"], l["filters"]))
                     for i, l in enumerate(legs)],
        }, ensure_ascii=False, indent=2)

    md = fmt == "md"
    title = (f"{out['origin'].label} ⇄ {out['dest'].label}   "
             f"{out['depart']}（周{week_cn(out['depart'])}）走 · "
             f"{back['depart']}（周{week_cn(back['depart'])}）回   {span}")
    parts = [f"# {title}" if md else title]
    if total is not None:
        parts += ["", f"往返合计 {money(total, currency)}"
                      f"  = 去 {money(lo_o.price, currency)}"
                      f"（{'/'.join(lo_o.airlines[:2]) or '?'}，{fmt_dur(lo_o.duration_min)}）"
                      f" + 回 {money(lo_b.price, currency)}"
                      f"（{'/'.join(lo_b.airlines[:2]) or '?'}，{fmt_dur(lo_b.duration_min)}）"]
    else:
        miss = "、".join(l["lead"] for l in legs if not l["itins"])
        parts += ["", f"往返合计算不出来 —— {miss}一条都没查到，原因在下面"]
    for l in legs:
        if not l["itins"]:
            # 失败原因里已经带了「哪到哪、哪天」，别跟标题再重复一遍；
            # 顺带把 hint 也印出来 —— 那句才是真正能救场的下一步。
            head2 = f"{l['lead']}查不到"
            parts += ["", f"## {head2}" if md else head2, "", str(l["error"])]
            if l["error"].hint:
                parts += [f"下一步：{l['error'].hint}"]
            continue
        parts += ["", render_search(l["itins"], l["origin"], l["dest"],
                                    l["depart"], seats, adults, children,
                                    currency, top, fmt, l["urls"], (), sort,
                                    l["dropped"], l["filters"],
                                    lead=l["lead"], standalone=False)]

    notes = [price_note(adults, children, seats),
             "往返合计是两张单程票相加，不是航司卖的往返联程价 —— "
             "联程有时更便宜、也可能更贵，值得再比一次。"]
    for l in legs:
        if more := more_note(l["itins"], top):
            notes.append(f"{l['lead']}{more}")
    notes += list(warnings)
    parts += ["", "## 注意" if md else "注意", ""] + [f"- {n}" for n in notes]
    links = [(f"{l['lead']}{n}", u) for l in legs for n, u in l["urls"]]
    parts += [""] + [f"在 Google 航班里打开（{n}）：{u}" for n, u in links]
    return "\n".join(parts)


def _leg_table(scan, days, currency, md, label):
    rows = []
    lo = min((r["floor"] for r in scan.values() if r["floor"] is not None),
             default=None)
    for d in days:
        r = scan.get(d) or {}
        when = f"{d:%m-%d}(周{week_cn(d)})"
        if r.get("floor") is None:
            rows.append([when, "—", "—", "—", "—", r.get("error", "")])
            continue
        rows.append([when, money(r["floor"], currency),
                     money(r.get("direct_floor"), currency),
                     fmt_dur(r.get("fastest_min")), str(r.get("count", "")),
                     (r.get("airlines") or "") + (" ◀" if r["floor"] == lo else "")])
    return table([label, "最低价", "直飞最低", "最快", "航班", "最便宜的航司"],
                 rows, ["<", ">", ">", ">", ">", "<"], md)


def _leg_stats(scan, currency):
    ok = [r for r in scan.values() if r["floor"] is not None]
    if not ok:
        return None
    best = min(ok, key=lambda r: r["floor"])
    worst = max(ok, key=lambda r: r["floor"])
    return best, worst, statistics.median([r["floor"] for r in ok]), len(ok)


def render_sweep(out_map, back_map, combos, rows, origin, dest, window, seat,
                 adults, children, currency, fmt, top=8, warnings=()):
    """两段独立的单程扫描 + 由它们算出的行程组合。

    刻意分成两张表而不是一张 —— 去程和回程各自定价、各自独立可选，摊成一张
    「组合表」反而把「哪天走便宜」和「哪天回便宜」这两个真正的决策点糊在了一起。
    """
    if fmt == "json":
        return json.dumps({
            "query": {"origin": origin.as_dict(), "destination": dest.as_dict(),
                      "window": [window[0].isoformat(), window[1].isoformat()],
                      "seat": SEATS_INV[seat], "adults": adults,
                      "children": children, "currency": currency},
            "price_means": "one_way_total_per_leg",
            "warnings": list(warnings),
            "outbound": [{**r, "day": r["day"].isoformat()}
                         for r in out_map.values()],
            "inbound": [{**r, "day": r["day"].isoformat()}
                        for r in back_map.values()],
            "combinations": [
                {"depart": r["depart"].isoformat(), "return": r["ret"].isoformat(),
                 "nights": r["nights"], "total": r["total"],
                 "outbound_price": (r["out"] or {}).get("floor"),
                 "inbound_price": (r["back"] or {}).get("floor"),
                 "outbound_airlines": (r["out"] or {}).get("airlines"),
                 "inbound_airlines": (r["back"] or {}).get("airlines")}
                for r in sorted(rows, key=lambda x: (x["total"] is None, x["total"] or 0))],
        }, ensure_ascii=False, indent=2)

    md = fmt == "md"
    title = (f"{origin.label} → {dest.label}   "
             f"{window[0]} ~ {window[1]}")
    parts = [f"# {title}" if md else title]

    parts += ["", "## 去程 " + f"{origin.label} → {dest.label}" if md
              else f"去程 {origin.label} → {dest.label}", "",
              _leg_table(out_map, sorted(out_map), currency, md, "出发日")]
    if back_map:
        parts += ["", "## 回程 " + f"{dest.label} → {origin.label}" if md
                  else f"回程 {dest.label} → {origin.label}", "",
                  _leg_table(back_map, sorted(back_map), currency, md, "返程日")]

    ok = [r for r in rows if r["total"] is not None]
    if back_map and ok:
        ok.sort(key=lambda r: r["total"])
        show = ok[:top]
        crows = [[f"{r['depart']:%m-%d}(周{week_cn(r['depart'])})",
                  f"{r['ret']:%m-%d}(周{week_cn(r['ret'])})", str(r["nights"]),
                  money(r["total"], currency),
                  money(r["out"]["floor"], currency),
                  money(r["back"]["floor"], currency),
                  (r["out"].get("airlines") or "") + " / "
                  + (r["back"].get("airlines") or "")] for r in show]
        # 窗口刚好等于天数时只有一种组合，这时「最划算的 1 种（共 1 种）」
        # 和下面的「最贵…贵 ¥0」都是自己跟自己比，纯噪音。
        one = len(ok) == 1
        head = ("唯一的走法" if one
                else f"最划算的 {len(show)} 种走法（共 {len(ok)} 种）")
        parts += ["", f"## {head}" if md else head, "",
                  table(["出发", "返程", "天数", "合计", "去程", "回程",
                         "去程航司 / 回程航司"], crows,
                        ["<", "<", ">", ">", ">", ">", "<"], md)]
        best, worst = ok[0], ok[-1]
        parts += ["", ("合计    " if one else "最便宜  ")
                  + f"{best['depart']} 走 · {best['ret']} 回"
                    f"（{best['nights']} 天）  {money(best['total'], currency)}"
                    f"  = 去 {money(best['out']['floor'], currency)}"
                    f" + 回 {money(best['back']['floor'], currency)}"]
        if not one:
            parts += [f"最贵    {worst['depart']} 走 · {worst['ret']} 回  "
                      f"{money(worst['total'], currency)}"
                      f"  贵 {money(worst['total'] - best['total'], currency)}"]

    for scan, name in ((out_map, "去程"), (back_map, "回程")):
        if not scan:
            continue
        st = _leg_stats(scan, currency)
        if st is None:
            parts += ["", f"{name}：一个有效报价都没扫到。"]
            continue
        b, w, med, n = st
        if n < 2:
            # 一天的「走势」是根单格柱子、自己跟自己比中位数，上面的表已经说完了
            continue
        parts += ["", f"{name}走势  {sparkline([ (scan[d]['floor']) for d in sorted(scan) ])}"
                      f"  最低 {b['day']:%m-%d} {money(b['floor'], currency)}"
                      f" · 最高 {w['day']:%m-%d} {money(w['floor'], currency)}"
                      f" · 中位 {money(med, currency)}"
                      f" · {n}/{len(scan)} 天有报价"]

    notes = [price_note(adults, children, [seat])]
    if back_map:      # 没扫回程时（不带 days）这句是废话，还容易看成算错了
        notes.append("去程和回程是两次独立的单程查询，合计价就是两段之和 —— "
                     "每一段都是确定的航班和价格。")
    notes += list(warnings)
    parts += ["", "## 注意" if md else "注意", ""] + [f"- {n}" for n in notes]
    return "\n".join(parts)


SEATS_INV = {v: k for k, v in SEATS.items()}


# ---------------------------------------------------------------- 子命令

def parse_day(s):
    """接受 2026-10-15、10-15，或 +7(从今天起 N 天后)。"""
    s = str(s).strip()
    if s.startswith("+"):
        try:
            return date.today() + timedelta(days=int(s[1:]))
        except ValueError:
            raise Failed(f"看不懂的日期：{s}（+7 表示 7 天后）")
    # 不带年份的写法自己补年份，别交给 strptime 去猜：Python 3.14 起它会打一条
    # DeprecationWarning 混进 stderr，3.15 打算直接改成报错。
    if m := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", s):
        month, day = int(m.group(1)), int(m.group(2))
        for year in (date.today().year, date.today().year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                continue                # 闰日：今年没有就看明年
            if d >= date.today():
                return d
        raise Failed(
            f"{s} 不是个有效日期（不带年份时按「月-日」读，如 10-15）")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise Failed(
        f"看不懂的日期：{s}（能写 2026-10-15、10-15、或 +7 表示 7 天后）")


def parse_window(s):
    """接受 09-29..10-08 / 2026-09-29..2026-10-08 / +30..+45。"""
    parts = re.split(r"\.\.|~|到", str(s).strip())
    if len(parts) != 2:
        raise Failed(
            f"看不懂的窗口：{s}（写成 起始..结束，例如 09-29..10-08）")
    start, end = parse_day(parts[0]), parse_day(parts[1])
    if end < start:
        raise Failed(f"窗口结束 {end} 早于开始 {start}")
    return (start, end)


def parse_days(s):
    """接受 5 或 5-7(玩 5 到 7 天都看看)。返回天数列表。"""
    s = str(s).strip()
    if m := re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", s):
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        if b - a > 30:
            raise Failed(f"天数区间 {s} 太宽了，最多跨 30 天")
        return list(range(a, b + 1))
    if s.isdigit():
        return [int(s)]
    raise Failed(f"看不懂的天数：{s}（写 5 或 5-7）")


def resolve_pair(a):
    origin, dest = resolve(a.origin, "出发地"), resolve(a.dest, "目的地")
    # 不拦的话 Google 会回一张没有结果列表的页面，而那条路径的诊断是「重试 3 次
    # 没结果，可能是限流」—— 把人往核对三字码的错路上引。
    if same := sorted(set(origin.codes) & set(dest.codes)):
        where = (origin.label if origin.label == dest.label else
                 f"{origin.label} 和 {dest.label} 都包含 {'、'.join(same)}")
        raise Failed(f"出发地和目的地是同一个地方（{where}）",
                     "查不了原地往返，把 origin 或 dest 换成别的城市")
    return origin, dest


def passthrough_warnings(*places):
    return [f"「{p.query}」不在内置机场表里，按 IATA 码 {p.codes[0]} 直接查的；"
            "如果结果不是你要的城市，换个写法或核对三字码。"
            for p in places if p.passthrough]


def seat_of(a):
    """把 seat 归一成一串舱位编号（sweep 只会有一个）。"""
    raw = a.seat or "economy"
    names = [raw] if isinstance(raw, str) else list(raw)
    if "all" in names:
        names = list(SEATS)
    # 去重但保持给定顺序，且始终按舱位等级排，输出才稳定
    return sorted({SEATS[n] for n in names})


def check_horizon(day, what="出发日"):
    limit = date.today() + timedelta(days=BOOKING_HORIZON_DAYS)
    if day > limit:
        raise Failed(
            f"{what} {day} 超出 Google 的售票窗口（大约 11 个月，到 {limit} 前后）",
            "航司一般只放到 11 个月后才开售，改近一点再查")


def stops_limit(a):
    """经停上限：0 只看直飞，None 不限。"""
    return a.max_stops


def filter_notes(a):
    """把本次生效的筛选条件说成人话。往返两段共用同一套条件，只算一次。"""
    notes = []
    if a.airline:
        notes.append("航司 " + "/".join(a.airline))
    if a.depart_time:
        notes.append(f"起飞 {a.depart_time[0]:%H:%M}-{a.depart_time[1]:%H:%M}")
    if a.max_stops == 0:
        notes.append("只看直飞")
    elif a.max_stops is not None:
        notes.append(f"最多转 {a.max_stops} 次")
    return notes


def leg_result(a, origin, dest, depart, lead, itins, seat_errs, filters, urls):
    """把一个方向查回来的东西整理成报表要的形状。

    查不到就把原因装进 error 一起返回，不抛 —— 往返里一段没票，不该把另一段
    已经查到的结果一并吞掉。单程时由 cmd_search 原样抛出去，行为不变。
    """
    raw = list(itins)                  # 筛之前的全量，价格历史要记这一份
    itins, dropped = apply_filters(itins, a.airline, a.depart_time)
    itins.sort(key=SORTS[a.sort])
    err = None
    if not itins:
        if not raw and seat_errs:
            err = seat_errs[0][1]      # 全军覆没，照原样把第一个错报出去
        elif dropped:
            err = Failed(
                f"{origin.label} → {dest.label} 在 {depart} 有航班，但 {dropped} 条"
                f"全被筛掉了（{'，'.join(filters)}）",
                "放宽 airline、depart_time，或去掉 max_stops")
        else:
            err = Failed(f"{origin.label} → {dest.label} 在 {depart} 没有可售航班",
                         "换个日期，或去掉 max_stops 让中转航班也进来")
    return {"origin": origin, "dest": dest, "depart": depart, "lead": lead,
            "itins": itins, "raw": raw, "dropped": dropped, "filters": filters,
            "urls": urls, "seat_errs": seat_errs, "error": err}


def cmd_search(a):
    origin, dest = resolve_pair(a)
    depart = a.depart or date.today() + timedelta(days=14)
    if depart < date.today():
        raise Failed(f"出发日 {depart} 已经过去了", "查未来的日期，或写 +14 表示两周后")
    check_horizon(depart)
    if a.ret is not None:
        if a.ret < depart:
            raise Failed(f"返程日 {a.ret} 排在出发日 {depart} 前面",
                         "depart 是去程、return 是回程，把 return 挪到出发日当天或之后")
        check_horizon(a.ret, "返程日")

    # 经停数交给 Google 过滤(能换回更多别的结果)，航司和时间段在本地筛。
    max_stops = stops_limit(a)
    seats = seat_of(a)
    # 往返就是两次独立的单程查询：两段各自定价、各自可选，合计是确定的相加值。
    routes = [(origin, dest, depart, "去程")]
    if a.ret is not None:
        routes.append((dest, origin, a.ret, "回程"))

    def fetch(job):
        """异常收进返回值。裸抛会被 pool.map 的迭代甩到外面，一个舱位没票
        (常见于头等)、或一次偶发限流，就把其余请求的结果一起带走。"""
        idx, st = job
        o, d, day, _ = routes[idx]
        try:
            return idx, st, google_search(o, d, day, st, a.adults, a.children,
                                          a.currency, max_stops, a.limit,
                                          a.region), None
        except Failed as exc:
            return idx, st, [], exc

    jobs = [(i, st) for i in range(len(routes)) for st in seats]
    if len(jobs) == 1:
        results = [fetch(jobs[0])]
    else:
        # 方向 × 舱位 彼此不相干，一次全铺开并发拉。
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            results = list(pool.map(fetch, jobs))

    filters = filter_notes(a)
    legs = []
    for idx, (o, d, day, lead) in enumerate(routes):
        got = [it for r in results if r[0] == idx for it in r[2]]
        errs = [(r[1], r[3]) for r in results if r[0] == idx and r[3] is not None]
        urls = [(SEAT_CN[st], flights_url(o, d, day, st, a.adults, a.children,
                                          a.currency, max_stops, a.region))
                for st in seats]
        legs.append(leg_result(a, o, d, day, lead, got, errs, filters, urls))
    if all(l["error"] for l in legs):
        raise legs[0]["error"]

    warnings = list(passthrough_warnings(origin, dest))
    for l in legs:
        tag = l["lead"] if len(legs) > 1 else ""
        warnings += [f"{tag}{SEAT_CN[st]}这次没查到，其余舱位照常：{exc}"
                     for st, exc in l["seat_errs"] if l["itins"]]
    if len(legs) > 1:
        print(render_roundtrip(legs, seats, a.adults, a.children, a.currency,
                               a.top, a.format, a.sort, warnings))
    else:
        l = legs[0]
        print(render_search(l["itins"], origin, dest, depart, seats, a.adults,
                            a.children, a.currency, a.top, a.format, l["urls"],
                            warnings, a.sort, l["dropped"], filters))
    if not a.no_record and max_stops is None:
        # 经停数是交给 Google 端筛的，拿回来的最低价不是这条航线的真实底价，
        # 混进历史库会让趋势线凭空「涨」一截。
        for l in legs:
            record(l["origin"], l["dest"], l["raw"], a.adults)
    return 0


def cmd_sweep(a):
    origin, dest = resolve_pair(a)
    window = a.window or (date.today() + timedelta(days=14),
                          date.today() + timedelta(days=28))
    if window[1] < date.today():
        raise Failed(f"窗口 {window[0]}..{window[1]} 整个都在过去", "查未来的日期")
    check_horizon(window[0], "窗口开头")
    # 过去的日期照发请求的话，每天还要白搭三次重试加退避，最后甩一句吓人的
    # 「重试 3 次都没拿到结果列表」，真实原因只是这天过完了；还照吃 --max-queries。
    past_note = []
    if window[0] < date.today():
        past_note = [f"窗口开头 {(date.today() - window[0]).days} 天已经过去，"
                     f"从今天（{date.today()}）算起"]
        window = (date.today(), window[1])

    # 不给 --days 就只扫去程(纯单程视角)；给了才连回程一起扫，并算出行程组合。
    days = parse_days(a.days) if a.days else None
    # 窗口尾巴超出售票视野的那几天不发请求:每天要白搭三次重试加退避,还照吃
    # --max-queries,最后渲染成"—"再甩一句"航线不通"——真实原因只是还没开售。
    horizon = date.today() + timedelta(days=BOOKING_HORIZON_DAYS)
    if window[1] > horizon:
        past_note.append(f"窗口尾巴 {window[1]} 超出售票视野，只扫到 {horizon}")
        window = (window[0], horizon)
    all_days = [window[0] + timedelta(days=i)
                for i in range((window[1] - window[0]).days + 1)]
    if days:
        combos, out_days, back_days = plan_combos(window, days)
        if not combos:
            span = (window[1] - window[0]).days
            raise Failed(
                f"窗口只有 {span + 1} 天，装不下 {min(days)} 天的行程",
                f"把窗口拉长(window={window[0]}..{window[0] + timedelta(days=max(days) + 2)})"
                "，或把 days 改小")
    else:
        combos, out_days, back_days = [], all_days, []

    total_q = len(out_days) + len(back_days)
    if total_q > a.max_queries:
        raise Failed(
            f"这个窗口要查 {total_q} 天（去程 {len(out_days)} + 回程 {len(back_days)}）"
            f"，超过上限 {a.max_queries}",
            "缩小 window")

    seat = seat_of(a)[0]
    max_stops = stops_limit(a)

    def progress(done, total):
        pass  # 标准错误现在会混进结果，进度先不报

    out_map = scan_days(origin, dest, out_days, seat, a.adults, a.children,
                        a.currency, max_stops, a.workers, progress, 0, total_q)
    back_map = {}
    if back_days:
        back_map = scan_days(dest, origin, back_days, seat, a.adults, a.children,
                             a.currency, max_stops, a.workers, progress,
                             len(out_days), total_q)

    rows = combine(combos, out_map, back_map) if combos else []
    warnings = past_note + list(passthrough_warnings(origin, dest))
    print(render_sweep(out_map, back_map, combos, rows, origin, dest, window,
                       seat, a.adults, a.children, a.currency, a.format,
                       a.top, warnings))
    if not a.no_record and max_stops is None:
        n = 0
        with db() as conn:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for scan, o, d in ((out_map, origin, dest), (back_map, dest, origin)):
                key = route_key(o, d)
                batch = [(now, key, o.label, d.label, r["day"].isoformat(),
                          seat, a.adults, a.currency, r["floor"], r.get("stops"),
                          r.get("airlines"), r.get("duration_min"))
                         for r in scan.values() if r["floor"] is not None]
                conn.executemany(
                    "INSERT INTO observations (observed_at, route, origin, dest,"
                    " depart, seat, adults, currency, price, stops,"
                    " airlines, duration_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch)
                n += len(batch)
    return 0


def cmd_trend(a):
    # 不给航线就是「历史库里都有啥」—— 单开一个动作不值得，语义也自洽。
    if not a.origin and not a.dest:
        rows = known_routes()
        if a.format == "json":
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("历史库还是空的：查一次票就会自动记下来。")
            return 0
        print(table(["航线", "出发地", "目的地", "观测数", "最近一次"],
                    [[r["route"], r["origin"], r["dest"], str(r["n"]), r["last"][:16]]
                     for r in rows], ["<", "<", "<", ">", "<"],
                    a.format == "md"))
        return 0
    if not (a.origin and a.dest):
        raise Failed("trend 要么 origin、dest 都不给（列出所有航线），要么两个都给",
                     "例如 origin=上海、dest=东京")

    origin, dest = resolve_pair(a)
    seat = SEATS[a.seat]
    scope = f"{SEAT_CN[seat]} · {a.adults} 成人 · {a.currency}"
    rows = floor_history(route_key(origin, dest), a.depart,
                         a.currency, seat, a.adults)
    if a.format == "json":
        print(json.dumps({"route": route_key(origin, dest),
                          "seat": a.seat, "adults": a.adults,
                          "currency": a.currency,
                          "depart": a.depart.isoformat() if a.depart else None,
                          "history": [{"observed_at": t, "floor": v, "currency": c}
                                      for t, v, c in rows]},
                         ensure_ascii=False, indent=2))
        return 0
    if len(rows) < 2:
        print(f"{origin.label} → {dest.label}（{scope}）目前只有 {len(rows)} 个"
              "观测点，还画不出趋势。")
        print("同一条线上币种、舱位、人数不同的观测是分开存的 —— 换 currency、"
              "seat、adults 看看是不是攒在别的组合下面了。趋势要隔几天查一次才攒得出来。")
        return 0
    vals = [v for _, v, _ in rows]
    cur = rows[-1][2]
    first, last = vals[0], vals[-1]
    delta = (last - first) / first * 100 if first else 0
    arrow = "↑" if delta > 1 else "↓" if delta < -1 else "→"
    print(f"{origin.label} → {dest.label} 底价历史（{scope}）  {sparkline(vals)}")
    print(f"最早 {rows[0][0][:16]}  {money(first, cur)}")
    print(f"最新 {rows[-1][0][:16]}  {money(last, cur)}  {arrow} {delta:+.1f}%")
    print(f"区间 {money(min(vals), cur)} ~ {money(max(vals), cur)}"
          f" · {len(rows)} 个观测点")
    return 0


def cmd_airports(a):
    q = a.query
    if not q:
        raise Failed("airports 要给 query", "例如 query=东京")
    hit = AIRPORT_INDEX.get(_key(q))
    found = list(hit) if hit else []
    if not found:
        # 精确匹配不上时才退到子串搜 —— 这里是「帮人查码」，列出候选由人来挑，
        # 跟 resolve() 里「绝不猜」并不矛盾。
        seen, found = set(), []
        for a_ in AIRPORTS:
            blob = _key("".join([a_.code, a_.name_cn, a_.name_en, a_.city_cn,
                                 a_.city_en, *a_.aliases]))
            if _key(q) in blob and a_.code not in seen:
                seen.add(a_.code)
                found.append(a_)
    if a.format == "json":
        print(json.dumps({"query": q, "exact": bool(hit),
                          "airports": [x.as_dict() for x in found]},
                         ensure_ascii=False, indent=2))
        return 0
    if not found:
        print(f"内置机场表里没有「{q}」。")
        print("表只收常用机场，没收全。知道三字码的话可以直接用，例如 dest=NRT。")
        return 1
    print(table(["三字码", "机场", "Airport", "城市", "City"],
                [[x.code, x.name_cn, x.name_en, x.city_cn, x.city_en] for x in found],
                None, a.format == "md"))
    if hit and len(found) > 1:
        print(f"\n写「{q}」会同时查这 {len(found)} 个机场：{'、'.join(x.code for x in found)}")
    return 0


def cmd_doctor(a):
    ok = True
    print(f"python      {sys.version.split()[0]}")
    print(f"机场表      {len(AIRPORTS)} 个机场 / "
          f"{len({x.city_cn for x in AIRPORTS})} 座城市")
    print(f"历史库      {DB_PATH}"
          f"{'  (还没建)' if not os.path.exists(DB_PATH) else ''}")

    probe_day = date.today() + timedelta(days=30)
    try:
        o, d = resolve("上海"), resolve("东京")
        itins = google_search(o, d, probe_day)   # 单程探针，跟默认行为一致
        if itins:
            b = itins[0]
            print(f"google      ✅ 可用，抓到 {len(itins)} 条"
                  f"（最低 {money(b.price, 'CNY')}，零依赖）")
            # 航班号走的是页面数据块那条独立链路，主链路好好的它也可能悄悄断掉
            # （返回的表会少一整列，但看起来完全正常）—— 单独盯一眼。
            got = sum(1 for i in itins if i.flights)
            rate = got / len(itins)
            if rate >= 0.8:
                print(f"航班号      ✅ {got}/{len(itins)} 条有航班号"
                      f"（{rate:.0%}，另含机型）")
            else:
                ok = False
                print(f"航班号      ❌ 只有 {got}/{len(itins)} 条抽到"
                      f"（{rate:.0%}，正常应 >95%）")
                print("            下一步：Google 改了首屏数据块的结构，"
                      "更新 _is_segment/_segment_flight 的形状判断")
        else:
            ok = False
            print("google      ❌ 请求成功但一条航班都没有（上海→东京不该没有）")
    except Failed as exc:
        ok = False
        print(f"google      ❌ {exc}")
        if exc.hint:
            print(f"            下一步：{exc.hint}")

    # 解析器是全脚本最脆的一环：Google 一改 aria-label 句式，抓取会「成功」
    # 但字段全空。拿一条实测样本当金标准回归，改版时 doctor 立刻报红。
    sample = ("From 5677 Chinese yuan. 1 stop flight with China Eastern and Air "
              "France. Leaves Chengdu Shuangliu International Airport at 1:30 PM "
              "on Thursday, October 1 and arrives at Aéroport de Paris-Charles de "
              "Gaulle at 5:50 AM on Friday, October 2. Total duration 22 hr 20 "
              "min.  Layover (1 of 1) is a 5 hr layover at Shanghai Pudong "
              "International Airport in Shanghai. Select flight")
    # 换机场中转是另一套句式，单独回归 —— 只测 "layover at" 那版的话，
    # "layover in … Transfer here from …" 挂掉时 doctor 依然全绿。
    xfer_sample = ("From 6607 Chinese yuan round trip total. 1 stop flight with "
                   "EVA Air. Leaves Shanghai Pudong International Airport at "
                   "8:05\u202fPM on Thursday, October 1 and arrives at Haneda "
                   "Airport at 11:30\u202fAM on Friday, October 2. Total duration "
                   "14 hr 25 min.  Layover (1 of 1) is a 9 hr 20 min layover in "
                   "Taipei City. Transfer here from Taiwan Taoyuan International "
                   "Airport to Taipei Songshan Airport. Select flight")
    it = parse_card(sample, "CNY", date(2026, 10, 1))
    bad = []
    xf = parse_card(xfer_sample, "CNY", date(2026, 10, 1))
    if not xf or xf.layovers != [(560, "Taipei City", True)]:
        bad.append(f"换机场中转实得 {xf.layovers if xf else None}")
    if not it:
        bad.append("整条解析不出来")
    else:
        for field, want in (("price", 5677.0), ("stops", 1), ("duration_min", 1340)):
            got = getattr(it, field)
            if got != want:
                bad.append(f"{field} 期望 {want} 实得 {got}")
        if it.airlines != ["China Eastern", "Air France"]:
            bad.append(f"airlines 实得 {it.airlines}")
        if it.layovers != [(300, "Shanghai", False)]:
            bad.append(f"layovers 实得 {it.layovers}")
        if not it.dep_at or it.dep_at.strftime("%Y-%m-%d %H:%M") != "2026-10-01 13:30":
            bad.append(f"dep_at 实得 {it.dep_at}")
    if bad:
        ok = False
        print(f"解析器      ❌ {len(bad)} 项对不上：{'；'.join(bad)}")
        print("            下一步：Google 改了 aria-label 句式，更新 _L_* 正则")
    else:
        print("解析器      ✅ 金标准样本七个字段全对")

    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

COMMANDS = {"search": cmd_search, "sweep": cmd_sweep, "trend": cmd_trend,
            "airports": cmd_airports, "doctor": cmd_doctor}

def _int(args, key, default, low):
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise Failed(f"{key} 要一个整数：{value!r}")
    try:
        value = int(str(value).strip())
    except ValueError:
        raise Failed(f"{key} 要一个整数：{value!r}") from None
    if value < low:
        raise Failed(f"{key} 至少是 {low}：{value}")
    return value


def _str(args, key):
    value = args.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, (str, int)):
        raise Failed(f"{key} 要一个字符串：{value!r}")
    return str(value).strip() or None


def build(args):
    action = args.get("action") or "search"
    if action not in COMMANDS:
        raise Failed(f"不认识的 action：{action}", "search、sweep、trend、airports、doctor 里挑一个")
    fmt = args.get("format") or "md"
    if fmt not in ("md", "json"):
        raise Failed(f"format 只能是 md 或 json：{fmt}")
    seat = args.get("seat") or "economy"
    names = [seat] if isinstance(seat, str) else seat
    if not isinstance(names, list) or not names or any(n not in (*SEATS, "all") for n in names):
        raise Failed(f"不认识的舱位：{seat!r}", "economy、premium、business、first、all")
    if action in ("sweep", "trend") and (len(names) > 1 or names[0] == "all"):
        raise Failed(f"{action} 一次只看一个舱位", "seat 写一个，例如 business")
    airline = args.get("airline") or None
    if isinstance(airline, str):
        airline = [airline]
    sort = args.get("sort") or "price"
    if sort not in SORTS:
        raise Failed(f"不认识的排序：{sort}", "price、duration、depart、arrive、stops")
    depart, ret, window = _str(args, "depart"), _str(args, "return"), _str(args, "window")
    times = _str(args, "depart_time")
    return SimpleNamespace(
        action=action, format=fmt,
        origin=_str(args, "origin"), dest=_str(args, "dest"), query=_str(args, "query"),
        depart=parse_day(depart) if depart else None,
        ret=parse_day(ret) if ret else None,
        window=parse_window(window) if window else None,
        days=_str(args, "days"),
        seat=seat if action == "search" else names[0],
        adults=_int(args, "adults", 1, 1), children=_int(args, "children", 0, 0),
        max_stops=_int(args, "max_stops", None, 0),
        airline=airline, depart_time=parse_time_range(times) if times else None,
        sort=sort, top=_int(args, "top", 8 if action == "sweep" else 15, 1),
        currency=(_str(args, "currency") or "CNY").upper(),
        no_record=bool(args.get("no_record")),
        limit=60, region="us", workers=6, max_queries=60)


def known_params():
    """开头 Parameters 里写的参数名：不认识的参数直接报错，免得写错的名字被悄悄当成没给。"""
    lines, inside = [], False
    with open(__file__, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            if line.startswith("# Parameters:"):
                inside = True
            elif inside:
                lines.append(line[1:])
    return set(json.loads("".join(lines))["properties"])


def main():
    try:
        try:
            args = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            raise Failed(f"参数不是 JSON：{exc}") from None
        if not isinstance(args, dict):
            raise Failed("参数要是一个 JSON 对象")
        unknown = sorted(set(args) - known_params())
        if unknown:
            raise Failed(f"不认识的参数：{'、'.join(unknown)}", f"能用的参数：{'、'.join(sorted(known_params()))}")
        a = build(args)
        return COMMANDS[a.action](a) or 0
    except Failed as exc:
        print(f"失败：{exc}")
        if exc.hint:
            print(f"下一步：{exc.hint}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
