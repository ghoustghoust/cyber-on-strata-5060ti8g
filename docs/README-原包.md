# Cyber on Strata · 内测包 v3（2026-10-10）—— 引擎 + 组件 + 说明

**一句话**：把 **22.75 GB 的 Cyber-Tiel-Coder-35B-A3B 跑在 8 GB 显存的显卡上** —— 专家权重留在内存、按需上卡，没命中 GPU 缓存的专家**就地交给 CPU 线程池算**。
实测（RTX 4080 SUPER，我们那台机器）：**8 GB 档 57.7–63.5 tok/s**；全常驻档 **134–136 tok/s**。**那些数字不是你的机器**，只作参考。

---

## 一、包里有什么

| 件 | 是什么 |
|---|---|
| `engine\strata.exe` | 引擎（v3；**一份 exe 装四代机器码 75/86/89/120**） |
| `engine\` 的 **6 个 DLL（组件）** | **本包已附带，不用另装 CUDA**：`cublas64_13` · `cublasLt64_13` · `cudart64_13`（CUDA 13.3）＋ `msvcp140` · `vcruntime140` · `vcruntime140_1`（MSVC）<br>⇒ **一个都不能少**：少一个就是 `0xC0000135` —— 窗口一闪、一个字都不打 |
| `engine\tokenize\` | 分词器（`llama-tokenize.exe` 及其依赖 DLL）——**引擎本身不带 tokenizer** |
| `engine\vocab_bytes.tsv` | 反分词表（id → 字节） |
| `pack\iq-native\` | 模型几何与 pack（86.8 MiB） |
| `profile\expert-profile-40x256-trace-cur.bin` | 专家静态预填 profile —— **命中率的主要来源**（不带它实测命中只有 2.97%） |
| `models\` | **放模型的地方**（见第二节） |
| `run.cmd` / `run.ps1` | 启动器（**文本进、文本出**） |
| `先读我.md` | 30 秒自检（含"引擎读得到提示尾部吗"） |
| `发货参数与显卡分档调优-20261010.md` | 分档表与逐项调参（§7 = v2 修订、§8 = v3 修订） |
| `docs\` | **两册防治方案**（工具调用/重复调用 · 死循环）＋ DSH 报告原件 |
| `LICENSE` / `NOTICE` | MIT（Strata fork）＋ 第三方归属 |

---

## 二、模型（**本包不含权重，自己下**）

```
链接：https://huggingface.co/peculiar-ragdoll/Cyber-Tiel-Coder-35B-A3B-GGUF-MTP
文件：Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf
大小：22,749,880,640 B
sha256：0BBCF3CC9BE4C976BAD20E641BAF629DAD9C178D39EBDC9CD72129179943C06A
放哪：解压后丢进 models\
```

> ⚠️ **别下别的量化** —— 上面那个 Q4_K_XL 才是我们测过的那个。启动器默认取 `models\` 下**最大的** `*.gguf`。

---

## 三、怎么跑

```text
run.cmd -Prompt "用一句话解释什么是快速排序"
run.cmd -PromptFile myprompt.txt -MaxNew 512
```

**默认参数就是 8 GB 档，一个都不用改**：`-Reserve 1500 -Context 4096 -MaxNew 256 -Prefill 2048 -Kv int8 -KvResident 0`。
卡更大再按 `发货参数与显卡分档调优-20261010.md` 的分档表动 `-Reserve` / `-Context`（启动器每次会报出**你这张卡的档位建议**）。

🔴 **`-KvResident` 保持 0**：非 0 时引擎只把"被做成常驻的 N 格"交给注意力，**其余 cell 在核心里被直接 mask** ⇒ **丢上下文**（实测 20480 还会卡住提示读取：21,385 token 的提示只读了 18,432，随后 0 输出）。

---

## 四、三条前置

1. **Windows x64 + NVIDIA 显卡**：`sm_75 / 86 / 89 / 120` 都能跑；**更老或非 N 卡不行**。
2. **内存 ≥ 32 GB** —— **16 GB 跑不了**：光钉住的专家银行就要 **18.32 GiB**（48 GB 才舒服）。
3. **显存 ≥ 8 GB**（引擎按 `空闲显存 − reserve` 自动定专家缓存槽数）。

---

## 五、注意事项

1. **只有 sm_89（Ada）是我们实机验证过的**；其余架构是"二进制里确实装了对应机器码"的推断。
2. **本包不含服务端**：该构建没有 `serve` 子命令。要接到 agent 上用，先读 `docs\01`。
3. **中文路径**：v3 把四处文件打开改成"先 UTF-8、失败退回 ANSI"—— **未验**。
4. **长提示会不会丢尾部**：用 `先读我.md` 里的 30 秒自检，别只看"启动没报错"。
5. **显存余量别贴着上限**：`-Reserve` 是唯一必须按显卡改的参数。
6. 上生产前先读 `docs\` 两册与 Release 页面里列的已知问题。

---

## 六、出处、找答案、校验

**仓库**：https://github.com/1314521gjy/cyber-on-strata （MIT，Strata fork）
**本包对应的 Release**：`beta-20261010-v3` → https://github.com/1314521gjy/cyber-on-strata/releases/tag/beta-20261010-v3

- **遇到问题先去仓库里找答案**：`docs\cyber\` 有十篇工程记录（总览 / 构建 / 打包与 profile / 跑起来 / 正确性 / 性能 / 内存账 / 发货参数 / **已知问题** / serve）。
- ⚠️ **但：改引擎需要极大的时间 —— 部署阶段不要动手改。** 部署阶段只做两件事：**按文档绕过去**、**把回执发回来**。
- **许可**：引擎与包内脚本为 **MIT**（见 `LICENSE` / `NOTICE`，Strata fork ＋ ggml ＋ llama.cpp 归属）。
  **本包另附 6 个第三方运行库 DLL**：`cublas64_13` / `cublasLt64_13` / `cudart64_13`（NVIDIA CUDA 13.3 运行库）＋ `msvcp140` / `vcruntime140` / `vcruntime140_1`（Microsoft VC++ 运行库）—— 各自按其原许可随包分发。
  **模型权重**由第三方发布（声明 MIT），**本包不含权重**。
- 校验：

```
certutil -hashfile engine\strata.exe SHA256
```

应为 `7D615BDD48F2947EAA0A450128775B0DBDCF6E9E3DCF4ED22C4982E10392B2BF`，与 `SHA256SUMS.txt` 里那一行逐字符比（清单里路径写成 `engine/xxx`，与 Windows 的 `engine\xxx` 是同一个文件，**只看哈希**即可）。
- **跑不起来请回三样**：完整命令行 · `nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader` · 引擎日志**最后 30 行**。
