"""编译运行环境 + 命令闭环信号 + Manim 引导测试。"""
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from core.tools import ToolRegistry
from core import build_tools as bt
from core import sandbox as sb
from core.agent import Agent


class BuildToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = ToolRegistry(workspace=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register(self):
        bt.register_build_tools(self.reg)
        names = set(self.reg.list_names())
        self.assertIn("build_environment", names)
        self.assertIn("compile_run", names)

    def test_detect_environment_has_python(self):
        env = bt.detect_environment()
        self.assertTrue(env["python"])
        self.assertIn("matlab", env)

    def test_cpp_compile_and_run(self):
        r = bt.compile_run({"language": "cpp",
                            "code": '#include <iostream>\nint main(){std::cout<<"OK"<<std::endl;return 0;}'})
        # g++ 可能不在某些环境；若在则必须真的跑出 OK
        env = bt.detect_environment()
        if not env["g++"]:
            self.skipTest("no g++")
        self.assertTrue(r.ok, r.output)
        self.assertIn("OK", r.output)

    def test_cpp_compile_failure_is_false(self):
        env = bt.detect_environment()
        if not env["g++"]:
            self.skipTest("no g++")
        r = bt.compile_run({"language": "cpp", "code": "int main(){ broken syntax }"})
        self.assertFalse(r.ok)
        self.assertIn("编译失败", r.output)

    def test_python_run(self):
        r = bt.compile_run({"language": "python", "code": "print('hello_py')"})
        self.assertTrue(r.ok, r.output)
        self.assertIn("hello_py", r.output)

    def test_matlab_honest_degrade(self):
        env = bt.detect_environment()
        if env["matlab"]:
            self.skipTest("MATLAB installed, skip degrade test")
        r = bt.compile_run({"language": "matlab", "code": "disp(1)"})
        self.assertFalse(r.ok)
        self.assertIn("未", r.output)

    def test_missing_code(self):
        r = bt.compile_run({"language": "python", "code": ""})
        self.assertFalse(r.ok)


class SandboxExitCodeTests(unittest.TestCase):
    """沙盒失败必须 ok=False，否则模型看不到失败信号。"""

    def test_sandbox_failure_signal(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ToolRegistry(workspace=tmp.name)
            sb.register_sandbox_tools(reg)
            r = reg.execute("sandbox_run", {"language": "python",
                                            "code": "raise SystemExit(3)", "timeout": 20})
            self.assertFalse(r.ok, "退出码非0时 ok 必须为 False")
        finally:
            tmp.cleanup()

    def test_sandbox_success(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ToolRegistry(workspace=tmp.name)
            sb.register_sandbox_tools(reg)
            r = reg.execute("sandbox_run", {"language": "python", "code": "print('hi')", "timeout": 20})
            self.assertTrue(r.ok, r.output)
        finally:
            tmp.cleanup()


class CodeIntentFilterTests(unittest.TestCase):
    def test_code_intent_selects_compile_tools(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            reg = ToolRegistry(workspace=tmp.name)
            bt.register_build_tools(reg)
            schemas = reg.get_schemas()
            for phrase in ("帮我编译这段C++", "用matlab算一下", "运行这段代码"):
                sel = Agent._select_relevant_tools(schemas, phrase)
                names = {s["function"]["name"] for s in sel}
                self.assertTrue(names & {"compile_run", "build_environment"},
                                f"{phrase} 应选中编译工具，实际 {names}")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
