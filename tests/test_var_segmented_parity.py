"""Offline VAR baseline and Arcus segmented execution acceptance. No startup or credentials."""
import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from core.adapters.exchanges.models import OrderBookData, OrderBookLevel, OrderData, OrderSide, OrderStatus, OrderType
from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
from core.services.arbitrage_monitor_v2.config.arbitrage_config import ExchangeFeeConfig, ExecutionConfig
from core.services.arbitrage_monitor_v2.config.symbol_config import SegmentedConfigManager
from core.services.arbitrage_monitor_v2.core.unified_orchestrator import UnifiedOrchestrator
from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
from core.services.arbitrage_monitor_v2.decision.unified_decision_engine import UnifiedDecisionEngine
from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import ArbitrageExecutor, ExecutionRequest, ExecutionResult

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'lighter-var-crypto-tranding'
SYMBOL = 'BTC-USDC-PERP'

def run(coro): return asyncio.run(coro)

def spread(buy='50000', sell='50040', reverse=False):
    buy, sell = D(buy), D(sell)
    return SpreadData(SYMBOL, 'arcus' if reverse else 'lighter', 'lighter' if reverse else 'arcus', buy, sell, D('100'), D('100'), sell-buy, float((sell-buy)/buy*100))

def historical_segmented_config():
    """Keep small-capital regression scenarios independent of the live capital profile."""
    config = yaml.safe_load((ROOT/'config/arbitrage/arbitrage_segmented.yaml').read_text())
    config['default_config']['risk_config']['max_position_value'] = 45.0
    risk = config['risk_control']
    risk['balance_management'].update(min_balance_warning=50.0,min_balance_close_position=150.0)
    risk['open_only_close']['low_balance']['min_equity_usdc'] = 200.0
    risk['open_only_close']['max_exchange_position_value']['max_value_usdc'] = 200.0
    for coin, quantity in {'BTC':.00020,'ETH':.0075,'BNB':.02,'SOL':.15,'HYPE':.20}.items():
        row = config['symbol_configs'][coin+'-USDC-PERP']
        row['quantity_config'].update(base_quantity=quantity,split_order_size=quantity)
        row['grid_config']['min_orderbook_quantity'] = quantity
    return config


def make_orchestrator(segmented_path):
    with patch.object(OrchestratorBootstrap, 'init_exchange_adapters'):
        return UnifiedOrchestrator(segmented_config_path=segmented_path,
            monitor_config_path=ROOT/'config/arbitrage/monitor_lighter_arcus.yaml',
            execution_config_path=ROOT/'config/arbitrage/arbitrage_lighter_arcus.yaml',
            multi_exchange_config_path=ROOT/'config/arbitrage/multi_exchange_arbitrage.yaml')


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = tmp_path/'historical-small-capital.yaml'
    profile.write_text(yaml.safe_dump(historical_segmented_config()))
    return make_orchestrator(profile)


@pytest.fixture
def capital5000_orchestrator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return make_orchestrator(ROOT/'config/arbitrage/arbitrage_segmented.yaml')

@pytest.mark.parametrize('coin', ['BTC','ETH','BNB','SOL','HYPE'])
def test_source_grid_and_risk_rules_preserved_with_authorized_capital_scale(coin):
    source_path = SOURCE/'config/arbitrage/arbitrage_segmented.yaml'
    if not source_path.is_file():
        pytest.skip('Historical VAR comparison requires optional sibling project')
    source = yaml.safe_load(source_path.read_text())
    target = historical_segmented_config()
    symbol = coin+'-USDC-PERP'
    expected = source['symbol_configs'][symbol]
    quantity = expected['quantity_config']
    qty = {'BTC':.00020,'ETH':.0075,'BNB':.02,'SOL':.15,'HYPE':.20}[coin]
    quantity['base_quantity'] = quantity['split_order_size'] = qty
    expected['grid_config'].update(initial_spread_threshold=.02,grid_step=.01,max_segments=2,require_orderbook_liquidity=True,min_orderbook_quantity=qty)
    if coin == 'ETH': quantity['quantity_precision'] = 4
    floors = {'BTC':(.00007,.0001),'ETH':(.002,.001),'SOL':(.055,.01),'HYPE':(.07,.1),'BNB':(.01,.001)}
    lighter, arcus = floors[coin]
    quantity['min_exchange_order_qty'] = {'default':max(lighter,arcus),'lighter':lighter,'arcus':arcus}
    assert target['symbol_configs'][symbol] == expected
    risk = source['risk_control']
    risk['balance_management']['min_balance_warning'] = 50.0
    risk['balance_management']['min_balance_close_position'] = 150.0
    risk['open_only_close']['low_balance']['min_equity_usdc'] = 200.0
    risk['open_only_close']['max_exchange_position_value']['max_value_usdc'] = 200.0
    risk['open_only_close']['dynamic_max_exchange_position_value']['equity_multiplier'] = 1.0
    assert target['risk_control'] == risk
    assert target['system_mode'] == source['system_mode']
    execution = yaml.safe_load((ROOT/'config/arbitrage/arbitrage_lighter_arcus.yaml').read_text())
    assert 'variational' not in execution['arbitrage_execution']['exchange_order_modes']
    assert execution['arbitrage_execution']['exchange_fees']['arcus']['market_fee_rate'] == .000225


def test_real_monitor_orchestrator_open_add_partial_and_full_close(orchestrator):
    o = orchestrator
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0)}
    o.exchange_adapters.update({name:NS(create_order=AsyncMock(), get_positions=AsyncMock(), get_balances=AsyncMock()) for name in ('lighter','arcus')})
    for name in ('lighter','arcus'):
        o.data_processor.orderbooks[name][SYMBOL] = OrderBookData(SYMBOL,[OrderBookLevel(D('50000'),D('100'))],[OrderBookLevel(D('50100'),D('100'))],datetime.now())
    async def scenario():
        edge = spread(sell='50100')
        allowed, qty = await o.decision_engine.should_open(SYMBOL, edge, skip_persistence=True)
        assert allowed and qty == D('.00020')
        for _ in range(2):
            await o._execute_and_record_open(SYMBOL, qty, edge, None, False, 1, D('.001'), None, None, o.decision_engine.get_position(SYMBOL))
        position = o.decision_engine.get_position(SYMBOL)
        assert position.total_quantity == D('.00040')
        exit_view = spread('50000','50000',True)
        await o._execute_and_record_close(SYMBOL,D('.00020'),exit_view,'网格平仓T1',False,1,D('.001'),None,None,position,None)
        assert position.total_quantity == D('.00020')
        await o._execute_and_record_close(SYMBOL,D('.00020'),exit_view,'网格平仓T1',True,1,D('.001'),None,None,position,None)
        assert position.total_quantity == 0 and not position.is_open
    run(scenario())
    for adapter in o.exchange_adapters.values():
        adapter.create_order.assert_not_awaited()
        adapter.get_positions.assert_not_awaited()
        adapter.get_balances.assert_not_awaited()


def test_fee_net_boundary_does_not_mutate_signal(orchestrator):
    e = orchestrator.decision_engine
    e.exchange_fee_config = {'default':ExchangeFeeConfig(0,0),'arcus':ExchangeFeeConfig(0,.000225)}
    edge = spread(sell='50030')
    assert not run(e.should_open(SYMBOL, edge, skip_persistence=True))[0]
    assert edge.spread_pct == .06
    higher = spread(sell='50035')
    assert run(e.should_open(SYMBOL, higher, skip_persistence=True))[0]


