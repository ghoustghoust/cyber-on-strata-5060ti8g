# DSH 本地模型重复调用工具问题 —— 完整诊断与解决报告

> 整合自 2026-10-08 的四份诊断报告与修复包文档
> 模型：qwen3.8-27b（三元V3，本地 ninfer 8094）· harness：DSH（DeepSeek Harness 桌面端）
> 附带 WorkBuddy 侧同类案例作跨 harness 对照
> 整合日期：2026-10-11

---

## 0. 问题一句话

**本地量化模型（三元V3/qwen3.8-27b）在 agent 环境里遇到确定性失败后不会换路，而是反复重发字节相同的调用（最多 200+ 次），且 harness 的防护"只提醒不阻断、提醒还会静默"，最终只能靠人手动中断。**

---

## 1. 问题现象（三个实证案例）

### 案例 A：WorkBuddy 侧 —— 404 重试循环（session 104da539）

| 指标 | 值 |
|---|---|
| WebFetch 调用 | 42 次，只对应 **9 个签名**（重复率 83%） |
| 最重的两个 | Data-Management ×18、Settings ×17（字节完全相同） |
| 结束方式 | 110s 后 `error`（人工中断，非熔断生效） |

- 触发：两个页面的 WebFetch 恒定返回 404（确定性终态），模型当瞬时失败反复重发。
- 上下文在第 8 轮后冻结在 99,998 字符（滑动窗口上限），**只剩最近 7 轮**，"我已失败 N 次"的累积证据被挤出窗口。
- 21 轮全部 `finish_reason: tool_calls`，模型从未主动 `stop`。

### 案例 B：DSH 鹈鹕骑车测试 —— 幻觉工具名 + SSRF 硬拦双循环（session-533ca96f）

| 指标 | 值 |
|---|---|
| 单轮步数 | **368**，其中 **343 次（93%）确定性失败** |
| Loop A（step 23–225） | 调用不存在的工具 `todo` **202 次**，其中 200 次参数完全相同（正确名 `todo_write`，step 1 曾成功用过） |
| Loop B（step 227–367） | web_fetch 抓自己的 localhost 页面 **139 次**（68× 127.0.0.1 + 67× localhost），全部被 `WEB_BLOCKED_URL` SSRF 防护硬拦 |
| 结束方式 | `turn/end aborted/user` —— 人工中断 |

**DSH 自带防护的实测表现（关键发现）**：
- `repeat-tool-reminder` 在 step 25/27/30 触发 3 组提醒，措辞已很硬（"Do not call this tool with these exact arguments again"）；
- 但 **step 30 之后永久静默**，后面约 330 次重复在零提醒状态下发生；
- 且它只提醒、不阻断。
- 5 次 compaction 反而是**放大器**：最后一份摘要里 `WEB_BLOCKED`/`blocked`/`localhost`/`失败` 各出现 **0 次**；还编造事实（"todo_write also returned the same error"，实际成功）、把状态重置回"文件还没写"⇒ 模型每次"重新开始"都写同一份 todo 清单。

### 案例 C：DSH 实时诊断 —— 网络不可达循环（session-83a8b76d）

- 同一个 `web_fetch`（huggingface.co API）字节相同重复 **14 次**，74% 调用返回 `fetch failed`。
- 本机实测：`nslookup huggingface.co` 解析到 **Dropbox 的 IP（DNS 污染）**，`curl` 10s 超时 ⇒ **这台机器根本连不上 HF**，每次 fetch 必然失败。
- 提醒机制现场复现了"3 次后静默"缺陷（此时 P0 未装）。

---

## 2. 跨模型对照：是不是模型的锅？

**方法**：同一套 harness（DSH）、同一份 42 工具清单、同一份 system prompt，换模型对比。

| 会话 | 模型 | 步数 | 调用 | unknown tool | 撞 SSRF | 完全重复调用 |
|---|---|---|---|---|---|---|
| 4fe0ca28 | deepseek-flash | 52 | 78 | **0** | 0 | **0** |
| c5c3ca01 | deepseek-flash | 41 | 105 | 0 | 1（撞 1 次即换路） | **0** |
| 533ca96f | **qwen3.8-27b** | 368 | 374 | **202** | **141** | **200** |

