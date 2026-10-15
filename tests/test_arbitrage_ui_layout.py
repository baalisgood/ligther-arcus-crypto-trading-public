"""Offline acceptance of the actual Rich V3 dashboard and resize loop."""
import asyncio
from io import StringIO

import pytest
from rich.segment import Segment

from core.services.arbitrage_monitor_v2.config.debug_config import DebugConfig
from core.services.arbitrage_monitor_v2.display.ui_manager import UIManager


SYMBOLS = [f'{symbol}-USDC-PERP' for symbol in ('BTC', 'ETH', 'BNB', 'SOL', 'HYPE')]
REASON = 'lighter: 存在已有持仓或挂单；新开仓被锁定，请人工核对后重启'


def dashboard(record_count=0, monitor_only=False):
    ui = UIManager(DebugConfig())
    ui.set_v3_mode(True, monitor_only)
    ui.config = {'exchanges': ['lighter', 'arcus'], 'symbols': SYMBOLS}
    ui.stats = {'exchanges': ['lighter', 'arcus'], 'symbols_count': 5}
    ui.risk_status = {'open_block_reason': REASON}
    ui.account_balances = {
        exchange: [{'currency': 'USDC', 'total': 30, 'free': 29, 'used': 1}]
        for exchange in ('lighter', 'arcus')
    }
    ui.positions_data = [{'symbol': 'BTC', 'quantity_buy': .0002, 'quantity_sell': .0002}]
    ui.execution_records = [
        {'execution_time': f'12:00:{i:02d}', 'symbol': 'BTC', 'success': True, 'quantity': i * .0001}
        for i in range(record_count)
    ]
    return ui


def frame(console, renderable):
    lines = console.render_lines(renderable, console.options, pad=False)
    return '\n'.join(''.join(segment.text for segment in line if not segment.control) for line in lines)


def assert_dashboard(text, risk_text, monitor_only, record_count):
    assert '套利执行终端 V3' in text
    assert ('监控模式' if monitor_only else '实盘模式') in text
    # Remove whitespace and panel borders so a wrapped reason remains verifiable.
    joined = ''.join(risk_text.split()).replace('│', '')
    assert ''.join(REASON.split()) in joined
    assert '持仓信息' in text and '账户余额' in text
    assert '30.00' in text and '0.0002' in text
    for symbol in SYMBOLS:
        assert symbol.split('-')[0] in text
    assert '执行记录' in text
    assert ('12:00:19' in text) is bool(record_count)
    assert text.startswith('╭') and text.splitlines()[-1].endswith('╯')


@pytest.mark.parametrize('monitor_only', [False, True])
@pytest.mark.parametrize('record_count', [0, 20])
def test_v3_live_resize_preserves_header_accounts_and_quotes(record_count, monitor_only):
    ui = dashboard(record_count, monitor_only)
    output = StringIO()
    ui.console = type(ui.console)(file=output, force_terminal=True, force_interactive=True, _environ={"TERM": "xterm-256color"}, width=213, height=54)
    ui.start()
    sizes = [(280, 100), (320, 110), (320, 120), (280, 100), (320, 110)]
    frames = []
    original_update = ui.live.update
    failures = []

    def update(layout, *, refresh=False):
        original_update(layout, refresh=refresh)
        rendered = frame(ui.console, layout)
        try:
            assert len(rendered.splitlines()) == ui.console.height
            region = layout.map[layout["risk_control"]].region
            lines = ui.console.render_lines(layout, ui.console.options, pad=False)
            risk_lines = [list(Segment.divide(line, [region.x, region.x + region.width]))[1]
                          for line in lines[region.y:region.y + region.height]]
            risk_text = "\n".join("".join(s.text for s in line) for line in risk_lines)
            assert_dashboard(rendered, risk_text, monitor_only, record_count)
        except AssertionError as error:
            failures.append(error)
            ui.running = False
        frames.append(rendered)
        if len(frames) == len(sizes):
            ui.running = False
        else:
            ui.console.width, ui.console.height = sizes[len(frames)]

    ui.live.update = update
    ui.console.width, ui.console.height = sizes[0]
    asyncio.run(ui.update_loop(interval_ms=0))
    if failures:
        raise failures[0]
    assert len(frames) == len(sizes)
    # Source Rich Live owns repainting and restores the terminal cursor.
    assert '\x1b[?1049h' in output.getvalue() and '\x1b[?1049l' in output.getvalue()
    assert '\x1b[?25l' in output.getvalue() and '\x1b[?25h' in output.getvalue()