def test_fifo_fee_zero_missing_and_partial_costs(orchestrator):
    o = orchestrator
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('.025'),spread('50000','50100'),filled_quantity=D('.025'),entry_fee_buy_per_unit=D('0'),entry_fee_sell_per_unit=D('0')))
    run(o.decision_engine.record_open(SYMBOL,D('.025'),spread('51000','51100'),filled_quantity=D('.025')))
    p = o.decision_engine.get_position(SYMBOL)
    exit_view = spread('50050','49950',True)
    assert o._estimate_fifo_close_profit(p,D('.025'),exit_view) == 0
    assert o._estimate_fifo_close_profit(p,D('.05'),exit_view) == 0
    p.segments[0].open_price_buy = D('0')
    assert o._estimate_fifo_close_profit(p,D('.01'),exit_view) is None


def test_zero_fill_is_not_recorded(orchestrator):
    e = orchestrator.decision_engine
    run(e.record_open(SYMBOL,D('.025'),spread(),filled_quantity=D('0')))
    assert e.get_position(SYMBOL) is None


def test_old_inflight_lock_never_expires_while_task_running(orchestrator):
    o=orchestrator;key='test'
    async def scenario():
        assert await o._try_register_open_pair(key)
        o._pending_open_pairs_started_at[key] = 1
        assert not await o._try_register_open_pair(key)
        await o._release_open_pair_async(key)
        assert await o._try_register_open_pair(key)
    run(scenario())


def test_common_precision_and_notional_failure_before_adapter_write(orchestrator):
    o=orchestrator
    req=ExecutionRequest(SYMBOL,'lighter','arcus',D('50000'),D('50040'),D('.000015'))
    with pytest.raises(ValueError):o.executor._validate_pair_quantity(req)
    req.quantity = D('.0001')
    with pytest.raises(ValueError):o.executor._validate_pair_quantity(req)
    req.quantity = D('.025')
    o.executor._validate_pair_quantity(req)


def test_cumulative_cost_fee_and_repeated_terminal_event(orchestrator):
    executor=orchestrator.executor;executor.monitor_only=False
    request=ExecutionRequest(SYMBOL,'lighter','arcus',D('50000'),D('50100'),D('.025'))
    def order(identity, qty, price, side):
        return OrderData(identity,identity,SYMBOL,side,OrderType.MARKET,D(qty),None,D(qty),D(0),D(qty)*D(price),D(price),OrderStatus.FILLED,datetime.now(),datetime.now(),{'currency':'USDC','cost':D(qty)*D(price)*D('.000225')},[],{}, {})
    executor.exchange_adapters={'lighter':NS(exchange_id='lighter'),'arcus':NS(exchange_id='arcus')}
    token=executor._execution_request_ctx.set(request);request._confirmed_fills={}
    try:
        l1=order('L1','.01','50000',OrderSide.BUY);l2=order('L2','.015','50100',OrderSide.BUY);a=order('A1','.025','50200',OrderSide.SELL)
        for row in (l1,l1,l2):executor._remember_confirmed_fill(executor.exchange_adapters['lighter'],row)
        executor._remember_confirmed_fill(executor.exchange_adapters['arcus'],a)
        result=ExecutionResult(True,l2,a)
        executor._apply_confirmed_fills(request,result)
        assert result.success and result.success_quantity==D('.025')
        assert result.order_buy.cost == D('1251.5')
        assert result.order_buy.average==D('50060')
        assert result.order_buy.fee['cost']==D('1251.5')*D('.000225')
    finally:executor._execution_request_ctx.reset(token)


def filled_order(identity, quantity, price, is_buy, fee=None):
    return OrderData(identity,identity,SYMBOL,OrderSide.BUY if is_buy else OrderSide.SELL,OrderType.MARKET,quantity,None,quantity,D(0),quantity*price,price,OrderStatus.FILLED,datetime.now(),datetime.now(),fee,[],{}, {})


@pytest.mark.parametrize('late_quantity', [D('.01'), D('.025')])
def test_zero_wait_cancel_late_fill_preserves_exposure_and_valuation(late_quantity):
    from core.services.arbitrage_monitor_v2.config.arbitrage_config import ExchangeOrderModeConfig
    first=filled_order('L1',D('.025'),D(50000),True,{'currency':'USDC','cost':D(0)})
    first.filled=D(0);first.cost=D(0);first.average=None;first.status=OrderStatus.OPEN;first.remaining=D('.025')
    terminal=replace(first,filled=late_quantity,cost=late_quantity*D(50001),average=D(50001),remaining=D(0),status=OrderStatus.CANCELED)
    closed_zero=replace(first,id='LC',client_id='LC',side=OrderSide.SELL,status=OrderStatus.CANCELED,remaining=D(0))
    lighter=NS(exchange_id='lighter',strict_order_state=True,create_order=AsyncMock(side_effect=[first,closed_zero]),get_order=AsyncMock(return_value=first),cancel_order=AsyncMock(return_value=terminal))
    arcus=NS(exchange_id='arcus',create_order=AsyncMock(return_value=filled_order('A1',D('.025'),D(50100),False)))
    cfg=ExecutionConfig(exchange_order_modes={'lighter':ExchangeOrderModeConfig(order_mode='limit'),'arcus':ExchangeOrderModeConfig(order_mode='market')})
    cfg.order_execution.max_retry_count=1
    e=ArbitrageExecutor(cfg,{'lighter':lighter,'arcus':arcus},monitor_only=False,is_segmented_mode=True)
    async def wait(order,*args,**kwargs): return D(0) if order.id=='L1' else order.filled
    e.order_monitor.wait_for_order_fill=AsyncMock(side_effect=wait)
    req=ExecutionRequest(SYMBOL,'lighter','arcus',D(50000),D(50100),D('.025'))
    result=run(e.execute_arbitrage(req))
    assert result.failure_code!='limit_zero_fill'
    assert result.order_buy.filled==late_quantity and result.order_buy.average==D(50001)
    assert result.order_buy.cost==late_quantity*D(50001)
    assert lighter.cancel_order.await_count==(1 if late_quantity==D(".025") else 2)
    if late_quantity==D('.025'):
        assert result.success and result.success_quantity==late_quantity
        arcus.create_order.assert_awaited_once();lighter.create_order.assert_awaited_once()
    else:
        assert not result.success and result.partial_failure
        assert e._unknown_execution_pairs
        arcus.create_order.assert_not_awaited()
        assert lighter.create_order.await_count==2
        assert lighter.create_order.await_args.kwargs['amount']==late_quantity
        assert lighter.create_order.await_args.kwargs['params']['reduce_only']


@pytest.mark.parametrize('multiple', [False,True])
def test_unknown_actual_cost_is_quarantined_and_never_uses_signal(orchestrator,multiple):
    e=orchestrator.executor;e.monitor_only=False
    req=ExecutionRequest(SYMBOL,'lighter','arcus',D(50000),D(50100),D('.025'));req._confirmed_fills={}
    buy=filled_order('L1',D('.025'),D(50000),True)
    if multiple:
        buy=replace(buy,amount=D('.015'),filled=D('.015'))
        req._confirmed_fills[('lighter',True,'L0')]=filled_order('L0',D('.01'),D(50000),True)
    buy.cost=D(0);buy.average=None
    req._confirmed_fills[('lighter',True,'L1')]=buy
    sell=filled_order('A1',D('.025'),D(50100),False)
    req._confirmed_fills[('arcus',False,'A1')]=sell
    result=ExecutionResult(True,buy,sell)
    e._apply_confirmed_fills(req,result)
    assert not result.success and result.failure_code=='EXECUTION_COST_UNKNOWN'
    assert result.success_quantity==D('.025') and result.order_buy.cost==0 and result.order_buy.average==0
    assert e._unknown_execution_pairs
    run(orchestrator.decision_engine.record_open(SYMBOL,D('.025'),spread(),filled_quantity=result.success_quantity,entry_price_buy=D(0),entry_price_sell=sell.average))
    assert orchestrator._estimate_fifo_close_profit(orchestrator.decision_engine.get_position(SYMBOL),D('.025'),spread('50000','50000',True)) is None


