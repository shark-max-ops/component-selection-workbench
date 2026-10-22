# 器件选型与证据核验工作台

围绕官方资料建立“参数证据 → 程序核验 → 可复查报告”的 ADC 调查流程。当前支持 ADS1115 指定手册的供电单项核验、任务持久化、终端交互和真实工具事件；未完成完整 ADC 选型和人工审核修订。

使用 Python 3.12 和 uv。在 `backend` 下使用现有 `.env` 配置模型，密钥不写入源码或文档：

```powershell
cd backend
uv sync --locked
uv run python -m scripts.run_terminal
```

终端内先 `/tasks`，再 `/open 任务ID` 查看已有任务；也可 `/new 3.3` 创建任务，再发送“开始核验”调用真实模型。`/status`、`/evidence`、`/report` 查询已有记录，`/help` 查看帮助。默认资料位于 `runtime/raw/ads1115.pdf`；可用 `/new 3.3 | PDF路径` 指定资料。

## 目录与阅读顺序

```text
backend/
  app/
    config.py              配置、项目路径
    contracts.py           需求、证据、参数事实、核验结果的共同契约
    cli/terminal.py        用户命令与结果展示
    services/task_service.py  任务创建、查询、执行、校验与归档
    adc/
      constants.py         当前示例的来源地址与 Skill 路径
      extractors.py        指定手册的参数提取
      rules.py             程序执行的供电规则
    agents/
      harness.py           统一创建 Agent
      tools.py             将业务能力包装成工具
      state_sync.py        图状态与运行时业务对象同步
      events.py            真实工具事件记录
      file_permissions.py  内置文件工具访问范围
      messages.py          工具消息读取统计
    infrastructure/
      documents.py         PDF 登记、哈希归档、逐页文本
      llm.py               模型连接
      json_store.py        JSON 文件保存
  scripts/
    run_terminal.py        日常终端入口
    run_persistent_adc.py  持久化命令行入口
    demos/                 早期开发演示实现
  skills/                  Agent 工作规范
  tests/                   契约、业务、权限、持久化与终端验证
docs/                      使用说明、架构、阶段记录
runtime/                   原始资料、解析文本、数据库、任务与运行归档
```

建议按 `contracts.py → cli/terminal.py → services/task_service.py → agents/tools.py → adc/rules.py` 阅读主流程，再看状态同步和存储实现。业务模块无需导入终端，服务无需导入 scripts。

旧的 `scripts.run_adc_agent`、`scripts.run_harness`、`scripts.import_adc`、`scripts.check_agent` 命令保留，转发到 `scripts.demos`。它们用于阶段演示，日常使用终端入口。

## 验证

在 backend 下运行，临时目录每次使用新名字，避免复用旧目录时的 Windows 清理权限问题：

```powershell
uv run ruff check app scripts tests
$testTemp = Join-Path (Resolve-Path ../runtime) ("tests-" + [guid]::NewGuid().ToString("N"))
uv run python -B -m pytest -q -p no:cacheprovider --basetemp $testTemp
```

离线测试不代表真实模型效果；模型执行会产生调用费用。只有已完成任务支持续聊，未完成节点恢复属于后续阶段。

详细文档：[目录与依赖](docs/项目结构.md)、[A1 应用服务](docs/A1应用服务.md)、[A2 终端使用](docs/A2终端交互.md)。阶段文档中的旧源码路径属于历史记录，以当前目录说明为准。`实习.md` 是本地学习记录，当前被 Git 忽略。
