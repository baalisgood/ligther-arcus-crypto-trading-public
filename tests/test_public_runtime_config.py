"""Synthetic root config and offline keepalive regressions; no real secrets or network."""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from core.utils import config_loader
from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_base import LighterBase
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket
from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap

VALUES = {
    'LIGHTER_API_KEY_PRIVATE_KEY': 'synthetic-lighter-key',
    'LIGHTER_ACCOUNT_INDEX': '0',
    'LIGHTER_API_KEY_INDEX': '0',
    'ARCUS_API_KEY': 'synthetic-arcus-key',
    'ARCUS_API_SIGNING_KEY': 'synthetic-arcus-seed',
    'ARCUS_WALLET_ADDRESS': '0x' + 'ab' * 20,
    'ARCUS_ACCOUNT_INDEX': '0',
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    runtime = tmp_path / 'config/exchanges'
    runtime.mkdir(parents=True)
    public_root = Path(__file__).resolve().parents[1]
    for name in ('lighter', 'arcus'):
        shutil.copy(public_root / f'config/exchanges/{name}_runtime.yaml', runtime)
    (tmp_path / '.env').write_text('\n'.join(f'{k}={v}' for k, v in VALUES.items()))
    monkeypatch.setattr(config_loader, 'PROJECT_ROOT', tmp_path)
    for key in VALUES:
        monkeypatch.setenv(key, 'conflicting-shell-value')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / '.env').write_text('LIGHTER_API_KEY_PRIVATE_KEY=wrong-cwd')
    monkeypatch.chdir(elsewhere)
    return tmp_path


def test_fresh_public_bootstrap_both_adapters_and_zero_credentials(project, monkeypatch):
    arcus_runtime = project / 'config/exchanges/arcus_runtime.yaml'
    arcus_runtime.write_text(arcus_runtime.read_text().replace(
        '  strict_order_state: true',
        '  strict_order_state: true\n  wallet_address: ignored-runtime-wallet'))
    signers = []
    def signer(self, **kwargs):
        signers.append(kwargs)
        return NS(create_auth_token_with_expiry=Mock(return_value=("synthetic-token", None)))
    monkeypatch.setattr(LighterBase, '_create_signer_client', signer)
    orc = NS(monitor_config=NS(exchanges=['lighter', 'arcus']), exchange_adapters={})
    OrchestratorBootstrap(orc).init_exchange_adapters()
    assert set(orc.exchange_adapters) == {'lighter', 'arcus'}
    lighter, arcus = (orc.exchange_adapters[name] for name in ('lighter', 'arcus'))
    assert lighter.strict_order_state and arcus.strict_order_state
    for transport in (lighter._base, lighter._rest, lighter._websocket):
        assert transport.api_key_private_key == VALUES['LIGHTER_API_KEY_PRIVATE_KEY']
        assert transport.account_index == transport.api_key_index == 0
    assert len(signers) == 3
    assert all(row['account_index'] == row['api_key_index'] == 0 for row in signers)
    assert all(row['api_key_private_key'] == VALUES['LIGHTER_API_KEY_PRIVATE_KEY'] for row in signers)
    assert arcus.api_key == VALUES['ARCUS_API_KEY']
    assert arcus._seed == VALUES['ARCUS_API_SIGNING_KEY']
    assert arcus.address == VALUES['ARCUS_WALLET_ADDRESS'] and arcus.account_index == 0
    assert arcus.config.extra_params['wallet_address'] == VALUES['ARCUS_WALLET_ADDRESS']
    async def authenticate_and_subscribe():
        socket = NS(send=AsyncMock())
        lighter._websocket._direct_ws = socket
        token = await lighter._websocket._create_auth_token()
        assert token == 'synthetic-token'
        await lighter._websocket._send_account_subscriptions(token)
        sent = [json.loads(call.args[0]) for call in socket.send.await_args_list]
        assert {message['channel'] for message in sent} == {
            'account_all_orders/0', 'account_all/0', 'account_all_positions/0', 'user_stats/0'}
        assert len(sent) == 4 and all(message['auth'] == token for message in sent)
        assert lighter._websocket._authenticated_account_socket is socket
        lighter._websocket.signer_client.create_auth_token_with_expiry.assert_called_once()
    asyncio.run(authenticate_and_subscribe())
    assert lighter._rest.ws_url.startswith('wss://mainnet.')
    assert arcus._websocket.adapter is arcus
    assert not list((project / 'config/exchanges').glob('*_config.yaml'))