def test_mixed_actual_zero_and_missing_fees_preserve_fifo_cost(orchestrator):
    o=orchestrator;e=o.executor
    e.config.exchange_fee_config={'default':ExchangeFeeConfig(0,.000225)}
    req=ExecutionRequest(SYMBOL,'lighter','arcus',D(50000),D(50100),D('.025'))
    buy1=filled_order('L1',D('.01'),D(50000),True,{'currency':'USDC','cost':D(0)})
    buy2=filled_order('L2',D('.015'),D(50100),True,None)
    sell=filled_order('A1',D('.025'),D(50200),False,{'currency':'USDC','cost':D('.1')})
    req._confirmed_fills={('lighter',True,'L1'):buy1,('lighter',True,'L2'):buy2,('arcus',False,'A1'):sell}
    result=ExecutionResult(True,buy2,sell);e._apply_confirmed_fills(req,result)
    assert result.success
    assert result.order_buy.cost==D('1251.5')
    assert result.order_buy.fee['cost']==D('751.5')*D('.000225')
    assert result.order_sell.fee['cost']==D('.1')
    run(o.decision_engine.record_open(SYMBOL,D('.025'),spread(),filled_quantity=D('.025'),entry_price_buy=result.order_buy.average,entry_price_sell=result.order_sell.average,entry_fee_buy_per_unit=o._entry_fee_per_unit(result.order_buy,D('.025')),entry_fee_sell_per_unit=o._entry_fee_per_unit(result.order_sell,D('.025'))))
    p=o.decision_engine.get_position(SYMBOL);exit_view=spread('50000','50000',True)
    all_profit=o._estimate_fifo_close_profit(p,D('.025'),exit_view)
    assert o._estimate_fifo_close_profit(p,D('.01'),exit_view)+o._estimate_fifo_close_profit(p,D('.015'),exit_view)==all_profit


@pytest.mark.parametrize('scenario', ['both_fail','one_fail','missing','mismatch','zero'])
def test_bootstrap_required_accounts_fail_closed_and_empty_success_only_verified(orchestrator,scenario):
    from core.services.arbitrage_monitor_v2.config.multi_exchange_config import TradingPair
    o=orchestrator
    settings=dict(o.config_manager.get_system_mode());settings.update(bootstrap_positions_on_startup=True,bootstrap_positions_retry_max_attempts=1,bootstrap_positions_empty_confirm_attempts=1,bootstrap_positions_block_open_on_mismatch=True)
    o.config_manager.get_system_mode=lambda:settings
    o.multi_exchange_pairs=[TradingPair(SYMBOL,'lighter','arcus',SYMBOL)]
    o.exchange_adapters={name:NS(get_positions=AsyncMock(return_value=[])) for name in ('lighter','arcus')}
    if scenario in ('both_fail','one_fail'):
        o.exchange_adapters['arcus'].get_positions.side_effect=RuntimeError('offline read failed')
        if scenario=='both_fail':o.exchange_adapters['lighter'].get_positions.side_effect=RuntimeError('offline read failed')
    elif scenario=='missing': del o.exchange_adapters['arcus']
    elif scenario=='mismatch':
        for name,qty,side in [('lighter','.025','long'),('arcus','.01','short')]:
            o.exchange_adapters[name].get_positions.return_value=[NS(symbol=SYMBOL,size=D(qty),side=side,timestamp=datetime.now())]
    run(o._bootstrap_positions_from_exchanges_once())
    assert o._bootstrap_positions_success is (scenario=='zero')
    assert o._bootstrap_positions_block_open is (scenario!='zero')
    assert o.decision_engine.get_position(SYMBOL) is None


def test_emergency_partial_actual_fill_leaves_residual_position(orchestrator):
    o=orchestrator
    run(o.decision_engine.record_open(SYMBOL,D('.05'),spread(),filled_quantity=D('.05')))
    for name in ('lighter','arcus'):
        o.data_processor.orderbooks[name][SYMBOL]=OrderBookData(SYMBOL,[OrderBookLevel(D(50000),D(100))],[OrderBookLevel(D(50100),D(100))],datetime.now())
    o.data_processor.get_orderbook=lambda exchange,*args,**kwargs:o.data_processor.orderbooks[exchange][SYMBOL]
    o.executor.monitor_only=False
    o.executor.execute_arbitrage=AsyncMock(return_value=ExecutionResult(True,filled_order('A',D('.01'),D(50000),True),filled_order('L',D('.01'),D(50100),False),success_quantity=D('.01')))
    assert not run(o.emergency_close_all_positions())
    assert o.decision_engine.get_position(SYMBOL).total_quantity==D('.04')


