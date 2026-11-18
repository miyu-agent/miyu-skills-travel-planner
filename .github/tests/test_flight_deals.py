# flight_deals.py 的测试（审计 2026-11-18 #180）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网：urllib.request.urlopen 换成记账的桩，断言根本没被调。
# HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "flight_deals.py"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("flight_deals_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"),
                   MIYU_STATE_DIR=str(root / "state"), MIYU_SCRIPT_STATE_DIR=str(root / "state"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load()
        self.calls = []

        def fake_urlopen(req, timeout=None):
            self.calls.append(getattr(req, "full_url", req))
            raise OSError("测试里不该联网")

        patcher = mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, args):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out):
            rc = self.mod.main()
        return rc, out.getvalue()


class AirlineType(Sandbox):
    # 审计 #180：build 不查 airline 列表元素的类型，[123] 通过；cmd_search 先并发查完票
    # （真环境就是一次 Google 请求），之后 filter_notes 才 TypeError、apply_filters
    # AttributeError，打 traceback。应在 build 里就报 Failed，不发请求。
    def test_non_string_airline_rejected_in_build(self):
        for airline in ([123], [""], ["MU", None]):
            with self.subTest(airline=airline):
                with self.assertRaises(self.mod.Failed):
                    self.mod.build({"action": "search", "origin": "PVG", "dest": "NRT",
                                    "airline": airline})

    def test_non_string_airline_fails_before_request(self):
        rc, out = self.call({"action": "search", "origin": "PVG", "dest": "NRT",
                             "depart": "+30", "airline": [123]})
        self.assertEqual(rc, 1, out)
        self.assertIn("airline", out)
        self.assertEqual(self.calls, [], "airline 不对应在发请求前就报错")


class OldCliHint(Sandbox):
    # 审计 #180：认不出地名时的提示还写旧 CLI「跑 flight-deals airports X」，
    # 现在的接口是 action=airports（schema、cmd_airports、SKILL.md 都这么写）。
    def test_unknown_place_hint_uses_action_airports(self):
        with self.assertRaises(self.mod.Failed) as cm:
            self.mod.resolve("东京湾某地xyz", "目的地")
        hint = cm.exception.hint
        self.assertNotIn("flight-deals airports", hint)
        self.assertIn("action=airports", hint.replace(" ", ""))


class DeadConstants(unittest.TestCase):
    # 审计 #180：COLUMNS、REQUIRED 的注释说「reshape() 靠两者对比判断老库该不该重建」，
    # 但全文件没有 reshape，这两个常量除了定义没人用。要么删掉，要么真有代码用它们。
    def test_columns_required_not_dead(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for name in ("COLUMNS", "REQUIRED"):
            with self.subTest(name=name):
                uses = len(re.findall(rf"\b{name}\b", src))
                defined = re.search(rf"^{name}\s*=", src, re.M) is not None
                if defined:
                    self.assertGreater(uses, 1, f"{name} 只有定义、没人用")
        if "reshape()" in src:
            self.assertRegex(src, r"\bdef reshape\(", "注释提到 reshape()，却没有这个函数")


class FarDates(Sandbox):
    # 审计收尾：出发日写成 +N 且 N 极大时 parse_day 的 date + timedelta 抛 OverflowError
    # （只接了 ValueError）；sweep 的 days 极大时 plan_combos 算返程日同样超界。
    # main 只接 Failed，打 traceback。写成 9999-12-25 这种日期的有售票窗口挡着，本来就是友好报错。
    CASES = [
        {"action": "search", "depart": "+99999999999"},
        {"action": "search", "depart": "+3000000"},
        {"action": "sweep", "window": "+10..+99999999999"},
        {"action": "sweep", "window": "+10..+20", "days": "3000000"},
        {"action": "sweep", "window": "+10..+20", "days": "99999999999"},
    ]

    def test_far_dates_fail_before_request(self):
        for args in self.CASES:
            with self.subTest(args=args):
                self.calls.clear()
                rc, out = self.call(dict(args, origin="PVG", dest="NRT"))
                self.assertEqual(rc, 1, out[:300])
                self.assertTrue(out.startswith("失败："), out[:300])
                self.assertEqual(self.calls, [], "超界的日期不该发请求")

    # 防改坏（现在就绿）：极远的具体日期由售票窗口挡下。
    def test_far_calendar_date_hits_horizon(self):
        for args in ({"action": "search", "depart": "9999-12-25"},
                     {"action": "sweep", "window": "9999-12-01..9999-12-31"}):
            with self.subTest(args=args):
                rc, out = self.call(dict(args, origin="PVG", dest="NRT"))
                self.assertEqual(rc, 1, out)
                self.assertIn("售票窗口", out)


if __name__ == "__main__":
    unittest.main()
