# 开发与评测

本地测试、Agent 回放与真实模型评测、提交前检查。所有命令在仓库根目录执行。

[返回文档首页](../README.md) · [项目首页](../../README.md)

## 运行测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

外部模型测试使用 Fake Provider 或 HTTPX MockTransport，不会调用真实接口或消耗额度。
Pytest 在收集测试模块前把文件 Handler 固定到当日 `logs/tests/YYYYMMDD-model-calls.log`，不会追加 `logs/runtime/` 下的真实运行日志。

## Agent 评测

仓库内置本地评测系统，用来验证当前“模型 + AGENTS.md + Skill + 记忆 + 工具组 + 审批与工具循环”的整体行为。默认回放模式使用真实 `ChatSession`、SkillRuntime 和工具实现，但由确定性 Provider 驱动，并把每个 Case 放入独立临时工作区；不会访问网络、消耗模型额度或修改当前源码仓库。

运行九类核心回放 Case：

```bash
.venv/bin/python -m app.evaluation run --suite evals/cases/core.jsonl
```

报告生成到被 Git 忽略的 `evals/reports/`，同时包含机器可读 JSON 和中文 Markdown。当前核心用例覆盖直接回答、只读工具、写入批准、写入拒绝、工具错误恢复、调用上限、按需工具披露、显式 Skill 和记忆上下文。

与仓库内已确认基线比较：

```bash
.venv/bin/python -m app.evaluation compare \
  --baseline evals/baselines/core.json \
  --candidate evals/reports/<本次报告>.json
```

总体得分下降超过 3 分、通过率下降超过 5 个百分点，或已有安全 Case 从通过变为失败时，命令返回退出码 1。运行模式不同，或真实评测的供应商/模型不同，不会强行比较。Harness 指纹变化会单独提示，但不会掩盖结果回归。

真实模型评测必须使用不含 `replay_steps` 的独立 JSONL Suite，并显式开启 `--live`：

```bash
.venv/bin/python -m app.evaluation run \
  --suite evals/cases/core-live.jsonl \
  --live \
  --provider deepseek \
  --model deepseek-v4-flash \
  --trials 3
```

未传 `--live` 时不能创建真实 Provider；真实密钥只从项目 `.env` 或已有环境变量读取，CLI 不接受密钥参数。`--trials` 范围为 1–20。真实评测会产生费用，且耗时、Token 只在 Case 明确配置预算时参与通过判定。

可以使用另一供应商或模型为既有报告追加辅助语义评分：

```bash
.venv/bin/python -m app.evaluation judge \
  --report evals/reports/<本次报告>.json \
  --provider aliyun \
  --model qwen3-max
```

Judge 不开放工具，输出到新的 `-judged.json/.md`，Token 与被测 Agent 分开记录。Judge 只能提供正确性、完整性和相关性辅助证据，不能覆盖审批绕过、路径越界等确定性安全失败。

用例使用一行一个 JSON 对象的 JSONL 格式。Case 可声明回放步骤、临时普通文本文件、初始对话、偏好、审批结果，以及文本、上下文、工具、逐步可见工具、文件、错误和成本断言。加载器拒绝未知字段、危险路径、密钥和损坏的角色序列；报告不保存完整系统提示词，只保存角色、哈希和已脱敏的有限工具轨迹。

## 提交前检查

```bash
.venv/bin/python -m compileall -q main.py app tools tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```

仓库当前没有 Formatter、Lint、类型检查或 CI，不要把不存在的命令当作现有质量门禁。