def test_close_revalidates_both_quotes_after_all_account_reads(orchestrator):
    o=orchestrator
    o.decision_engine.exchange_fee_config={'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('.025'),spread('50000','50100'),filled_quantity=D('.025'),entry_fee_buy_per_unit=D(0),entry_fee_sell_per_unit=D(0)))
    prices={'arcus':D('50000'),'lighter':D('50000')}
    async def first(): return [NS(symbol=SYMBOL,size=D('.025'),side='short',timestamp=datetime.now())]
    async def second():
        prices['arcus']=D('51000')
        return [NS(symbol=SYMBOL,size=D('.025'),side='long',timestamp=datetime.now())]
    o.exchange_adapters={'arcus':NS(get_positions=first),'lighter':NS(get_positions=second)}
    o.executor._get_live_market_price=lambda exchange,*args:prices[exchange]
    req=ExecutionRequest(SYMBOL,'arcus','lighter',D('50000'),D('50000'),D('.025'),is_open=False,spread_data=spread('50000','50000',True))
    allowed,reason=run(o._validate_segmented_close(req))
    assert not allowed, 'Accepted stale first-leg quote after second account await caused negative FIFO profit'

def test_open_rejects_same_quantity_wrong_real_position_direction(orchestrator):
    o=orchestrator
    o.decision_engine.exchange_fee_config={'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('.025'),spread('50000','50100'),filled_quantity=D('.025')))
    balances=[NS(currency='USDC',total=D('100000'),free=D('100000'),used=D(0),timestamp=datetime.now())]
    o.exchange_adapters={name:NS(get_balances=AsyncMock(return_value=balances), get_open_orders=AsyncMock(return_value=[]), get_positions=AsyncMock(return_value=[NS(symbol=SYMBOL,size=D('.025'),side='short' if name=='lighter' else 'long',timestamp=datetime.now())])) for name in ('lighter','arcus')}
    o._bootstrap_positions_block_open=False
    o._open_only_close_guard=NS(should_block_open=lambda **kwargs:(False,'',None))
    o.executor._get_live_market_price=lambda exchange,*args:D('50000') if exchange=='lighter' else D('50100')
    req=ExecutionRequest(SYMBOL,'lighter','arcus',D('50000'),D('50100'),D('.025'),spread_data=spread('50000','50100'))
    req._expected_position_quantity=D('.025')
    allowed,reason=run(o._validate_segmented_open(req))
    assert not allowed, 'Same quantity but inverted actual long/short passed segmented opening gate'


def test_forced_grid_partial_fill_retries_only_residual_and_preserves_failure(orchestrator):
    o=orchestrator
    run(o.decision_engine.record_open(SYMBOL,D('.0002'),spread(),filled_quantity=D('.0002')))
    books={name:OrderBookData(SYMBOL,[OrderBookLevel(D(50000),D(100))],[OrderBookLevel(D(50100),D(100))],datetime.now()) for name in ('lighter','arcus')}
    o.data_processor.get_orderbook=lambda exchange,*args,**kwargs:books[exchange]
    o.executor.monitor_only=False;o.executor.max_retry_count=1
    o.executor.execute_arbitrage=AsyncMock(side_effect=[ExecutionResult(True,filled_order('A',D('.0001'),D(50000),True),filled_order('L',D('.0001'),D(50100),False),success_quantity=D('.0001')),ExecutionResult(False,error_message='offline rejection')])
    assert not run(o.force_close_all_positions_by_grid_sequential())
    assert o.decision_engine.get_position(SYMBOL).total_quantity==D('.0001')
    assert [call.args[0].quantity for call in o.executor.execute_arbitrage.await_args_list]==[D('.0002'),D('.0001')]


def strict_bootstrap(orchestrator):
    from core.services.arbitrage_monitor_v2.config.multi_exchange_config import TradingPair
    o = orchestrator
    settings = dict(o.config_manager.get_system_mode())
    assert settings['bootstrap_positions_empty_confirm_attempts'] == 10
    settings['bootstrap_positions_retry_max_attempts'] = 1
    o.config_manager.get_system_mode = lambda:settings
    o.multi_exchange_pairs = [TradingPair(SYMBOL, 'lighter', 'arcus', SYMBOL)]
    o.exchange_adapters = {name:NS(strict_order_state=True, get_open_orders=AsyncMock(return_value=[]),
                                   get_positions=AsyncMock(return_value=[]), _uncertain=set())
                           for name in ('lighter', 'arcus')}
    o.risk_controller._validated_balance = AsyncMock()
    return o, settings


def test_bootstrap_stdout_shows_all_ten_strict_empty_rounds_and_completion(orchestrator, monkeypatch, capsys):
    o, _ = strict_bootstrap(orchestrator)
    sleep = AsyncMock(); monkeypatch.setattr(asyncio, 'sleep', sleep)
    run(o._bootstrap_positions_from_exchanges_once())
    output = capsys.readouterr().out
    assert '正在核对余额、挂单和持仓' in output and '界面将在核对结束后启动' in output
    for round in range(1, 11):
        assert f'账户快照第{round}/10轮' in output
    assert '核对完成：已恢复0组持仓' in output
    assert o._bootstrap_positions_success and not o._bootstrap_positions_block_open
    assert o.risk_controller._validated_balance.await_count == 20
    for adapter in o.exchange_adapters.values():
        assert adapter.get_open_orders.await_count == adapter.get_positions.await_count == 10
    assert sleep.await_count == 9


@pytest.mark.parametrize('failure', ['balance', 'orders', 'positions', 'timeout', 'uncertain'])
def test_bootstrap_strict_failure_keeps_open_blocked_and_shows_failure(orchestrator, monkeypatch, capsys, failure):
    o, settings = strict_bootstrap(orchestrator)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    adapter = o.exchange_adapters['arcus']
    if failure == 'balance': o.risk_controller._validated_balance.side_effect = ValueError('offline balance invalid')
    elif failure == 'orders': adapter.get_open_orders.return_value = [NS(id='unresolved')]
    elif failure == 'uncertain': adapter._uncertain = {'unresolved'}
    elif failure == 'positions': adapter.get_positions.side_effect = ValueError('offline positions invalid')
    else:
        settings['bootstrap_positions_fetch_timeout_seconds'] = .001
        async def pending(**kwargs): await asyncio.Event().wait()
        adapter.get_positions.side_effect = pending
    run(o._bootstrap_positions_from_exchanges_once())
    output = capsys.readouterr().out
    assert '账户快照第1/10轮' in output and '账户快照第2/10轮' not in output
    assert '核对结束' in output and '禁止开仓' in output
    assert '核对完成' not in output
    assert o._bootstrap_positions_done and not o._bootstrap_positions_success and o._bootstrap_positions_block_open
    if failure == 'timeout': assert 'TimeoutError' in output


def test_bootstrap_stdout_exposes_retry_without_lowering_empty_confirmation(orchestrator, monkeypatch, capsys):
    o, settings = strict_bootstrap(orchestrator)
    settings['bootstrap_positions_retry_max_attempts'] = 2
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    o.exchange_adapters['arcus'].get_open_orders.side_effect = [RuntimeError('offline temporary read')] + [[]] * 19
    run(o._bootstrap_positions_from_exchanges_once())
    output = capsys.readouterr().out
    assert '第1次核对失败，禁止开仓' in output and '后重试账户核对' in output
    assert '第2次核对：账户快照第10/10轮' in output and '核对完成' in output
    assert o._bootstrap_positions_success


def test_bootstrap_cancel_reports_incomplete_and_cleans_both_parallel_reads(orchestrator, capsys):
    o, _ = strict_bootstrap(orchestrator)
    async def scenario():
        started = asyncio.Event(); count = 0; cancelled = []
        async def pending(name, **kwargs):
            nonlocal count
            count += 1
            if count == 2: started.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name); raise
        for name, adapter in o.exchange_adapters.items():
            async def read(*, symbols, name=name): return await pending(name, symbols=symbols)
            adapter.get_positions.side_effect = read
        task = asyncio.create_task(o._bootstrap_positions_from_exchanges_once())
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert set(cancelled) == {'lighter', 'arcus'}
    run(scenario())
    output = capsys.readouterr().out
    assert '核对已取消，尚未完成账户确认' in output and '核对完成' not in output
    assert not o._bootstrap_positions_done and not o._bootstrap_positions_success and o._bootstrap_positions_block_open


def test_actual_subscription_plan_contains_only_five_perpetual_symbols(orchestrator):
    from core.services.arbitrage_monitor_v2.config.multi_leg_pairs_config import MultiLegPairsConfigManager
    o = orchestrator
    o.multi_leg_pairs = MultiLegPairsConfigManager(ROOT/'config/arbitrage/multi_leg_pairs.yaml').get_pairs()
    assert o.multi_leg_pairs == []
    o.data_receiver.subscribe_all = AsyncMock()
    run(o.bootstrapper._subscribe_market_data())
    o.data_receiver.subscribe_all.assert_awaited_once()
    symbols = o.data_receiver.subscribe_all.await_args.args[0]
    assert len(symbols) == 5 and set(symbols) == {base+'-USDC-PERP' for base in ('BTC','ETH','BNB','SOL','HYPE')}


@pytest.mark.parametrize('balance,critical', [('49',True),('149.99',True),('150',False),('199.99',False),('200',False),('278',False),('280',False)])
def test_scaled_balance_monitor_uses_150_critical_threshold_and_original_priority(orchestrator, balance, critical):
    o = orchestrator; controller = o.risk_controller
    controller.exchange_adapters = {name:NS(get_balances=AsyncMock(return_value=[NS(currency='USDC',total=D(balance),timestamp=datetime.now())]),
                                           get_positions=AsyncMock(return_value=[])) for name in ('lighter','arcus')}
    controller._handle_critical_balance = AsyncMock(); controller._handle_low_balance = AsyncMock()
    run(controller._check_all_balances())
    assert controller._min_balance_warning_threshold('lighter') == D(50)
    assert controller._min_balance_close_threshold('arcus') == D(150)
    assert controller._handle_critical_balance.await_count == int(critical)
    controller._handle_low_balance.assert_not_awaited()
    assert controller.risk_status.critical_balance_exchanges == ({'lighter','arcus'} if critical else set())
    assert not controller.risk_status.account_data_unavailable


@pytest.mark.parametrize('balance,value,block_type', [('199.99','0','low_balance'),('200','0',None),('278','0',None),('280','0',None),
                                                     ('280','199.99',None),('280','200','max_exchange_position_value'),
                                                     ('300','199.99',None),('300','200','max_exchange_position_value')])
def test_scaled_open_gate_preserves_200_equity_and_minimum_of_absolute_and_dynamic_caps(orchestrator, balance, value, block_type):
    guard = orchestrator._open_only_close_guard
    guard._get_equity = lambda exchange:D(balance)
    guard._get_pos_value = lambda exchange:D(value)
    blocked, _, observed = guard.should_block_open(exchange_buy='lighter',exchange_sell='arcus',symbol=SYMBOL)
    assert blocked is (block_type is not None) and observed == block_type
    assert guard._dynamic_multiplier('lighter') == D('1.0')
    assert guard._max_value_threshold('arcus') == D(200)


def test_scaled_eth_split_keeps_precision_and_bnb_old_half_split_still_fails_notional(orchestrator):
    o = orchestrator
    assert o._format_quantity('ETH-USDC-PERP',D('.0075')) == D('.0075')
    for coin in ('BTC','ETH','BNB','SOL','HYPE'):
        symbol = coin+'-USDC-PERP'; config = o.config_manager.get_config(symbol)
        minimums = o._build_min_exchange_order_qty_map(symbol)
        split = D(str(config.grid_config.split_order_size))
        assert minimums['default'] == max(minimums['lighter'],minimums['arcus'])
        assert split >= minimums['lighter'] and split >= minimums['arcus']
    request = ExecutionRequest('BNB-USDC-PERP','lighter','arcus',D('749.3'),D('749.3'),D('.01'))
    with pytest.raises(ValueError,match='最小金额'):o.executor._validate_pair_quantity(request)
    request.quantity=D('.02');o.executor._validate_pair_quantity(request)

@pytest.mark.parametrize('loss,trigger', [('1.99',False),('2',True),('2.01',True)])
def test_pair_loss_single_leg_notional_boundary(orchestrator, loss, trigger):
    o = orchestrator
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1'),entry_fee_buy_per_unit=D('0'),entry_fee_sell_per_unit=D('0')))
    position = o.decision_engine.get_position(SYMBOL)
    view = spread(str(D('100')+D(loss)), '100', True)
    loss_books(o, buy=view.price_buy)
    assert bool(o._position_loss_reason(position,view,o.config_manager.get_config(SYMBOL))) == trigger


def test_pair_loss_fees_remaining_segments_and_invalid_cost(orchestrator):
    o = orchestrator
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0),'arcus':ExchangeFeeConfig(0,.001)}
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1'),entry_fee_buy_per_unit=D('0'),entry_fee_sell_per_unit=D('1.9')))
    p = o.decision_engine.get_position(SYMBOL)
    cfg = o.config_manager.get_config(SYMBOL)
    loss_books(o, buy='100')
    assert o._position_loss_reason(p,spread('100','100',True),cfg)
    segment = p.get_open_segments()[0]
    segment.entry_fee_sell_per_unit = D('0')
    loss_books(o, buy='110', sell='110')
    assert not o._position_loss_reason(p,spread('110','110',True),cfg)
    loss_books(o, buy='90', sell='90')
    assert not o._position_loss_reason(p,spread('90','90',True),cfg)
    assert not o._position_loss_reason(p,spread('NaN','100',True),cfg)
    segment.open_price_buy = None
    assert not o._position_loss_reason(p,spread('102','100',True),cfg)


