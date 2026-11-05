# 数学与比赛自主 agent：GitHub 调研

核对日期：2026-11-05。下面核对了仓库、README、许可证信息和部分关键实现；
没有运行第三方模型实验，也没有复现论文分数。优先级按对本项目的帮助排列，
不是榜单排名。不同模型、预算、数据划分下的 medal rate 不宜直接比较。

## 先看这些

| 项目 | 适合的问题 | 最值得借鉴的部分 | 与 Infinity 的关系 |
| --- | --- | --- | --- |
| [ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) | 几何构造、数值优化、可评分算法 | 程序种群、异步生成和评测、可复用操作 skill | 候选搜索器与方法包 |
| [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) | 有独立 evaluator 的程序演化 | 多样性档案、岛屿、程序变异 | 与 ShinkaEvolve 做同任务对照 |
| [Noemesis](https://github.com/kbr-/math-research) | 持续多轮的数学研究 | 命题台账、证据关系、恢复包、独立上下文复核 | 数学记忆与证据协议 |
| [math-research-agent](https://github.com/dongxuelian2/math-research-agent) | 证明任务与研究 campaign | 战略规划和证明执行分开、检查点、可选 Lean | 数学能力接口设计参考 |
| [AIDE](https://github.com/WecoAI/aideml) | 机器学习比赛、快速建立实验循环 | draft / debug / improve 候选树 | 最小可比较的比赛搜索基线 |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | 比赛、量化及数据驱动研发 | 提出假设、实现、评价、反馈的循环 | 通用研究工作流与场景适配 |
| [EvoMaster](https://github.com/sjtu-sai-agents/EvoMaster) | 科学任务、ML-Master 比赛流程 | 工具、skills、记忆、实验，以及运行后的方法改进 | 通用架构的重点参考 |
| [MLEvolve](https://github.com/InternScience/MLEvolve) | 多路线机器学习搜索 | 图搜索、经验检索、跨分支融合 | 搜索策略和失败经验复用 |

## 数学：构造搜索和证明研究需要不同反馈

### ShinkaEvolve

[核对版本](https://github.com/SakanaAI/ShinkaEvolve/tree/8adc053a2ce4511ad2ac310e004c530a73fb974a)，Apache-2.0。
它把候选程序的生成、执行评测和种群管理分开；已有 circle packing 等示例。
可以优先借它的异步搜索组织方式，给我们的正方形构造接独立几何检查器。
circle packing 示例不能直接等同于 Squares in Squares 比赛适配。

源码入口是 `shinka/core/async_runner.py`、`config.py`、`runtime_slots.py`。
仓库还提供 setup / convert / run / inspect 四类 Agent Skills，值得参考工具与方法的分工。
这些方法围绕可计算的评价函数，不自动证明一个数学命题或最优性下界。

### OpenEvolve

[核对版本](https://github.com/algorithmicsuperintelligence/openevolve/tree/73448d715968b3578d8458b16295e827edd2b6bf)，Apache-2.0。
这是第三方的 AlphaEvolve 路线实现；不是 Google 官方公开了完整 AlphaEvolve。
它用模型修改程序，通过 evaluator 筛选，并保存不同类型的候选。
可以与 ShinkaEvolve 使用相同实例、相同检查器、相同调用预算比较搜索表现。

我们需要吸收的是候选表示、谱系、选择和多样性机制。它本身不替代无人值守运行
所需的工作租约、全局资源限制、工具中断记录和证据恢复。

### Noemesis

[核对版本](https://github.com/kbr-/math-research/tree/25239211058ef718402a908b3272f01480c159c4)。
[LICENSE](https://github.com/kbr-/math-research/blob/25239211058ef718402a908b3272f01480c159c4/LICENSE)
区分原始软件 MIT 与研究文字、图表等 CC BY 4.0，不能统一写成“全部 MIT”。

这是围绕 Codex CLI / Claude Code、Git 和研究文件组织的长期数学研究框架。
命题记录区分依赖、修正和反驳；恢复工具提供有哈希的分段上下文；复核使用新的
上下文。实际查看了 `tools/resume.py`：它读取已有研究材料、保存恢复包并校验分段。
这种设计适合解决长跑中遗忘前提、重复失败路线、把猜想写成定理的问题。

自然语言复核、有限计算和形式化检查仍是不同证据。仓库的研究成果描述属于作者报告，
本次没有独立审核其数学证明；也不把这个围绕编码助手的工作环境当成现成分布式服务。

### math-research-agent

[核对版本](https://github.com/dongxuelian2/math-research-agent/tree/6d0f429ccb8517fb3cd756e9ce7e4f8e41f6a240)，MIT。
README 将证明执行和研究规划分成不同 runtime，支持检查点、任务依赖和可选 Lean。
它比较接近“数学专用的 agent 应用”。可以参考命题、证明尝试与审查结果的接口，
但不必将整个应用变成通用调度内核。启用 Lean 环境与自然语言 verifier 是不同配置。

### 两个补充项目

- [LeanAgent](https://github.com/lean-dojo/LeanAgent)：偏持续学习的形式化证明、前提检索和数学库。
  适合作为未来 Lean 侧的研究参考，环境和训练路径较重。核对版本
  `13f63bd8ed42057dbf909791ba39ea6f8759c690` 的根目录未发现 LICENSE，GitHub 也未识别许可证；
  本次不把“代码可见”直接当作已确认可复用授权。
- [ResearchMathAgent](https://github.com/sjtuytc/ResearchMathAgent)：提议、验证、修改循环与研究记忆。
  核对版本 `d1fe055ea6f32a5b7e9efddc288e6bfbd25bb86a` 的 README 标 MIT，根目录未找到对应
  LICENSE 文件；复用具体代码前需要核实。作者报告的证明成绩也不等于本次复现结果。

## 比赛：先固定裁判，再选择搜索方式

### AIDE

[核对版本](https://github.com/WecoAI/aideml/tree/ad73d227af481bea3de06f884f41b2993dd6453a)，MIT。
实际检查了 `aide/agent.py`：`search_policy()` 在新方案、修复失败和改进现有好方案间选择；
`step()` 调用执行器并写入 journal。树上的节点是候选程序，不是不同管理级别的 agent。
这是最适合先做基线的比赛系统，代码结构相对直接。

另一个具体差异是它的反馈模型会解释程序输出并报告 metric。接入本项目时，应继续由
独立宿主评测器计算用于晋级的正式分数，不能让生成程序或它的口头评价担任最终裁判。

### RD-Agent

[核对版本](https://github.com/microsoft/RD-Agent/tree/484776c211e4fbbeef03e0ec00d6bbee7362a4f4)，MIT。
它把提出假设与实现、评价连接成反复迭代的过程，提供 data science、Kaggle、量化等场景。
对我们最有价值的是将具体场景放在适配层，保留通用的研发循环。
阅读入口是 `rdagent/scenarios/data_science`、`rdagent/app/data_science` 及项目文档。

### EvoMaster 与 ML-Master

[EvoMaster 核对版本](https://github.com/sjtu-sai-agents/EvoMaster/tree/50dc0000ebaf7a1dfe8b5fef44c690b2026bf176)，Apache-2.0。
仓库有 `playground/ml_master`、`playground/ml_master_2`，以及多种科学任务示例。
[ML-Master 独立仓库](https://github.com/sjtu-sai-agents/ML-Master)也值得读；具体复用优先核对
EvoMaster 中许可证明确的文件与其依赖。

重点看 [self-evolution 文档](https://github.com/sjtu-sai-agents/EvoMaster/blob/50dc0000ebaf7a1dfe8b5fef44c690b2026bf176/docs/evolution.md)：
完成一轮后分析轨迹，生成本轮目录中的 skill 和 prompt 覆盖层，再运行比较。
工具改进保留为提案，不会直接启用生成的工具代码。当前该功能只支持串行演化，
不能与 `--parallel` 同开；这与系统其他部分支持并行并不矛盾。
我们的后续实现可以借这个试验流程，再补固定案例、对照与晋级条件。

### MLEvolve

[核对版本](https://github.com/InternScience/MLEvolve/tree/f4a37b96c948bfed6564e62f3229e0b38e908d33)，Apache-2.0。
它采用 Monte Carlo Graph Search，配合经验记忆和跨分支融合。这里的“图”描述搜索方案
如何产生和组合，不意味着必须增加 agent 管理层数。适合参考历史失败检索与有根据的
路线合并；候选组合之后依然要重新运行和独立评分。

### Google MLE-STAR：使用已核对的历史代码链接

原 `google/adk-samples` 目前重定向到 [google/adk-recipes](https://github.com/google/adk-recipes)。
核对当前完整文件树时，旧 `python/agents/machine-learning-engineering` 路径已不在其中。
不要将旧 main 链接写成现在可直接执行的安装入口。

仍可读到的官方实现：
[固定提交的 MLE-STAR 目录](https://github.com/google/adk-recipes/tree/c7355fbebe51bd273d131c483a84a713ea84ac37/python/agents/machine-learning-engineering)，
以及 [该版本 README](https://github.com/google/adk-recipes/blob/c7355fbebe51bd273d131c483a84a713ea84ac37/python/agents/machine-learning-engineering/README.md)。
我们实际读取了这个版本；仓库许可证是 Apache-2.0。历史可读不等于已验证其依赖仍能安装。

它先搜索候选方法，再用消融定位影响大的组件并重点改进，最后探索集成；另有错误与
数据泄漏检查。机制依据也可见 [Google 的官方论文页](https://research.google/pubs/mle-star-machine-learning-engineering-agent-via-search-and-targeted-refinement/)。
最值得借鉴的是“用实验定位瓶颈后再改组件”，而不是每轮让 agent 重写全部训练代码。

## 怎样接回 Infinity

先把三件事分开：

1. **agent 组织树**：root → child → grandchild，最多两次委派。
2. **候选搜索图**：一次构造或程序如何修改、组合、回退，可以有很多轮。
3. **证据依赖图**：结论依赖哪些数据、运行、引理或检查，不受管理树形状限制。

当前实现落实第 1 项的限深、绑定、等待和全局资源限制。其他项目没有被自动安装或
整体嵌入。第 2、3 项的进一步实现建议如下：

| 顺序 | 工作 | 怎样判断有效 |
| --- | --- | --- |
| 1 | 用 AIDE 式 draft/debug/improve 建立比赛基线；用固定几何检查器建立构造基线 | 同任务、同评价、可复现 |
| 2 | 借 ShinkaEvolve / OpenEvolve 加候选谱系、多样性和异步搜索 | 相同预算下更好的可行解和更少重复失败 |
| 3 | 借 Noemesis 增加命题与证据台账、路线停止记录 | 重启后能解释每条结论依据及未知部分 |
| 4 | 借 RD-Agent / MLE-STAR 增加假设、消融和定向修改 | 改动与测得收益有可追溯联系 |
| 5 | 借 EvoMaster / MLEvolve 试验 skill 改进和经验检索 | 在未用于编写方法的任务上仍有收益 |

若把外部搜索器包装成 tool，内部模型调用也必须经过共享的模型并发与调用额度入口；
不能只限制外层 agent，而让工具私自再起一个无界模型池。纯数值优化器主要消耗的是
CPU/GPU 实验槽位，应该与 LLM 槽位分别调度。

这部分是后续路线，当前 PR 没有宣称已经集成上述所有搜索器。实现边界见
[分层委派设计](hierarchical-agents.md) 和 [验证记录](../VALIDATION.md)。
