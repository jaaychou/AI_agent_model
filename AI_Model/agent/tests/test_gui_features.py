import base64
import tempfile
import unittest
from pathlib import Path
import sys
import json
from unittest import mock
from urllib.error import URLError

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
from agent_gui import (AgentApp, prepare_attachments, ensure_ollama, bind_chat_shortcuts,
                       add_hover_effect, responsive_input_margin, choose_tool_model)
from core.agent import Agent, AgentConfig
from core.llm import OllamaProvider
from core.tools import ToolRegistry, register_builtin_tools, execute_shell_command
from core.rag import LocalKnowledgeBase
from core.sessions import SessionStore
from core.mcp import MCPClient, load_mcp_servers, register_mcp_tools
from core.git_manager import (configure_identity, git_config_value, initialize_repository,
                              is_repository, set_remote, stage_and_commit)
from core.progress import classify_task, estimated_progress, expected_seconds, format_duration

class GUIFeatureTests(unittest.TestCase):
    def test_action_intent_detects_requests_that_need_tools(self):
        self.assertTrue(AgentApp._looks_like_action_request('运行命令打开 VSCode'))
        self.assertTrue(AgentApp._looks_like_action_request('查询电脑连接了哪些外部设备'))
        self.assertTrue(AgentApp._looks_like_action_request('你自己排查并解决这个问题'))
        self.assertTrue(AgentApp._looks_like_action_request('帮我配置 Git 远端仓库'))
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

    def test_open_vscode_uses_real_install_path_and_reports_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            register_builtin_tools(registry)
            with mock.patch('core.tools.subprocess.Popen') as popen, \
                 mock.patch('core.tools._find_visible_vscode_window', return_value='test - Visual Studio Code'):
                popen.return_value = mock.Mock(pid=4321)
                result = registry.execute('open_vscode', {})
            if sys.platform == 'win32':
                self.assertTrue(result.ok, result.output)
                self.assertIn('PID：4321', result.output)
                self.assertIn('窗口可见', result.output)
                self.assertTrue(any('Code.exe' in str(item) for item in popen.call_args.args[0]))

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
        self.assertIn('连续 4 次重复调用', result)

    def test_agent_omits_irrelevant_mcp_tool_schemas_from_small_context_prompts(self):
        def tool(name, description=''):
            return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': {}}}}
        schemas = [tool('read_file'),
                   tool('mcp_playwright_browser_navigate', '[MCP:playwright] navigate'),
                   tool('mcp_playwright_browser_snapshot', '[MCP:playwright] snapshot'),
                   tool('mcp_playwright_browser_click', '[MCP:playwright] click'),
                   tool('mcp_playwright_browser_evaluate', '[MCP:playwright] evaluate')]
        local = Agent._select_relevant_tools(schemas, '生成一张小猫照片')
        self.assertEqual([s['function']['name'] for s in local], ['read_file'])
        browser = Agent._select_relevant_tools(schemas, '用浏览器打开网页并点击登录')
        names = [s['function']['name'] for s in browser]
        self.assertIn('mcp_playwright_browser_navigate', names)
        self.assertIn('mcp_playwright_browser_snapshot', names)
        self.assertIn('mcp_playwright_browser_click', names)
        self.assertNotIn('mcp_playwright_browser_evaluate', names)

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

if __name__ == '__main__':
    unittest.main(verbosity=2)