@pytest.mark.parametrize('enabled,auto,hours,called', [(True,True,167,False),(True,True,168,False),(True,True,169,True),(False,True,169,False),(True,False,169,False)])
def test_position_duration_provider_and_boundary(orchestrator,enabled,auto,hours,called):
    from datetime import timedelta
    o = orchestrator
    rc = o.risk_controller
    assert rc.config.position_duration.max_position_duration == 168
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1')))
    now = datetime.now()
    o.decision_engine.get_position(SYMBOL).get_open_segments()[0].open_time = now-timedelta(hours=hours)
    rc.config.position_duration.enabled = enabled
    rc.config.position_duration.auto_close_on_timeout = auto
    rc.on_close_position = AsyncMock()
    with patch('core.services.arbitrage_monitor_v2.risk_control.global_risk_controller.datetime') as clock:
        clock.now.return_value = now
        run(rc._check_position_duration())
    assert rc.on_close_position.await_count == int(called)
    if called: assert rc.on_close_position.await_args.args[0] == SYMBOL


def test_position_duration_loader_uses_existing_settings(orchestrator):
    with patch.object(orchestrator,'_load_root_config',return_value={'risk_control':{'position_duration':{'enabled':False,'auto_close_on_timeout':False,'max_position_duration':72}}}):
        cfg=orchestrator._load_risk_control_config().position_duration
    assert (cfg.enabled,cfg.auto_close_on_timeout,cfg.max_position_duration)==(False,False,72)


def test_risk_pair_close_monitor_inflight_failure_and_open_gate(orchestrator):
    o = orchestrator
    o.force_close_all_positions_by_grid_sequential = AsyncMock(return_value=False)
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    o.force_close_all_positions_by_grid_sequential.assert_not_awaited()
    o.monitor_only_mode=False; o.executor.monitor_only=False
    o._pending_open_pairs.add(o._build_open_pair_key(SYMBOL,'lighter','arcus'))
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    o.force_close_all_positions_by_grid_sequential.assert_not_awaited()
    o._pending_open_pairs.clear()
    o._pending_close_symbols.add(SYMBOL)
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    o._pending_close_symbols.clear()
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    assert SYMBOL in o._position_risk_close_failed
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    assert o.force_close_all_positions_by_grid_sequential.await_count == 1
    assert not o._pending_close_symbols
    allowed,_ = run(o._validate_segmented_open(NS(symbol=SYMBOL)))
    assert not allowed


def test_risk_pair_close_filters_other_position_and_unknown_no_retry(orchestrator):
    o = orchestrator
    for symbol in (SYMBOL,'ETH-USDC-PERP'):
        run(o.decision_engine.record_open(symbol,D('.0005'),spread(),filled_quantity=D('.0005')))
    books={name:OrderBookData(SYMBOL,[OrderBookLevel(D(50000),D(100))],[OrderBookLevel(D(50100),D(100))],datetime.now()) for name in ('lighter','arcus')}
    o.data_processor.get_orderbook=lambda exchange,*args,**kwargs:books[exchange]
    o.monitor_only_mode=False; o.executor.monitor_only=False; o.executor.max_retry_count=3
    o.executor.execute_arbitrage=AsyncMock(side_effect=[ExecutionResult(True,filled_order('A',D('.0001'),D(50000),True),filled_order('L',D('.0001'),D(50100),False),success_quantity=D('.0001')),ExecutionResult(False,failure_code='ORDER_STATE_UNKNOWN')])
    assert not run(o._close_position_for_risk(SYMBOL,'loss'))
    assert o.decision_engine.get_position(SYMBOL).total_quantity==D('.0004')
    assert o.decision_engine.get_position('ETH-USDC-PERP').total_quantity==D('.0005')
    assert o.executor.execute_arbitrage.await_count==2
    assert all(call.args[0].symbol==SYMBOL for call in o.executor.execute_arbitrage.await_args_list)


def test_loss_trigger_precedes_normal_profit_gate_and_resets_after_success(orchestrator):
    o=orchestrator
    loss_books(o, buy='103')
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1')))
    o.monitor_only_mode=False; o.executor.monitor_only=False
    o.decision_engine.should_close=AsyncMock()
    async def closed(**kwargs):
        assert kwargs['target_keys']=={SYMBOL}
        o.decision_engine.get_position(SYMBOL).total_quantity=D(0)
        return True
    o.force_close_all_positions_by_grid_sequential=closed
    run(o._check_and_close(SYMBOL,spread('103','100',True),None))
    o.decision_engine.should_close.assert_not_awaited()
    assert SYMBOL in o._position_risk_close_reasons
    assert not run(o._validate_segmented_open(NS(symbol=SYMBOL)))[0]
    # The next quote clears only a completed exit; failed exits remain latched.
    o.monitor_only_mode=True
    o.decision_engine.should_close=AsyncMock(return_value=(False,D(0),'',None))
    run(o._check_and_close(SYMBOL,spread('103','100',True),None))
    assert SYMBOL not in o._position_risk_close_reasons


