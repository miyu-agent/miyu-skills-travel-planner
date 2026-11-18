# hotel_deals.py、flight_deals.py 读新环境变量名的测试（审计 2026-11-18 #168）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库，不联网：只建历史库、跑 places，不发请求。HOME、XDG_*、MIYU_* 都指到临时目录。
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
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
DBS = {"hotel_deals": "hotel-history.db", "flight_deals": "flight-history.db"}


def load(name):
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EnvNames(unittest.TestCase):
    # 审计 #168：核心 FH-6 以后只给新名字 MIYU_STATE_DIR / MIYU_CACHE_DIR，旧名
    # MIYU_SCRIPT_STATE_DIR / MIYU_SCRIPT_CACHE_DIR 会删。两个脚本 import 时只读旧名，
    # 只设新名时 DB_PATH 是空的，db() 报「没有 MIYU_SCRIPT_STATE_DIR……价格历史存不住」。
    # 两个旧名都要换：状态目录优先，没有就用缓存目录。
    def check(self, name, var):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target = root / "dir"
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
            env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                       XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                       XDG_DATA_HOME=str(root / "xdg-data"), **{var: str(target)})
            with mock.patch.dict(os.environ, env, clear=True):
                mod = load(name)     # DATA_DIR 在 import 时读，先改环境再加载
                self.assertEqual(mod.DB_PATH, str(target / DBS[name]))
                with mod.db():
                    pass
                self.assertTrue((target / DBS[name]).is_file())
                if name == "hotel_deals":
                    out = io.StringIO()
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"action": "places"}))), \
                            redirect_stdout(out):
                        rc = mod.main()
                    self.assertEqual(rc, 0, out.getvalue())
                    self.assertIn("历史库还是空的", out.getvalue())

    def test_new_state_dir(self):
        for name in DBS:
            with self.subTest(script=name):
                self.check(name, "MIYU_STATE_DIR")

    def test_new_cache_dir_fallback(self):
        for name in DBS:
            with self.subTest(script=name):
                self.check(name, "MIYU_CACHE_DIR")


if __name__ == "__main__":
    unittest.main()
