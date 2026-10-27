#!/usr/bin/env python3
# Display name: Hotel prices
# 显示名称：酒店比价
# Description: Hotel prices for a place and dates from Google Hotels (already aggregating OTAs), optional Booking; or scan days for the cheapest check-in. Prefer English place names.
# 描述：查某地某几天的酒店价格（Google 酒店，本身聚合各家平台，可选再加 Booking），按价排序；也能扫未来几天找哪天入住最便宜。
# Timeout: 300
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "action": {"type": "string", "enum": ["search", "sweep", "trend", "places", "doctor"], "description": "search (default): price a stay. sweep: scan the next days for the cheapest check-in. trend: price history of a place. places: places with history. doctor: self-check."},
#   "location": {"type": "string", "description": "Place, English preferred (Shinjuku, Tokyo). A CJK name only fuzzy-matches and can land 30 km off; the output warns then. Required except places and doctor."},
#   "checkin": {"type": "string", "description": "+7, 10-15 or 2026-10-15. search: default +7. sweep: first day, default tomorrow."},
#   "checkout": {"type": "string", "description": "search: default checkin + nights."},
#   "nights": {"type": "integer", "description": "Default 1, which keeps nightly prices comparable."},
#   "days": {"type": "integer", "description": "sweep: days to scan, default 30."},
#   "adults": {"type": "integer", "description": "Default 2 (Booking only)."},
#   "rooms": {"type": "integer", "description": "Default 1 (Booking only)."},
#   "currency": {"type": "string", "description": "Default CNY."},
#   "sources": {"type": "array", "items": {"type": "string", "enum": ["google", "booking"]}, "description": "search: default [google]. booking needs playwright and a browser, takes 10-20 s and gets rate-limited; add it to cross-check before quoting."},
#   "top": {"type": "integer", "description": "Rows, default 20."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."},
#   "no_record": {"type": "boolean", "description": "Don't add this result to the price history."}
# }}

"""酒店比价：Google 旅游搜索把日期、币种编在 ts 参数里（base64url 过的 protobuf），
自己拼得出来，所以查一天只要一条 HTTP 请求，只用标准库。Google 给的起价本身就是
各家 OTA 里的最低价。Booking 挡纯 HTTP，要 playwright 开无头浏览器，是可选的交叉验证。

价格历史（trend、places 用）存在 MIYU_SCRIPT_STATE_DIR，没有就放 MIYU_SCRIPT_CACHE_DIR。
"""

import base64
import html as _html
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from difflib import SequenceMatcher

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
# 价格历史放在能留住的地方：有持久的状态目录用它，没有就放缓存目录（重启会清空）。
DATA_DIR = os.environ.get("MIYU_SCRIPT_STATE_DIR") or os.environ.get("MIYU_SCRIPT_CACHE_DIR") or ""
DB_PATH = os.path.join(DATA_DIR, "hotel-history.db") if DATA_DIR else ""
SPARK = "▁▂▃▄▅▆▇█"
SYMBOL = {"CNY": "¥", "JPY": "JP¥", "USD": "$", "EUR": "€", "HKD": "HK$", "KRW": "₩"}


class Failed(RuntimeError):
    """带「下一步」建议的失败。"""

    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint


# ---------------------------------------------------------------- 环境

def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def chromium_bundled():
    for base in ("~/.cache/ms-playwright", "~/Library/Caches/ms-playwright"):
        cache = os.path.expanduser(base)
        if not os.path.isdir(cache):
            continue
        # 光看目录名不够：playwright 升级后旧的 chromium-<build> 目录还在，但版本
        # 对不上，launch 会直接报 Executable doesn't exist。得确认可执行文件在。
        for d in os.listdir(cache):
            if not d.startswith("chromium"):
                continue
            for rel in ("chrome-linux/chrome",
                        "chrome-headless-shell-linux64/chrome-headless-shell",
                        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                        "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
                        "chrome-headless-shell-mac-arm64/chrome-headless-shell",
                        "chrome-headless-shell-mac-x64/chrome-headless-shell"):
                if os.path.exists(os.path.join(cache, d, rel)):
                    return True
    return False


def launch_kwargs(headless=True):
    kw = dict(headless=headless, args=["--disable-blink-features=AutomationControlled",
                                       "--disable-dev-shm-usage"])
    if sys.platform.startswith("linux"):
        # Chrome 自己的沙箱只在 Linux 的 root / 容器里起不来; macOS 上用不着关。
        kw["args"].insert(0, "--no-sandbox")
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        kw["channel"] = "chrome"
    elif shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
        kw["channel"] = "msedge"
    elif shutil.which("chromium") or shutil.which("chromium-browser"):
        kw["channel"] = "chromium"
    return kw