@pytest.mark.parametrize('quote_state', ['fresh', 'missing', 'stale'])
def test_paused_loop_checks_only_target_loss_with_fresh_books(orchestrator, quote_state):
    from datetime import timedelta
    o = orchestrator
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1'),entry_fee_buy_per_unit=D(0),entry_fee_sell_per_unit=D(0)))
    timestamp = datetime.now() - timedelta(hours=1 if quote_state == 'stale' else 0)
    for exchange, bid, ask in [('lighter','100','100.01'),('arcus','101.99','102')]:
        if quote_state == 'missing' and exchange == 'arcus':
            continue
        o.data_processor.orderbooks[exchange][SYMBOL] = OrderBookData(
            SYMBOL,[OrderBookLevel(D(bid),D('10'))],[OrderBookLevel(D(ask),D('10'))],timestamp)
        o.data_processor.orderbook_timestamps[exchange][SYMBOL] = timestamp
    o.monitor_only_mode = False
    o.executor.monitor_only = False
    o.risk_controller.get_risk_status = lambda: NS(is_paused=True)
    o.force_close_all_positions_by_grid_sequential = AsyncMock(return_value=True)
    o.spread_pipeline.process_symbol = AsyncMock()
    o.spread_pipeline.process_trading_pairs = AsyncMock()
    o.spread_pipeline.process_multi_leg_pairs = AsyncMock()
    o._check_and_close = AsyncMock()
    o._check_and_open = AsyncMock()
    async def stop_loop(_):
        o.running = False
    o.running = True
    with patch('core.services.arbitrage_monitor_v2.core.unified_orchestrator_runtime_mixin.asyncio.sleep', side_effect=stop_loop):
        run(o._main_loop())
    assert o.force_close_all_positions_by_grid_sequential.await_count == int(quote_state == 'fresh')
    if quote_state == 'fresh':
        assert o.force_close_all_positions_by_grid_sequential.await_args.kwargs['target_keys'] == {SYMBOL}
    o.spread_pipeline.process_symbol.assert_not_awaited()
    o.spread_pipeline.process_trading_pairs.assert_not_awaited()
    o.spread_pipeline.process_multi_leg_pairs.assert_not_awaited()
    o._check_and_close.assert_not_awaited()
    o._check_and_open.assert_not_awaited()


def live_budget_setup(o, *, equity='280', existing=(), prices=None):
    """Fresh offline accounts; adapters and prices are explicit test doubles."""
    prices = prices or {SYMBOL:D('50000'),'ETH-USDC-PERP':D('10000'),'SOL-USDC-PERP':D('100')}
    o._bootstrap_positions_block_open = False
    o.decision_engine.exchange_fee_config = {'default':ExchangeFeeConfig(0,0)}
    o._open_only_close_guard.should_block_open = lambda **kwargs:(False,'',None)
    for symbol, quantity in existing:
        view = replace(spread('50000','50000'),symbol=symbol)
        run(o.decision_engine.record_open(symbol,D(quantity),view,filled_quantity=D(quantity)))
    def rows(exchange):
        return [NS(symbol=symbol,size=p.total_quantity,side='long' if exchange=='lighter' else 'short',timestamp=datetime.now())
                for symbol,p in o.decision_engine.iter_pair_positions() if p.total_quantity > 0]
    o.exchange_adapters = {exchange:NS(get_balances=AsyncMock(side_effect=lambda:[NS(currency='USDC',total=D(equity),timestamp=datetime.now())]),
                                      get_positions=AsyncMock(side_effect=lambda ex=exchange:rows(ex)),
                                      get_open_orders=AsyncMock(return_value=[]),_uncertain=set()) for exchange in ('lighter','arcus')}
    o.executor._get_live_market_price = lambda exchange,symbol,is_buy:prices.get(symbol)
    req = ExecutionRequest(SYMBOL,'lighter','arcus',D('50000'),D('50000'),D('.0002'),spread_data=spread('50000','50000'),grid_threshold_pct=D(0))
    position = o.decision_engine.get_position(SYMBOL)
    req._expected_position_quantity = position.total_quantity if position else D(0)
    return req


@pytest.mark.parametrize('quantity,allowed',[('.00069',True),('.00070',True),('.00071',False)])
def test_live_symbol_cap_includes_new_order_and_does_not_double_count(orchestrator,quantity,allowed):
    req = live_budget_setup(orchestrator,existing=[(SYMBOL,quantity)])
    result,reason = run(orchestrator._validate_segmented_open(req))
    assert result is allowed,reason
    if not allowed: assert '本币开仓后名义价值' in reason


@pytest.mark.parametrize('equity,existing,allowed',[('280','.01899',True),('280','.019',True),('280','.01901',False),
                                                    ('190','.018',True),('190','.01801',False)])
def test_live_total_static_dynamic_caps_use_realtime_balance(orchestrator,equity,existing,allowed):
    req = live_budget_setup(orchestrator,equity=equity,existing=[('ETH-USDC-PERP',existing)])
    # Isolate the dynamic limit below 200; the production low-balance rule remains enabled.
    orchestrator._open_only_close_guard._cfg.low_balance.enabled = False
    result,reason = run(orchestrator._validate_segmented_open(req))
    assert result is allowed,reason
    if not allowed: assert '总名义价值' in reason


@pytest.mark.parametrize('fault',['balance_missing','balance_nan','balance_low','positions_missing','orders_missing','orders_pending',
                                  'orders_timeout','quote_missing','quote_nan','other_quote_missing','delayed_snapshot',
                                  'local_ahead','unknown','uncertain','inverted_other'])
def test_live_budget_fail_closed_on_incomplete_or_unreconciled_state(orchestrator,fault):
    from datetime import timedelta
    o=orchestrator
    req=live_budget_setup(o,existing=[('ETH-USDC-PERP','.001')])
    adapter=o.exchange_adapters['arcus']
    if fault=='balance_missing': adapter.get_balances.side_effect=lambda:[]
    elif fault=='balance_nan': adapter.get_balances.side_effect=lambda:[NS(currency='USDC',total=D('NaN'),timestamp=datetime.now())]
    elif fault=='balance_low': adapter.get_balances.side_effect=lambda:[NS(currency='USDC',total=D(199),timestamp=datetime.now())]
    elif fault=='positions_missing': adapter.get_positions.side_effect=lambda:None
    elif fault=='orders_missing': adapter.get_open_orders.return_value=None
    elif fault=='orders_pending': adapter.get_open_orders.return_value=[NS(id='pending')]
    elif fault=='orders_timeout': adapter.get_open_orders.side_effect=TimeoutError('offline timeout')
    elif fault in ('quote_missing','quote_nan','other_quote_missing'):
        o.executor._get_live_market_price=lambda exchange,symbol,is_buy: (None if fault=='quote_missing' or (fault=='other_quote_missing' and symbol!=SYMBOL) else D('NaN') if fault=='quote_nan' else D(50000))
    elif fault=='delayed_snapshot':
        balances=[NS(currency='USDC',total=D(280),timestamp=datetime.now())]
        o.exchange_adapters['lighter'].get_balances.side_effect=lambda:balances
        async def delayed():
            balances[0].timestamp-=timedelta(seconds=6)
            return []
        adapter.get_open_orders.side_effect=delayed
    elif fault=='local_ahead': adapter.get_positions.side_effect=lambda:[]
    elif fault=='unknown': o.executor._unknown_execution_pairs.add('unresolved')
    elif fault=='uncertain': adapter._uncertain.add('unresolved')
    elif fault=='inverted_other': adapter.get_positions.side_effect=lambda:[NS(symbol='ETH-USDC-PERP',size=D('.001'),side='long',timestamp=datetime.now())]
    allowed,reason=run(o._validate_segmented_open(req))
    assert not allowed and reason


