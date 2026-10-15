# Arcus–Lighter segmented VAR parity candidate

Scope: local source migration only. No process startup, exchange connection, deployment, SSH or real order. Source A and other projects remain read-only. Neither tree has Git; `backups/var-parity-20261011/before`, `manifest.json` and `candidate.diff` are the review/rollback boundary. Restore only listed files; new files have a null before hash.

Default wrapper now invokes `main_unified.py` with the segmented strategy, Arcus monitor profile and an explicit Arcus execution profile. Monitor remains default; `live` explicitly overrides only the temporary segmented profile. BTC/ETH/BNB/SOL/HYPE grid rules, five-second persistence and source risk logic are retained. The authorized 280U capital profile scales quantities and four monetary thresholds as described below. Old 100/200 USD and one-symbol caps are absent from the segmented active path. The generic strategy profiles are Arcus aliases; source VAR browser/request/cookie routes are not selected.

Moved source orchestrator mixins, LatestStore, source data processor/receiver, source UI components, source segmented/config/decision/risk dependencies and shared rotating-log handlers. Kept native Arcus/Lighter adapters, signing, nonce, terminal confirmation and unknown-order latches. Legacy explicit-USD risk fields/methods remain for inactive V3 compatibility, with no such caps configured in the segmented profile.

Intentional corrections: in-flight locks do not expire solely by age; explicit zero fill does not create a position; recovery checks quantity imbalance at zero tolerance; monitor skips real account bootstrap; strict recovery validates balance/positions/open orders/unknown state before adoption. Fee-adjusted opening edge feeds source grid sizing; normal close uses FIFO entry prices and actual entry fee when available (zero is a known fee), otherwise configured entry/exit estimates. Missing entry cost blocks normal profit exit. Risk/forced exit bypasses the profit requirement while native reduce-only and configured slippage protections remain. Fifty-fold batch retry slippage was removed. Additional execution fields, monitoring synthetic orders, final pause gate, WS latency reporting and cumulative confirmed fill valuation bridge source callers to native execution.

Fee assumptions: Arcus public Base taker 0.000225/maker 0; Lighter Standard zero. Actual account tier/fees have not been read or verified. These remain configurable. Public five-market minima/steps are recorded in segmented market constraints; no credentials or account snapshot is used.

Builder final offline verification: all seven repository suites plus 25 independent QA cases passed: 379 passed, 9 deprecation warnings (26.80s). The repository portion is 354 cases. The runner rejects all socket connections, disables file logging/bytecode/cache and runs from `/private/tmp/var-independent-qa-61` using a config symlink; no live entrypoint or credentials are loaded. Exact command: `ENABLE_FILE_LOGS=false PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /private/tmp/var-independent-qa-61/run_qa.py`. Durable builder log: `backups/var-parity-20261011/builder-final-offline.log`.

Updated obsolete seven-market, 100/200 USD, single-position and old Rich terminal expectations to the source five-market segmented contract. Fake successful executions now contain terminal confirmed fill/cost evidence. Added permanent regression checks for account-await quote changes, incorrect real long/short direction, zero-wait cancellation late fills, actual fee zero versus missing fee estimates, unknown single/multiple fill costs, required-account bootstrap failures/imbalance/verified emptiness, and partial emergency/grid exits that retain residual quantity. Strict native protocol/account tests remain enabled. Independent reviewer, QA and security results must be recorded by the coordinator after this source freeze; builder results do not issue final acceptance.

Review commands: `PYTHONDONTWRITEBYTECODE=1 ENABLE_FILE_LOGS=false .venv/bin/python -m pytest -q -p no:cacheprovider tests`; targeted: the same command for `tests/test_var_segmented_parity.py` and native adapter/protocol/strict-state suites. Tests must operate in temporary state/log directories with patched adapter creation; do not import/run the launcher as a live process. Any remaining failures are visible and must be resolved or explicitly classified before acceptance.


## 278/280U 资金配置（2026-10-11）

只调整实际启用的 `config/arbitrage/arbitrage_segmented.yaml`。金额按原配置的1%缩放：余额警告50U、严重不足平仓150U、仅平仓开仓门槛200U、单平台名义仓位检查线1600U。保持原优先级，严重不足检查在警告之前；50U不是150U之前的提前预警。已有总名义价值达到1600U与已有账户余额口径×5.5中的较小值后，限制新开仓；单个split可能越过这条检查线，它不是原子硬上限或最大保证金，5.5不是交易所杠杆设置。

| 币种 | 基础数量 | 拆单数量 | 最大段数 |
| --- | ---: | ---: | ---: |
| BTC | 0.0005 | 0.00025 | 10 |
| ETH | 0.015 | 0.0075 | 10 |
| SOL | 0.3 | 0.15 | 12 |
| HYPE | 1.5 | 0.75 | 10 |
| BNB | 0.04 | 0.02 | 20 |

ETH保留四位数量精度，避免0.0075被截断。BNB例外按市场步长向上取最小可行拆单：当前约750U价格下0.01约7.5U，低于Lighter的10U最小订单金额，因此采用0.04/0.02保持原2:1比例。两所最小数量分别写入配置，共同下限取较大值；动态最小金额与滑点检查保留，行情变化后仍可能拒绝小额订单。Tn阈值、五秒持久性、段数、分段平仓规则及费率配置均未改变。

公开快照和请求时间、HTTP Date、来源哈希见 `backups/startup-fix-20261011/public-market.json`，原始公开响应见同目录 `public-market-raw.json`。来源为 [Arcus公开市场](https://api.arcus.xyz/v1/markets)、[Lighter公开市场](https://mainnet.zklighter.elliot.ai/api/v1/orderBooks)及各币公开订单簿。快照仅验证当时的数量/步长/最小金额，不证明真实成交或利润。Lighter公开Standard费率为零；Arcus公开费率说明未给当前档位数值，既有Arcus费率继续作为配置假设，双方实际账户档位未查询。未设置交易所杠杆、未下单、未启动或停止实盘。