@pytest.mark.parametrize('field', list(VALUES))
@pytest.mark.parametrize('value', ['', None])
def test_missing_required_fields_do_not_fall_back_to_shell_or_yaml(project, field, value):
    fields = dict(VALUES)
    if value is None:
        fields.pop(field)
    else:
        fields[field] = value
    (project / '.env').write_text('\n'.join(f'{k}={v}' for k, v in fields.items()))
    legacy = project / 'config/exchanges/legacy.yaml'
    legacy.write_text('authentication:\n  api_key: yaml-secret\n')
    with pytest.raises(ValueError, match=field):
        config_loader.ExchangeConfigLoader().load_auth_config(field.split('_')[0], config_file=str(legacy))


@pytest.mark.parametrize('field,value', [('LIGHTER_ACCOUNT_INDEX', '-1'), ('LIGHTER_API_KEY_INDEX', 'bad'), ('ARCUS_ACCOUNT_INDEX', '10'), ('ARCUS_ACCOUNT_INDEX', '1.2')])
def test_invalid_indices_fail_without_echoing_value(project, field, value):
    with (project / '.env').open('a') as file:
        file.write(f'\n{field}={value}\n')
    with pytest.raises(ValueError, match=field) as exc:
        config_loader.ExchangeConfigLoader().load_auth_config(field.split('_')[0])
    assert value not in str(exc.value)


def test_dotenv_does_not_interpolate_shell(project):
    with (project / '.env').open('a') as file:
        file.write('\nLIGHTER_API_KEY_PRIVATE_KEY=${LIGHTER_API_KEY_PRIVATE_KEY}\n')
    assert config_loader.ExchangeConfigLoader().load_auth_config('lighter').api_key_private_key == '${LIGHTER_API_KEY_PRIVATE_KEY}'


def test_other_exchange_auth_keeps_existing_environment_behavior(monkeypatch):
    monkeypatch.setenv('BINANCE_API_KEY', 'public-test-key')
    monkeypatch.setenv('BINANCE_API_SECRET', 'public-test-secret')
    auth = config_loader.ExchangeConfigLoader().load_auth_config('binance')
    assert auth.api_key == 'public-test-key' and auth.api_secret == 'public-test-secret'


def test_outbound_pong_every_30_seconds_does_not_refresh_receive_state(monkeypatch):
    ws = NS(send=AsyncMock(), close=AsyncMock())
    client = NS(_running=True, _direct_ws=ws, _network_bytes_sent=0,
                _last_message_time=5, _last_business_message_time=3)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 3:
            client._direct_ws = NS()  # old loop must not send to replacement socket
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    asyncio.run(LighterWebSocket._heartbeat_loop(client, ws))
    assert sleeps == [30, 30, 30]
    assert [json.loads(call.args[0]) for call in ws.send.await_args_list] == [{'type': 'pong'}] * 2
    assert client._network_bytes_sent == 2 * len(json.dumps({'type': 'pong'}).encode())
    assert (client._last_message_time, client._last_business_message_time) == (5, 3)


def test_pong_send_failure_closes_only_bound_socket(monkeypatch):
    ws = NS(send=AsyncMock(side_effect=ConnectionError()), close=AsyncMock())
    client = NS(_running=True, _direct_ws=ws, _network_bytes_sent=0)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    asyncio.run(LighterWebSocket._heartbeat_loop(client, ws))
    ws.close.assert_awaited_once()
    assert client._network_bytes_sent == 0


@pytest.mark.parametrize('message_type', ['ping', 'pong'])
def test_inbound_heartbeat_does_not_refresh_business_state(message_type):
    client = object.__new__(LighterWebSocket)
    client._direct_ws = NS(send=AsyncMock())
    client._authenticated_account_socket = None
    client.auth_enabled = False
    client._last_message_time = 0
    client._last_business_message_time = 3
    client._ws_manual_health_ping_sent = True
    client._last_heartbeat_log_time = 0
    client._heartbeat_log_interval = 30
    asyncio.run(client._handle_direct_ws_message({'type': message_type}))
    assert client._last_message_time > 0
    assert client._last_business_message_time == 3
    assert client._ws_manual_health_ping_sent
    assert client._direct_ws.send.await_count == (message_type == 'ping')


