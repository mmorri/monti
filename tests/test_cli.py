import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from model_router.agent import TerminalAgent
from model_router.classifier import Classifier
from model_router.cli import main
from model_router.config import RouterConfig
from model_router.errors import ReloginRequired
from model_router.http import HttpStatusError
from model_router.providers.anthropic import to_messages_payload, _non_stream_chunks as anthropic_chunks
from model_router.providers.base import ChatChunk, ChatRequest
from model_router.providers.openai_compat import _stream_chunks, _non_stream_chunks
from model_router.router import Router
from model_router.tools import TOOLS, WorkspaceTools


class Transport:
    def __init__(self, events):
        self.events = events

    def run(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def agent_with(tmp_path, responses, *, input_fn=lambda _: 'n', yes=False):
    requests, logs = [], []

    class Gateway:
        def chat(self, provider, request):
            # Snapshot the messages; the real agent appends in-place.
            requests.append(json.loads(json.dumps(request.messages)))
            return Transport(responses.pop(0)), lambda: Transport(responses.pop(0))

    config = RouterConfig()
    app = SimpleNamespace(config=config, gateway=Gateway(),
                          router=Router(config, Classifier()),
                          requests=SimpleNamespace(append=logs.append))
    agent = TerminalAgent(app, tmp_path, output=io.StringIO(), diagnostic=io.StringIO(),
                          input_fn=input_fn, yes=yes)
    return agent, requests, logs


def tool_events(name='read_file', arguments='{"path":"hello.txt"}'):
    # Real providers send the arguments in separate deltas.
    return [ChatChunk(kind='tool_call', tool_call={'index': 0, 'id': 'call-1',
                'function': {'name': name, 'arguments': arguments[:8]}}),
            ChatChunk(kind='tool_call', tool_call={'index': 0,
                'function': {'arguments': arguments[8:]}}),
            ChatChunk(kind='finish', finish_reason='tool_calls')]


def test_agent_tool_loop_and_conversation(tmp_path):
    (tmp_path / 'hello.txt').write_text('world')
    agent, requests, logs = agent_with(tmp_path, [tool_events(),
        [ChatChunk(kind='delta', text='Found world.')],
        [ChatChunk(kind='delta', text='Follow-up.')]])
    agent.turn('read hello.txt')
    assert requests[1][-1] == {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'world'}
    assert requests[1][-2]['tool_calls'][0]['function']['arguments'] == '{"path":"hello.txt"}'
    assert agent.output.getvalue() == 'Found world.\n'
    agent.turn('explain it')
    assert requests[2][-2]['content'] == 'Found world.'
    assert logs[0]['tool_calls'] == 1


def test_agent_approval_denied_then_continues(tmp_path):
    agent, requests, _ = agent_with(tmp_path, [tool_events('write_file',
        '{"path":"hello.txt","content":"changed"}'), [ChatChunk(kind='delta', text='Denied.')]])
    agent.turn('edit a file')
    assert not (tmp_path / 'hello.txt').exists()
    assert requests[1][-1]['content'].startswith('Denied by user')


def test_workspace_paths_and_approved_write(tmp_path):
    tools = WorkspaceTools(tmp_path, lambda _: True)
    assert tools.execute('write_file', '{"path":"a/b.txt","content":"ok"}') == 'Wrote a/b.txt'
    assert tools.execute('read_file', '{"path":"a/b.txt"}') == 'ok'
    assert tools.execute('read_file', '{"path":"../outside"}').startswith('Error:')
    (tmp_path / 'link').symlink_to(tmp_path.parent)
    assert tools.execute('write_file', '{"path":"link/outside","content":"x"}').startswith('Error:')
    assert tools.execute('write_file', '{"path":".git/config","content":"x"}').startswith('Error:')
    assert tools.execute('read_file', '[]').startswith('Error:')


def test_shell_runs_in_workspace_and_reports_exit(tmp_path):
    tools = WorkspaceTools(tmp_path, lambda _: True)
    result = tools.execute('run_shell', '{"command":"pwd; exit 7"}')
    assert 'Exit code: 7' in result and str(tmp_path) in result
    assert WorkspaceTools(tmp_path, lambda _: False).execute('run_shell',
        '{"command":"touch denied"}').startswith('Denied by user')
    assert not (tmp_path / 'denied').exists()


def test_edit_file_unique_ambiguous_and_missing(tmp_path):
    tools = WorkspaceTools(tmp_path, lambda _: True)
    (tmp_path / 'a.txt').write_text('alpha beta gamma\nbeta again\n')
    assert tools.execute('edit_file', '{"path":"a.txt","old":"beta gamma","new":"BETA"}') \
        == 'Edited a.txt (1 replacement(s))'
    assert (tmp_path / 'a.txt').read_text() == 'alpha BETA\nbeta again\n'
    # 'beta' now appears once -> unique replace succeeds without replace_all
    assert tools.execute('edit_file', '{"path":"a.txt","old":"beta again","new":"x"}') \
        == 'Edited a.txt (1 replacement(s))'
    (tmp_path / 'c.txt').write_text('dup dup\n')
    assert tools.execute('edit_file', '{"path":"c.txt","old":"dup","new":"x"}') \
        .startswith('Error:')
    assert tools.execute('edit_file', '{"path":"c.txt","old":"nope","new":"x"}') \
        .startswith('Error:')
    assert tools.execute('edit_file',
        '{"path":"c.txt","old":"dup","new":"x","replace_all":true}') == \
        'Edited c.txt (2 replacement(s))'
    denied = WorkspaceTools(tmp_path, lambda _: False)
    (tmp_path / 'b.txt').write_text('keep me')
    assert denied.execute('edit_file',
        '{"path":"b.txt","old":"keep","new":"change"}').startswith('Denied by user')
    assert (tmp_path / 'b.txt').read_text() == 'keep me'


def test_find_and_grep_skip_hidden_and_respect_scope(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'app.py').write_text('def main():\n    return TARGET\n')
    (tmp_path / 'notes.md').write_text('mention TARGET too')
    (tmp_path / '.secret').write_text('TARGET hidden')
    tools = WorkspaceTools(tmp_path, lambda _: True)
    found = tools.execute('find_files', '{"pattern":"*.py"}')
    assert found == 'src/app.py' and 'notes.md' not in found
    grep = tools.execute('grep_files', '{"pattern":"TARGET"}')
    assert 'src/app.py:2' in grep and 'notes.md:1' in grep and '.secret' not in grep
    scoped = tools.execute('grep_files', '{"pattern":"TARGET","path":"src"}')
    assert 'src/app.py:2' in scoped and 'notes.md' not in scoped
    py_only = tools.execute('grep_files', '{"pattern":"TARGET","glob":"*.md"}')
    assert 'notes.md:1' in py_only and 'app.py' not in py_only
    assert tools.execute('grep_files', '{"pattern":"["}').startswith('Error:')
    assert tools.execute('grep_files', '{"pattern":"nothing-matches"}') == 'no matches'


def test_agent_escalates_fast_tier_failure_to_strong(tmp_path):
    class Gateway:
        def __init__(self):
            self.calls = []

        def chat(self, provider, request):
            from model_router.providers.base import ChatChunk
            self.calls.append((provider, request.model))
            if provider == 'kimi':
                return Transport([HttpStatusError('POST', 'https://x.invalid', 503)]), None
            return Transport([ChatChunk(kind='delta', text='recovered.')]), None

    config = RouterConfig()  # fast=kimi, strong=anthropic
    app = SimpleNamespace(config=config, gateway=Gateway(),
                          router=Router(config, Classifier()),
                          requests=SimpleNamespace(append=lambda _r: None))
    agent = TerminalAgent(app, tmp_path, output=io.StringIO(), diagnostic=io.StringIO(),
                          input_fn=lambda _: 'n', yes=True)
    agent.turn('hello')
    assert agent.output.getvalue() == 'recovered.\n'
    providers = [call[0] for call in app.gateway.calls]
    assert providers == ['kimi', 'anthropic']
    assert app.gateway.calls[1][1] == config.strong.model
    assert 'escalating to strong' in agent.diagnostic.getvalue()


def test_agent_401_retry_and_second_401(tmp_path):
    error = HttpStatusError('POST', 'https://provider.invalid', 401)
    agent, _, _ = agent_with(tmp_path, [[error], [ChatChunk(kind='delta', text='ok')]])
    agent.turn('hello')
    assert agent.output.getvalue() == 'ok\n'
    agent, _, _ = agent_with(tmp_path, [[error], [error]])
    with pytest.raises(ReloginRequired, match='RELOGIN_REQUIRED:kimi'):
        agent.turn('hello')


def test_agent_does_not_retry_after_output(tmp_path):
    error = HttpStatusError('POST', 'https://provider.invalid', 401)
    agent, requests, _ = agent_with(tmp_path, [[ChatChunk(kind='delta', text='partial'), error]])
    with pytest.raises(HttpStatusError):
        agent.turn('hello')
    assert len(requests) == 1


def test_openai_tool_events_both_response_modes():
    call = {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}
    stream = list(_stream_chunks({'choices': [{'delta': {'tool_calls': [{'index': 0, **call}]}}]}))
    nonstream = list(_non_stream_chunks({'choices': [{'message': {'tool_calls': [call]},
                                                     'finish_reason': 'tool_calls'}]}))
    assert stream[0].tool_call == nonstream[0].tool_call
    assert nonstream[-1].finish_reason == 'tool_calls'


def test_anthropic_tool_exchange_translation():
    request = ChatRequest(model='claude', messages=[
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'c1', 'function': {'name': 'read_file', 'arguments': '{"path":"x"}'}},
            {'id': 'c2', 'function': {'name': 'read_file', 'arguments': '{"path":"y"}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'one'},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': 'two'},
    ], tools=TOOLS)
    payload = to_messages_payload(request)
    assert payload['messages'][0]['content'][0]['input'] == {'path': 'x'}
    assert len(payload['messages']) == 2
    assert [b['tool_use_id'] for b in payload['messages'][1]['content']] == ['c1', 'c2']
    assert payload['tools'][0]['name'] == 'list_files'
    chunks = list(anthropic_chunks({'content': [{'type': 'tool_use', 'id': 'c1',
        'name': 'read_file', 'input': {'path': 'x'}}], 'stop_reason': 'tool_use'}))
    assert chunks[0].tool_call['function']['arguments'] == '{"path": "x"}'
    assert chunks[-1].finish_reason == 'tool_calls'


def test_latest_task_and_pinned_mode_skip_classifier():
    seen = []
    router = Router(RouterConfig(), Classifier(model_fn=lambda text: seen.append(text) or 'easy'))
    messages = [{'role': 'system', 'content': 'architecture design ' * 300},
                {'role': 'user', 'content': 'fix typo'}]
    assert router.route(messages, 'router-auto')[0].tier == 'fast'
    assert seen == ['user: fix typo']
    router.route(messages, 'router-strong')
    assert len(seen) == 1
    assert Classifier(model_fn=lambda _: 'invalid').classify('design a system').verdict == 'hard'


def test_weak_first_starts_fast_and_does_not_recount_history():
    router = Router(RouterConfig(), Classifier())
    messages = [{'role': 'user', 'content': 'design architecture'}]
    assert router.route(messages, '', 'weak-first-escalate', 's')[0].tier == 'fast'
    messages.append({'role': 'tool', 'content': 'Error: failed'})
    for _ in range(4):
        assert router.route(messages, '', 'weak-first-escalate', 's')[0].tier == 'fast'


def test_interactive_commands_and_error_recovery(tmp_path):
    prompts = iter(['/mode strong', 'hello', '/clear', '/exit'])
    agent, _, _ = agent_with(tmp_path, [[HttpStatusError('POST', 'https://x.invalid', 500)]],
                              input_fn=lambda _: next(prompts))
    assert agent.run() == 0
    assert agent.mode == 'strong-only' and len(agent.messages) == 1
    assert 'Error:' in agent.diagnostic.getvalue()


def test_cli_dispatch_default_and_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr('model_router.cli.cmd_chat', lambda args: seen.append(args) or 0)
    assert main([]) == 0
    assert seen[-1].prompt == []
    assert main(['fix the tests', '--mode', 'fast', '--yes']) == 0
    assert seen[-1].prompt == ['fix the tests'] and seen[-1].yes


def test_escalation_counts_errors_across_tool_rounds():
    router = Router(RouterConfig(), Classifier())
    messages = [{"role": "user", "content": "do it"}]
    for i in range(3):
        messages.extend([{"role": "assistant", "content": None},
                         {"role": "tool", "content": f"Error: failed {i}"}])
        route, _ = router.route(messages, "", "weak-first-escalate", "s")
        assert route.tier == ("strong" if i == 2 else "fast")


def test_cli_one_shot_with_piped_context(tmp_path, monkeypatch, capsys):
    from model_router.proxy import ProxyApp
    from model_router.store import TokenStore, Credentials

    store = TokenStore(tmp_path / 'tokens')
    store.save('kimi', Credentials(access='test'))
    config = RouterConfig()
    app = ProxyApp(config, store=store, log_dir=tmp_path / 'logs')
    requests = []

    class Provider:
        def needs_refresh(self, creds):
            return False

        def open_chat(self, creds, request):
            requests.append(request)
            return Transport([ChatChunk(kind='delta', text='Explanation.')])

    app.gateway._providers['kimi'] = Provider()
    monkeypatch.setattr('model_router.proxy.ProxyApp', lambda _: app)
    monkeypatch.setattr('sys.stdin', io.StringIO('Error: example failure'))
    assert main(['--workspace', str(tmp_path), '--mode', 'fast', 'explain this']) == 0
    captured = capsys.readouterr()
    assert captured.out == 'Explanation.\n'
    assert 'kimi/kimi-latest' in captured.err
    assert requests[-1].messages[1]['content'] == 'explain this\n\nError: example failure'


def test_anthropic_streamed_tool_arguments(monkeypatch):
    from model_router.providers.anthropic import AnthropicTransport

    events = [
        ('message_start', {'message': {'usage': {'input_tokens': 10}}}),
        ('content_block_start', {'index': 0, 'content_block':
            {'type': 'tool_use', 'id': 'c1', 'name': 'read_file', 'input': {}}}),
        ('content_block_delta', {'index': 0, 'delta':
            {'type': 'input_json_delta', 'partial_json': '{"path":"x"}'}}),
        ('message_delta', {'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}}),
        ('message_stop', {}),
    ]
    raw = ''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in events).encode()
    monkeypatch.setattr('urllib.request.urlopen', lambda *_a, **_k: io.BytesIO(raw))
    chunks = list(AnthropicTransport('test', ChatRequest(messages=[], model='claude')).run())
    assert chunks[0].tool_call['id'] == 'c1'
    assert chunks[1].tool_call['function']['arguments'] == '{"path":"x"}'
    assert chunks[-2].usage == {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    assert chunks[-1].finish_reason == 'tool_calls'