@pytest.mark.parametrize('cancel',[False,True])
def test_open_lock_covers_recording_and_releases_after_cancellation(orchestrator,cancel):
    o=orchestrator
    req=live_budget_setup(o,existing=[('ETH-USDC-PERP','.0185')])
    async def scenario():
        entered=asyncio.Event(); release=asyncio.Event(); outcomes=[]
        async def execute_and_record(**kwargs):
            symbol=kwargs['symbol']
            request=req if symbol==SYMBOL else replace(req,symbol=symbol,quantity=D('.15'),spread_data=replace(req.spread_data,symbol=symbol))
            allowed,reason=await o._validate_segmented_open(request)
            outcomes.append((symbol,allowed,reason))
            if symbol==SYMBOL:
                entered.set()
                await release.wait()
            if allowed:
                await o.decision_engine.record_open(symbol,request.quantity,request.spread_data,filled_quantity=request.quantity)
        o._execute_and_record_open=execute_and_record
        async def task(symbol,key):
            o._pending_open_pairs.add(key)
            await o._execute_open_with_lock(symbol=symbol,open_quantity=req.quantity,spread_data=req.spread_data,funding_rate_data=None,
                is_last_split=True,grid_level=1,slippage_pct=None,buy_limit_offset=None,sell_limit_offset=None,position=None,open_key=key)
        first=asyncio.create_task(task(SYMBOL,'first'))
        await entered.wait()
        second=asyncio.create_task(task('SOL-USDC-PERP','second'))
        await asyncio.sleep(0)
        assert len(outcomes)==1
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await first
        else:
            release.set(); await first
        await second
        assert outcomes[0][1] and outcomes[1][1] is cancel, outcomes
        assert not o._pending_open_pairs and not o._pending_open_lock.locked()
    run(scenario())


@pytest.mark.parametrize('coin,quantity,price',[('BTC','.0002','83000'),('ETH','.0075','2200'),('BNB','.02','800'),('SOL','.15','110'),('HYPE','.2','80')])
def test_small_capital_two_levels_and_market_constraints(orchestrator,coin,quantity,price):
    o=orchestrator; symbol=coin+'-USDC-PERP';cfg=o.config_manager.get_config(symbol)
    assert cfg.grid_config.max_segments==2
    assert cfg.quantity_config.base_quantity==D(str(cfg.grid_config.split_order_size))==D(quantity)
    assert cfg.grid_config.initial_spread_threshold==.02 and cfg.grid_config.grid_step==.01
    assert cfg.grid_config.spread_persistence_seconds==3 and cfg.grid_config.strict_persistence_check
    assert cfg.grid_config.require_orderbook_liquidity and D(str(cfg.grid_config.min_orderbook_quantity))==D(quantity)
    assert cfg.risk_config.max_position_value==D(45) and cfg.risk_config.max_loss_percent==D(2)
    o.executor._validate_pair_quantity(ExecutionRequest(symbol,'lighter','arcus',D(price),D(price),D(quantity)))
    assert o._resolve_grid_threshold_pct(symbol,1)==D('.02')
    assert o._resolve_grid_threshold_pct(symbol,2)==D('.03')


@pytest.mark.parametrize('risk_close',[False,True])
def test_normal_and_risk_close_remain_available_above_open_caps(orchestrator,risk_close):
    o=orchestrator
    live_budget_setup(o,existing=[(SYMBOL,'.01')])
    request=ExecutionRequest(SYMBOL,'arcus','lighter',D(50000),D(50000),D('.0002'),is_open=False,
                             spread_data=spread('50000','50000',True),allow_when_global_paused=risk_close)
    allowed,reason=run(o._validate_segmented_close(request))
    assert allowed,reason
    for adapter in o.exchange_adapters.values():
        adapter.get_balances.assert_not_awaited()
        adapter.get_open_orders.assert_not_awaited()


@pytest.mark.parametrize('risk_close',[False,True])
def test_busy_budget_does_not_stall_main_pipeline_or_following_close_checks(orchestrator,risk_close):
    from core.services.arbitrage_monitor_v2.config.multi_exchange_config import TradingPair
    o=orchestrator
    o.decision_engine.exchange_fee_config={'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('.0002'),spread('100','100'),filled_quantity=D('.0002')))
    o.monitor_only_mode=False; o.executor.monitor_only=False
    o.monitor_config.symbols=[]
    o.multi_leg_pairs=[]
    o.multi_exchange_pairs=[TradingPair(SYMBOL,'lighter','arcus',SYMBOL)]
    o.event_driven_analysis_enabled=False
    o.risk_controller.get_risk_status=lambda:NS(is_paused=False)
    o.decision_engine.should_close=AsyncMock(return_value=(False,D(0),'',None))
    o.force_close_all_positions_by_grid_sequential=AsyncMock(return_value=True)
    registrations=[]; cycles=[]
    async def process_pair(pair):
        # Use the real pipeline gather and registration entry that used to wait for the budget mutex.
        registrations.append(await o._try_register_open_pair('next-open'))
        loss_books(o, buy='103' if risk_close else '100')
        await o._check_and_close(SYMBOL,spread('103' if risk_close else '100','100',True),None)
    o.spread_pipeline._process_single_trading_pair=process_pair
    async def next_cycle(_):
        cycles.append(True)
        o._first_analysis_done=False
        if len(cycles)==2: o.running=False
    async def scenario():
        o.running=True
        async with o._pending_open_lock:
            with patch('core.services.arbitrage_monitor_v2.core.unified_orchestrator_runtime_mixin.asyncio.sleep',side_effect=next_cycle):
                await asyncio.wait_for(o._main_loop(),timeout=1)
        assert registrations==[False,False] and len(cycles)==2
        if risk_close:
            assert o.force_close_all_positions_by_grid_sequential.await_count>=1
        else:
            assert o.decision_engine.should_close.await_count==2
        assert not o._pending_open_pairs
    run(scenario())


def test_registration_does_not_wait_during_fair_budget_lock_handoff(orchestrator):
    o=orchestrator
    async def scenario():
        release=asyncio.Event()
        await o._pending_open_lock.acquire()
        async def queued_execution():
            async with o._pending_open_lock:
                await release.wait()
        waiter=asyncio.create_task(queued_execution())
        await asyncio.sleep(0)
        o._pending_open_lock.release()
        assert not o._pending_open_lock.locked() and not waiter.done()
        registration=o._try_register_open_pair('handoff')
        try:
            # It must finish without suspension even while an older lock waiter is waking up.
            with pytest.raises(StopIteration) as completed:
                registration.send(None)
            assert completed.value.value is True
            assert 'handoff' in o._pending_open_pairs
        finally:
            registration.close()
            release.set()
            await waiter
            o._release_open_pair('handoff')
    run(scenario())


def test_completed_pair_cleanup_does_not_queue_behind_another_open_or_block_its_risk_close(orchestrator):
    o=orchestrator
    o.monitor_only_mode=False; o.executor.monitor_only=False
    o.force_close_all_positions_by_grid_sequential=AsyncMock(return_value=True)
    first_key=o._build_open_pair_key(SYMBOL,'lighter','arcus')
    second_symbol='SOL-USDC-PERP'
    second_key=o._build_open_pair_key(second_symbol,'lighter','arcus')
    async def scenario():
        first_started=asyncio.Event(); finish_first=asyncio.Event()
        second_started=asyncio.Event(); finish_second=asyncio.Event()
        async def execute_and_record(**kwargs):
            if kwargs['symbol']==SYMBOL:
                first_started.set()
                await finish_first.wait()
                await o.decision_engine.record_open(SYMBOL,D('.0002'),spread(),filled_quantity=D('.0002'))
            else:
                second_started.set()
                await finish_second.wait()
        o._execute_and_record_open=execute_and_record
        async def execute(symbol,key):
            o._pending_open_pairs.add(key);o._pending_open_pairs_started_at[key]=1
            await o._execute_open_with_lock(symbol=symbol,open_quantity=D('.0002'),spread_data=spread(),funding_rate_data=None,
                is_last_split=True,grid_level=1,slippage_pct=None,buy_limit_offset=None,sell_limit_offset=None,position=None,open_key=key)
        first=asyncio.create_task(execute(SYMBOL,first_key))
        await first_started.wait()
        second=asyncio.create_task(execute(second_symbol,second_key))
        await asyncio.sleep(0)
        finish_first.set()
        await second_started.wait()
        try:
            assert first.done() and not second.done()
            assert first_key not in o._pending_open_pairs and first_key not in o._pending_open_pairs_started_at
            assert second_key in o._pending_open_pairs
            assert await o._close_position_for_risk(SYMBOL,'test loss')
            o.force_close_all_positions_by_grid_sequential.assert_awaited_once()
        finally:
            finish_second.set()
            await asyncio.gather(first,second)
        assert not o._pending_open_pairs and not o._pending_open_lock.locked()
    run(scenario())


def loss_books(o, buy='102', sell='100'):
    for exchange, bid, ask in [('arcus',D(buy)-D('.01'),D(buy)),('lighter',D(sell),D(sell)+D('.01'))]:
        now = datetime.now()
        o.data_processor.orderbooks[exchange][SYMBOL] = OrderBookData(
            SYMBOL,[OrderBookLevel(bid,D('10'))],[OrderBookLevel(ask,D('10'))],now)
        o.data_processor.orderbook_timestamps[exchange][SYMBOL] = now


@pytest.mark.parametrize('bad', ['wide','missing','nan','infinite','zero','crossed','stale'])
def test_loss_rejects_invalid_local_books_and_recovers(orchestrator,bad):
    from datetime import timedelta
    o=orchestrator
    o.decision_engine.exchange_fee_config={'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1'),entry_fee_buy_per_unit=D(0),entry_fee_sell_per_unit=D(0)))
    p=o.decision_engine.get_position(SYMBOL);cfg=o.config_manager.get_config(SYMBOL)
    loss_books(o)
    book=o.data_processor.orderbooks['lighter'][SYMBOL]
    if bad=='wide':book.asks[0].price=D('103.05')
    elif bad=='missing':book.bids=[]
    elif bad=='nan':book.asks[0].price=D('NaN')
    elif bad=='infinite':book.asks[0].price=D('Infinity')
    elif bad=='zero':book.bids[0].price=D(0)
    elif bad=='crossed':book.asks[0].price=D('99')
    else:book.timestamp=datetime.now()-timedelta(hours=1)
    assert o._position_loss_reason(p,spread('102','100',True),cfg) is None
    loss_books(o)
    assert o._position_loss_reason(p,spread('102','100',True),cfg)


