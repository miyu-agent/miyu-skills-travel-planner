# hotel_deals.py 的测试（审计 2026-11-18 #172 的 1、2、4 子项）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网、不起浏览器：urllib.request.urlopen 换成返回构造页面的桩。
# 历史库、HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "hotel_deals.py"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("hotel_deals_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Sandbox(unittest.TestCase):
    """临时目录当状态目录（新旧两个名字都给，这里不测 #168）；urlopen 换成桩。"""

    PAGE = '<div aria-label="Prices starting from $80, Fake Hotel"></div>'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        state = root / "state"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"),
                   MIYU_STATE_DIR=str(state), MIYU_SCRIPT_STATE_DIR=str(state))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load()     # DATA_DIR 在 import 时读，先改环境再加载
        self.assertTrue(self.mod.DB_PATH.startswith(str(root)))
        self.calls = []

        def fake_urlopen(req, timeout=None):
            self.calls.append(getattr(req, "full_url", req))
            return FakeResponse(self.PAGE.encode())

        patcher = mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, args):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out):
            rc = self.mod.main()
        return rc, out.getvalue()


class MixedCurrencyTrend(Sandbox):
    # 审计 #172-1：floor_history 的 SQL 只按 place/checkin/source 过滤，不看 currency；
    # cmd_trend 再拿请求的币种给所有点贴符号。USD 80 和 JPY 12000 混在一条线里，
    # 算出 +14900% 还全标成同一个币种。
    def test_trend_only_uses_requested_currency(self):
        with self.mod.db() as c:
            c.executemany(
                "INSERT INTO observations (observed_at,place,source,name,name_key,checkin,checkout,"
                "nights,price,per_night,currency) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [("2026-11-01T01:00:00+00:00", "Tokyo", "google", "H", "h", "2026-12-01", "2026-12-02", 1, 80, 80, "USD"),
                 ("2026-11-02T01:00:00+00:00", "Tokyo", "google", "H", "h", "2026-12-01", "2026-12-02", 1, 12000, 12000, "JPY"),
                 ("2026-11-03T01:00:00+00:00", "Tokyo", "google", "H", "h", "2026-12-01", "2026-12-02", 1, 88, 88, "USD")])
        rc, out = self.call({"action": "trend", "location": "Tokyo", "currency": "USD"})
        self.assertNotIn("12,000", out, "JPY 的点不该混进 USD 的趋势")
        self.assertNotIn("14900", out)
        # 修法二选一都算对：只画 USD 的两点（80 → 88，+10%），或者明确说币种混了。
        self.assertTrue("+10.0%" in out or "币种" in out, out)


class CurrencyCheck(Sandbox):
    # 审计 #172-2（a）：build 只把 currency .upper()，不校验；XYZ 照样拿去请求 Google、
    # 照样记进历史库。应在发请求前报错。
    def test_unknown_currency_rejected_before_request(self):
        with self.assertRaises(self.mod.Failed):
            self.mod.build({"action": "search", "location": "Tokyo", "currency": "xyz"})
        rc, out = self.call({"action": "search", "location": "Tokyo", "currency": "XYZ",
                             "no_record": True})
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.calls, [], "非法币种不该发请求")

    # 审计 #172-2（b）：_G_PRICE 第 1 组抓了价格前的币种符号，google_search 不用，
    # 直接把请求的币种写进报价。页面上是 $80、请求 CNY 时，报价被标成 80 CNY。
    # 应照页面标 USD，或者报错说币种对不上。
    def test_page_currency_symbol_not_discarded(self):
        try:
            offers = self.mod.google_search("Tokyo", date(2026, 12, 1), date(2026, 12, 2),
                                            currency="CNY")
        except self.mod.Failed:
            return
        self.assertEqual([(o.name, o.price, o.currency) for o in offers],
                         [("Fake Hotel", 80.0, "USD")])

    # 防改坏（现在就绿）：页面符号和请求币种一致时照常给出。
    def test_matching_symbol_still_works(self):
        self.PAGE = '<div aria-label="Prices starting from CN¥355, Fake Hotel"></div>'
        offers = self.mod.google_search("Tokyo", date(2026, 12, 1), date(2026, 12, 2),
                                        currency="CNY")
        self.assertEqual([(o.name, o.price, o.currency) for o in offers],
                         [("Fake Hotel", 355.0, "CNY")])


class SweepDays(Sandbox):
    # 审计 #172-4：_int(args, "days", 30, 1) 只有下限。days=1000 会真发 1000 个 Google 请求；
    # days=3000000 在 sweep 里 OverflowError（main 只接 Failed，打 traceback）。
    # 应在发请求前报 Failed（返回 1）。
    def test_huge_days_rejected_before_request(self):
        for days in (1000, 3_000_000):
            with self.subTest(days=days):
                self.calls.clear()
                rc, out = self.call({"action": "sweep", "location": "Tokyo", "days": days,
                                     "no_record": True, "format": "json"})
                self.assertEqual(rc, 1, out[:300])
                self.assertIn("days", out)
                self.assertEqual(len(self.calls), 0, f"days={days} 发了 {len(self.calls)} 个请求")


if __name__ == "__main__":
    unittest.main()