def test_reconnect_starts_and_cleans_each_socket_heartbeat(monkeypatch):
    from core.adapters.exchanges.adapters import lighter_websocket as module
    sockets = [NS(close=AsyncMock(), send=AsyncMock()), NS(close=AsyncMock(), send=AsyncMock())]
    client = object.__new__(LighterWebSocket)
    client._running = True
    client.ws_url = 'ws://offline.invalid'
    client._heartbeat_task = None
    client._data_timeout_task = None
    client._data_timeout_seconds = 60
    client._subscribed_market_stats = set()
    client._subscribed_markets = set()
    client._local_orderbooks = {}
    client._create_auth_token = AsyncMock(return_value=None)
    client._send_market_stats_subscriptions = AsyncMock()
    client._send_orderbook_subscriptions = AsyncMock()
    real_sleep = asyncio.sleep
    async def no_delay(_):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', no_delay)
    contexts = []
    class Connection:
        def __init__(self, socket): self.socket = socket
        async def __aenter__(self): return self.socket
        async def __aexit__(self, *_): pass
    def connect(*_, **__):
        context = Connection(sockets[len(contexts)])
        contexts.append(context)
        return context
    monkeypatch.setattr(module.websockets, 'connect', connect)
    heartbeat_tasks = []
    async def listen(socket):
        task = client._heartbeat_task
        assert task is not None and not task.done()
        if heartbeat_tasks:
            assert heartbeat_tasks[-1].done()
        heartbeat_tasks.append(task)
        if len(heartbeat_tasks) == 2:
            client._running = False
    client._listen_websocket = listen
    client._data_timeout_monitor = AsyncMock()
    asyncio.run(client._run_direct_ws_subscription())
    assert len(heartbeat_tasks) == 2 and all(task.done() for task in heartbeat_tasks)
    assert client._heartbeat_task is None and client._direct_ws is None
    assert all(socket.close.await_count == 1 for socket in sockets)


def test_heartbeat_only_traffic_still_times_out(monkeypatch):
    from core.adapters.exchanges.adapters import lighter_websocket as module
    client = NS(_running=True, _direct_ws=NS(close=AsyncMock()),
                _last_message_time=99, _last_business_message_time=1,
                _data_timeout_seconds=60, _ws_manual_ping_threshold=30,
                _ws_manual_health_ping_sent=True)
    async def close(): client._running = False
    client._direct_ws.close.side_effect = close
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(module.time, 'time', lambda: 100)
    asyncio.run(LighterWebSocket._data_timeout_monitor(client))
    client._direct_ws.close.assert_awaited_once()


def test_missing_root_env_cannot_use_shell(project):
    (project / '.env').unlink()
    for exchange in ('lighter', 'arcus'):
        with pytest.raises(ValueError, match='project-root .env'):
            config_loader.ExchangeConfigLoader().load_auth_config(exchange)


def test_rest_initialize_keeps_injected_root_credentials_despite_shell(project, monkeypatch):
    from core.adapters.exchanges.adapters import lighter_rest as module
    auth = config_loader.ExchangeConfigLoader().load_auth_config('lighter')
    rest = object.__new__(module.LighterRest)
    rest._connected = False
    rest.base_url = 'https://mainnet.zklighter.elliot.ai'
    rest.api_key_private_key = auth.api_key_private_key
    rest.account_index, rest.api_key_index = auth.account_index, auth.api_key_index
    rest.close = AsyncMock()
    rest._load_markets = AsyncMock()
    rest._websocket = None
    captured = []
    def signer(**kwargs):
        captured.append(kwargs)
        return NS(check_client=lambda: None)
    rest._create_signer_client = signer
    monkeypatch.setattr(module, 'ApiClient', lambda **_: NS())
    for name in ('AccountApi', 'OrderApi', 'TransactionApi', 'CandlestickApi', 'FundingApi'):
        monkeypatch.setattr(module, name, lambda _: NS())
    asyncio.run(rest.initialize())
    assert rest._connected and len(captured) == 1
    assert captured[0]['api_key_private_key'] == VALUES['LIGHTER_API_KEY_PRIVATE_KEY']
    assert captured[0]['account_index'] == captured[0]['api_key_index'] == 0


def test_missing_ws_account_index_still_refuses_token_and_private_subscriptions():
    client = object.__new__(LighterWebSocket)
    client.auth_enabled = True
    client.account_index = None
    client.signer_client = NS(create_auth_token_with_expiry=Mock())
    client._direct_ws = NS(send=AsyncMock())
    async def scenario():
        assert await client._create_auth_token() is None
        await client._send_account_subscriptions('synthetic-token')
    asyncio.run(scenario())
    client.signer_client.create_auth_token_with_expiry.assert_not_called()
    client._direct_ws.send.assert_not_awaited()
