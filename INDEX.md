# 项目索引

> 本包 = 「Cyber on Strata」内测包 v3（2026-10-10）+ 本机部署改造（OpenAI 兼容服务、图形控制台、参数调优）。
> 逐项说明如下；部署故事与调参依据见 [README.md](README.md)，原包文档见 [docs/](docs/)。

```
cyber-on-strata-beta-v3-20261010/
├── README.md                  项目 README：硬件/调参/设置/鹈鹕测试
├── INDEX.md                   本索引
├── LICENSE / NOTICE           MIT 许可与第三方清单（原包）
├── SHA256SUMS.txt             原包文件校验清单
│
├── strata.json                ★ 服务配置：引擎参数(args) + 采样(sampling) + 控制台私有块(launcher)
├── serve.cmd                  命令行一键启动服务（python -m serve.server，端口 8090）
├── stop-serve.ps1             命令行停止服务（控制台的「停止服务」按钮可替代）
├── strata控制台.py             ★ 图形控制台：实时看板 / 参数 / 日志 / 一键测速 / 接入信息
├── strata控制台.bat            控制台入口（双击；GBK 编码，勿转 UTF-8）
│
├── serve/                     OpenAI·Anthropic 兼容 HTTP 层（取自上游 fork，纯标准库）
│   ├── server.py              主服务：/v1/chat/completions、/v1/messages、/v1/models、/health
│   ├── frontend.py            消息形态与 chat 模板渲染
│   ├── responses.py           Responses API 支持
│   ├── mcp.py                 MCP 工具挂载（可选）
│   ├── structured.py          结构化输出
│   ├── runconfig.py           配置键校验与热更新
│   ├── telemetry.py / winjob.py / structured.py …
│   └── chat_template.jinja    默认 chat 模板（实际用 pack 内的）
│
├── tools/
│   └── strata_tokenizer.py    纯 python 分词器（读 pack/iq-native/tokenizer/）
│
├── engine/                    ★ strata.exe（v3）+ CUDA 13 运行时 DLL + 分词工具
│                              （体积原因不随 git 分发；引擎源码见 README「致谢」）
├── models/                    ★ 模型权重放这里
│                              （Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf，21.2 GiB，不随 git 分发）
├── pack/iq-native/            专家 pack：dense.bin、tokenizer/、index.txt（本部署派生的 MIT 制品）
├── profile/
│   ├── expert-profile-40x256-trace-cur.bin   作者的专家热度预填表
│   └── serve-learned.bin      本机自学习档案（运行中每 10 分钟刷新）
│
├── logs/
│   ├── serve-engine.log       引擎运行日志（请求 tok/s、命中率、档案保存点）
│   ├── serve-api.log          服务层 stdout（控制台启动时接管）
│   ├── last-run.log           run.cmd 命令行形态的最近一次输出
│   └── pelican_bicycle.svg    ★ 鹈鹕骑车 SVG 基准测试产物（模型真实输出，8 KB）
│
├── tests/
│   ├── smoke-prompt.txt       冒烟提示（"用 python 写一个快速排序"）
│   └── test-req.json          /v1/chat/completions 测试请求体
│
└── docs/
    ├── README-原包.md                          内测包原版说明（物件清单/指纹/授权）
    ├── 先读我.md                               原包三步跑起来 + 已知问题登记
    ├── 发货参数与显卡分档调优-20261010.md        参数总表 / 显卡分档 / 风险清单（出问题先查它）
    ├── 01-工具调用与重复调用-防治.md             本地模型在 harness 里的工具循环防治（平台侧）
    ├── 02-死循环与卡死-防治.md                  死循环/卡死防治（模型侧）
    ├── DSH本地模型重复调用工具问题-完整报告.md    DSH × 三元V3 的诊断报告原件
    └── screenshots/                            控制台五页截图
```

★ = 本部署的核心文件。改 `strata.json` 后需重启服务生效；改引擎参数用控制台的「参数」页最稳（保存前有校验）。