**决定性证据**：`request/header` 里发给模型的工具清单是 **42 个，含 `todo_write`、不含 `todo`** ⇒ `todo` 是模型**幻觉的工具名**（用了 201 次）。

**结论：主因在模型本身**，但差异不在"会不会撞墙"（flash 也撞同样的墙），而在**"失败是否触发策略更新"**：

- flash 失败 → 换工具类别 / 换路径 / 换数据源，甚至**向用户申请权限**（DSH 的 escalate 机制，flash 撞 3 次就申请了，三元V3 撞 22 次审批事件数为 0）；
- 三元V3 失败 → 原样重发，仅有的"变化"是局部 token 变体（localhost↔127.0.0.1 换 139 次）。

**机制假设（置信度中高，因果链未做对照实验）**：
- 三元V3 包 `Ternary-Bonsai-2-27B-ninfer-v3.ninfer` 仅 8.87 GiB（等效 ~2.6 bit/权重的三值量化）——"从 42 个工具里精确召回正确名字"正是极低比特量化最容易损伤的能力：保语义、丢精确符号。模型不是不会用工具（同会话 `todo_write`/`web_fetch`/`edit`/`write` 都调对过）。
- 训练分布干扰：`todo` 是各类 agent 框架最常见的工具名，模型可能从训练分布"顺手补全"。
- 不主动收敛（高置信）：WorkBuddy 21 轮 + DSH 368 步里从未主动输出终止答复，把"何时停止"完全外包给 harness。

---

## 3. 根因分析（分层归因，别混为一谈）

| 层 | 角色 | 内容 |
|---|---|---|
| **触发层** | 环境（常见）或模型 | 每次循环的具体触发点：404、工具名不存在、SSRF 硬拦、沙箱 ACL 拒绝、DNS 污染导致网络不可达 —— 大多是"确定性失败" |
| **放大层·模型** | 三元V3 | 把终态失败当瞬时失败重发；不会换路、不会申请提权、不主动收敛 |
| **放大层·harness** | DSH | `repeat-tool-reminder` 阈值 `[3,5,8]` ⇒ **第 8 次后永久静默**；官方定位 advisory **不阻断**；guard 家族没有任何阻断型包 |
| **加剧层·上下文管理** | 两套 harness | WorkBuddy 滑动截断挤掉失败证据；DSH 摘要式 compaction **漏记失败 + 重置状态 + 编造事实** |

> ⚠️ 方法论教训：初版报告曾凭 asar 关键词搜索断言"DSH 没有重复检测"，后实测发现有（只是软且静默）——**别凭"没搜到"就断言"不存在"**。同样，曾误判"hook 在某工作区跑不了是沙箱问题"，实际是 PowerShell 引号语法错误；曾误判"必须手动 icacls"，实际用户批准 DSH 升权请求即可。

**共同根因三件套**（WorkBuddy 与 DSH 通用）：
1. 模型把确定性失败当可重试失败；
2. 工具错误不回结构化 `retryable:false` 信号（404 甚至以 `isError:false` 返回整页正文）；
3. 两套 harness 都没有强熔断（一个没有提醒，一个提醒太软且静默）。

---

## 4. 解决方案

### 4.1 设计思路

- 不改 app.asar、不写原生插件，用 DSH 自带的 **hook 桥**（`@deepseek-ai/dsh-hooks-claude-code`）挂 Claude Code 格式的 `hooks.json`；
- **`PreToolUse` 退出码 2 + stderr = 阻断且理由模型可见**（最稳路径，不依赖 JSON 字段名）；
- 时序依据：`ToolNotFoundError` 抛在 `dispatchToolBody` 内、晚于 `tools/execute` 拦截缝 ⇒ "工具名不存在"也能被 PreToolUse 拦到。

### 4.2 最终版 guard（`dsh-p0-guard.py`，Pre + Post 双时机）

