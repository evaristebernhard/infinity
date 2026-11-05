# 通用任务示例

这里使用同一个 Infinity launcher 和 schema 2 工作图。数学构造只是接入的一个
工具；另一个示例只用 Python 执行来研究整数分拆算法。完整说明见
[通用架构](../../docs/general-agent-architecture.md)。

## 先跑完整的离线流程

在仓库根目录运行：

```bash
python3 -m pip install -e ./research
python3 research/examples/generic-research/offline_demo.py --output /tmp/infinity-generic-demo
```

它真实执行：创建任务 → 拆成两项工作 → 父任务等待 → Python 生成四正方形布局
→ 独立检查 n=4 和 n=5 → 两个子任务完成 → 重新领取父任务并汇合。
输出完整 `report.json`、源码、输入、产物和精确证书。

这是固定策略的集成示例，没有调用模型、没有自主发现新构造。为了在没有 Docker
的开发机上测试，它显式启用只用于已知可信程序的 local 模式。生产生成代码使用
下面的 Docker 配置。

## 用原生 agent 运行小规模精确构造任务

```bash
python3 -m infinity_research.cli init \
  --task research/examples/generic-research/task.json \
  --campaign research/runs/generic-squares-001

cargo run --release -p infinity-research -- \
  --campaign research/runs/generic-squares-001 \
  --research-root research \
  --python python3 \
  --provider openai-compatible \
  --api-base "$RESEARCH_API_BASE" \
  --api-key-env RESEARCH_API_KEY \
  --model "$RESEARCH_MODEL" \
  --workers 100
```

`RESEARCH_API_BASE`、`RESEARCH_API_KEY`、`RESEARCH_MODEL` 由宿主环境设置。
provider 的特殊参数说明见 [launcher 文档](../../../crates/infinity-research/README.md)。
例如当前通用 rig 适配器使用 DeepSeek 时，需要 `--disable-thinking`；DeepSeek
SuperAgent 的组织设计参考并不意味着已经接入它的 harness。

100 是可用 worker 数，启动时只有根任务，会按实际 ready work 唤醒。该配置只
启用精确几何检查，不执行 agent 生成代码，因此不要求 Docker。agent 可以自行
拆分与验证任务；工具只接受精确 `a+b√2` 系数，默认最多检查 64 个正方形。

这是小构造工具示例，不是完整 Kaggle 比赛参赛器。它没有自动维护 1000 个实例
的最优档案、生成最终 decimal CSV 或上传提交。

## 用同一内核研究算法

```bash
docker build -t infinity-generic-python:local research/examples/generic-research

python3 -m infinity_research.cli init \
  --task research/examples/generic-research/algorithm-task.json \
  --campaign research/runs/partition-study-001
```

然后用相同 Rust 命令，把 campaign 路径换成 `partition-study-001`。
此任务只启用 `run_python`，不用几何、训练标签或留出集。初始化固定镜像 ID；
模型不能在运行时更换镜像或安装远程依赖。需要 NumPy、求解器等时，应在启动前
构建相应镜像并修改宿主配置。

`run_python` 的源码入口是 `main.py`。参数依次为 JSON 输入文件、空输出目录；
程序必须写 `result.json`。工具保存所有允许的输出文件和哈希。程序写出的判断
始终是候选输出；有限测试相符也不自动成为一般正确性证明。

## 加自己的 skill

宿主可以导入符合 Agent Skills 格式的本地包：

```bash
python3 -m infinity_research.cli init \
  --task research/examples/generic-research/algorithm-task.json \
  --skills-root /absolute/path/to/reviewed-skills \
  --campaign research/runs/partition-study-002
```

目录可包含多个 `<skill-name>/SKILL.md`，也可直接指向一个包。有 `skills` 字段时
只选其中列出的名称；要使用新 skill，将名称加入任务 JSON。没有该字段时包含
全部发现的包。内置 skill 与导入包同名会报错，需要宿主明确选择版本。

可以参考 skills.sh 的包；导入前由宿主检查来源和内容。其 CLI 可能创建软链接，
应选择真实包目录或使用复制模式。初始化复制并固定所选包；后续源目录变化不
影响已运行的任务。每个包的 scripts 只是资源，读取 skill 不会自动执行它们。

## 状态和恢复

```bash
python3 -m infinity_research.cli status --campaign research/runs/generic-squares-001
python3 -m infinity_research.cli report \
  --campaign research/runs/generic-squares-001 --output /tmp/generic-report.json
python3 -m infinity_research.cli stop --campaign research/runs/generic-squares-001
```

重新运行同一个 launcher 会读取已有工作与证据，过期租约可重新领取。新进程不
重建未持久化的对话。失联工具结果记为未知，不会自动再跑一遍外部动作。
schema 2 没有隐含的留出集 `finalize` 步骤；最终报告按实际工具证据解释。

宿主定期给活跃 native worker 续租，工具运行也续租；模型长时间推理不会单纯
因为默认租期较短而丢掉工作。单次模型调用和整个 campaign 仍各有硬期限。
