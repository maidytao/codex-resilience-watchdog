# Codex Resilience Watchdog

[![CI](https://github.com/maidytao/codex-resilience-watchdog/actions/workflows/ci.yml/badge.svg)](https://github.com/maidytao/codex-resilience-watchdog/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![许可证：MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

一个只面向 Codex 的 Windows 长任务看门狗：任务卡住时先核验真实结果，再执行严格限次的安全恢复，不盲目重放副作用。

**[English](README.md)**

> [!IMPORTANT]
> 当前为实验版本 `0.1.x`。在重要任务中启用自动恢复前，请先理解下面的安全边界。

## 核心能力

- 只读观察 Codex `logs_2.sqlite`，不修改 Codex 历史数据。
- 使用 SQLite 持久保存任务、检查点、心跳、租约、恢复次数和熔断状态。
- 上一步结果不确定时，先检查文件、SHA-256 或后端终止证据。
- 只有 `read-only` 且显式 `repeatable` 的操作可以自动续跑。
- 写文件、发消息、删除、付费和未知操作绝不自动重放。
- 每个任务最多自动恢复 2 次、验证后重启 Codex Desktop 1 次。
- 同一故障指纹第二次出现时提前熔断，熔断状态跨重启保留。
- 守护进程采用单实例锁，停用后快速退出，审计日志有界轮转。

本项目不会修改 Codex `config.toml`、任务历史、rollout 或项目文件，也不会安装或操作 OpenClaw。

## 环境要求

- Windows 10 或 Windows 11
- 已安装 Codex Desktop，Codex CLI 支持 `codex exec resume`
- Python 3.12 或更高版本
- Windows PowerShell 5.1 或 PowerShell 7

## 安装

```powershell
git clone https://github.com/maidytao/codex-resilience-watchdog.git
cd codex-resilience-watchdog
python -m pip install -e .
python -m unittest discover -s tests -t . -v
```

先预演，不写入任何内容：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -DryRun -Enable
```

正式安装并启用当前用户守护进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Enable
```

无需管理员权限。默认只写入 `%USERPROFILE%\.codex\skills\codex-resilience-watchdog` 和 `%USERPROFILE%\.codex\watchdog`。

## 查看状态

```powershell
$watchdog = "$env:USERPROFILE\.codex\skills\codex-resilience-watchdog\scripts\watchdog.py"
python $watchdog status
python $watchdog incidents --limit 50
```

安装后 Codex 可以自动发现 `$codex-resilience-watchdog` 技能。

## 任务协议

```powershell
python $watchdog arm --task TASK_ID --session SESSION_ID --class ordinary --threshold 300
python $watchdog heartbeat --task TASK_ID --evidence "工具输出已前进"
python $watchdog checkpoint --task TASK_ID --step STEP_ID --effect read-only --repeatable --input-digest DIGEST --probe file-exists --target PATH
python $watchdog complete --task TASK_ID
```

不要在任务 ID、心跳证据或摘要中填写完整提示词、密码、令牌和消息正文。全部状态、效果分类与结果探针见[协议参考](skills/codex-resilience-watchdog/references/protocol.md)。

## 安全边界

| 场景 | 行为 |
|---|---|
| `read-only + repeatable` 且结果确认缺失 | 可以限次自动续跑 |
| 写入、消息、删除、付费或未知效果 | 进入 `pending-confirmation` |
| 同一故障第二次出现 | 进入 `circuit-open` |
| 达到两次恢复或一次重启上限 | 进入 `circuit-open` |
| 无法验证 Codex 主程序身份或窗口无响应 | 不执行重启 |
| 定时器、重复 Thinking 文本 | 不计为真实进度 |

## 日常操作

```powershell
python $watchdog disable
python $watchdog enable
python $watchdog reset-circuit --task TASK_ID --reason "已核验实际结果和故障原因"
```

只有核验真实结果与故障原因后才能重置熔断。重置不会清除恢复计数，也不会让副作用变得可重放。

## 更新与卸载

```powershell
# 覆盖更新并启用
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Enable -Force

# 卸载运行时、技能和启动项，保留状态库与日志
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

# 同时删除看门狗自有数据
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -PurgeData
```

更多设计细节见[架构说明](docs/architecture.md)，参与开发请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

[MIT](LICENSE) © 2026 maidytao