⚠️ 注意熔断判据有一版重要修正：初版按"同参数连续 3 次"判，会**误伤合法轮询**（如反复读正在写入的日志，内容每次在变）。最终判据改为 **「同参数 + 同失败结果」**：

| 时机 | 职责 |
|---|---|
| **PreToolUse** | ① 工具名白名单：清单外的名字直接阻断 + difflib 给最近候选（`todo` → `todo_write`；对不存在的名字阻断**零误报**）；② 熔断：读状态，若当前签名连续失败 ≥3 次 → 阻断并指路 |
| **PostToolUse** | ① 只在结果为失败时计数，**成功即清零**（合法轮询不误伤）；② 第 2 次相同失败起用 `decision.feedback` **改写结果文本**，给出绕行地图；第 5 次起转硬措辞要求收尾 |

- **失败签名** = sha1(结果前 300 字符去数字归一化) ⇒ 同类错误（带时间戳/耗时也算同一种）。
- **绕行地图 HINTS**：禁非公网 IP → pwsh+read_image；fetch failed → hf-mirror.com；ACL 拒绝 → 申请升权；404 → 换源；unknown tool → 核对拼写；timed out → 缩小范围。
- 安全设计：检测到违规 fail-closed；脚本自身异常/脏输入 fail-open（exit 0 放行），绝不因 guard 出错卡死 agent。
- 配套：`repeat-tool-reminder` 的 `thresholds` 阈值密集化（治提醒密度），hook 提供阻断（治硬熔断），两者互补。
- 自测 22/22 通过（含关键回归 B1–B7：同参数轮询 6 次 + pre 同参数，全程放行）。

### 4.3 配套 AGENTS.md（`C:\工作文件\DSH工作区_1\AGENTS.md`）

- 「工具失败处理协议」：4 类失败判定表 + 3 条行为原则（同参数重复≠有进展 / 失败后扩大检索半径 / 允许如实报告做不到）；
- 「本机环境事实（绕行地图）」：huggingface.co 不可达 → hf-mirror.com（同仓库同路径实测 200/1.0s）；web_fetch 禁本地 IP；命令类工具依赖沙箱授权（缺权限时申请升权）等 —— 让模型开局就知道墙在哪。

### 4.4 已安装（2026-10-08 05:17）

- `~/.dsh/hooks/dsh-p0-guard.py` + `hooks.json`（Pre + Post 两时机，**命令不带引号**，见踩坑 1）
- desktop profile 的 `cordis.patch.yml` 追加 P0 段（备份：`cordis.patch.yml.bak-p0-20261008-051708`）
- 回滚：还原 `.bak` 或删两条 patch 即可；`~/.dsh/guard-state/` 是纯状态目录，删掉无副作用。

### 4.5 实战踩坑（已解决，复用价值高）

| # | 坑 | 结论 |
|---|---|---|
| 1 | **PowerShell 引号语法错误**：hook 桥用 PowerShell 执行 command，`"a" "b"` 并排引号串是语法错误，且**解析错误中止整个脚本**（表现为 hook 静默不执行） | 路径不含空格就直接不带引号：`C:/Python314/python.exe C:/Users/MSI/.dsh/hooks/dsh-p0-guard.py` |
| 2 | **hook 执行路径不受工作区沙箱 ACL 影响** | 同工作区 pwsh 100% 失败，但 hook 的 PowerShell 能正常启动 ⇒ 两件事走不同代码路径 |
| 3 | **沙箱 ACL 失败的正解**：`C:\工作文件\` 缺 WRITE_OWNER，DSH 会自己申请 escalate to danger-full-access | **批准 DSH 的升权请求即可**，无需手动 icacls（该问题用户已实际修复） |
| 4 | **CLI-headless 全部命令被沙箱拒（134/134）**：`workspace-write` 的 Windows ACL runner 起不来 | 沙箱问题与工作区 ACL 相关，与启动方式无关；CLI headless 做不了命令类实验 |
| 5 | patch 语法 | 改已有条目用 `- id:`；新增用 `- insert: [...]`；`entry not found` = 拿不存在的 id 去改 |
| 6 | Windows 文本模式写文件 | Python 默认写 `\n` 会变 CRLF，DSH 的 YAML 是 LF 敏感，追加后要归一化换行符 |
| 7 | hook 耗时 | 实测约 900–1000 ms/次（PowerShell + Python 启动），嫌慢应改 Node |

---

## 5. 生效验证方法（重启 DSH 后）

```bash
# 1. hook 是否在跑：状态目录应出现会话文件与 _invocations.log（文件空 = 没跑）
ls -la ~/.dsh/guard-state/

