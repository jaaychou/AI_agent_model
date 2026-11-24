import base64
import tempfile
import unittest
from pathlib import Path
import sys
import json
import zipfile
import importlib.util
import os
import shlex
import subprocess
from unittest import mock
from urllib.error import URLError

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
from agent_gui import (AgentApp, prepare_attachments, ensure_ollama, bind_chat_shortcuts,
                       add_hover_effect, responsive_input_margin, choose_tool_model,
                       model_supports_vision, supported_attachment_paths, image_dib_bytes,
                       latex_to_readable)
from core.agent import Agent, AgentConfig
from core.llm import OllamaProvider
from core.tools import ToolRegistry, ToolResult, register_builtin_tools, execute_shell_command
from core.rag import LocalKnowledgeBase
from core.sessions import SessionStore
from core.mcp import MCPClient, load_mcp_servers, register_mcp_tools
from core.git_manager import (configure_identity, git_config_value, initialize_repository,
                              is_repository, set_remote, stage_and_commit, push_current_branch, GitError)
from core.progress import classify_task, estimated_progress, expected_seconds, format_duration

EMAIL_MCP_PATH = AGENT_DIR / 'tools' / 'email_mcp_server.py'
EMAIL_MCP_SPEC = importlib.util.spec_from_file_location('local_email_mcp_server', EMAIL_MCP_PATH)
email_mcp = importlib.util.module_from_spec(EMAIL_MCP_SPEC)
EMAIL_MCP_SPEC.loader.exec_module(email_mcp)