# ---------------------------------------------------------------- Google ts 参数

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


def _pdate(field, d):
    return _sub(field, _uint(1, d.year) + _uint(2, d.month) + _uint(3, d.day))


def build_ts(checkin, checkout, currency="CNY", place_id=None, place_name=None):
    """拼出 google.com/travel/search 的 ts 参数。

    实测(2026-08)结构，重新编码能字节级还原::

        1: 1
        3 { 1 { 2 { 1:<place_id> 7:<place_name> } }   # 有 q= 时可不填
            2 { 2 { 1{y,m,d}=入住 2{y,m,d}=退房 3=晚数 } 6 { 2:0 } } }
        5 { 1 { 7:<currency> } }
    """
    nights = max((checkout - checkin).days, 1)
    inner = b""
    if place_id:
        loc = _pstr(1, place_id) + (_pstr(7, place_name) if place_name else b"")
        inner += _sub(1, _sub(2, loc))
    stay = _pdate(1, checkin) + _pdate(2, checkout) + _uint(3, nights)
    inner += _sub(2, _sub(2, stay) + _sub(6, _uint(2, 0)))
    body = _uint(1, 1) + _sub(3, inner) + _sub(5, _sub(1, _pstr(7, currency)))
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


# ---------------------------------------------------------------- 数据结构

class Offer:
    """某平台上某家酒店的一个报价。price 一律是整段住宿总价。"""

    __slots__ = ("source", "name", "price", "currency", "checkin", "checkout",
                 "nights", "rating", "rating_scale", "reviews", "deal", "url")

    def __init__(self, source, name, price, currency, checkin, checkout, nights,
                 rating=None, rating_scale=5.0, reviews=None, deal=None, url=None):
        self.source, self.name, self.price = source, name, price
        self.currency, self.checkin, self.checkout = currency, checkin, checkout
        self.nights, self.rating, self.rating_scale = nights, rating, rating_scale
        self.reviews, self.deal, self.url = reviews, deal, url

    @property
    def per_night(self):
        if self.price is None or self.nights <= 0:
            return None
        return self.price / self.nights

    @property
    def rating5(self):
        """统一折算到 5 分制。Booking 是 10 分制，混着比会把 8.1 分当成好一倍。"""
        if self.rating is None:
            return None
        return self.rating * 5.0 / self.rating_scale

    def as_dict(self):
        return {"source": self.source, "name": self.name, "price": self.price,
                "per_night": self.per_night, "currency": self.currency,
                "checkin": self.checkin.isoformat(), "checkout": self.checkout.isoformat(),
                "nights": self.nights, "rating": self.rating,
                "rating_scale": self.rating_scale, "rating5": self.rating5,
                "reviews": self.reviews, "deal": self.deal, "url": self.url}


_SPLIT = re.compile(r"[\s\-_·・,，.。'\"“”‘’()（）\[\]&/\\]+")
_NOISE = re.compile(r"(hotels?|inn|resort|hostel|ryokan|酒店|飯店|饭店|旅馆|旅店|宾馆|民宿|客栈)", re.I)


def norm_name(name):
    s = unicodedata.normalize("NFKC", name).casefold()
    return _SPLIT.sub("", _NOISE.sub("", s))


def name_tokens(name):
    s = unicodedata.normalize("NFKC", name).casefold()
    return {t for t in (_NOISE.sub("", p) for p in _SPLIT.split(s)) if len(t) > 1}