def test_loss_uses_validated_snapshot_not_old_spread(orchestrator):
    o=orchestrator
    o.decision_engine.exchange_fee_config={'default':ExchangeFeeConfig(0,0)}
    run(o.decision_engine.record_open(SYMBOL,D('1'),spread('100','100'),filled_quantity=D('1'),entry_fee_buy_per_unit=D(0),entry_fee_sell_per_unit=D(0)))
    loss_books(o,buy='100')
    assert o._position_loss_reason(o.decision_engine.get_position(SYMBOL),spread('103','100',True),o.config_manager.get_config(SYMBOL)) is None


def test_risk_close_rejection_defers_after_recorded_batch_then_only_closes_remainder(orchestrator):
    o=orchestrator
    run(o.decision_engine.record_open(SYMBOL,D('.0004'),spread(),filled_quantity=D('.0004')))
    loss_books(o,buy='50000',sell='50000')
    o.monitor_only_mode=False;o.executor.monitor_only=False;o.executor.max_retry_count=3
    filled=lambda:ExecutionResult(True,filled_order('A',D('.0002'),D(50000),True),filled_order('L',D('.0002'),D(50000),False),success_quantity=D('.0002'))
    o.executor.execute_arbitrage=AsyncMock(side_effect=[filled(),ExecutionResult(False,failure_code='CLOSE_REJECTED'),filled()])
    assert run(o._close_position_for_risk(SYMBOL,'loss')) is False
    assert o.executor.execute_arbitrage.await_count==2
    assert o.decision_engine.get_position(SYMBOL).total_quantity==D('.0002')
    assert SYMBOL not in o._position_risk_close_failed and SYMBOL in o._position_risk_close_reasons
    assert not run(o._validate_segmented_open(NS(symbol=SYMBOL)))[0]
    assert not o._force_close_in_progress and not o._pending_close_symbols
    assert run(o._close_position_for_risk(SYMBOL,'loss')) is True
    assert o.decision_engine.get_position(SYMBOL) is None
    assert [call.args[0].quantity for call in o.executor.execute_arbitrage.await_args_list]==[D('.0002')]*3


@pytest.mark.parametrize('coin,base,split,price', [
    ('BTC','.005','.0025','83000'),('ETH','.15','.075','2200'),
    ('BNB','.5','.25','800'),('SOL','3','1.5','110'),('HYPE','4','2','80')])
def test_capital5000_actual_loader_and_open_split(capital5000_orchestrator,coin,base,split,price):
    o=capital5000_orchestrator;symbol=coin+'-USDC-PERP';cfg=o.config_manager.get_config(symbol)
    assert cfg.quantity_config.base_quantity==D(base)
    assert D(str(cfg.grid_config.split_order_size))==D(split)
    assert D(str(cfg.grid_config.min_orderbook_quantity))==D(split)
    assert cfg.risk_config.max_position_value==D(1000) and cfg.risk_config.max_loss_percent==D(2)
    view=replace(spread(),symbol=symbol)
    allowed,quantity=run(o.decision_engine.should_open(symbol,view,skip_persistence=True))
    assert allowed and quantity==D(split)
    o.executor._validate_pair_quantity(ExecutionRequest(symbol,'lighter','arcus',D(price),D(price),quantity))
    assert cfg.grid_config.max_segments==2


@pytest.mark.parametrize('balance,value,block_type', [
    ('4499.99','0','low_balance'),('4500','0',None),('4800','4800','max_exchange_position_value'),
    ('5000','4999.99',None),('5000','5000','max_exchange_position_value'),('5001','5000','max_exchange_position_value')])
def test_capital5000_actual_static_dynamic_and_low_balance_gates(capital5000_orchestrator,balance,value,block_type):
    guard=capital5000_orchestrator._open_only_close_guard
    guard._get_equity=lambda exchange:D(balance);guard._get_pos_value=lambda exchange:D(value)
    blocked,_,observed=guard.should_block_open(exchange_buy='lighter',exchange_sell='arcus',symbol=SYMBOL)
    assert blocked is (block_type is not None) and observed==block_type
    assert guard._dynamic_multiplier('lighter')==D(1)


@pytest.mark.parametrize('existing,allowed',[('.01749',True),('.0175',True),('.01751',False)])
def test_capital5000_actual_live_symbol_cap_includes_half_grid(capital5000_orchestrator,existing,allowed):
    req=live_budget_setup(capital5000_orchestrator,equity='5000',existing=[(SYMBOL,existing)])
    req.quantity=D('.0025')
    result,reason=run(capital5000_orchestrator._validate_segmented_open(req))
    assert result is allowed,reason


@pytest.mark.parametrize('balance,critical,warning',[('3999.99',True,False),('4000',False,True),('4499.99',False,True),('4500',False,False)])
def test_capital5000_actual_balance_warning_and_serious_priority(capital5000_orchestrator,balance,critical,warning):
    controller=capital5000_orchestrator.risk_controller
    controller.exchange_adapters={name:NS(get_balances=AsyncMock(return_value=[NS(currency='USDC',total=D(balance),timestamp=datetime.now())]),
        get_positions=AsyncMock(return_value=[])) for name in ('lighter','arcus')}
    controller._handle_critical_balance=AsyncMock();controller._handle_low_balance=AsyncMock()
    run(controller._check_all_balances())
    assert controller._min_balance_warning_threshold('lighter')==D(4500)
    assert controller._min_balance_close_threshold('arcus')==D(4000)
    assert controller._handle_critical_balance.await_count==int(critical)
    assert controller._handle_low_balance.await_count==int(warning)