def test_segmented_layout_uses_source_panels_and_small_frames_remain_bounded():
    from core.services.arbitrage_monitor_v2.display.ui_manager import UIMode
    ui=dashboard()
    ui.set_ui_mode(UIMode.SEGMENTED_GRID)
    for width,height in ((280,80),(70,12)):
        ui.console=type(ui.console)(file=StringIO(),width=width,height=height)
        layout=ui._generate_layout();ui._fill_layout_content(layout)
        rendered=frame(ui.console,layout)
        assert len(rendered.splitlines())==height
        assert layout['pair_holdings'] and layout['exchange_holdings'] and layout['prices']
        if width==280:
            assert '套利对持仓' in rendered and '交易所总持仓' in rendered
            assert '新开仓受限' in rendered
            for symbol in SYMBOLS: assert symbol.split('-')[0] in rendered


def test_terminal_safe_rendering_preserves_status_style_and_cursor_controls():
    from rich.console import Console
    from rich.control import Control
    from rich.text import Text

    console = UIManager(DebugConfig()).console
    status = Text('⚡ 实盘模式 🛡️ 风险 ⏸️ 错误避让', style='bold yellow')
    actual = list(console.render(status))
    original = list(Console().render(status))
    rendered = ''.join(segment.text for segment in actual)
    assert '实盘模式' in rendered and '风险' in rendered and '错误避让' in rendered
    assert '⚡' not in rendered and '🛡' not in rendered
    assert sum(segment.cell_length for segment in actual) == sum(segment.cell_length for segment in original)
    assert [segment.style for segment in actual] == [segment.style for segment in original]
    cursor = Control.home()
    assert list(console.render(cursor)) == list(Console().render(cursor))


def test_execution_success_and_failure_remain_visible_and_distinct():
    ui = dashboard()
    ui.console = type(ui.console)(file=StringIO(), width=213, height=54)
    records = [
        {'execution_time': '12:01:01', 'symbol': 'BTC', 'success': True},
        {'execution_time': '12:01:02', 'symbol': 'ETH', 'success': False},
    ]
    rendered = frame(ui.console, ui.components.create_execution_records_table(records))
    success = next(line for line in rendered.splitlines() if '12:01:01' in line)
    failure = next(line for line in rendered.splitlines() if '12:01:02' in line)
    assert '✓' in success and '✗' not in success
    assert '✗' in failure and '✓' not in failure


def test_position_alignment_and_quantity_warning_keep_semantic_markers():
    ui = dashboard()
    ui.console = type(ui.console)(file=StringIO(), width=213, height=54)
    rows = []
    for consistent in (True, False):
        ui.alignment_summary = {'consistent': consistent}
        rendered = frame(ui.console, ui._build_alignment_panel())
        rows.append(next(line for line in rendered.splitlines() if '状态' in line))
    assert '✅' in rows[0] and '⚠' not in rows[0]
    assert '⚠' in rows[1] and '✅' not in rows[1]
    mismatch = ui.components.create_positions_table([
        {'symbol': 'BTC', 'quantity_buy': .0002, 'quantity_sell': .0004},
    ])
    rendered = frame(ui.console, mismatch)
    assert '0.0002⚠' in rendered and '0.0004⚠' in rendered
