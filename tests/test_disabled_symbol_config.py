"""Offline disabled-symbol regression checks; no adapters or real accounts."""
import asyncio
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
from core.services.arbitrage_monitor_v2.config.symbol_config import SegmentedConfigManager
from core.services.arbitrage_monitor_v2.config.multi_exchange_config import MultiExchangeArbitrageConfigManager, TradingPair
from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
from core.services.arbitrage_monitor_v2.core.spread_pipeline import SpreadPipeline
from core.services.arbitrage_monitor_v2.core.unified_orchestrator_execution_utils_mixin import UnifiedOrchestratorExecutionUtilsMixin
from core.services.arbitrage_monitor_v2.decision.unified_decision_engine import UnifiedDecisionEngine
from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import ExecutionRequest

ROOT = Path(__file__).resolve().parents[1]
SYMBOL = 'BNB-USDC-PERP'
PAIR = 'ARCUS_LIGHTER_BNB'


def manager(tmp_path, strict=False, entries=None):
    config = yaml.safe_load((ROOT/'config/arbitrage/arbitrage_segmented.yaml').read_text())
    config['system_mode']['exchange_pair_symbol_config_enabled'] = strict
    config['symbol_configs'] = entries or {}
    path = tmp_path/'segmented.yaml'
    path.write_text(yaml.safe_dump(config))
    return SegmentedConfigManager(path)


def spread(symbol=SYMBOL):
    return SpreadData(symbol, 'lighter', 'arcus', D('100'), D('101'), D('100'), D('100'), D('1'), 1.0,
                      buy_symbol=SYMBOL, sell_symbol=SYMBOL)


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('key,query,alias', [
    (SYMBOL, SYMBOL.lower(), False),
    (SYMBOL, PAIR, True),
    ('BNB', PAIR, False),
    ('BNB', SYMBOL, False),
    ('bnb@lighter-arcus', PAIR, False),
])
def test_disabled_identity_and_custom_exit_config_survive(tmp_path, strict, key, query, alias):
    cfg = manager(tmp_path, strict, {key: {'enabled': False, 'grid_config': {'grid_step': .321}}})
    if alias:
        cfg.register_config_alias(' arcus_lighter_bnb ', ' bnb-usdc-perp ')
    for _ in range(2):
        assert not cfg.is_symbol_enabled(query)
        assert cfg.is_symbol_explicitly_disabled(query)
        assert cfg.get_config(query).grid_config.grid_step == .321
    assert cfg.get_all_configured_symbols() == []
    assert not cfg.symbol_configs
    assert cfg.is_symbol_enabled('UNKNOWN-USDC-PERP') is (not strict)
    assert cfg.get_config('UNKNOWN-USDC-PERP').grid_config.grid_step == cfg.default_config.grid_config.grid_step


@pytest.mark.parametrize('strict', [False, True])
def test_explicit_deny_wins_over_enabled_pair_override(tmp_path, strict):
    cfg = manager(tmp_path, strict, {'BNB': {'enabled': False}, PAIR: {'enabled': True}})
    assert not cfg.is_symbol_enabled(PAIR)


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('disabled_key', [SYMBOL, 'BNB', 'custom_pair'])
def test_real_decision_and_execution_reject_before_account_reads(tmp_path, strict, disabled_key):
    cfg = manager(tmp_path, strict, {'custom_pair': {'enabled': True}, disabled_key: {'enabled': False}})
    view = spread('custom_pair')
    engine = UnifiedDecisionEngine(cfg, deduct_fees=False)
    assert asyncio.run(engine.should_open('custom_pair', view, skip_persistence=True)) == (False, D('0'))
    account_read = AsyncMock(side_effect=AssertionError('disabled leg reached account API'))
    orc = NS(config_manager=cfg, _bootstrap_positions_block_open=False,
             exchange_adapters={'lighter': NS(get_balances=account_read)})
    request = ExecutionRequest('custom_pair', 'lighter', 'arcus', D('100'), D('101'), D('1'),
                               buy_symbol=SYMBOL, sell_symbol=SYMBOL)
    allowed, reason = asyncio.run(UnifiedOrchestratorExecutionUtilsMixin._validate_segmented_open(orc, request))
    assert not allowed and '禁用' in reason
    account_read.assert_not_awaited()


