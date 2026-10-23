# 器件选型与证据核验工作台

围绕官方资料建立“参数证据 → 程序核验 → 可复查报告”的 ADC 调查流程。A3 已完成有边界的人工审核、显式暂停/恢复、供电证据提取超时与有限重试、取消请求和一次失败恢复。完整 ADC 选型与任意节点故障恢复仍待后续阶段。

使用 Python 3.12 和 uv。在 `backend` 下使用现有 `.env` 配置模型，密钥不写入源码或文档：

```powershell
cd backend
uv sync --locked
uv run python -m scripts.run_terminal
```

终端内先 `/tasks`，再 `/open 任务ID` 查看已有任务；也可 `/new 3.3` 创建任务，再发送“开始核验”调用真实模型。`/status`、`/evidence`、`/report` 查询已有记录，`/help` 查看帮助。默认资料位于 `runtime/raw/ads1115.pdf`；可用 `/new 3.3 | PDF路径` 指定资料。

终端默认使用中文摘要：`/status` 看状态和下一步，`/evidence` 看参数、条件与原文定位，`/report` 看判定及待办，`/reviews` 看逐版审核摘要。`/raw` 才展开完整 JSON。等待事项会显示可复制的恢复命令，原始业务记录仍完整保存。

`/cancel` 请求在下一工具边界取消当前运行；`/retry` 对检查点已完成、没有待执行节点的失败运行提供一次恢复。供电提取在独立进程中运行，单次超时 15 秒，临时故障最多尝试两次。详见 [A3 完整验收](docs/A3完整验收.md)。

完成首次核验后，先 `/status` 查看参数与 `review_revision`，再 `/evidence` 并对照原 PDF 核对范围、单位和适用条件。人工确认可用 `/review review-001 | 0 | 姓名 | approve | 已核对推荐供电范围及条件`；撤回用新的请求 ID、当前版本和 `reject`。审核立即运行程序规则，不调用模型；`/reviews` 查看版本历史。审核人姓名由操作者声明，当前没有身份认证。详见 [A3 人工审核](docs/A3人工审核.md)。

B1 已在根目录建立 `library/` 资料库：5 份真实手册/应用笔记、3 张器件简介、逐页状态、MarkItDown 对照以及旧任务只读元信息索引。正式证据仍以可定位页码的 PyMuPDF 文本和原 PDF 为准。操作及边界见 [B1 资料库](docs/B1资料库.md)。A 阶段任务仍使用原 `runtime/` 资料。

## 目录与阅读顺序

完成核验后可用 `/wait-review` 暂停等待审核，或 `/ask 问题` 暂停等待澄清。`/status` 显示真实中断 ID；审核等待需先 `/review`，再 `/resume 中断ID | 审核请求ID`；澄清等待用 `/resume 中断ID | 回答`。普通消息不能恢复，暂停后可以退出进程再打开原任务。恢复后会继续调用模型。详见 [A3.2 暂停与恢复](docs/A3暂停与恢复.md)。

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
      library.py           B1 资料库、版本登记和逐页状态
      llm.py               模型连接
      json_store.py        JSON 文件保存
  scripts/
    run_terminal.py        日常终端入口
    library.py             B1 资料导入和查询入口
    run_persistent_adc.py  持久化命令行入口
    demos/                 早期开发演示实现
  skills/                  Agent 工作规范
  tests/                   契约、业务、权限、持久化与终端验证
docs/                      使用说明、架构、阶段记录
library/                   B1 原件、解析产物、cards/*.md 器件简介和目录数据库
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

离线测试不代表真实模型效果；模型执行会产生调用费用。已完成任务支持续聊，显式人工等待点支持恢复；失败恢复只覆盖检查点没有待执行节点的受控场景。

详细文档：[A1 应用服务](docs/A1应用服务.md)、[A2 终端使用](docs/A2终端交互.md)、[A3 完整验收](docs/A3完整验收.md)。阶段文档中的旧源码路径属于历史记录，以当前目录说明为准。`实习.md` 是本地学习记录，当前被 Git 忽略。