class GUIFeatureTests(unittest.TestCase):
    def test_action_intent_detects_requests_that_need_tools(self):
        self.assertTrue(AgentApp._looks_like_action_request('运行命令打开 VSCode'))
        self.assertTrue(AgentApp._looks_like_action_request('跑一下 Python 文件 demo.py'))
        self.assertTrue(AgentApp._looks_like_action_request('查询电脑连接了哪些外部设备'))
        self.assertTrue(AgentApp._looks_like_action_request('你自己排查并解决这个问题'))
        self.assertTrue(AgentApp._looks_like_action_request('帮我配置 Git 远端仓库'))
        self.assertTrue(AgentApp._looks_like_action_request('读取 QQ 邮箱最近的未读邮件'))
        self.assertTrue(AgentApp._looks_like_action_request('给自己发一封测试邮件'))
        self.assertTrue(AgentApp._looks_like_action_request('生成一张小猫的图'))
        self.assertFalse(AgentApp._looks_like_action_request('Python 有什么新功能？'))
        self.assertEqual(choose_tool_model(['qwen2.5vl:7b', 'qwen3.5:4b'], {}, True), 'qwen3.5:4b')
        self.assertEqual(choose_tool_model(['qwen2.5-coder:7b', 'deepseek-r1:8b'], {}, True), None)

    def test_markdown_renderer_styles_fenced_inline_code_and_headings(self):
        class FakeText:
            def __init__(self): self.parts = []
            def insert(self, _index, text, tags): self.parts.append((text, tags))
        widget = FakeText()
        AgentApp._insert_markdown(None, widget, "# Title\nUse `pathlib`\n```python\nprint('hi')\n```\n", "agent")
        self.assertTrue(any("md_heading" in tags for _text, tags in widget.parts))
        self.assertTrue(any("md_inline_code" in tags for _text, tags in widget.parts))
        self.assertTrue(any("md_code" in tags for _text, tags in widget.parts))

    def test_markdown_renderer_preserves_selectable_code_text_when_copy_controls_are_unavailable(self):
        class FakeText:
            def __init__(self): self.parts = []
            def insert(self, _index, text, tags): self.parts.append((text, tags))
        widget = FakeText()
        AgentApp._insert_markdown(None, widget, "```matlab\nx = 1;\n```\n, then done", "agent")
        rendered = "".join(part for part, _tags in widget.parts)
        self.assertIn("x = 1;", rendered)
        self.assertIn(", then done", rendered)

    def test_code_copy_button_appears_below_code_block(self):
        class FakeText:
            def __init__(self):
                self.events = []
            def insert(self, _index, text, _tags=None):
                self.events.append(("text", text))
            def window_create(self, _index, window=None, **_kwargs):
                self.events.append(("button", window))
        widget = FakeText()
        class FakeApp:
            root = object()
            def _insert_code_copy_action(self, widget, language, code):
                widget.window_create(None, window=("copy", language, code))
        AgentApp._insert_markdown(FakeApp(), widget, "```python\nprint('hi')\n```\n", "agent")
        code_index = next(i for i, (_kind, value) in enumerate(widget.events)
                          if isinstance(value, str) and "print('hi')" in value)
        button_index = next(i for i, (kind, _value) in enumerate(widget.events) if kind == "button")
        self.assertLess(code_index, button_index, "复制按钮应出现在代码块下方")

    def test_latex_math_is_rendered_as_readable_text(self):
        kalman = (r"预测：\(\hat{\mathbf{x}}_{k|k-1} = \mathbf{F}_{k-1}\hat{\mathbf{x}}_{k-1|k-1}\) "
                  r"增益：\(K_k = \frac{P_{k|k-1}}{P_{k|k-1}+R}\) "
                  r"\[\hat{\mathbf{x}}_{k|k} = \hat{\mathbf{x}}_{k|k-1} + K_k(z_k - H\hat{\mathbf{x}}_{k|k-1})\]")
        readable = latex_to_readable(kalman)
        self.assertNotIn(r"\mathbf", readable)
        self.assertNotIn(r"\frac", readable)
        self.assertNotIn("\\(", readable)
        self.assertNotIn("\\[", readable)
        self.assertIn("P", readable)
        self.assertIn("/", readable)
        self.assertIn("k|k-1", readable)
        self.assertIn("x", readable)
        self.assertIn("F", readable)

    def test_markdown_renders_inline_latex_without_raw_markup(self):
        class FakeText:
            def __init__(self): self.parts = []
            def insert(self, _index, text, tags): self.parts.append((text, tags))
        widget = FakeText()
        AgentApp._insert_markdown(None, widget, r"增益 K = \(\frac{a}{b}\) 结束", "agent")
        rendered = "".join(part for part, _tags in widget.parts)
        self.assertNotIn(r"\frac", rendered)
        self.assertNotIn("\\(", rendered)
        self.assertIn("a", rendered)

    def test_default_answer_budget_is_large_enough(self):
        from core.llm import OllamaProvider
        provider = object.__new__(OllamaProvider)
        provider.base_url = "http://127.0.0.1:11434"
        provider.default_model = "qwen3.5:4b"
        captured = {}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self): return iter([])
            def read(self): return b'{"model":"qwen3.5:4b","message":{"role":"assistant","content":"ok"}}'
        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            provider.chat(messages=[{"role": "user", "content": "hi"}])
        self.assertGreaterEqual(captured["payload"]["options"]["num_predict"], 2048)

    def test_attachment_types_follow_model_vision_capability(self):
        self.assertTrue(model_supports_vision("qwen2.5vl:7b"))
        self.assertTrue(model_supports_vision("qwen3.5:4b", ["tools", "vision"]))
        self.assertFalse(model_supports_vision("qwen2.5-coder:7b", ["tools"]))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "photo.png"
            text = Path(directory) / "code.py"
            image.write_bytes(b"png")
            text.write_text("print('hi')", encoding="utf-8")
            accepted, rejected = supported_attachment_paths([str(image), str(text)], vision=False)
            self.assertEqual(accepted, [str(text)])
            self.assertEqual(rejected, ["photo.png"])

    def test_generated_image_has_windows_dib_clipboard_payload(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "sample.png"
            Image.new("RGB", (2, 3), (255, 0, 0)).save(image_path)
            dib = image_dib_bytes(image_path)
            self.assertEqual(int.from_bytes(dib[:4], "little"), 40)  # BITMAPINFOHEADER
            self.assertGreater(len(dib), 40)

    def test_progress_helpers_classify_tasks_format_elapsed_and_hold_under_100_percent(self):
        self.assertEqual(classify_task('生成一张小猫照片'), 'image')
        self.assertEqual(classify_task('帮我写一个 Python 脚本'), 'code')
        self.assertEqual(classify_task('你好'), 'chat')
        self.assertEqual(format_duration(125), '2分05秒')
        self.assertEqual(expected_seconds('qwen3.5:4b', 'chat', {'qwen3.5:4b|chat': [10, 20]}), 15)
        percent, eta = estimated_progress(120, 60)
        self.assertEqual(percent, 95)
        self.assertGreater(eta, 0)

    def test_full_local_file_access_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside = root / 'workspace', root / 'outside.txt'
            workspace.mkdir()
            outside.write_text('secret', encoding='utf-8')
            limited = ToolRegistry(workspace)
            register_builtin_tools(limited)
            denied = limited.execute('read_file', {'path': str(outside)})
            self.assertFalse(denied.ok)
            full = ToolRegistry(workspace, allow_outside_workspace=True)
            register_builtin_tools(full)
            allowed = full.execute('read_file', {'path': str(outside)})
            self.assertTrue(allowed.ok)
            self.assertIn('secret', allowed.output)
            denied_write = full.execute('write_file', {'path': str(root / 'new.txt'), 'content': 'x'})
            self.assertFalse(denied_write.ok)
            full.confirm = lambda _message: True
            allowed_write = full.execute('write_file', {'path': str(root / 'new.txt'), 'content': 'x'})
            self.assertTrue(allowed_write.ok)

    def test_generate_image_tool_confirms_and_returns_comfyui_output(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory, confirm_callback=lambda _message: True)
            paths = []
            progress = []
            register_builtin_tools(registry, image_callback=paths.append, progress_callback=progress.append)
            ps_response = mock.MagicMock()
            ps_response.__enter__.return_value.read.return_value = b'{"models": []}'
            image_process = mock.Mock()
            image_process.stdout = iter([
                'AGENT_PROGRESS:{"stage":"ComfyUI 生图中（估算）","percent":45,"remaining":20}\n',
                'IMAGE_PATH: D:/ComfyUI/output/ZImage.png\n',
            ])
            image_process.wait.return_value = 0
            with mock.patch('core.tools.subprocess.Popen') as popen, \
                 mock.patch('core.tools.threading.Thread') as thread, \
                 mock.patch('core.tools.urllib.request.urlopen',
                            side_effect=[ps_response, URLError('ComfyUI stopped'), mock.MagicMock()]), \
                 mock.patch('core.tools.subprocess.run') as run:
                popen.side_effect = [mock.Mock(), image_process]
                thread.side_effect = lambda target, daemon: mock.Mock(start=lambda: target())
                result = registry.execute('generate_image', {'prompt': 'a red fox'})
            self.assertTrue(result.ok, result.output)
            self.assertIn('ZImage.png', result.output)
            self.assertEqual(paths, ['D:/ComfyUI/output/ZImage.png'])
            self.assertIn('zimage_generate.py', popen.call_args_list[1].args[0][1])
            self.assertEqual(popen.call_count, 2)
            self.assertTrue(any(item.get('percent') == 45 for item in progress))

    def test_open_vscode_uses_generic_discovery_and_reports_visible_window(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            shortcut = Path(directory) / 'Visual Studio Code.lnk'
            with mock.patch('core.tools._find_start_menu_shortcuts', return_value=[shortcut]), \
                 mock.patch('core.tools._shell_execute', return_value=(4321, None)), \
                 mock.patch('core.tools._visible_window_title', return_value='test - Visual Studio Code'):
                result = registry.execute('open_vscode', {})
            if sys.platform == 'win32':
                self.assertTrue(result.ok, result.output)
                self.assertIn('PID：4321', result.output)
                self.assertIn('窗口可见', result.output)
                self.assertIn('Visual Studio Code.lnk', result.output)

    def test_generic_application_launch_only_claims_success_after_visible_window_check(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            shortcut = Path(directory) / 'Example Editor.lnk'
            with mock.patch('core.tools._find_start_menu_shortcuts', return_value=[shortcut]), \
                 mock.patch('core.tools._shell_execute', return_value=(2468, None)), \
                 mock.patch('core.tools._visible_window_title', return_value='Untitled - Example Editor') as visible:
                result = registry.execute('open_application', {'name': 'Example Editor'})
            if sys.platform == 'win32':
                self.assertTrue(result.ok, result.output)
                self.assertIn('窗口可见', result.output)
                self.assertIn('PID：2468', result.output)
                visible.assert_called()

            with mock.patch('core.tools._find_start_menu_shortcuts', return_value=[shortcut]), \
                 mock.patch('core.tools._shell_execute', return_value=(2468, None)), \
                 mock.patch('core.tools._visible_window_title', return_value=None), \
                 mock.patch('core.tools.time.time', side_effect=[0, 13]), \
                 mock.patch('core.tools.time.sleep'):
                result = registry.execute('open_application', {'name': 'Example Editor'})
            if sys.platform == 'win32':
                self.assertFalse(result.ok)
                self.assertIn('未确认应用已显示', result.output)

    def test_application_intent_parses_direct_and_multiword_names(self):
        self.assertTrue(Agent._application_open_request('打开一下 Visual Studio Code'))
        self.assertEqual(Agent._application_query('打开一下 Visual Studio Code'), 'Visual Studio Code')
        self.assertFalse(Agent._application_open_request('打开 D:\\Projects\\hello.py 文件'))

    def test_requested_file_path_keeps_spaces_in_absolute_windows_paths(self):
        path = r'D:\\Users\\jayyh\\Desktop\\My Project\\hello world.md'
        self.assertEqual(Agent._requested_file_path(f'打开文件 {path}'), path)

    def test_file_creation_router_supports_bat_markdown_javascript_c_and_cpp(self):
        cases = [
            ('创建一个 BAT 文件 run.bat', 'run.bat'),
            ('创建一个 Markdown 文件 notes.md', 'notes.md'),
            ('创建一个 JavaScript 文件 main.js', 'main.js'),
            ('创建一个 C 语言文件 main.c', 'main.c'),
            ('创建一个 C++ 文件 main.cpp', 'main.cpp'),
        ]

        class SourceProvider:
            def chat(self, **_kwargs): return {'content': 'generated source'}

        for request, filename in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                registry = ToolRegistry(directory)
                register_builtin_tools(registry)
                agent = Agent(SourceProvider(), registry, config=AgentConfig(verbose=False))
                answer = agent.run(request)
                self.assertIn('已创建并验证文件', answer)
                self.assertEqual((Path(directory) / filename).read_text(encoding='utf-8'), 'generated source\n')

    def test_open_file_auto_mode_edits_scripts_and_opens_regular_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            script = Path(directory) / 'sample.bat'
            document = Path(directory) / 'notes.md'
            script.write_text('echo safe', encoding='utf-8')
            document.write_text('# Notes', encoding='utf-8')
            with mock.patch('core.tools._shell_execute', return_value=(100, None)) as launch, \
                 mock.patch('core.tools._visible_window_title', side_effect=['sample.bat - Editor', 'notes.md - Editor']):
                script_result = registry.execute('open_file', {'path': 'sample.bat'})
                document_result = registry.execute('open_file', {'path': 'notes.md'})
            if sys.platform == 'win32':
                self.assertTrue(script_result.ok, script_result.output)
                self.assertTrue(document_result.ok, document_result.output)
                self.assertEqual([call.kwargs['verb'] for call in launch.call_args_list], ['edit', 'open'])

    def test_device_query_reports_actual_powershell_json(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            with mock.patch('core.tools.subprocess.run') as run:
                run.return_value = mock.Mock(returncode=0,
                                             stdout=base64.b64encode('[{"type":"USB","name":"Test drive","status":"OK"}]'.encode('utf-16le')).decode('ascii'),
                                             stderr='')
                result = registry.execute('query_devices', {})
            if sys.platform == 'win32':
                self.assertTrue(result.ok, result.output)
                self.assertIn('Test drive', result.output)

    def test_enter_sends_shift_enter_inserts_newline_and_buttons_hover(self):
        class FakeWidget:
            def __init__(self): self.bindings, self.inserted, self.colors = {}, [], []
            def bind(self, event, callback): self.bindings[event] = callback
            def insert(self, _index, text): self.inserted.append(text)
            def configure(self, **kwargs): self.colors.append(kwargs)
        widget = FakeWidget()
        sent = []
        bind_chat_shortcuts(widget, lambda: sent.append(True))
        self.assertEqual(widget.bindings['<Return>'](None), 'break')
        self.assertEqual(widget.bindings['<Control-Return>'](None), 'break')
        self.assertEqual(len(sent), 2)
        self.assertEqual(widget.bindings['<Shift-Return>'](type('Event', (), {'widget': widget})()), 'break')
        self.assertEqual(widget.inserted, ['\n'])
        add_hover_effect(widget, '#fff', '#eee')
        widget.bindings['<Enter>'](None)
        widget.bindings['<Leave>'](None)
        self.assertEqual(widget.colors[-2:], [{'bg': '#eee'}, {'bg': '#fff'}])

    def test_input_margin_scales_with_chat_pane_width(self):
        self.assertGreater(responsive_input_margin(2000), responsive_input_margin(900))
        self.assertEqual(responsive_input_margin(400), 20)

    def test_session_history_persists_turns_and_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / 'history.sqlite3')
            session_id = store.create('讨论 Git 仓库', directory, 'qwen2.5-coder:7b')
            store.append_turn(session_id, 'user', '检查当前分支')
            store.append_turn(session_id, 'assistant', '当前分支是 main。')
            saved = store.load(session_id)
            self.assertEqual(saved['session']['title'], '讨论 Git 仓库')
            self.assertEqual([t['role'] for t in saved['turns']], ['user', 'assistant'])
            self.assertTrue(store.delete(session_id))
            self.assertIsNone(store.load(session_id))

    def test_session_history_can_pin_bulk_delete_and_fork_before_an_edited_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / 'history.sqlite3')
            original = store.create('原始对话', directory, 'qwen3.5:4b')
            store.append_turn(original, 'user', '第一问')
            store.append_turn(original, 'assistant', '第一答')
            store.append_turn(original, 'user', '待编辑的问题')
            branch = store.fork_prefix(original, 2)
            self.assertIsNotNone(branch)
            self.assertEqual([t['content'] for t in store.load(branch)['turns']], ['第一问', '第一答'])
            self.assertEqual(len(store.load(original)['turns']), 3)
            self.assertTrue(store.set_pinned(original, True))
            self.assertEqual(store.list_sessions()[0]['id'], original)
            self.assertEqual(store.list_sessions()[0]['pinned'], 1)
            self.assertEqual(store.delete_many([branch, original, branch]), 2)
            self.assertEqual(store.list_sessions(), [])
            store.create('新会话', directory, 'qwen3.5:4b')
            self.assertEqual(store.count_sessions(), 1)
            self.assertEqual(store.delete_all(), 1)
            self.assertEqual(store.count_sessions(), 0)

    def test_python_file_execution_uses_absolute_argv_and_reports_verified_process_result(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'folder with spaces' / 'hello world.py'
            script.parent.mkdir()
            script.write_text("print('path-ok')\n", encoding='utf-8')
            registry = ToolRegistry(directory)
            register_builtin_tools(registry, allow_dangerous=True)
            agent = Agent(object(), registry, config=AgentConfig(verbose=False))
            answer = agent.run(f'请运行 Python 文件 {script}')
            self.assertIn('退出码：0', answer)
            self.assertIn('path-ok', answer)
            self.assertIn(str(script), answer)

    def test_python_file_execution_does_not_claim_missing_file_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry, allow_dangerous=True)
            agent = Agent(object(), registry, config=AgentConfig(verbose=False))
            missing = Path(directory) / 'missing script.py'
            answer = agent.run(f'执行 Python 文件 {missing}')
            self.assertIn('文件不存在，未运行', answer)

    def test_mcp_environment_placeholders_and_calls_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'mcp.json'
            config.write_text(json.dumps({"mcpServers": {"browser": {
                "command": "python", "args": ["server.py"],
                "env": {"TOKEN": "${AGENT_TEST_TOKEN}"}}}}), encoding="utf-8")
            with mock.patch.dict('os.environ', {'AGENT_TEST_TOKEN': 'secret'}, clear=False):
                clients = load_mcp_servers(config)
            self.assertEqual(clients[0].env['TOKEN'], 'secret')
            class FakeClient:
                name = 'browser'
                def start(self): return self
                def list_tools(self): return [{'name': 'open_page', 'description': 'Open a URL', 'inputSchema': {'type': 'object', 'properties': {}}}]
                def call_tool(self, _name, _args): return 'opened'
            registry = ToolRegistry(confirm_callback=lambda _message: False)
            client = FakeClient()
            # Avoid starting the configured subprocess while exercising approval routing.
            register_mcp_tools(registry, [client])
            denied = registry.execute('mcp_browser_open_page', {})
            self.assertFalse(denied.ok)
            registry.confirm = lambda _message: True
            allowed = registry.execute('mcp_browser_open_page', {})
            self.assertTrue(allowed.ok)
            self.assertIn('opened', allowed.output)

    def test_mcp_server_child_process_hides_windows_console(self):
        client = MCPClient('test', 'node', ['server.js'])
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch('core.mcp.subprocess.Popen', return_value=process) as popen, \
             mock.patch.object(client, '_request', return_value={}), \
             mock.patch.object(client, '_notify'):
            client.start()
        if sys.platform == 'win32':
            self.assertEqual(popen.call_args.kwargs['creationflags'], 0x08000000)
        self.assertIs(popen.call_args.kwargs['stderr'], __import__('subprocess').DEVNULL)

    def test_git_manager_initializes_local_repo_sets_remote_identity_and_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            self.assertFalse(is_repository(root))
            self.assertIn('main 分支', initialize_repository(root))
            self.assertTrue(is_repository(root))
            self.assertTrue((root / '.gitignore').exists())
            configure_identity(root, 'Local Test', 'local@example.com')
            self.assertEqual(git_config_value(root, 'user.name'), 'Local Test')
            remote = 'https://github.com/example/project.git'
            self.assertIn('已添加远端', set_remote(root, remote))
            self.assertEqual(set_remote(root, remote), f'已更新远端 origin：{remote}')
            (root / 'hello.txt').write_text('hello', encoding='utf-8')
            self.assertIn('hello.txt', stage_and_commit(root, 'Initial test commit'))

    def test_git_manager_rejects_invalid_remote_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_repository(root)
            with self.assertRaisesRegex(Exception, 'HTTPS、SSH'):
                set_remote(root, 'not-a-repository-url')

    def test_git_push_without_first_commit_explains_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            initialize_repository(directory)
            set_remote(directory, 'https://github.com/example/project.git')
            with self.assertRaisesRegex(GitError, '先填写提交说明.*暂存并提交'):
                push_current_branch(directory)

    def test_command_shell_selector_uses_cmd_or_powershell_without_shell_injection_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            for shell, expected in [('CMD', 'cmd.exe'), ('PowerShell', 'powershell.exe')]:
                with mock.patch('core.tools.subprocess.run') as run:
                    run.return_value = mock.Mock(returncode=0, stdout='ok', stderr='')
                    code, output, used = execute_shell_command('Write-Output ok', directory, shell)
                self.assertEqual(code, 0)
                self.assertEqual(output, 'ok')
                self.assertEqual(used.upper(), shell.upper())
                self.assertEqual(run.call_args.args[0][0], expected)
                self.assertEqual(run.call_args.kwargs['cwd'], str(Path(directory)))

    def test_auto_start_uses_vulkan_and_configured_model_dir(self):
        import os
        import urllib.request
        with mock.patch.object(urllib.request, 'urlopen', side_effect=[URLError('offline'), mock.MagicMock()]), \
             mock.patch('subprocess.Popen') as popen, \
             mock.patch('time.sleep'):
            ok, status = ensure_ollama()
        self.assertTrue(ok)
        self.assertEqual(status, '已自动启动')
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['OLLAMA_LLM_LIBRARY'], 'vulkan')
        self.assertTrue(env['OLLAMA_MODELS'].endswith('models'))

    def test_text_file_is_included_in_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / 'sample.py'
            path.write_text('print("hello")', encoding='utf-8')
            prompt, images = prepare_attachments([str(path)], 'Review this', str(workspace))
        self.assertIn('sample.py', prompt)
        self.assertIn('print("hello")', prompt)
        self.assertIn('不要再用文件工具', prompt)
        self.assertIn('附件 sample.py', prompt)
        self.assertEqual(images, [])

    def test_image_path_reaches_ollama_as_base64(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.png'
            path.write_bytes(b'fake-png')
            prompt, images = prepare_attachments([str(path)], 'Describe it')
            captured = {}
            provider = OllamaProvider()
            provider._post = lambda route, payload, timeout=300: (captured.update(payload) or {'message': {'content': 'ok'}})
            provider.chat([{'role': 'user', 'content': prompt, 'image_paths': images}], model='qwen2.5vl:7b')
        self.assertEqual(captured['messages'][0]['images'], [base64.b64encode(b'fake-png').decode('ascii')])
        self.assertEqual(captured['model'], 'qwen2.5vl:7b')

    def test_agent_retains_image_paths_for_provider(self):
        class Provider:
            def chat(self, messages, tools=None, model=None):
                self.message = messages[-1]
                return {'content': 'recognized', 'tool_calls': []}
        provider = Provider()
        agent = Agent(provider, ToolRegistry(), config=AgentConfig(step_delay=0))
        agent.set_model('qwen2.5vl:7b')
        self.assertEqual(agent.run('看图', images=['x.png']), 'recognized')
        self.assertEqual(provider.message['image_paths'], ['x.png'])

    def test_agent_stops_repeating_same_tool_call(self):
        class Provider:
            def chat(self, messages, tools=None, model=None):
                return {'content': '', 'tool_calls': [{'id': 'json_0', 'name': 'missing', 'arguments': {'path': 'same.py'}}]}
        registry = ToolRegistry()
        agent = Agent(Provider(), registry, config=AgentConfig(max_steps=20, step_delay=0))
        result = agent.run('test')
        self.assertIn('连续两次完全相同', result)

    def test_agent_omits_irrelevant_mcp_tool_schemas_from_small_context_prompts(self):
        def tool(name, description=''):
            return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': {}}}}
        schemas = [tool('read_file'),
                   tool('mcp_playwright_browser_navigate', '[MCP:playwright] navigate'),
                   tool('mcp_playwright_browser_snapshot', '[MCP:playwright] snapshot'),
                   tool('mcp_playwright_browser_click', '[MCP:playwright] click'),
                   tool('mcp_playwright_browser_evaluate', '[MCP:playwright] evaluate')]
        local = Agent._select_relevant_tools(schemas, '生成一张小猫照片')
        self.assertEqual([s['function']['name'] for s in local], [])
        browser = Agent._select_relevant_tools(schemas, '用浏览器打开网页并点击登录')
        names = [s['function']['name'] for s in browser]
        self.assertIn('mcp_playwright_browser_navigate', names)
        self.assertIn('mcp_playwright_browser_snapshot', names)
        self.assertIn('mcp_playwright_browser_click', names)
        self.assertNotIn('mcp_playwright_browser_evaluate', names)

        email_schemas = [tool('mcp_email_qq_send_email', '[MCP:email_qq] send'),
                         tool('mcp_email_163_list_recent_emails', '[MCP:email_163] read')]
        qq_tools = Agent._select_relevant_tools(email_schemas, '读取 QQ 邮箱未读邮件')
        self.assertEqual([schema['function']['name'] for schema in qq_tools], ['mcp_email_qq_send_email'])
        netease_tools = Agent._select_relevant_tools(email_schemas, '检查 163 邮箱')
        self.assertEqual([schema['function']['name'] for schema in netease_tools], ['mcp_email_163_list_recent_emails'])

    def test_relevant_builtin_tool_schemas_are_filtered_to_reduce_prompt_tokens(self):
        def tool(name):
            return {'type': 'function', 'function': {'name': name, 'description': name, 'parameters': {'type': 'object', 'properties': {}}}}
        schemas = [tool(name) for name in ('read_file', 'write_file', 'run_command', 'run_python_file', 'web_search', 'generate_image', 'query_devices')]
        casual = Agent._select_relevant_tools(schemas, '你好')
        self.assertEqual(casual, [])
        image = Agent._select_relevant_tools(schemas, '生成一张小猫图片')
        self.assertEqual([item['function']['name'] for item in image], ['generate_image'])
        file_task = Agent._select_relevant_tools(schemas, '读取 notes.txt 内容')
        self.assertEqual([item['function']['name'] for item in file_task], ['read_file'])
        run_python = Agent._select_relevant_tools(schemas, '运行 Python 文件 demo.py')
        self.assertIn('run_python_file', [item['function']['name'] for item in run_python])

    def test_tool_loop_detector_stops_repeating_cycles_with_same_observations(self):
        calls = []
        class Provider:
            def chat(self, **_kwargs):
                index = len(calls)
                name = ('tool_a', 'tool_b')[index % 2]
                return {'content': '', 'tool_calls': [{'id': f'call-{index}', 'name': name, 'arguments': {}}]}
        registry = ToolRegistry()
        registry.register('tool_a', 'a', {'type': 'object', 'properties': {}}, lambda _args: ToolResult(True, 'same a'))
        registry.register('tool_b', 'b', {'type': 'object', 'properties': {}}, lambda _args: ToolResult(True, 'same b'))
        original_execute = registry.execute
        def counted_execute(name, args):
            calls.append(name)
            return original_execute(name, args)
        registry.execute = counted_execute
        # 关闭 L4 策略切换，单独验证底层检测器：检测到周期重复即停止
        result = Agent(Provider(), registry,
                       config=AgentConfig(max_steps=20, step_delay=0, planning_enabled=False)).run('完成一项多步操作')
        self.assertIn('以 2 步为周期重复', result)
        self.assertEqual(calls, ['tool_a', 'tool_b', 'tool_a', 'tool_b'])

    def test_tool_loop_detector_allows_same_call_when_result_changes(self):
        class Provider:
            def __init__(self): self.calls = 0
            def chat(self, **_kwargs):
                self.calls += 1
                if self.calls <= 2:
                    return {'content': '', 'tool_calls': [{'id': f'call-{self.calls}', 'name': 'changing', 'arguments': {}}]}
                return {'content': 'verified', 'tool_calls': []}
        provider = Provider()
        registry = ToolRegistry()
        registry.register('changing', 'changing', {'type': 'object', 'properties': {}},
                          lambda _args: ToolResult(True, f'value {provider.calls}'))
        answer = Agent(provider, registry, config=AgentConfig(max_steps=4, step_delay=0)).run('验证值')
        self.assertEqual(answer, 'verified')

    def test_failed_tool_status_is_explicitly_returned_to_model(self):
        agent = Agent(type('Provider', (), {'chat': lambda self, **_kwargs: {'content': '', 'tool_calls': []}})(),
                      ToolRegistry(), config=AgentConfig(verbose=False))
        message = agent._build_tool_result_message(
            {'id': 'call-1', 'name': 'write_file', 'arguments': {}}, ToolResult(False, 'disk full'))
        self.assertIn('TOOL_STATUS=FAILED', message['content'])
        self.assertIn('不能把失败说成成功', message['content'])

    def test_agent_compacts_context_without_losing_task_goal_or_tool_results(self):
        snapshots = []
        class Provider:
            def __init__(self): self.calls = 0
            def chat(self, messages, **_kwargs):
                snapshots.append(list(messages))
                self.calls += 1
                if self.calls <= 5:
                    return {'content': '', 'tool_calls': [{'id': f'call-{self.calls}', 'name': 'step',
                            'arguments': {'n': self.calls}}]}
                return {'content': 'done', 'tool_calls': []}
        registry = ToolRegistry()
        registry.register('step', 'step', {'type': 'object', 'properties': {'n': {'type': 'integer'}}},
                          lambda args: ToolResult(True, f"observed {args['n']}"))
        agent = Agent(Provider(), registry, system_prompt='system',
                      config=AgentConfig(max_steps=8, max_context_messages=6, step_delay=0))
        goal = '完成这个具体的文件校验任务'
        self.assertEqual(agent.run(goal), 'done')
        compacted = snapshots[-1]
        contents = '\n'.join(str(message.get('content', '')) for message in compacted)
        self.assertIn(goal, contents)
        self.assertIn('observed 5', contents)
        self.assertFalse(any(message.get('role') == 'tool' and not message.get('tool_call_id') for message in compacted))

    def test_file_write_read_copy_move_delete_and_verify_close_the_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory, confirm_callback=lambda _message: True)
            register_builtin_tools(registry)
            created = registry.execute('write_file', {'path': 'data.json', 'content': '{"ok": true}\n'})
            self.assertTrue(created.ok)
            self.assertIn('回读校验', created.output)
            self.assertTrue(registry.execute('verify_file', {'path': 'data.json'}).ok)
            self.assertIn('"ok": true', registry.execute('read_file', {'path': 'data.json'}).output)
            copied = registry.execute('copy_file', {'source': 'data.json', 'destination': 'copy.bin'})
            self.assertTrue(copied.ok)
            self.assertTrue(registry.execute('inspect_file', {'path': 'copy.bin'}).ok)
            moved = registry.execute('move_file', {'source': 'copy.bin', 'destination': 'moved.bin'})
            self.assertTrue(moved.ok)
            self.assertFalse((Path(directory) / 'copy.bin').exists())
            self.assertTrue((Path(directory) / 'moved.bin').is_file())
            deleted = registry.execute('delete_file', {'path': 'moved.bin'})
            self.assertTrue(deleted.ok)
            self.assertFalse((Path(directory) / 'moved.bin').exists())

    def test_agent_routes_explicit_file_read_copy_and_delete_to_real_tools(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('explicit CRUD request should call local tool directly')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.bin').write_bytes(b'\x00binary bytes')
            registry = ToolRegistry(root, confirm_callback=lambda _message: True)
            register_builtin_tools(registry)
            agent = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False))
            read_answer = agent.run('读取 source.bin 内容')
            self.assertIn('二进制文件', read_answer)
            viewed = agent.run('查看 source.bin 内容')
            self.assertIn('二进制文件', viewed)
            copied = agent.run('复制 source.bin 到 destination.bin')
            self.assertIn('已复制并校验', copied)
            self.assertEqual((root / 'destination.bin').read_bytes(), b'\x00binary bytes')
            removed = agent.run('删除 destination.bin')
            self.assertIn('已删除文件', removed)
            self.assertFalse((root / 'destination.bin').exists())

    def test_agent_routes_explicit_filename_search_without_model_guessing(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('explicit filename search should use the search tool')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sub').mkdir()
            (root / 'sub' / 'report.txt').write_text('result', encoding='utf-8')
            registry = ToolRegistry(root)
            register_builtin_tools(registry)
            answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run('查找 report.txt')
            self.assertIn('sub', answer)
            self.assertIn('report.txt', answer)

    def test_write_file_never_silently_overwrites_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'keep.txt'
            target.write_text('original', encoding='utf-8')
            registry = ToolRegistry(directory, confirm_callback=lambda _message: False)
            register_builtin_tools(registry)
            result = registry.execute('write_file', {'path': 'keep.txt', 'content': 'replacement'})
            self.assertFalse(result.ok)
            self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_edit_file_requires_an_exact_match_and_verifies_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'notes.md'
            target.write_text('alpha\nbeta\n', encoding='utf-8')
            registry = ToolRegistry(directory, confirm_callback=lambda _message: True)
            register_builtin_tools(registry)
            missed = registry.execute('edit_file', {'path': 'notes.md', 'old_text': 'missing', 'new_text': 'x'})
            self.assertFalse(missed.ok)
            self.assertEqual(target.read_text(encoding='utf-8'), 'alpha\nbeta\n')
            changed = registry.execute('edit_file', {'path': 'notes.md', 'old_text': 'beta', 'new_text': 'gamma'})
            self.assertTrue(changed.ok, changed.output)
            self.assertEqual(target.read_text(encoding='utf-8'), 'alpha\ngamma\n')

    def test_binary_exe_is_not_decoded_as_text_but_can_be_copied_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = bytearray(68)
            executable[:2] = b'MZ'
            executable[0x3C:0x40] = (64).to_bytes(4, 'little')
            executable[64:68] = b'PE\x00\x00'
            (root / 'program.exe').write_bytes(executable)
            registry = ToolRegistry(root, confirm_callback=lambda _message: True)
            register_builtin_tools(registry)
            self.assertFalse(registry.execute('read_file', {'path': 'program.exe'}).ok)
            self.assertFalse(registry.execute('write_file', {'path': 'program.exe', 'content': 'not a real exe'}).ok)
            self.assertTrue(registry.execute('copy_file', {'source': 'program.exe', 'destination': 'program-copy.exe'}).ok)
            self.assertIn('Windows PE 签名', registry.execute('verify_file', {'path': 'program-copy.exe'}).output)

    def test_read_file_extracts_docx_xlsx_and_pptx_text_without_office_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / 'brief.docx', 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Doc text</w:t></w:r></w:p></w:body></w:document>')
            with zipfile.ZipFile(root / 'sheet.xlsx', 'w') as archive:
                archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
                archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
                archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Cell text</t></is></c></row></sheetData></worksheet>')
            with zipfile.ZipFile(root / 'slides.pptx', 'w') as archive:
                archive.writestr('ppt/slides/slide1.xml', '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:spTree></p:cSld></p:sld>')
            registry = ToolRegistry(root)
            register_builtin_tools(registry)
            for name, expected in [('brief.docx', 'Doc text'), ('sheet.xlsx', 'Cell text'), ('slides.pptx', 'Slide text')]:
                result = registry.execute('read_file', {'path': name})
                self.assertTrue(result.ok, result.output)
                self.assertIn(expected, result.output)

    def test_zip_archive_create_list_extract_and_path_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'hello.txt').write_text('hello', encoding='utf-8')
            registry = ToolRegistry(root, confirm_callback=lambda _message: True)
            register_builtin_tools(registry)
            self.assertTrue(registry.execute('create_archive', {'paths': ['source'], 'output_path': 'bundle.zip'}).ok)
            self.assertIn('hello.txt', registry.execute('list_archive', {'path': 'bundle.zip'}).output)
            extracted = registry.execute('extract_archive', {'path': 'bundle.zip', 'destination': 'unpacked'})
            self.assertTrue(extracted.ok, extracted.output)
            self.assertEqual((root / 'unpacked' / 'source' / 'hello.txt').read_text(encoding='utf-8'), 'hello')
            with zipfile.ZipFile(root / 'bad.zip', 'w') as archive:
                archive.writestr('../escape.txt', 'bad')
            result = registry.execute('extract_archive', {'path': 'bad.zip', 'destination': 'bad-out'})
            self.assertFalse(result.ok)
            self.assertFalse((root.parent / 'escape.txt').exists())

    def test_download_file_is_confirmed_streamed_and_not_executed(self):
        class Response:
            headers = {'Content-Length': '5'}
            def __init__(self): self.parts = [b'hello', b'']
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return 'https://example.test/file.bin'
            def read(self, _size): return self.parts.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            confirmations = []
            registry = ToolRegistry(directory, confirm_callback=lambda message: confirmations.append(message) or True)
            register_builtin_tools(registry)
            with mock.patch('core.tools.urllib.request.urlopen', return_value=Response()):
                result = registry.execute('download_file', {'url': 'https://example.test/file.bin', 'path': 'file.bin'})
            self.assertTrue(result.ok, result.output)
            self.assertEqual((Path(directory) / 'file.bin').read_bytes(), b'hello')
            self.assertEqual(len(confirmations), 1)
            self.assertIn('未自动打开或执行', result.output)

    def test_thinking_models_skip_thinking_for_simple_questions(self):
        self.assertFalse(Agent._needs_deep_reasoning('怎么使用 ComfyUI 工具'))
        self.assertFalse(Agent._needs_deep_reasoning('生成一张小猫照片'))
        self.assertTrue(Agent._needs_deep_reasoning('分析这个错误并修复问题'))

    def test_local_knowledge_base_indexes_searches_and_removes_workspace_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            workspace.mkdir()
            data = Path(directory) / 'agent-data'
            source = workspace / 'manual.md'
            source.write_text('电池保修期限为三年。\n\n维护时请保持设备干燥。', encoding='utf-8')
            kb = LocalKnowledgeBase(workspace, data)
            self.assertEqual(kb.add_file(source), 1)
            hits = kb.search('电池保修期限', limit=2)
            self.assertTrue(hits)
            self.assertEqual(hits[0]['source'], 'manual.md')
            self.assertIn('三年', hits[0]['content'])
            self.assertEqual(len(kb.list_documents()), 1)
            self.assertTrue(kb.remove_file('manual.md'))
            self.assertEqual(kb.search('电池保修期限'), [])

    def test_shared_knowledge_base_accepts_files_and_searches_across_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_a, workspace_b = root / 'a', root / 'b'
            workspace_a.mkdir(); workspace_b.mkdir()
            source = root / 'reference.md'
            source.write_text('跨工作区检索的共享知识条目。', encoding='utf-8')
            shared_a = LocalKnowledgeBase(root / 'agent', root / 'data', allow_external=True)
            self.assertEqual(shared_a.add_file(source), 1)
            shared_b = LocalKnowledgeBase(root / 'agent', root / 'data', allow_external=True)
            hits = shared_b.search('共享知识条目')
            self.assertTrue(hits)
            self.assertEqual(hits[0]['source'], str(source.resolve()))
            self.assertTrue(shared_b.remove_file(str(source.resolve())))

    def test_knowledge_base_refuses_files_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ws = root / 'ws'
            ws.mkdir()
            outside = root / 'private.txt'
            outside.write_text('private', encoding='utf-8')
            kb = LocalKnowledgeBase(ws, root / 'data')
            with self.assertRaises(ValueError):
                kb.add_file(outside)

    def test_file_delete_requires_confirmation_and_is_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'sample.txt'
            target.write_text('keep', encoding='utf-8')
            registry = ToolRegistry(root, confirm_callback=lambda _message: False)
            register_builtin_tools(registry)
            result = registry.execute('delete_file', {'path': 'sample.txt'})
            self.assertFalse(result.ok)
            self.assertTrue(target.exists())
            registry.confirm = lambda _message: True
            result = registry.execute('delete_file', {'path': 'sample.txt'})
            self.assertTrue(result.ok)
            self.assertFalse(target.exists())

    def test_shell_command_requires_confirmation_and_runs_from_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            denied = registry.execute('run_command', {'command': 'echo test'})
            self.assertFalse(denied.ok)
            registry.confirm = lambda _message: True
            with mock.patch('core.tools.subprocess.run') as run:
                run.return_value = mock.Mock(returncode=0, stdout='ok', stderr='')
                result = registry.execute('run_command', {'command': 'echo test'})
            self.assertTrue(result.ok)
            self.assertEqual(run.call_args.kwargs['cwd'], str(Path(directory).resolve()))

    def test_autonomous_shell_command_mode_runs_without_per_command_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory, confirm_callback=lambda _message: self.fail('unexpected confirmation'))
            register_builtin_tools(registry, allow_dangerous=True)
            with mock.patch('core.tools.subprocess.run') as run:
                run.return_value = mock.Mock(returncode=0, stdout='verified', stderr='')
                result = registry.execute('run_command', {'command': 'Write-Output verified'})
            self.assertTrue(result.ok)
            self.assertIn('verified', result.output)

    def test_real_background_command_returns_working_directory_exit_code_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry, allow_dangerous=True)
            parts = [sys.executable, '-c', "print('tool-exec-ok')"]
            command = subprocess.list2cmdline(parts) if os.name == 'nt' else shlex.join(parts)
            result = registry.execute('run_command', {'command': command, 'timeout': 20})
            self.assertTrue(result.ok, result.output)
            self.assertIn('工作目录：', result.output)
            self.assertIn('退出码：0', result.output)
            self.assertIn('tool-exec-ok', result.output)

    def test_ollama_streaming_emits_tokens_and_aggregates_final_response(self):
        payloads = []
        chunks = [
            {'message': {'role': 'assistant', 'thinking': 'hidden reasoning', 'content': 'Hello'}, 'done': False},
            {'message': {'role': 'assistant', 'content': ' world'}, 'done': True},
        ]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                return iter([json.dumps(chunk).encode('utf-8') + b'\n' for chunk in chunks])
        provider = OllamaProvider()
        tokens = []
        progress = []
        with mock.patch('core.llm.urllib.request.urlopen', side_effect=lambda request, **_kw: (payloads.append(json.loads(request.data)) or Response())):
            result = provider.chat([{'role': 'user', 'content': 'say hi'}], on_token=tokens.append,
                                   on_progress=progress.append, think=False)
        self.assertTrue(payloads[0]['stream'])
        self.assertFalse(payloads[0]['think'])
        self.assertEqual(payloads[0]['options']['repeat_penalty'], 1.12)
        self.assertEqual(payloads[0]['options']['repeat_last_n'], 128)
        self.assertEqual(payloads[0]['options']['num_ctx'], 8192)
        self.assertEqual(tokens, ['Hello', ' world'])
        self.assertEqual(progress, ['hidden reasoning'])
        self.assertEqual(result['content'], 'Hello world')
        self.assertEqual(result['raw_message']['thinking'], 'hidden reasoning')

    def test_streaming_buffers_json_tool_call_instead_of_showing_it_as_chat(self):
        chunk = {'message': {'role': 'assistant', 'content': '{"name":"workspace_info","arguments":{}}'}, 'done': True}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self): return iter([json.dumps(chunk).encode('utf-8') + b'\n'])
        provider = OllamaProvider()
        visible = []
        with mock.patch('core.llm.urllib.request.urlopen', return_value=Response()):
            result = provider.chat([{'role': 'user', 'content': 'call tool'}], on_token=visible.append)
        self.assertEqual(visible, [])
        self.assertEqual(result['tool_calls'][0]['name'], 'workspace_info')

    def test_streaming_tool_arguments_are_reassembled_as_object_for_ollama_history(self):
        chunks = [
            {'message': {'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {'name': 'web_search', 'arguments': '{"query":"黎曼'}}]}, 'done': False},
            {'message': {'tool_calls': [{'index': 0, 'function': {'arguments': '猜想 最新"}'}}]}, 'done': True},
        ]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self): return iter([json.dumps(chunk, ensure_ascii=False).encode('utf-8') + b'\n' for chunk in chunks])
        with mock.patch('core.llm.urllib.request.urlopen', return_value=Response()):
            result = OllamaProvider().chat([{'role': 'user', 'content': 'search'}], on_token=lambda _text: None)
        self.assertEqual(result['tool_calls'][0]['arguments'], {'query': '黎曼猜想 最新'})
        self.assertEqual(result['raw_message']['tool_calls'][0]['function']['arguments'], {'query': '黎曼猜想 最新'})
        self.assertEqual(result['tool_call_errors'], [])

    def test_malformed_stream_tool_arguments_are_reported_and_never_executed(self):
        chunk = {'message': {'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {'name': 'workspace_info', 'arguments': '{"bad":'}}]}, 'done': True}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self): return iter([json.dumps(chunk).encode('utf-8') + b'\n'])
        with mock.patch('core.llm.urllib.request.urlopen', return_value=Response()):
            result = OllamaProvider().chat([{'role': 'user', 'content': 'do it'}], on_token=lambda _text: None)
        self.assertEqual(result['tool_calls'], [])
        self.assertTrue(result['tool_call_errors'])

    def test_live_search_and_time_intents_are_detected(self):
        from core.web_search import (_prepare_query, is_current_time_request, is_live_search_request,
                                     search_topic_is_missing)
        self.assertTrue(is_live_search_request('联网搜索黎曼猜想被谁解决了现在'))
        self.assertTrue(is_live_search_request('今天武汉天气'))
        self.assertTrue(is_live_search_request('你联网查一下吧'))
        self.assertTrue(search_topic_is_missing('你联网查一下吧'))
        self.assertFalse(search_topic_is_missing('联网搜索黎曼猜想现在'))
        self.assertTrue(is_current_time_request('今天日期是多少'))
        self.assertTrue(is_current_time_request('现在几点了'))
        self.assertTrue(is_current_time_request('今天几月几号'))
        self.assertTrue(is_current_time_request('今天星期几'))
        cleaned, terms = _prepare_query('联网搜索黎曼猜想被谁解决了现在')
        self.assertIn('黎曼猜想', cleaned)
        self.assertEqual(terms[0], '黎曼猜想')
        natural, _terms = _prepare_query('我联网了。联网搜索黎曼猜想被谁解决了现在。模型回答一直不准确')
        self.assertLess(len(natural), 40)
        weather, weather_terms = _prepare_query('今天武汉天气')
        self.assertIn('武汉天气', weather)
        self.assertEqual(weather_terms[0], '武汉天气')

    def test_docx_to_markdown_tool_extracts_text_into_workspace(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'brief.docx'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('word/document.xml', (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>你好，文档。</w:t></w:r></w:p></w:body></w:document>'))
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            result = registry.execute('document_to_markdown', {'path': 'brief.docx'})
            self.assertTrue(result.ok, result.output)
            output = Path(directory) / 'brief.extracted.md'
            self.assertIn('你好，文档。', output.read_text(encoding='utf-8'))

    def test_current_time_request_uses_local_tool_without_model_generation(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('time should be answered directly')
        registry = ToolRegistry()
        registry.register('current_datetime', 'time', {'type': 'object', 'properties': {}},
                          lambda _args: type('Result', (), {'output': '2026-11-23 20:00:00 Asia/Shanghai'})())
        answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run('今天几号？')
        self.assertEqual(answer, '2026-11-23 20:00:00 Asia/Shanghai')

    def test_python_file_creation_writes_and_verifies_before_claiming_success(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('deterministic file creation should not hallucinate')
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            agent = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False))
            answer = agent.run('创建一个python文件，随便什么都行，最简单的两数之和，哈希表的那个')
            path = Path(directory) / 'two_sum.py'
            self.assertTrue(path.is_file())
            self.assertIn('已创建并验证文件', answer)
            source = path.read_text(encoding='utf-8')
            self.assertIn('def twoSum', source)
            self.assertIn('[0, 1]', source)
            compile(source, str(path), 'exec')

    def test_simple_requested_text_content_is_written_without_model_rephrasing(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('explicit text content should be written directly')
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            agent = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False))
            answer = agent.run('在根目录创建 hello_world.txt，内容为“你好世界”')
            target = Path(directory) / 'hello_world.txt'
            self.assertIn('已创建并验证文件', answer)
            self.assertEqual(target.read_text(encoding='utf-8'), '你好世界\n')

    def test_python_file_creation_reports_failure_without_false_success(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('failed write should not call model')
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            registry._tools['write_file']['handler'] = lambda _args: type(
                'Result', (), {'ok': False, 'output': 'simulated disk error'})()
            answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run(
                '创建一个 Python 文件，写两数之和算法')
            self.assertIn('文件创建失败', answer)
            self.assertNotIn('已创建', answer)
            self.assertFalse((Path(directory) / 'two_sum.py').exists())

    def test_non_python_text_file_creation_uses_write_tool_and_verifies_content(self):
        class SourceProvider:
            def chat(self, **_kwargs): return {'content': '# Notes\n\nCreated as Markdown.\n'}
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            agent = Agent(SourceProvider(), registry, config=AgentConfig(verbose=False))
            answer = agent.run('创建一个 Markdown 文件 test.md，写一个简短说明')
            target = Path(directory) / 'test.md'
            self.assertIn('已创建并验证文件', answer)
            self.assertEqual(target.read_text(encoding='utf-8'), '# Notes\n\nCreated as Markdown.\n')

    def test_application_request_is_routed_to_generic_verified_launcher(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('app launch should route to the OS tool')
        class FakeRegistry:
            called = None
            def list_names(self): return ['open_application']
            def execute(self, name, args):
                self.called = (name, args)
                return type('Result', (), {'ok': True, 'output': '已确认应用窗口可见：微信'})()
        registry = FakeRegistry()
        request = '检查一下我电脑上的微信，有的话帮我打开'
        self.assertTrue(Agent._application_open_request(request))
        self.assertEqual(Agent._application_query(request), '微信')
        answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run(request)
        self.assertIn('已确认应用窗口可见', answer)
        self.assertEqual(registry.called, ('open_application', {'name': '微信'}))

    def test_generic_start_menu_matching_uses_shortcut_display_names(self):
        from core.tools import _rank_application_entries
        with tempfile.TemporaryDirectory() as directory:
            shortcut = Path(directory) / '微信.lnk'
            shortcut.touch()
            matches = _rank_application_entries('检查一下我电脑上的微信，有的话帮我打开', [shortcut])
            self.assertEqual(matches, [shortcut])
            code_shortcut = Path(directory) / 'Visual Studio Code.lnk'
            code_shortcut.touch()
            matches = _rank_application_entries('vscode', [code_shortcut])
            self.assertEqual(matches, [code_shortcut])

    def test_path_executable_discovery_matches_abbreviated_names(self):
        from core.tools import _find_path_executables
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'Code.exe'
            executable.touch()
            with mock.patch.dict(os.environ, {'PATH': directory}):
                matches = _find_path_executables('vscode')
            if sys.platform == 'win32':
                self.assertEqual(matches, [executable])

    def test_generic_search_asks_for_topic_and_empty_answer_does_not_recommend_model_switch(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('generic search should ask for a topic')
        registry = ToolRegistry()
        registry.register('web_search', 'search', {'type': 'object', 'properties': {}},
                          lambda _args: type('Result', (), {'output': 'not called'})())
        answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run('你联网查一下吧')
        self.assertIn('搜索什么主题', answer)
        empty = Agent(type('EmptyProvider', (), {'chat': lambda self, **_kwargs: {'content': '', 'tool_calls': []}})(),
                      ToolRegistry(), config=AgentConfig(verbose=False)).run('你好')
        self.assertIn('模型未自动切换', empty)

    def test_explicit_mail_read_uses_real_registered_mcp_tool_without_model_guessing(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('mail intent should route directly')
        class FakeRegistry:
            def list_names(self): return ['mcp_email_qq_list_recent_emails']
            def execute(self, name, args):
                self.called = (name, args)
                return type('Result', (), {'ok': True, 'output': json.dumps([
                    {'from': 'sender@example.com', 'subject': '测试主题', 'date': 'Mon, 23 Nov 2026'}])})()
        registry = FakeRegistry()
        answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(tools_enabled=False, verbose=False)).run(
            '用 QQ 邮箱读取最近 1 封未读邮件')
        self.assertIn('测试主题', answer)
        self.assertEqual(registry.called[0], 'mcp_email_qq_list_recent_emails')
        self.assertEqual(registry.called[1], {'limit': 1, 'unread_only': True, 'include_body': False})

    def test_mail_content_followup_reuses_last_mailbox_and_reads_body_deterministically(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('mail followup should route directly')
        class FakeRegistry:
            calls = []
            def list_names(self): return ['mcp_email_qq_list_recent_emails', 'mcp_email_163_list_recent_emails']
            def execute(self, name, args):
                self.calls.append((name, args))
                row = {'from': 'sender@example.com', 'subject': '安全提醒', 'date': 'today'}
                if args['include_body']:
                    row['body'] = '新设备登录提醒正文'
                return type('Result', (), {'ok': True, 'output': json.dumps([row])})()
        registry = FakeRegistry()
        agent = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False))
        agent.run('读取一下我的163邮箱，最新的一条消息是什么时候的')
        answer = agent.run('那它的内容是什么？')
        self.assertIn('新设备登录提醒正文', answer)
        self.assertEqual(registry.calls[0][0], 'mcp_email_163_list_recent_emails')
        self.assertFalse(registry.calls[0][1]['include_body'])
        self.assertEqual(registry.calls[1][0], 'mcp_email_163_list_recent_emails')
        self.assertTrue(registry.calls[1][1]['include_body'])

    def test_natural_latest_email_content_request_reads_all_states_without_model_guessing(self):
        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('explicit email content should route directly')
        class FakeRegistry:
            called = None
            def list_names(self): return ['mcp_email_qq_list_recent_emails']
            def execute(self, name, args):
                self.called = (name, args)
                return type('Result', (), {'ok': True, 'output': json.dumps([
                    {'from': 'sender@example.com', 'subject': '测试', 'date': 'today', 'body': '正文内容'}])})()
        registry = FakeRegistry()
        answer = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False)).run(
            '请你看一下我QQ邮箱最新的一个内容是啥，不管未读还是已读')
        self.assertIn('正文内容', answer)
        self.assertEqual(registry.called[0], 'mcp_email_qq_list_recent_emails')
        self.assertEqual(registry.called[1], {'limit': 1, 'unread_only': False, 'include_body': True})

    def test_email_mcp_exposes_only_mail_tools_and_requires_credentials(self):
        initialized = email_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(initialized['result']['serverInfo']['name'], 'local-email-mcp')
        listed = email_mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        self.assertEqual({tool['name'] for tool in listed['result']['tools']}, {'list_recent_emails', 'send_email'})
        keys = ['EMAIL_IMAP_HOST', 'EMAIL_SMTP_HOST', 'EMAIL_USER']
        old = {key: os.environ.pop(key, None) for key in keys}
        try:
            reply = email_mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                      'params': {'name': 'send_email', 'arguments': {'to': 'x@example.com', 'subject': 'test', 'body': 'test'}}})
            self.assertTrue(reply['result']['isError'])
            self.assertIn('EMAIL_SMTP_HOST', reply['result']['content'][0]['text'])
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value

    def test_email_mcp_encodes_imap_mailbox_names_without_private_imaplib_api(self):
        self.assertEqual(email_mcp.encode_imap_utf7('INBOX'), 'INBOX')
        self.assertEqual(email_mcp.encode_imap_utf7('A&B'), 'A&-B')
        encoded = email_mcp.encode_imap_utf7('收件箱')
        self.assertTrue(encoded.startswith('&') and encoded.endswith('-'))
        self.assertTrue(encoded.isascii())
        self.assertNotIn('收', encoded)

    def test_email_html_body_is_rendered_as_readable_text(self):
        from email import message_from_bytes
        message = message_from_bytes(
            b'Content-Type: text/html; charset=utf-8\r\n\r\n'
            b'<html><head><title>hidden</title></head><body><p>Hello <b>world</b></p>'
            b'<script>secret()</script></body></html>')
        body = email_mcp._text_body(message)
        self.assertIn('Hello world', body)
        self.assertNotIn('<p>', body)
        self.assertNotIn('secret()', body)

    def test_imap_internal_date_prefers_arrival_timestamp(self):
        newer = email_mcp._internal_date_key(b'1 (INTERNALDATE "23-Nov-2026 21:00:00 +0800")')
        older = email_mcp._internal_date_key(b'2 (INTERNALDATE "31-Dec-2024 17:58:00 +0800")')
        self.assertGreater(newer, older)

    def test_two_day_mail_request_sends_real_received_time_window_and_clarifies_it(self):
        from datetime import datetime
        start, end, label = Agent._mail_date_window(
            '这两天的邮件', datetime.fromisoformat('2026-11-23T12:00:00+08:00'))
        self.assertEqual(start, '2026-11-21T12:00:00+08:00')
        self.assertEqual(end, '2026-11-23T12:00:00+08:00')
        self.assertEqual(label, '最近 48 小时')

        class NeverCalledProvider:
            def chat(self, **_kwargs): raise AssertionError('email date tasks should be deterministic')
        class FakeRegistry:
            called = None
            def list_names(self): return ['mcp_email_qq_list_recent_emails']
            def execute(self, name, args):
                self.called = (name, args)
                return type('Result', (), {'ok': True, 'output': '[]'})()
        registry = FakeRegistry()
        agent = Agent(NeverCalledProvider(), registry, config=AgentConfig(verbose=False))
        answer = agent.run('请你看一下我QQ邮箱这两天的邮件任意一封，不管未读还是已读')
        self.assertIn('最近 48 小时内没有', answer)
        self.assertFalse(registry.called[1]['unread_only'])
        self.assertIn('received_after', registry.called[1])
        clarified = agent.run('这两天是啥时候，你查查')
        self.assertIn('2026-', clarified)

    def test_email_mcp_uses_encoded_mailbox_name_for_imap_select(self):
        class FakeIMAP:
            selected = None
            events = []
            def __init__(self, *_args, **_kwargs): pass
            def login(self, *_args):
                FakeIMAP.events.append('login')
                return 'OK', []
            def _simple_command(self, command, identity):
                FakeIMAP.events.append((command, identity))
                return 'OK', [b'* ID NIL']
            def select(self, folder, readonly=True):
                self.selected = (folder, readonly)
                FakeIMAP.selected = self.selected
                return 'OK', [b'0']
            def search(self, *_args): return 'OK', [b'']
            def logout(self): return 'BYE', []

        with mock.patch.object(email_mcp, '_config', return_value={
                'host': 'imap.163.com', 'port': 993, 'user': 'user@example.com', 'password': 'secret'}), \
             mock.patch.object(email_mcp.imaplib, 'IMAP4_SSL', FakeIMAP):
            result = email_mcp.list_recent_emails({'folder': '收件箱'})
        self.assertEqual(result, '[]')
        self.assertEqual(FakeIMAP.selected, (email_mcp.encode_imap_utf7('收件箱'), True))
        self.assertEqual(FakeIMAP.events[0], 'login')
        self.assertEqual(FakeIMAP.events[1][0], 'ID')
        self.assertIn('AUTH', email_mcp.imaplib.Commands['ID'])
        self.assertIn('SELECTED', email_mcp.imaplib.Commands['ID'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