def test_strict_pair_only_config_still_allows_unconfigured_real_legs(tmp_path):
    cfg = manager(tmp_path, True, {'custom_pair': {'enabled': True}})
    engine = UnifiedDecisionEngine(cfg, deduct_fees=False)
    assert not cfg.is_symbol_enabled(SYMBOL)
    assert asyncio.run(engine.should_open('custom_pair', spread('custom_pair'), skip_persistence=True))[0]
    account_read = AsyncMock(side_effect=ValueError('synthetic account unavailable'))
    orc = NS(config_manager=cfg, _bootstrap_positions_block_open=False,
             exchange_adapters={'lighter': NS(get_balances=account_read)})
    request = ExecutionRequest('custom_pair', 'lighter', 'arcus', D('100'), D('101'), D('1'),
                               buy_symbol=SYMBOL, sell_symbol=SYMBOL)
    allowed, reason = asyncio.run(UnifiedOrchestratorExecutionUtilsMixin._validate_segmented_open(orc, request))
    assert not allowed and '账户状态不可用' in reason
    account_read.assert_awaited_once()


def test_disabled_existing_position_still_gets_real_close_decision(tmp_path):
    cfg = manager(tmp_path, entries={SYMBOL: {'enabled': False}})
    engine = UnifiedDecisionEngine(cfg, deduct_fees=False)
    asyncio.run(engine.record_open(SYMBOL, D('1'), spread(), None))
    close = spread()
    close.exchange_buy, close.exchange_sell = close.exchange_sell, close.exchange_buy
    close.price_buy = close.price_sell = D('100')
    close.spread_abs = D('0')
    close.spread_pct = 0.0
    assert asyncio.run(engine.should_close(SYMBOL, close, skip_persistence=True))[0]


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('has_position', [False, True])
def test_disabled_retained_pair_skips_new_opens_but_reaches_close(tmp_path, strict, has_position):
    cfg = manager(tmp_path, strict, {SYMBOL: {'enabled': False}})
    cfg.register_config_alias(PAIR, SYMBOL)
    view = spread()
    orc = NS(config_manager=cfg,
             decision_engine=NS(get_position=Mock(return_value=NS(total_quantity=D('1')) if has_position else None)),
             data_processor=NS(get_orderbook=Mock(return_value=NS())),
             get_data_freshness_seconds=Mock(return_value=10),
             _record_price_sample=Mock(), _capture_decision_snapshot=Mock(),
             _check_and_close=AsyncMock(), _check_and_open=AsyncMock())
    pipeline = SpreadPipeline(orc)
    pipeline._build_spreads_from_position = Mock(return_value=(view, view))
    pipeline._get_funding_rate_data = Mock(return_value=None)
    pair = TradingPair(PAIR, 'arcus', 'lighter', SYMBOL)
    asyncio.run(pipeline._process_single_trading_pair(pair))
    orc._check_and_open.assert_not_awaited()
    assert orc._check_and_close.await_count == int(has_position)
    assert orc.data_processor.get_orderbook.call_count == (2 if has_position else 0)


def test_public_bootstrap_subscribes_only_four_enabled_coins():
    monitor = yaml.safe_load((ROOT/'config/arbitrage/monitor_lighter_arcus.yaml').read_text())
    pairs = MultiExchangeArbitrageConfigManager(ROOT/'config/arbitrage/multi_exchange_arbitrage.yaml').get_pairs()
    expected = {f'{coin}-USDC-PERP' for coin in ['BTC', 'ETH', 'SOL', 'HYPE']}
    assert set(monitor['symbols']) == {pair.normalized_symbol() for pair in pairs} == expected
    orc = NS(monitor_config=NS(symbols=monitor['symbols']), multi_exchange_pairs=pairs,
             multi_leg_pairs=[], data_receiver=NS(subscribe_all=AsyncMock()))
    asyncio.run(OrchestratorBootstrap(orc)._subscribe_market_data())
    orc.data_receiver.subscribe_all.assert_awaited_once()
    assert set(orc.data_receiver.subscribe_all.await_args.args[0]) == expected
    cfg = SegmentedConfigManager(ROOT/'config/arbitrage/arbitrage_segmented.yaml')
    assert not cfg.is_symbol_enabled(SYMBOL)
    assert all(cfg.is_symbol_enabled(symbol) for symbol in expected)
