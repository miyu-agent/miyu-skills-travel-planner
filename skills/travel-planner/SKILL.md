---
name: travel-planner
description: "Plan a trip: a sourced day-by-day itinerary with budget and booking links, using the flight_deals and hotel_deals tools for prices. Use when the user mentions travel, a holiday, an itinerary, flights or hotels, even in passing: 旅游、出游、度假、行程、攻略、去哪玩、机票、酒店、景点、假期安排、带家人出行."
metadata:
  display_name: Travel planner
  summary: Plan a trip with sources, itinerary, budget, flights and hotels
  display_name_zh: 旅行规划
  summary_zh: 带出处地排行程、算预算，查机票和酒店价格
---

# Travel planner

## Principles

- **Search first, cite everything.** Opening hours, ticket prices, timetables and room rates change; from memory they mislead. Put a source link and the date you checked after every key fact. What you could not verify, say so ("未能核实").
- **Use what this session has.** Web search is always there; use other search tools (Zhihu, Xiaohongshu, second-hand markets for tickets and gear) only if they are actually in your tool list. Xiaohongshu and Zhihu carry a lot of soft ads: cross-check prices and recommendations with another source.
- **Give links for anything to buy or book**: tickets, hotels, trains, attraction reservations, official or mainstream platforms.

## Flight and hotel prices

Two tools in this package. Load them before the first call.

- **`flight_deals`**: one-way fares from Google Flights, cheapest first, with flight numbers, times, stops, and the cheapest per airline and per cabin.
  - Never guess `origin`. If the user only named a destination, ask where they leave from.
  - Round trip: give `return`; both legs are searched and a total given (two one-ways, which is close to a round-trip fare).
  - No fixed dates: `action: "sweep"` with `window` (the span they can travel, e.g. `09-29..10-08`) and `days` (`5` or `5-7`) ranks departure and return combinations.
  - A city covers all its airports (上海 = PVG + SHA). Unsure of a code: `action: "airports"`.
- **`hotel_deals`**: room rates from Google Hotels, which already shows the lowest price across OTAs.
  - Use **English place names** (`Shinjuku, Tokyo`). A Chinese name only fuzzy-matches: 苏州金鸡湖 once returned hotels 30 km away with no error. The output warns when the name is CJK.
  - No fixed check-in: `action: "sweep"` scans the next days for the cheapest night.
  - Before quoting a minimum, read the `注意` section and any failed source. To cross-check, add `"sources": ["google", "booking"]` (Booking needs a browser on the machine, takes 10–20 s and gets rate-limited; if it fails, say the price is Google only).
- Prices are "from" prices for the stated cabin, passengers and nights; the final checkout price can be higher. Say so.
- Both keep a price history; `action: "trend"` shows whether a route or place got cheaper since earlier checks.

## Step 1: what the user wants

Take what the user already said; ask only for what is missing, at most three questions at a time, with options where you can (use the question tool if you have one).

1. **From and to.** No destination yet: see "Suggesting destinations".
2. **Dates**, out and back. They set the length and the season (blossoms, snow, rain, typhoons) and holiday crowds and prices.
3. **Who goes**: how many; elderly, children, pregnant, limited mobility. This drives daily walking, sights, where to stay, accessibility.
4. **Taste**: history, nature, food, photo spots, off the beaten track, relaxing.
5. **Getting there and around**: driving, train, plane; renting a car there.
6. **Budget**: total or per person, and where to spend (e.g. "nice hotel, cheap food").

Reasonable defaults need no question; state them at the top of the plan (e.g. "未说明预算，按中档标准规划，人均约 X 元/天").

### Suggesting destinations

With no destination, search with their dates, group, taste and budget, then suggest 2–3, each with: weather and seasonal highlights, anything unusual right now (construction, closures, incidents), and how hard and how costly it is to get there. Continue once they pick.

## Step 2: research the destination

At least:

- **Weather and season**: temperature, rain, sunrise and sunset (usable hours).
- **Holidays and crowds**: local holidays or big events on those dates; price rises; reservations needed.
- **Sights**: opening hours, closing days (many museums close on Mondays), prices and where to buy, real-name booking, temporary closures.
- **Transport**: airport or station to town, cost; public transport, taxis, car hire; real travel time between sights.
- **Where to stay**: 2–3 areas that fit the group and budget (close to sights, good transport, no stairs for the elderly or kids), with `hotel_deals` prices.
- **Food** that fits their taste; hours, queues, reservations.
- **Papers and safety**: visas, passport validity; border permits for some regions in China; recent safety or disaster warnings.
- **Special needs**: accessibility, baby rooms, hospitals for elderly and kids; no high altitude or strenuous activities for pregnancy.

## Step 3: the day-by-day plan

Each day: where to stay (area, candidate hotels, links), where to go in order, how (mode, time, cost), tickets (where, link, booking needed), meals (with alternatives), cost for the day.

Then check the usual mistakes:

- Travel time between places counted? No more than 3–4 main places a day, fewer with elderly or kids.
- Anything closed on the day you put it (Monday museums)? Opening hours match arrival times?
- Arrival and departure travel taken off the first and last day?
- Total within budget? If not, change the hotel or cut paid sights.
- A plan B for rain or a closure?

## Step 4: output

Unless asked otherwise, Markdown in the user's language:

```markdown
# [目的地] X 天行程（[日期]）
## 概览
（人员、预算假设、天气提示、需提前预约的事项）
## 每日行程
### Day 1 [日期]：[主题]
- 住宿：
- 上午 / 下午 / 晚上：（景点 + 交通 + 门票 + 用餐）
- 当日预计花费：
## 预算汇总
（表格：交通 / 住宿 / 门票 / 餐饮 / 其他 / 合计）
## 预订清单
（按时间顺序列出需要提前订的东西及链接）
## 备选方案
（雨天、临时闭馆的替代安排）
## 信息来源
（链接 + 查询日期）
```