# 2. 阻断是否生效：新会话里 unknown-tool 事件应为 0
#    （用 stream_reader 解 zstd，普通 decompress 只能读到 1 行 header）
```

---

## 6. 遗留待办（诚实标注）

| 项 | 状态 |
|---|---|
| **桌面端装 P0 后的实际效果验证**（跑一遍必现循环的任务） | ⚠️ **未完成**——判断依据是 `~/.dsh/guard-state/_invocations.log` 有内容 |
| PreToolUse 对未注册工具名是否真触发 | ⚠️ 中高置信（时序依据成立），**未实测** |
| "三值量化 → 工具名幻觉"因果链 | ⚠️ 假设，需同模型高比特档（gsq，13.99 GiB ≈ int4）A/B 对照 |
| 三元V3 上下文窗口调低（127K → 32–64K）对工具错误率的影响 | ⚠️ 未测 |
| 更彻底的模型侧修复（工具返回加 `retryable:false` 结构化终态、compaction 保留失败账本） | 属 harness/模型侧改造，本次仅以 hook 层兜底 |

**预期管理**：P0 只能把重复压到 3 次后熔断终止，**不会让本不可达的请求成功**。触发点若是环境问题（如 HF 被墙），仍要靠换源（hf-mirror）或用本地已有信息解决。

---

## 7. 总结

### 7.1 一图看懂归因

```
环境确定性失败（404/工具不存在/SSRF/ACL/断网）   ← 触发（多为环境）
        ↓
模型不换路，原样重发字节相同调用               ← 放大（模型：三元V3 量化丢精确符号 + 不收敛）
        ↓
提醒式防护 3 次后静默 + 只提醒不阻断           ← 放大（harness：thresholds [3,5,8] 缺陷）
        ↓
上下文管理抹掉"已失败 N 次"的证据             ← 加剧（滑动截断 / compaction 重置状态）
        ↓
     无限循环，直到人手动中断
```

### 7.2 最终方案一览

| 层 | 措施 | 状态 |
|---|---|---|
| 熔断（hook） | Pre/Post 双时机 guard：白名单 + 「同参数+同失败结果」计数熔断 + 失败信息改写带绕行地图 | ✅ 已装，自测 22/22 |
| 提醒密度 | `thresholds` 阈值密集化（patch 覆盖层） | ✅ 已装 |
| 环境事实 | AGENTS.md 写明绕行地图 + 失败处理协议 | ✅ 已写 |
| 模型选型 | 云模型（flash）同环境零问题；三元V3 可试 gsq 高比特档 | 📌 建议 A/B |
| 换源等环境修复 | HF → hf-mirror.com；本地信息优先 | ✅ 已验证可行 |

### 7.3 可复用的通用规律

1. **这类循环的触发点大多是"环境里的确定性失败"**——与其反复加固 agent，不如在工具层就把终态失败标注为不可重试。
2. **模型的差别不在"会不会撞墙"，而在"失败是否触发策略更新"**——云模型会换路/换源/向人要权限；三元V3 只有局部 token 变体。
3. **「向人申请权限」是换路集合里最容易被低估的一员**——三元V3 撞 22 次从没想过申请提权，flash 撞 3 次就申请了。
4. **别凭"没搜到"断言"不存在"、别把"启动失败"想当然归因**——两次误判（DSH 无熔断 / hook 败于沙箱）都被实测推翻，正确答案是提醒静默 + PowerShell 引号坑。
5. **压不等于救**：WorkBuddy 的滑动截断和 DSH 的摘要 compaction 都会把"我已经失败 N 次"的证据挤掉——上下文管理必须保留失败账本，否则等于帮模型失忆、重启循环。
