<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

# 旅行规划

> <sub>本文档由 Anthropic 的 Claude Fable 5.2 生成。</sub>

这是 Miyu 的一个技能软件包，提供一个技能 `travel-planner` 和两件工具 `flight_deals`、`hotel_deals`。AI 按照技能的说明安排行程、计算预算，并且给信息注明出处；机票和酒店的价格用这两件工具查询。

## 工具 `flight_deals`：机票比价

Google 航班把航线、日期、舱位和人数编码在 `tfs` 参数里（经过 base64url 编码的 protobuf）。脚本自己拼出这个参数，每次查询只发一条 HTTP 请求，不需要浏览器。

| 参数 | 说明 |
|---|---|
| `action` | 要执行的操作：`search`（默认）查询某一天；`sweep` 扫描一段日期，找出最便宜的那一天；`trend` 查看价格历史（不填写航线时，列出记录过的航线）；`airports` 查询机场代码；`doctor` 运行自检。 |
| `origin`、`dest` | 出发地和目的地，可以写城市、英文名或三字代码；写城市时包含这个城市的所有机场（例如上海 = PVG + SHA）。工具不会猜测，必须填写。 |
| `depart` | 出发日期，可以写成 `+30`、`10-01` 或 `2026-10-01`，默认是 `+14`。 |
| `return` | 返程日期。填写之后，工具会连同回程一起查询，并在开头给出合计价格。 |
| `window`、`days` | 只用于 `sweep`：可以出行的那段日期（`09-29..10-08`）和游玩的天数（`5` 或 `5-7`）。填写了 `days` 时，工具会连同回程一起扫描，并排出组合。 |
| `seat` | 舱位：`economy`（默认）、`premium`、`business`、`first`。使用 `search` 时可以传数组或 `all`，比较几个舱位。 |
| `adults`、`children` | 成人和儿童的人数，默认分别是 1 和 0。 |
| `max_stops` | 最多经停的次数；`0` 表示只看直飞。 |
| `airline`、`depart_time`、`sort`、`top` | 依次用来筛选航空公司、筛选起飞时间段（`08:00-18:00`）、指定排序方式和指定显示的条数。 |
| `currency`、`format`、`no_record` | 依次是币种（默认是 CNY）、输出格式（`md`/`json`），以及不记录价格历史。 |

## 工具 `hotel_deals`：酒店比价

Google 酒店把日期和币种编码在 `ts` 参数里，脚本同样自己拼出这个参数。Google 给出的起价本身就是各家 OTA（在线旅行社）里的最低价。

| 参数 | 说明 |
|---|---|
| `action` | 要执行的操作：`search`（默认）查询一段住宿；`sweep` 扫描未来几天，找出最便宜的入住日期；`trend` 查看价格历史；`places` 列出记录过的地方；`doctor` 运行自检。 |
| `location` | 地名，最好用英文写（`Shinjuku, Tokyo`）：中文地名只能模糊匹配，可能落到 30 公里以外，输出里会给出提醒。 |
| `checkin`、`checkout`、`nights` | 入住日期、退房日期和住几晚；默认是 7 天后入住，住 1 晚。 |
| `days` | 只用于 `sweep`：扫描的天数，默认是 30，最多是 90。 |
| `sources` | 数据来源，默认是 `["google"]`；加上 `booking` 可以交叉验证。使用 `trend` 时，填写了来源就只看那个来源，不填写就不区分来源。 |
| `adults`、`rooms` | 只对 Booking 有效：成人人数和房间数，默认分别是 2 和 1。 |
| `currency`、`top`、`format`、`no_record` | 和 `flight_deals` 里的同名参数相同。 |

## 依赖

工具对运行环境的要求如下：

- 工具只使用 Python 标准库，能联网访问 `google.com` 就可以了。
- **Booking 来源是可选的**：使用它需要先运行 `pip install --user playwright`，再安装一个浏览器（系统里的 Chrome、Edge、Chromium，或者运行 `python3 -m playwright install chromium`）。浏览器以无头模式运行，每次查询需要 10–20 秒，连续查询几次会被限流。`hotel_deals` 的 `doctor` 会告诉你是否安装成功。
- Miyu 的沙盒会把 `~/.config/chromium` 替换成空的只读目录，所以 `hotel_deals` 给浏览器另外设置了 `XDG_CONFIG_HOME` 和 `XDG_CACHE_HOME`，指向缓存目录 `MIYU_CACHE_DIR` 下的 `browser-home/`；没有缓存目录时使用临时目录，用完之后删除。这个做法还没有在 Miyu 的沙盒里实测过。

## 注意

使用这些工具时，要注意以下几点：

- 工具给出的价格是起价，下单时的价格可能更高；往返的合计价格是两张单程票相加，不是联程票的价格。
- 价格历史存在 `MIYU_STATE_DIR` 里（核心还没有提供这个目录时，放在 `MIYU_CACHE_DIR`，重启后会被清空；旧版核心提供的是 `MIYU_SCRIPT_STATE_DIR`、`MIYU_SCRIPT_CACHE_DIR`，工具也识别）。
- 网络出口所在的国家会影响 Google 的结果；`gl` 参数在机票查询里固定为 `us`，在酒店查询里固定为 `jp`，其他地区没有实测过。
