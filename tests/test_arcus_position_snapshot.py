"""Offline position snapshot/collector/audit regression; no exchange requests."""
import asyncio
import time
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_arcus_adapter import adapter, ADDRESS
from core.adapters.exchanges.order_state import OrderStateUnknownError
from core.services.arbitrage_monitor_v2.core.orchestrator_ui_controller import OrchestratorUIController as UI
from core.services.arbitrage_monitor_v2.core.unified_orchestrator_position_audit_mixin import UnifiedOrchestratorPositionAuditMixin as Audit

SYMBOL = 'SOL-USDC-PERP'


def position_row(symbol='SOL-USD', size='.45'):
    return dict(address=ADDRESS, accountIndex=0, marketId=3,
                marketDisplayName=symbol, side='LONG', size=size,
                averageEntryPrice='110', leverage=1, marginMode='CROSS',
                borrowedCapital='0', marginUsed='50', positionValueNotional='50',
                unrealizedPnl='0', markPx='110')


def request_positions(a, rows):
    a._request = AsyncMock(return_value=(200, {'positions': rows}))


class Harness(Audit):
    def _collect_exchange_position_cache(self):
        return self.controller._collect_exchange_position_cache()


def harness(a, *, old=None, empty=False):
    o = Harness()
    short = {'size': '.45', 'side': 'short', 'entry_price': '110', 'timestamp': datetime.now()}
    o.exchange_adapters = {'arcus': a, 'lighter': NS(_position_cache={} if empty else {SYMBOL: short})}
    pair = NS(total_quantity=D('.45'), exchange_buy='arcus', exchange_sell='lighter',
              buy_symbol=SYMBOL, sell_symbol=SYMBOL, symbol=SYMBOL)
    o.decision_engine = NS(quantity_epsilon=D('.0001'), pair_positions={} if empty else {'SOL': {'pair': pair}})
    o.ui_manager = NS(exchange_position_cache=old or {})
    o.ui_manager.update_exchange_position_cache = lambda cache: setattr(o.ui_manager, 'exchange_position_cache', cache)
    o.risk_controller = NS(risk_status=NS(account_data_unavailable=set()),
                           config=NS(position_mismatch_pause=NS(enabled=True, mismatch_seconds=10)),
                           clear_position_mismatch_pause=Mock())
    o.monitor_only_mode = False
    o._last_alignment_snapshot = {}; o._last_alignment_log_time = {}; o._alignment_log_interval = 0
    o.controller = object.__new__(UI); o.controller._orc = o; o.controller._logger = Mock()
    return o


def test_real_rest_full_snapshot_filter_and_actual_collector_alignment():
    a = adapter(); request_positions(a, [position_row(), position_row('ETH-USD', '.015')])
    assert [p.symbol for p in asyncio.run(a.get_positions([SYMBOL]))] == [SYMBOL]
    assert set(a._positions) == {SYMBOL, 'ETH-USDC-PERP'}
    assert a._positions_snapshot_valid and time.monotonic() - a._positions_snapshot_at < 5
    # Separate all-account SOL snapshot to model the screenshot's three .15 fills.
    a._invalidate_account_reads(); request_positions(a, [position_row()])
    asyncio.run(a.get_positions())
    o = harness(a)
    assert o._collect_exchange_net_positions() == {('arcus', 'SOL'): D('.45'), ('lighter', 'SOL'): D('-.45')}
    assert o._audit_position_alignment('LIGHTER_ARCUS_SOL') is True
    assert o._last_alignment_ui_data['consistent'] is True


