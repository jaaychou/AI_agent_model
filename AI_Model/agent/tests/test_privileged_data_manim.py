"""高权限开关、抓包、数据图表、Manim 集成测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry, ToolResult, register_builtin_tools
from core import privileged_tools as pt
from core import capture_tools as ct
from core import data_tools as dt
from core import manim_tools as mt
from core.agent import Agent


class _RegHelper:
    def make(self):
        self.tmp = tempfile.TemporaryDirectory()
        return ToolRegistry(workspace=self.tmp.name, confirm_callback=lambda m: True)


class PrivilegedToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name, confirm_callback=lambda m: True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_groups_registers_nothing(self):
        pt.register_privileged_tools(self.reg, groups=set())
        names = set(self.reg.list_names())
        self.assertNotIn("firmware_info", names)
        self.assertNotIn("startup_manager", names)
        self.assertNotIn("run_windows_command", names)

    def test_firmware_group_only_firmware_tools(self):
        pt.register_privileged_tools(self.reg, groups={"firmware"})
        names = set(self.reg.list_names())
        self.assertIn("firmware_info", names)
        self.assertIn("uefi_boot_entries", names)
        self.assertIn("bios_update_guidance", names)
        self.assertNotIn("startup_manager", names)

    def test_startup_group_only_startup(self):
        pt.register_privileged_tools(self.reg, groups={"startup"})
        names = set(self.reg.list_names())
        self.assertIn("startup_manager", names)
        self.assertNotIn("firmware_info", names)

    def test_winr_group_only_command(self):
        pt.register_privileged_tools(self.reg, groups={"winr"})
        names = set(self.reg.list_names())
        self.assertIn("run_windows_command", names)
        self.assertNotIn("firmware_info", names)

    def test_bios_guidance_honest(self):
        res = pt._bios_update_guidance({})
        self.assertTrue(res.ok)
        self.assertIn("不能无人值守", res.output)
        self.assertIn("变砖", res.output)

    def test_startup_add_requires_name_and_command(self):
        res = pt._startup_add({"name": "x"}, lambda m: True)
        self.assertFalse(res.ok)

    def test_startup_remove_denied_by_user(self):
        res = pt._startup_remove({"name": "x"}, lambda m: False)
        self.assertFalse(res.ok)
        self.assertIn("取消", res.output)

    def test_intents(self):
        self.assertTrue(pt.select_privileged_tools("帮我改BIOS"))
        self.assertTrue(pt.select_privileged_tools("查看开机自启动"))
        self.assertTrue(pt.select_privileged_tools("运行msconfig"))
        self.assertFalse(pt.select_privileged_tools("今天天气不错"))


class CaptureToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name, confirm_callback=lambda m: True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_by_default(self):
        ct.register_capture_tools(self.reg, enabled=False)
        self.assertNotIn("capture_status", set(self.reg.list_names()))

    def test_enabled_registers_all(self):
        ct.register_capture_tools(self.reg, enabled=True)
        names = set(self.reg.list_names())
        for n in ("capture_status", "capture_packets", "scan_wifi_80211",
                  "scan_ble_beacon", "wifi_profile_passwords"):
            self.assertIn(n, names)

    def test_capture_denied_by_user(self):
        res = ct._capture_packets({"count": 1}, lambda m: False)
        self.assertFalse(res.ok)
        self.assertIn("取消", res.output)

    def test_wifi_password_requires_admin(self):
        with mock.patch.object(ct, "_is_admin", return_value=False):
            res = ct._wifi_profiles_key({}, lambda m: True)
        self.assertFalse(res.ok)
        self.assertIn("管理员", res.output)

    def test_capture_intent(self):
        self.assertTrue(ct.has_capture_intent("帮我抓包看看"))
        self.assertTrue(ct.has_capture_intent("查下已保存的wifi密码"))
        self.assertFalse(ct.has_capture_intent("写个冒泡排序"))


class DataToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name, confirm_callback=lambda m: True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register(self):
        dt.register_data_tools(self.reg)
        names = set(self.reg.list_names())
        for n in ("venv_manager", "plot_chart", "data_analyze"):
            self.assertIn(n, names)

    def test_plot_chart_creates_png(self):
        res = dt._plot_chart({"kind": "bar", "title": "测试图",
                              "labels": ["A", "B", "C"], "values": [3, 7, 5]})
        self.assertTrue(res.ok, res.output)
        self.assertIn(".png", res.output)
        p = Path(res.output.split("：")[1].split("（")[0])
        self.assertTrue(p.exists())

    def test_plot_chart_requires_data(self):
        res = dt._plot_chart({"kind": "bar", "labels": ["A"]})
        self.assertFalse(res.ok)

    def test_data_analyze_csv(self):
        csv = Path(self.tmp.name) / "d.csv"
        csv.write_text("name,score\nA,10\nB,20\nC,30\n", encoding="utf-8")
        res = dt._data_analyze({"path": str(csv)})
        self.assertTrue(res.ok, res.output)
        self.assertIn("3 行", res.output)

    def test_venv_list(self):
        res = dt._venv_list({})
        self.assertTrue(res.ok)

    def test_data_intent(self):
        self.assertTrue(dt.has_data_intent("帮我画个柱状图"))
        self.assertTrue(dt.has_data_intent("建个虚拟环境"))
        self.assertFalse(dt.has_data_intent("你好"))


class ManimToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register(self):
        mt.register_manim_tools(self.reg)
        names = set(self.reg.list_names())
        for n in ("manim_status", "manim_examples", "manim_render"):
            self.assertIn(n, names)

    def test_examples_lists_eola(self):
        res = mt._list_examples({})
        self.assertTrue(res.ok, res.output)
        self.assertIn("chapter1.py", res.output)

    def test_render_requires_code_or_script(self):
        res = mt._render({})
        self.assertFalse(res.ok)

    def test_intent(self):
        self.assertTrue(mt.has_manim_intent("做个线性代数动画"))
        self.assertFalse(mt.has_manim_intent("今天吃什么"))


class WebSearchToggleTests(unittest.TestCase):
    def test_disabled_skips_web_search(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ToolRegistry(workspace=tmp.name)
            register_builtin_tools(reg, web_search_enabled=False)
            self.assertNotIn("web_search", set(reg.list_names()))
        finally:
            tmp.cleanup()

    def test_enabled_registers_web_search(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ToolRegistry(workspace=tmp.name)
            register_builtin_tools(reg, web_search_enabled=True)
            self.assertIn("web_search", set(reg.list_names()))
        finally:
            tmp.cleanup()


class NewToolIntentFilterTests(unittest.TestCase):
    def test_new_intents_detected(self):
        """验证 agent.py 的意图正则能识别新工具关键词。"""
        reg = ToolRegistry(workspace=tempfile.gettempdir())
        register_builtin_tools(reg, web_search_enabled=True)
        pt.register_privileged_tools(reg, groups={"firmware", "startup", "winr"})
        ct.register_capture_tools(reg, enabled=True)
        dt.register_data_tools(reg)
        mt.register_manim_tools(reg)
        # 用 Agent 的静态方法筛选 schema
        method = Agent._select_relevant_tools
        schemas = reg.get_schemas()
        for phrase, tool in [
            ("画个折线图", "plot_chart"),
            ("做个manim动画", "manim_render"),
            ("查看BIOS版本", "firmware_info"),
            ("抓包分析", "capture_packets"),
            ("设置开机自启动", "startup_manager"),
        ]:
            selected = method(schemas, phrase)
            names = set()
            for s in selected:
                fn = s.get("function", {}) if isinstance(s, dict) else getattr(s, "function", {})
                names.add(fn.get("name", "") if isinstance(fn, dict) else "")
            self.assertIn(tool, names, f"{phrase} 应选中 {tool}")


if __name__ == "__main__":
    unittest.main()