def similarity(a, b):
    """两个酒店名有多像，0~1。

    各平台的差异主要在后缀("…, Tokyo")、语序和通用词的有无，所以词集合
    包含率比整串比对准；中日文名不带空格切不出词，退回字符级相似度。
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if len(ta) >= 2 and len(tb) >= 2:
        overlap = len(ta & tb) / min(len(ta), len(tb))
        if overlap >= 0.8:
            return overlap
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


# ---------------------------------------------------------------- 数据源：Google

GOOGLE_URL = "https://www.google.com/travel/search"

# "Prices starting from CN¥355, TOKYO-W-inn Asakusa DEAL 21% less than usual"
_G_PRICE = re.compile(
    r'aria-label="Prices starting from ([^\d"]*)([\d,.]+), '
    r'(.+?)(?: (?:GREAT DEAL|DEAL) (\d+% less than usual))?"')
# "4.7 out of 5 stars from 6 reviews, Komorebi Rill Hotel"
_G_RATING = re.compile(r'aria-label="([\d.]+) out of 5 stars from ([\d,]+) reviews?, (.+?)"')


def google_url(place, checkin, checkout, currency, region):
    return GOOGLE_URL + "?" + urllib.parse.urlencode({
        "q": place if re.search(r"(?i)hotel|酒店", place) else place + " hotels",
        "hl": "en",          # 英文站的酒店名是原名，跟 Booking 对得上；中文站是译名
        "gl": region, "curr": currency,
        "ts": build_ts(checkin, checkout, currency)})


def google_search(place, checkin, checkout, currency="CNY", region="jp",
                  limit=40, timeout=30):
    """纯 HTTP 抓 Google 酒店。价格/评分都在卡片的 aria-label 上，首屏 HTML 就有。"""
    url = google_url(place, checkin, checkout, currency, region)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as exc:
        raise Failed(f"Google 请求失败：{exc}",
                     "检查网络后重试；search 时可以 sources 加 booking 换个源"
                     "（sweep 只能走 google）")

    nights = max((checkout - checkin).days, 1)
    prices, ratings = {}, {}
    for m in _G_PRICE.finditer(page):
        prices[_html.unescape(m.group(3)).strip()] = (float(m.group(2).replace(",", "")),
                                                      m.group(4))
    for m in _G_RATING.finditer(page):
        ratings[_html.unescape(m.group(3)).strip()] = (
            float(m.group(1)), int(m.group(2).replace(",", "")))

    offers = []
    for name, (per_night, deal) in prices.items():
        rating, reviews = ratings.get(name, (None, None))
        offers.append(Offer(
            source="google", name=name,
            price=per_night * nights,   # 卡片上的数字实测是每晚价，不随晚数变
            currency=currency, checkin=checkin, checkout=checkout, nights=nights,
            rating=rating, reviews=reviews, deal=deal, url=url))
    offers.sort(key=lambda o: o.price)
    return offers[:limit]


# ---------------------------------------------------------------- 数据源：Booking

BOOKING_URL = "https://www.booking.com/searchresults.html"


def booking_url(place, checkin, checkout, adults, rooms, currency, offset=0):
    params = {"ss": place, "checkin": checkin.isoformat(),
              "checkout": checkout.isoformat(), "group_adults": adults,
              "no_rooms": rooms, "group_children": 0,
              "selected_currency": currency, "lang": "en-us", "order": "price"}
    if offset:
        params["offset"] = offset
    return BOOKING_URL + "?" + urllib.parse.urlencode(params)


def booking_search(place, checkin, checkout, adults=2, rooms=1, currency="CNY",
                   limit=40, headless=True):
    """Booking 挡纯 HTTP(202 挑战页)，必须真浏览器。好处是原生支持按价排序。"""
    if not playwright_available():
        raise Failed("没装 playwright，Booking 源用不了",
                     "pip install --user playwright && python3 -m playwright install chromium"
                     "；或者 sources 只写 google")
    from playwright.sync_api import sync_playwright

    nights = max((checkout - checkin).days, 1)
    offers, seen = [], set()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(**launch_kwargs(headless))
            except Exception as exc:
                raise Failed(f"启动浏览器失败：{exc}",
                             "python3 -m playwright install chromium")
            try:
                page = browser.new_context(
                    locale="en-us", user_agent=UA,
                    viewport={"width": 1400, "height": 950}).new_page()
                offset = 0
                while len(offers) < limit:
                    cards = _booking_page(page, place, checkin, checkout, adults,
                                          rooms, currency, offset)
                    if not cards:
                        break
                    fresh = 0
                    for card in cards:
                        o = _booking_card(card, checkin, checkout, nights, currency,
                                          adults, rooms)
                        if o is None or o.name in seen:
                            continue
                        seen.add(o.name)
                        offers.append(o)
                        fresh += 1
                    if not fresh:
                        break
                    offset += len(cards)
            finally:
                browser.close()
    except Failed:
        raise
    except Exception as exc:
        raise Failed(f"Booking 抓取失败：{exc}",
                     "多半是被限流了，等几分钟再试")

    if not offers:
        raise Failed("Booking 没返回任何房源",
                     "多半是被限流了，等几分钟再试")
    offers.sort(key=lambda o: (o.price is None, o.price))
    return offers[:limit]


def _booking_page(page, place, checkin, checkout, adults, rooms, currency,
                  offset, tries=3):
    """打开一页结果。Booking 会限流，退避重试几次。"""
    for attempt in range(tries):
        page.goto(booking_url(place, checkin, checkout, adults, rooms, currency, offset),
                  wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector('[data-testid="property-card"]', timeout=25_000)
            return page.query_selector_all('[data-testid="property-card"]')
        except Exception:
            if attempt == tries - 1:
                return []
            page.wait_for_timeout(3_000 * (attempt + 1))
    return []


def booking_hotel_url(href, checkin, checkout, adults, rooms, currency):
    """把卡片上的酒店链接洗干净，再挂回本次查询的日期。

    原始 href 拖着一长串会话/追踪参数，存下来又脏又活不过今天；但整串剥掉的话
    点进去是一个空日期的页面，调用方还得自己回填日期才能核价 —— 链接给了等于没给。
    """
    base = href.split("?")[0]
    if base.startswith("/"):
        base = "https://www.booking.com" + base
    return base + "?" + urllib.parse.urlencode({
        "checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
        "group_adults": adults, "no_rooms": rooms, "group_children": 0,
        "selected_currency": currency})


def _booking_card(card, checkin, checkout, nights, currency, adults=2, rooms=1):
    def text(sel):
        el = card.query_selector(sel)
        return el.inner_text().strip() if el else None

    name = text('[data-testid="title"]')
    if not name:
        return None

    price = None
    if raw := text('[data-testid="price-and-discounted-price"]'):
        if m := re.search(r"([\d,.]+)", raw.replace("\xa0", " ")):
            price = float(m.group(1).replace(",", ""))

    # 评分优先读 aria-label("Scored 8.1 8.1 Very good 78 reviews")，
    # 比 inner_text 稳 —— 后者会把分数重复两遍。
    rating = reviews = None
    if el := card.query_selector('[data-testid="review-score"]'):
        flat = " ".join((el.get_attribute("aria-label") or el.inner_text() or "").split())
        m = (re.search(r"(?:Scored|评分)\s*([\d.]+)", flat)
             or re.search(r"([\d.]+)\s*(?:out of 10|/\s*10)", flat)
             or re.search(r"([\d.]+)", flat))
        if m:
            rating = float(m.group(1))
        if m := re.search(r"([\d,]+)\s*(?:条|reviews?)", flat):
            reviews = int(m.group(1).replace(",", ""))

    link = card.query_selector('a[href*="/hotel/"]')
    url = booking_hotel_url(link.get_attribute("href"), checkin, checkout,
                            adults, rooms, currency) if link else None
    return Offer(source="booking", name=name, price=price, currency=currency,
                 checkin=checkin, checkout=checkout, nights=nights,
                 rating=rating, rating_scale=10.0, reviews=reviews,
                 deal=text('[data-testid="property-card-deal"]'), url=url)


SOURCES = {"google": google_search, "booking": booking_search}


# ---------------------------------------------------------------- 价格历史

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL, place TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT NOT NULL, name_key TEXT NOT NULL,
    checkin TEXT NOT NULL, checkout TEXT NOT NULL, nights INTEGER NOT NULL,
    price REAL, per_night REAL, currency TEXT NOT NULL,
    rating REAL, rating_scale REAL, reviews INTEGER, deal TEXT);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON observations (place, checkin, source, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_hotel ON observations (name_key, checkin);
"""