def test_confirmed_empty_clears_full_snapshot_and_stale_ui_without_fallback():
    a = adapter(); request_positions(a, [position_row()]); asyncio.run(a.get_positions())
    o = harness(a)
    o._collect_exchange_net_positions(); old = o.ui_manager.exchange_position_cache
    a._invalidate_account_reads(); request_positions(a, []); assert asyncio.run(a.get_positions()) == []
    assert a._positions == {} and a._positions_snapshot_valid
    o = harness(a, old=old, empty=True)
    assert o._collect_exchange_net_positions() == {}
    assert o.ui_manager.exchange_position_cache == {'arcus': {}, 'lighter': {}}
    o._last_alignment_ui_data = {'consistent': False, 'decision': '+.45', 'exchange': '-.45'}
    assert o._audit_position_alignment('__global__') is True
    assert o._last_alignment_ui_data['consistent'] is True
    assert '.45' not in o._last_alignment_ui_data['decision']
    assert '.45' not in o._last_alignment_ui_data['exchange']
    o.risk_controller.clear_position_mismatch_pause.assert_not_called()


@pytest.mark.parametrize('state', ['not-ready', 'failed', 'stale', 'risk-unavailable', 'cancelled'])
def test_unknown_does_not_make_false_zero_clear_pause_or_trigger_fix(state):
    a = adapter(); request_positions(a, [position_row()]); asyncio.run(a.get_positions())
    if state == 'not-ready': a._positions_snapshot_valid = False
    elif state == 'stale': a._positions_snapshot_at -= 6
    elif state in ('failed', 'cancelled'):
        a._account_reads = {}
        a._request = AsyncMock(side_effect=TimeoutError('offline') if state == 'failed' else asyncio.CancelledError())
        with pytest.raises(OrderStateUnknownError if state == 'failed' else asyncio.CancelledError):
            asyncio.run(a.get_positions())
        assert a._positions[SYMBOL].size == D('.45')
        assert not a._positions_snapshot_valid
    o = harness(a)
    if state == 'risk-unavailable': o.risk_controller.risk_status.account_data_unavailable.add('lighter')
    o._position_mismatch_first_seen_ts = 123
    consistent = o._audit_position_alignment('__global__')
    assert consistent is None
    assert o._last_alignment_ui_data['consistent'] is None
    assert '未确认' in o._last_alignment_ui_data['title'] and o._last_alignment_ui_data['delta'] is None
    o._update_position_mismatch_pause_state(consistent=consistent)
    assert o._position_mismatch_first_seen_ts == 123
    o.risk_controller.clear_position_mismatch_pause.assert_not_called()
    # Existing poll schedules auto-fix only for `consistent is False`.
    assert consistent is not False


def test_generation_rejected_read_never_publishes_over_existing_ws_cache():
    a = adapter(); old = NS(symbol=SYMBOL, size=D('.6')); a._positions = {SYMBOL: old}
    original = a._read_positions
    request_positions(a, [position_row()])
    async def changed():
        positions = await original()
        a._invalidate_account_reads()
        return positions
    a._read_positions = changed
    with pytest.raises(OrderStateUnknownError): asyncio.run(a.get_positions())
    assert a._request.await_count == 3 and not a._account_reads
    assert a._positions == {SYMBOL: old} and not a._positions_snapshot_valid


def test_cached_read_does_not_extend_snapshot_freshness():
    a = adapter(); request_positions(a, [position_row()])
    async def check():
        await a.get_positions(); at = a._positions_snapshot_at
        await a.get_positions(); assert a._positions_snapshot_at == at
        assert a._request.await_count == 1
    asyncio.run(check())


