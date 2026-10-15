# Arcus–Lighter 分段默认入口

本地迁移候选，未部署、未启动、未连接真实账户、未实盘验收。默认采用 VAR `main_unified` 分段规则，交易对仅 BTC、ETH、BNB、SOL、HYPE。

| 交易对 | T1 (%) | 格间距 (%) | 最大格数 | 每格数量 | 每笔半格 |
|---|---:|---:|---:|---:|---:|
| BTC | 0.08 | 0.01 | 10 | 0.05 | 0.025 |
| ETH | 0.08 | 0.01 | 10 | 1.5 | 0.75 |
| BNB | 0.01 | 0.01 | 20 | 2 | 1 |
| SOL | 0.08 | 0.01 | 12 | 30 | 15 |
| HYPE | 0.04 | 0.02 | 10 | 150 | 75 |

持续条件五秒；对称退出首格 T1×0.4，其余按源前格门槛。开仓净价差扣往返费用再匹配网格；普通平仓还要求实际 FIFO 入场成本与可成交退出报价估算净收益至少为零。成本缺失拒绝普通止盈；风险退出不受盈利门槛限制，仍需可信订单状态、实仓数量与原配置滑点。

数量及源风险门槛适用于原 VAR 分段体系，不能套用历史 530U、100U单腿、200U总额或一币限制。低权益 20000USDC、余额警告5000、强平15000、总持仓160000与权益倍率5.5均保留源值；配置与账户不匹配会阻止开仓。

配置：`arbitrage_segmented.yaml` 是分段/风控；`arbitrage_lighter_arcus.yaml` 是专用执行；`monitor_lighter_arcus.yaml` 是行情，`multi_exchange_arbitrage.yaml` 是五币双向配对。真实私钥/环境文件由用户自行保管与填写，不放入迁移证据。

当前启动方式（这里只列文档，未执行）：

```bash
bash scripts/start_lighter_arcus.sh monitor
# 完成独立验收且决定实盘时，显式选择：
bash scripts/start_lighter_arcus.sh live
```

脚本实际运行 `main_unified.py --config 临时分段配置 --monitor-config config/arbitrage/monitor_lighter_arcus.yaml --execution-config config/arbitrage/arbitrage_lighter_arcus.yaml`。monitor 只模拟本地分段成交，跳过真实账户仓位恢复。旧 V3 入口保留兼容，不是本次默认策略。

费用配置假设 Arcus 公共 Base maker0/taker0.0225%，Lighter Standard零费。实际账户档位、返佣与真实费用未核实；Plus/Premium可能收费，需要在专用配置确认。Arcus 原生签名/nonce/顶层tick/32天订单expiry与账户WS保留；202/200响应本身不是成交证据。未知结果锁存、撤单终态和最终累计成交须确认。

回滚只按 `backups/var-parity-20261011/manifest.json` 恢复对应 `before/` 文件；`before_sha256=null` 是本次新增文件。不要恢复整个历史备份或覆盖env/data/logs。当前独立门禁与离线测试结果见 `VAR_PARITY_NOTES.md`；未宣称上线或实盘成功。