def db():
    if not DB_PATH:
        raise Failed("没有 MIYU_SCRIPT_STATE_DIR 或 MIYU_SCRIPT_CACHE_DIR，价格历史存不住",
                     "在 Miyu 里调用；或者设 no_record=true")
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record(place, offers):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [(now, place, o.source, o.name, norm_name(o.name),
             o.checkin.isoformat(), o.checkout.isoformat(), o.nights,
             o.price, o.per_night, o.currency, o.rating, o.rating_scale,
             o.reviews, o.deal) for o in offers if o.price is not None]
    with db() as conn:
        conn.executemany(
            "INSERT INTO observations (observed_at, place, source, name, name_key,"
            " checkin, checkout, nights, price, per_night, currency, rating,"
            " rating_scale, reviews, deal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def floor_history(place, checkin=None, source=None):
    """按观测时刻返回当时的最低每晚价 —— "近期涨跌"的时间轴。"""
    sql = ("SELECT observed_at, MIN(per_night) AS floor FROM observations"
           " WHERE place = ? AND per_night IS NOT NULL")
    args = [place]
    if checkin:
        sql += " AND checkin = ?"
        args.append(checkin.isoformat())
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " GROUP BY substr(observed_at, 1, 13) ORDER BY observed_at"
    with db() as conn:
        return [(r["observed_at"], r["floor"]) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- 趋势

def sweep(place, start, days, nights, adults, currency, region, workers=6,
          progress=None):
    """扫未来 N 个入住日，每天取一份行情。只对 google 源(纯 HTTP)并发。"""
    targets = [start + timedelta(days=i) for i in range(days)]

    def one(day):
        try:
            offers = google_search(place, day, day + timedelta(days=nights),
                                   currency, region, limit=40)
        except Exception as exc:            # 单天失败不该拖垮整条曲线
            return {"checkin": day, "floor": None, "error": str(exc)[:60]}
        per = [o.per_night for o in offers if o.per_night is not None]
        if not per:
            return {"checkin": day, "floor": None, "error": "无报价"}
        cheapest = min(offers, key=lambda o: o.per_night)
        return {"checkin": day, "floor": min(per), "median": statistics.median(per),
                "count": len(per), "cheapest": cheapest.name, "error": None}

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(one, targets):
            out[res["checkin"]] = res
            if progress:
                progress(len(out), days)
    return [out[d] for d in targets]


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


def cluster(offers, threshold=0.85):
    """把各平台指向同一家酒店的报价归一组。名字全等的先秒并，剩下的两两比。"""
    groups, index = [], {}
    for o in offers:
        if o.price is None:
            continue
        key = norm_name(o.name)
        gi = index.get(key)
        if gi is None:
            for i, g in enumerate(groups):
                if similarity(o.name, g[0].name) >= threshold:
                    gi = i
                    break
        if gi is None:
            index[key] = len(groups)
            groups.append([o])
        else:
            index[key] = gi
            groups[gi].append(o)
    return groups


# ---------------------------------------------------------------- 结果可信度

_NONLATIN = re.compile(r"[一-鿿぀-ヿ가-힯]")


def drift_warning(place):
    """搜索词是中文时，提醒结果可能不在你要的那个地方。

    Google 这边固定走英文站（酒店名得跟 Booking 的英文原名对得上，中文站给的是
    译名），中文地名过去只能做模糊匹配。实测搜「苏州金鸡湖」，排在最前面的几家
    在同里、昆山、木渎 —— 价格是真的，地方是错的。

    这种结果不报错、排版也正常，光看输出发现不了。调用方要是不熟当地地理，
    会把「离目标 40 公里的酒店」当成最便宜的答案端给用户，比直接失败更糟，
    所以宁可每次都啰嗦一句。
    """
    if not _NONLATIN.search(place):
        return None
    return (f"搜索词「{place}」含中日韩文字，平台只能做模糊匹配，结果可能混进目标区域之外的酒店。"
            "下结论前建议换英文地名再跑一次对照，两次都出现的才可信。")


# ---------------------------------------------------------------- 输出

def wcwidth(s):
    """CJK 字符占两格，不算宽度表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, width, align="<"):
    gap = width - wcwidth(s)
    if gap < 0:                      # 太长就截断，留个省略号
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
    widths = [max(wcwidth(h), max(wcwidth(str(r[i])) for r in rows))
              for i, h in enumerate(headers)]
    if md:
        sep = ["---:" if a == ">" else "---" for a in aligns]
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
        lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        return "\n".join(lines)
    out = ["  ".join(pad(h, widths[i], aligns[i]) for i, h in enumerate(headers)).rstrip(),
           "  ".join("─" * widths[i] for i in range(len(headers)))]
    out += ["  ".join(pad(str(c), widths[i], aligns[i])
                      for i, c in enumerate(r)).rstrip() for r in rows]
    return "\n".join(out)


def money(v, currency):
    return "—" if v is None else f"{SYMBOL.get(currency, currency + ' ')}{v:,.0f}"


def week_cn(d):
    return "一二三四五六日"[d.weekday()]


def render_search(offers, place, checkin, checkout, nights, currency, top, fmt,
                  warnings=(), sources_ok=(), sources_failed=()):
    """出报表。

    warnings / sources_* 一定要进 stdout：源挂了、结果可能漂了，这些话以前只写在
    stderr，程序调用时通常只收 stdout —— 于是「booking 挂了只剩 google 半个源」
    看起来和「两个源都好好的」一模一样。
    """
    if fmt == "json":
        return json.dumps({
            "query": {"location": place, "checkin": checkin.isoformat(),
                      "checkout": checkout.isoformat(), "nights": nights,
                      "currency": currency},
            "sources_ok": list(sources_ok),
            "sources_failed": list(sources_failed),
            "warnings": list(warnings),
            "returned": min(top, len(offers)), "total": len(offers),
            "offers": [o.as_dict() for o in offers[:top]],
        }, ensure_ascii=False, indent=2)

    md = fmt == "md"
    head = (f"{place}  {checkin} → {checkout} ({nights} 晚, {currency})")
    rows = []
    for i, o in enumerate(offers[:top], 1):
        r5 = o.rating5
        rows.append([i, money(o.per_night, currency), money(o.price, currency),
                     f"{r5:.1f}" if r5 is not None else "—",
                     f"{o.reviews:,}" if o.reviews else "—",
                     o.source, o.name, o.deal or ""])
    body = table(["#", "每晚", f"{nights}晚总价", "评分", "评价", "来源", "酒店", "优惠"],
                 rows, [">", ">", ">", ">", ">", "<", "<", "<"], md)

    parts = [f"# {head}" if md else head, "", body]

    # 同店跨平台价差
    diffs = []
    for group in cluster(offers):
        best = {}
        for o in group:
            if o.source not in best or o.price < best[o.source].price:
                best[o.source] = o
        if len(best) < 2:
            continue
        cheap = min(best.values(), key=lambda o: o.price)
        dear = max(best.values(), key=lambda o: o.price)
        if dear.price > 0:
            diffs.append((cheap, dear, (dear.price - cheap.price) / dear.price * 100))
    if diffs:
        diffs.sort(key=lambda r: -r[2])
        rows = [[c.name, f"{c.source} {money(c.per_night, currency)}",
                 f"{d.source} {money(d.per_night, currency)}", f"{p:.0f}%"]
                for c, d, p in diffs[:top]]
        parts += ["", "## 同店跨平台价差" if md else "同店跨平台价差", "",
                  table(["酒店", "便宜的", "贵的", "省"], rows, ["<", "<", "<", ">"], md)]

    notes = list(warnings)
    if sources_failed:
        got = "、".join(sources_ok) or "无"
        for f in sources_failed:
            notes.append(f"数据源 {f['source']} 没出结果（{f['error']}）；"
                         f"下面的价格只来自：{got}。")
    if notes:
        parts += ["", "## 注意" if md else "注意", ""]
        parts += [f"- {n}" for n in notes]
    link = next((o.url for o in offers if o.source == "google" and o.url), None)
    if link:
        parts += ["", f"在 Google 酒店里打开：{link}"]
    return "\n".join(parts)


def render_sweep(series, place, currency, nights, fmt, warnings=()):
    if fmt == "json":
        days = [{**d, "checkin": d["checkin"].isoformat()} for d in series]
        return json.dumps({
            "query": {"location": place, "nights": nights, "currency": currency,
                      "days": len(series)},
            "warnings": list(warnings),
            "scanned": sum(1 for d in series if d["floor"] is not None),
            "days_data": days,
        }, ensure_ascii=False, indent=2)

    md = fmt == "md"
    valid = [d for d in series if d["floor"] is not None]
    lo = min((d["floor"] for d in valid), default=None)
    rows = []
    for d in series:
        if d["floor"] is None:
            rows.append([d["checkin"].isoformat(), "", "—", "—", "—", d.get("error", "")])
            continue
        mark = "◀" if d["floor"] == lo else ""
        rows.append([d["checkin"].isoformat(), week_cn(d["checkin"]),
                     money(d["floor"], currency), money(d["median"], currency),
                     d["count"], (d["cheapest"] or "") + (" " + mark if mark else "")])
    body = table(["入住日", "周", "最低/晚", "中位/晚", "房源", "最便宜的那家"],
                 rows, ["<", "<", ">", ">", ">", "<"], md)

    parts = [f"# {place} 未来 {len(series)} 天行情（{nights} 晚, {currency}）" if md
             else f"{place}  未来 {len(series)} 天（{nights} 晚, {currency}）", "", body]
    if not valid:
        return "\n".join(parts + ["", "没扫到任何有效报价。"])

    best = min(valid, key=lambda d: d["floor"])
    worst = max(valid, key=lambda d: d["floor"])
    floors = [d["floor"] for d in valid]
    spread = (worst["floor"] - best["floor"]) / best["floor"] * 100 if best["floor"] else 0
    missing = len(series) - len(valid)
    parts += [
        "", f"走势  {sparkline([d['floor'] for d in series])}  "
            f"({series[0]['checkin']:%m-%d} → {series[-1]['checkin']:%m-%d})",
        f"最便宜 {best['checkin']:%m-%d}（周{week_cn(best['checkin'])}） "
        f"{money(best['floor'], currency)}/晚 · {best['cheapest']}",
        f"最贵   {worst['checkin']:%m-%d} {money(worst['floor'], currency)}/晚"
        f"  贵 {spread:.0f}%",
        f"中位 {money(statistics.median(floors), currency)}/晚 · {len(valid)} 天有数据"
        + (f" · {missing} 天失败" if missing else ""),
    ]
    if warnings:
        parts += ["", "## 注意" if md else "注意", ""] + [f"- {w}" for w in warnings]
    return "\n".join(parts)


# ---------------------------------------------------------------- 子命令

def parse_day(s):
    """接受 2026-10-15、10-15（过了就算明年），或 +7（从今天起 N 天后）。"""
    s = str(s).strip()
    if s.startswith("+"):
        try:
            return date.today() + timedelta(days=int(s[1:]))
        except ValueError:
            raise Failed(f"看不懂的日期：{s}（+7 表示 7 天后）") from None
    # 不带年份的自己补年份：交给 strptime 会打 DeprecationWarning，3.15 起要报错。
    if m := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", s):
        month, day = int(m.group(1)), int(m.group(2))
        for year in (date.today().year, date.today().year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d >= date.today():
                return d
        raise Failed(f"{s} 不是个有效日期（不带年份时按「月-日」读，如 10-15）")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise Failed(
        f"看不懂的日期：{s}（能写 2026-10-15、10-15、或 +7 表示 7 天后）")


def cmd_search(a):
    checkin = a.checkin or date.today() + timedelta(days=7)
    checkout = a.checkout or checkin + timedelta(days=a.nights)
    if checkout <= checkin:
        raise Failed(f"退房日 {checkout} 不晚于入住日 {checkin}",
                     "去掉 checkout 改用 nights，或把日期调过来")
    nights = (checkout - checkin).days
    sources = a.source

    offers, errors, ok, failed = [], [], [], []
    for src in sources:
        try:
            if src == "google":
                got = google_search(a.location, checkin, checkout, a.currency,
                                    a.region, a.limit)
            else:
                got = booking_search(a.location, checkin, checkout, a.adults,
                                     a.rooms, a.currency, a.limit)
            offers.extend(got)
            # 「跑通了但一条没有」跟「跑挂了」对结果的影响一样，都得如实说。
            (ok if got else failed).append(
                src if got else {"source": src, "error": "返回 0 条"})
        except Failed as exc:
            errors.append(exc)
            failed.append({"source": src, "error": f"{exc}（{exc.hint}）" if exc.hint else str(exc)})
        except Exception as exc:
            err = Failed(str(exc))
            errors.append(err)
            failed.append({"source": src, "error": str(exc)})

    if not offers:
        raise Failed("所有数据源都没拿到结果",
                     errors[0].hint if errors else "用 action=doctor 看看环境")

    warnings = [w for w in (drift_warning(a.location),) if w]
    offers.sort(key=lambda o: (o.per_night is None, o.per_night))
    print(render_search(offers, a.location, checkin, checkout, nights,
                        a.currency, a.top, a.format,
                        warnings=warnings, sources_ok=ok, sources_failed=failed))
    if not a.no_record:
        record(a.location, offers)
    return 0


def cmd_sweep(a):
    start = a.checkin or date.today() + timedelta(days=1)

    def progress(done, total):
        pass  # 标准错误现在会混进结果，进度先不报

    series = sweep(a.location, start, a.days, a.nights, a.adults, a.currency,
                   a.region, a.workers, progress)

    print(render_sweep(series, a.location, a.currency, a.nights, a.format,
                       warnings=[w for w in (drift_warning(a.location),) if w]))
    if not a.no_record:
        offers = [Offer("google", d["cheapest"], d["floor"] * a.nights, a.currency,
                        d["checkin"], d["checkin"] + timedelta(days=a.nights), a.nights)
                  for d in series if d["floor"] is not None]
        record(a.location, offers)
    return 0


def cmd_trend(a):
    src = (a.source or [None])[0]
    rows = floor_history(a.location, a.checkin, src)
    if a.format == "json":
        print(json.dumps([{"observed_at": t, "floor_per_night": v} for t, v in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if len(rows) < 2:
        print(f"{a.location} 目前只有 {len(rows)} 个观测点，还画不出趋势。")
        print("趋势要隔几天查一次才攒得出来。")
        return 0
    vals = [v for _, v in rows]
    first, last = vals[0], vals[-1]
    delta = (last - first) / first * 100 if first else 0
    arrow = "↑" if delta > 1 else "↓" if delta < -1 else "→"
    print(f"{a.location} 底价历史  {sparkline(vals)}")
    print(f"最早 {rows[0][0][:16]}  {money(first, a.currency)}/晚")
    print(f"最新 {rows[-1][0][:16]}  {money(last, a.currency)}/晚  {arrow} {delta:+.1f}%")
    print(f"区间 {money(min(vals), a.currency)} ~ {money(max(vals), a.currency)}"
          f" · {len(rows)} 个观测点")
    return 0


def cmd_places(a):
    with db() as conn:
        rows = list(conn.execute(
            "SELECT place, COUNT(*) n, MAX(observed_at) last FROM observations"
            " GROUP BY place ORDER BY last DESC"))
    if a.format == "json":
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("历史库还是空的：查一次房就会自动记下来。")
        return 0
    print(table(["地区", "观测数", "最近一次"],
                [[r["place"], r["n"], r["last"][:16]] for r in rows],
                ["<", ">", "<"], a.format == "md"))
    return 0



def cmd_doctor(a):
    ok = True
    print(f"python      {sys.version.split()[0]}")
    print(f"历史库      {DB_PATH}"
          f"{'  (还没建)' if not os.path.exists(DB_PATH) else ''}")
    try:
        n = len(google_search("Tokyo", date.today() + timedelta(days=14),
                              date.today() + timedelta(days=15)))
        print(f"google      ✅ 可用，抓到 {n} 条（零依赖）")
    except Exception as exc:
        ok = False
        print(f"google      ❌ {exc}")
    if not playwright_available():
        print("booking     ⚠️  没装 playwright —— 只能用 google 源（可选，google 本身已聚合各家 OTA）")
        print("            下一步：pip install --user playwright"
              " && python3 -m playwright install chromium")
    else:
        chan = launch_kwargs().get("channel")
        if chan or chromium_bundled():
            print(f"booking     ✅ playwright 就绪"
                  f"（浏览器：{chan or 'playwright 自带 chromium'}）")
        else:
            ok = False
            print("booking     ❌ 有 playwright 但找不到浏览器")
            print("            下一步：python3 -m playwright install chromium")

    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

COMMANDS = {"search": cmd_search, "sweep": cmd_sweep, "trend": cmd_trend,
            "places": cmd_places, "doctor": cmd_doctor}


def _int(args, key, default, low):
    value = args.get(key, default)
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
        raise Failed(f"不认识的 action：{action}", "search、sweep、trend、places、doctor 里挑一个")
    fmt = args.get("format") or "md"
    if fmt not in ("md", "json"):
        raise Failed(f"format 只能是 md 或 json：{fmt}")
    location = _str(args, "location")
    if not location and action in ("search", "sweep", "trend"):
        raise Failed(f"{action} 要给 location", "例如 location=Shinjuku, Tokyo")
    sources = args.get("sources") or ["google"]
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list) or any(s not in ("google", "booking") for s in sources):
        raise Failed(f"不认识的数据源：{sources!r}", "google、booking")
    if action == "sweep" and sources != ["google"]:
        raise Failed("sweep 只能走 google", "去掉 sources")
    checkin, checkout = _str(args, "checkin"), _str(args, "checkout")
    return SimpleNamespace(
        action=action, format=fmt, location=location,
        checkin=parse_day(checkin) if checkin else None,
        checkout=parse_day(checkout) if checkout else None,
        nights=_int(args, "nights", 1, 1), days=_int(args, "days", 30, 1),
        adults=_int(args, "adults", 2, 1), rooms=_int(args, "rooms", 1, 1),
        currency=(_str(args, "currency") or "CNY").upper(),
        source=list(dict.fromkeys(sources)), top=_int(args, "top", 20, 1),
        no_record=bool(args.get("no_record")),
        limit=40, region="jp", workers=6)


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