def test_audit_refresh_six_second_phase_offset_and_failed_balance_latch():
    from core.services.arbitrage_monitor_v2.risk_control.global_risk_controller import GlobalRiskController
    a = adapter(); request_positions(a, [position_row()]); asyncio.run(a.get_positions())
    o = harness(a)
    o.risk_controller._validated_positions = lambda exchange, owner: GlobalRiskController._validated_positions(o.risk_controller, exchange, owner)
    o.risk_controller._validate_positions_snapshot = GlobalRiskController._validate_positions_snapshot
    async def check():
        # Risk read happened 6s before this audit tick; a fresh snapshot is required.
        a._positions_snapshot_at -= 6
        a._account_reads['positions'] = (time.monotonic()-6, a._account_reads['positions'][1])
        await o._refresh_arcus_position_snapshot_for_audit()
        assert a._request.await_count == 2
        assert o._audit_position_alignment('__global__') is True
        await o._refresh_arcus_position_snapshot_for_audit()
        assert a._request.await_count == 2
        # A position success must not clear a separate balance failure latch.
        o.risk_controller.risk_status.account_data_unavailable.add('arcus')
        a._positions_snapshot_at -= 6; a._account_reads = {}
        await o._refresh_arcus_position_snapshot_for_audit()
        assert o.risk_controller.risk_status.account_data_unavailable == {'arcus'}
        assert o._audit_position_alignment('__global__') is None
        a._positions_snapshot_at -= 6; a._account_reads = {}
        a._request.side_effect = TimeoutError('offline')
        await o._refresh_arcus_position_snapshot_for_audit()
        assert o._audit_position_alignment('__global__') is None
    asyncio.run(check())


@pytest.mark.parametrize('activity', ['open', 'close', 'recent'])
def test_poll_rechecks_quiet_window_after_refresh_await(monkeypatch, activity):
    import core.services.arbitrage_monitor_v2.core.unified_orchestrator_position_audit_mixin as module
    o = harness(adapter()); o.running = True
    o._pending_open_pairs = set(); o._pending_close_symbols = set()
    o._last_open_close_activity_ts = 0
    o._position_alignment_poll_interval_seconds = lambda: 1
    o._audit_position_alignment = Mock(return_value=True)
    o._update_position_mismatch_pause_state = Mock()
    o._run_position_alignment_auto_fix_all = AsyncMock()
    ticks = 0
    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            # First tick acquired data while a trade entered: no audit/pause/fix.
            o._audit_position_alignment.assert_not_called()
            o._update_position_mismatch_pause_state.assert_not_called()
            o._run_position_alignment_auto_fix_all.assert_not_awaited()
            o._pending_open_pairs.clear(); o._pending_close_symbols.clear()
            o._last_open_close_activity_ts = 0
        if ticks == 3: o.running = False
    async def refresh():
        if ticks == 1:
            if activity == 'open': o._pending_open_pairs.add('SOL')
            elif activity == 'close': o._pending_close_symbols.add('SOL')
            else: o._last_open_close_activity_ts = module.time.time()
    o._refresh_arcus_position_snapshot_for_audit = refresh
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    monkeypatch.setattr(module.time, 'time', lambda: 100 + 20*ticks)
    asyncio.run(o._position_alignment_poll_loop())
    # The next quiet tick can audit; only the interrupted first tick is skipped.
    o._audit_position_alignment.assert_called_once_with('__global__')
    o._update_position_mismatch_pause_state.assert_called_once_with(consistent=True)


def test_generation_race_reloads_fresh_snapshot_without_publishing_old_result():
    a = adapter(); old = NS(symbol=SYMBOL, size=D('.6')); a._positions = {SYMBOL: old}
    reads = 0
    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            a._invalidate_account_reads()
            return [NS(symbol=SYMBOL, size=D('.4'))]
        assert a._positions == {SYMBOL: old} and not a._positions_snapshot_valid
        return [NS(symbol=SYMBOL, size=D('.2'))]
    a._read_positions = read
    result = asyncio.run(a.get_positions())
    assert reads == 2 and result[0].size == D('.2')
    assert a._positions_snapshot_valid and a._positions[SYMBOL].size == D('.2')


@pytest.mark.parametrize('error', ['429', '403', 'malformed positions'])
def test_account_read_errors_are_not_fast_retried(error):
    a = adapter(); a._read_positions = AsyncMock(side_effect=ValueError(error))
    with pytest.raises(OrderStateUnknownError): asyncio.run(a.get_positions())
    assert a._read_positions.await_count == 1 and not a._positions_snapshot_valid
    assert a._account_retry_at > time.monotonic()
