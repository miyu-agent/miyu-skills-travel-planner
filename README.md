# 旅行规划

Miyu 包：一个技能 `travel-planner` 加两件工具 `flight_deals`、`hotel_deals`。AI 照技能带出处地排行程、算预算，机票和酒店价格用这两件工具查。

## 工具 `flight_deals`：机票比价

Google 航班的航线、日期、舱位、人数编在 `tfs` 参数里（base64url 过的 protobuf），脚本自己拼，一次查询一条 HTTP 请求，不用浏览器。

| 参数 | 说明 |
|---|---|
| `action` | `search`（默认）查一天；`sweep` 扫一段日期找最便宜的那天；`trend` 价格历史（不给航线就列出记过的航线）；`airports` 查机场码；`doctor` 自检 |
| `origin`、`dest` | 城市、英文名或三字码；城市包含它的所有机场（上海 = PVG + SHA）。不猜，必须给 |
| `depart` | `+30`、`10-01` 或 `2026-10-01`，默认 `+14` |
| `return` | 给了就连回程一起查，开头给合计 |
| `window`、`days` | 只给 `sweep`：能出行的那段日期（`09-29..10-08`）和玩几天（`5` 或 `5-7`）；给了 `days` 连回程一起扫、排出组合 |
| `seat` | `economy`（默认）、`premium`、`business`、`first`；`search` 可以给数组或 `all` 比几个舱位 |
| `adults`、`children` | 默认 1、0 |
| `max_stops` | `0` 只看直飞 |
| `airline`、`depart_time`、`sort`、`top` | 筛航司、起飞时间段（`08:00-18:00`）、排序、条数 |
| `currency`、`format`、`no_record` | 币种（默认 CNY）、`md`/`json`、不记价格历史 |

## 工具 `hotel_deals`：酒店比价

Google 酒店的日期、币种编在 `ts` 参数里，同样自己拼。Google 给的起价本身就是各家 OTA 里的最低价。

| 参数 | 说明 |
|---|---|
| `action` | `search`（默认）查一段住宿；`sweep` 扫未来几天找最便宜的入住日；`trend` 价格历史；`places` 记过的地方；`doctor` 自检 |
| `location` | 地名，最好写英文（`Shinjuku, Tokyo`）：中文只能模糊匹配，可能落到 30 公里外，输出里会提醒 |
| `checkin`、`checkout`、`nights` | 默认 7 天后住 1 晚 |
| `days` | 只给 `sweep`：扫几天，默认 30 |
| `sources` | 默认 `["google"]`；加 `booking` 交叉验证 |
| `adults`、`rooms` | 只对 Booking 有用，默认 2、1 |
| `currency`、`top`、`format`、`no_record` | 同上 |

## 依赖

- 只用 Python 标准库，能联网到 `google.com` 就行。
- **Booking 源是可选的**：要 `pip install --user playwright`，再装一个浏览器（系统的 Chrome、Edge、Chromium，或 `python3 -m playwright install chromium`）。无头运行，一次 10–20 秒，连着查几次会被限流。`hotel_deals` 的 `doctor` 会说装没装好。

## 注意

- 价格是起价，下单价可能更高；往返合计是两张单程相加，不是联程价。
- 价格历史存在 `MIYU_SCRIPT_STATE_DIR`（核心还没提供时放 `MIYU_SCRIPT_CACHE_DIR`，重启会清空）。
- 出口在哪个国家会影响 Google 的结果；`gl` 写死机票 `us`、酒店 `jp`，别的地区没实测过。
