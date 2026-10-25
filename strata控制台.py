# -*- coding: utf-8 -*-
"""
strata 控制台 —— Cyber-Tiel-Coder-35B-A3B (strata serve) 图形控制台
==============================================================================
仿照 3.8_27b 的 ninfer 控制台为本包（cyber-on-strata-beta-v3）定制：

  * 实时看板 : GPU(nvidia-smi) / CPU·内存(psutil，缺失降级) / 服务与引擎进程
               / 健康探测(/health) / 实测吞吐(解析 serve-engine.log 的请求行)
  * 参数     : strata.json 表单 —— 只收录引擎与服务层真实存在的键，
               表单未覆盖的 args 元素原样保留；保存前校验、argv 预览
  * 日志     : 引擎日志(serve-engine.log)增量渲染
  * 一键测速 : 1000 token "数数字"快测 + 超长上下文测速(上限 90% 预算自动收缩)；
               读数 = HTTP usage + 引擎日志完成行（tok/s / 命中率），接数自动判定
  * 接入信息 : base URL / 端点 / DSH(our-free-model 聚合渠道) 配置步骤

依赖：标准库 + psutil(可选，缺失降级不崩溃)。jinja2/regex 由服务自身需要（已装）。
运行：双击 strata控制台.bat，或 python strata控制台.py
==============================================================================
"""

import os
import re
import sys
import json
import time
import shutil
import socket
import threading
import subprocess
import statistics
from pathlib import Path
from collections import deque
from urllib import request as urlrequest
from urllib import error as urlerror

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ============================================================================
# 路径与常量
# ============================================================================
ROOT = Path(__file__).resolve().parent
ENGINE_EXE = ROOT / "engine" / "strata.exe"
ENGINE_LOG = ROOT / "serve-engine.log"
API_LOG = ROOT / "logs" / "serve-api.log"          # 本程序启动服务时的 stdout/stderr
CONFIG_FILE = ROOT / "strata.json"
LOG_DIR = ROOT / "logs"

DEFAULT_PORT = 8090
HEALTH_TIMEOUT = 3.0
MEM_WARN_GIB = 22.0        # 可用内存低于此值时启动前警告（页缓存 + 服务 + 系统的底线）
REFRESH_MS = 1000
MEDIAN_WINDOW = 40
REQ_MAX = 400              # 请求表最多保留行数
LOG_VIEW_MAX = 20000

FONT_UI = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)
FONT_TITLE = ("Microsoft YaHei UI", 9, "bold")

# ============================================================================
# 引擎参数（strata.exe --help 实际存在的键）
# ============================================================================
# 表单管理的带值参数：表单值优先，重组时替换/插入；未在表单出现的 args 元素原样保留
VALUED_ARGS = [
    "--max-context", "--vram-reserve-mib", "--kv", "--prefill",
    "--pool-workers", "--pool-affinity",
    "--expert-profile", "--expert-profile-save", "--expert-profile-save-every",
    "--prompt-cache", "--batch",
]
# 表单管理的布尔/单选参数（互斥组单列）
BOOL_ARGS = ["--vram-elastic"]
PLACEHOLDER_CHOICES = ["", "--resident-experts", "--mmap-experts"]
# 固定保留、不进表单的元素（与 run.ps1 的 8G 档一致）
FIXED_ARGS = ["--pack", "--native", "--no-ple", "--expert-cache",
              "--spec", "--suffix-draft", "--mtp-max-t", "--stats"]
KV_CHOICES = ["fp16", "int8", "k8v4", "q4_0"]
AFFINITY_CHOICES = ["auto", "all", "p-cores"]

# strata.json 顶层键（serve/server.py 真实读取的；端口不在此——它只来自 CLI --port）
TOP_KEYS = ["exe", "cwd", "log", "tokenizer", "model_name", "fit_max_tokens",
            "engine_silence_s", "reasoning_budget_tokens", "effort_position",
            "lazy_load", "host"]
TOP_CHOICES = {"effort_position": ["start", "end"]}
SAMPLING_KEYS = ["temperature", "top_p", "top_k", "min_p"]

# 表单元数据：(key, 中文标签, 类型, 提示)  类型: str/int/float/bool/path/choice
ARG_FIELDS = [
    ("max-context",               "上下文窗口",     "int",   "KV+状态容量；KV int8 下 32k 仅约 345 MiB 显存"),
    ("vram-reserve-mib",          "显存预留 MiB",   "int",   "给 KV/激活/桌面让出的显存；8G 档 1500"),
    ("kv",                        "KV 精度",        "choice",KV_CHOICES, "fp16/int8/k8v4/q4_0"),
    ("prefill",                   "预填块",         "int",   "批式预填块大小；mmap 模式引擎可能自动再收缩"),
    ("pool-workers",              "CPU 池线程",     "int",   "0 = 自动"),
    ("pool-affinity",             "池核绑定",       "choice",AFFINITY_CHOICES, "auto/all/p-cores"),
    ("expert-profile",            "专家预填档案",   "path",  "R4.2e：显存层预填来源"),
    ("expert-profile-save",       "自学习档案保存", "path",  "#477：定时/退出时保存学到的路由"),
    ("expert-profile-save-every", "自学习间隔分钟", "int",   "0 = 仅退出时保存"),
    ("prompt-cache",              "会话检查点数",   "int",   "--serve 专用；批切分非数值中性(答案可能与命令行不同)"),
    ("batch",                     "并发批槽",       "int",   "--serve opt-in 2..8；8G 卡单请求吃满，一般留 0"),
    ("vram-elastic",              "显存弹性分段",   "bool",  "#533：空闲可 POST /v1/vram 还显存"),
    ("__placeholder",             "专家放置",       "choice",PLACEHOLDER_CHOICES, "空=默认钉住 / resident=低内存页锁定 / mmap=纯映射"),
]

LAUNCH_DEFAULTS = {"port": DEFAULT_PORT, "idle_unload_s": 0, "api_monitor": False}


# ============================================================================
# strata.json 读写与 args 重组
# ============================================================================
def blank_config():
    return {
        "exe": "engine/strata.exe",
        "cwd": ".",
        "log": "serve-engine.log",
        "tokenizer": "pack/iq-native/tokenizer",
        "model_name": "Cyber-Tiel-Coder-35B-A3B",
        "fit_max_tokens": True,
        "engine_silence_s": 0,
        "reasoning_budget_tokens": 4096,
        "effort_position": "start",
        "args": [
            "--pack", "pack/iq-native",
            "--native", "models/Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf",
            "--resident-experts",
            "--no-ple",
            "--expert-profile", "profile/expert-profile-40x256-trace-cur.bin",
            "--expert-profile-save", "serve-learned.bin",
            "--expert-profile-save-every", "10",
            "--expert-cache", "auto",
            "--vram-reserve-mib", "1500",
            "--prefill", "2048",
            "--max-context", "32768",
            "--kv", "int8",
            "--spec", "2", "--suffix-draft", "0", "--mtp-max-t", "1",
            "--pool-workers", "0",
            "--pool-affinity", "auto",
            "--stats",
        ],
        "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0},
        "launcher": dict(LAUNCH_DEFAULTS),
    }


def load_json(path, fallback):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else fallback
    except Exception:
        return fallback


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def parse_args(seq):
    """args 数组 -> [(flag, value|None), ...]（保持原顺序；未知孤值并入前一个，首位孤值作为 (tok,None) 保留）。"""
    out = []
    i = 0
    while i < len(seq):
        tok = str(seq[i])
        if tok.startswith("--"):
            nxt = str(seq[i + 1]) if i + 1 < len(seq) else None
            if nxt is not None and not nxt.startswith("--"):
                out.append((tok, nxt))
                i += 2
            else:
                out.append((tok, None))
                i += 1
        else:
            if out:                      # 常见兜底：孤值粘到前一项
                f, v = out[-1]
                out[-1] = (f, (v + " " + tok).strip() if v else tok)
            else:                        # 首位孤值：当作无值 flag 原样保留，不静默丢弃
                out.append((tok, None))
            i += 1
    return out


def args_from_form(cfg, form_vals):
    """按表单重组 args：表单键替换/插入，其余原样保留；布尔占位互斥。"""
    pairs = parse_args(cfg.get("args") or [])
    managed = set(VALUED_ARGS) | set(BOOL_ARGS) | {"--resident-experts", "--mmap-experts"}
    kept = [(f, v) for f, v in pairs if f not in managed and f.startswith("--")]

    def form(key):
        return form_vals.get(key)

    rebuilt = []
    # 顺序原则：固定段在前（pack/native/放置模式/no-ple/profile/...），表单数值段随后，尾部固定段最后
    head_flags, tail_flags = [], []
    for f, v in kept:
        if f in FIXED_ARGS and f in ("--pack", "--native", "--no-ple",
                                     "--expert-cache", "--spec",
                                     "--suffix-draft", "--mtp-max-t"):
            head_flags.append((f, v))
        elif f == "--stats":
            tail_flags.append((f, v))
        else:
            head_flags.append((f, v))

    rebuilt += head_flags
    ph = (form("__placeholder") or "").strip()
    if ph in PLACEHOLDER_CHOICES and ph:      # 只接受合法占位值，防提示文案被当参数写入
        rebuilt.append((ph, None))
    for flag in BOOL_ARGS:
        if form(flag.lstrip("-")):
            rebuilt.append((flag, None))
    for flag in VALUED_ARGS:
        v = form(flag.lstrip("-"))
        if v is None or str(v).strip() == "":
            continue
        rebuilt.append((flag, str(v).strip()))
    rebuilt += [t for t in tail_flags]

    out = []
    for f, v in rebuilt:
        out.append(f)
        if v is not None:
            out.append(v)
    return out


# ============================================================================
# 引擎日志解析（serve-engine.log 的请求完成行 / 命中率行）
# ============================================================================
REQ_RE = re.compile(
    r"prompt (\d+) tokens = .*? read in ([\d.]+) ms \(([\d.]+) tok/s\), "
    r"(\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\)")
HIT_RE = re.compile(r"decode expert cache hit rate: ([\d.]+)% \((\d+) hits / (\d+) lookups\)")


def parse_request_lines(lines):
    """从日志行里抽请求完成记录（与 hit 率挂到同一条记录上）。"""
    reqs = []
    cur = None
    for ln in lines:
        m = REQ_RE.search(ln)
        if m:
            cur = {
                "ts": _ts_of_line(ln),
                "prompt": int(m.group(1)), "prefill_tps": float(m.group(3)),
                "completion": int(m.group(4)), "decode_tps": float(m.group(6)),
                "hit": None,
            }
            reqs.append(cur)
            continue
        m = HIT_RE.search(ln)
        if m and cur is not None:
            cur["hit"] = float(m.group(1))
            cur = None
    return reqs


def _ts_of_line(ln):
    return now_hms()


# ============================================================================
# 共享状态与采样线程
# ============================================================================
LOCK = threading.Lock()
STATE = {
    "port": DEFAULT_PORT,       # svc_poller 每轮读它（参数页改端口后即时生效）
    "gpu": {"ok": False},
    "sys": {"cpu_name": "", "cpu_total": None, "mem_pct": None,
            "mem_used": None, "mem_total": None},
    "svc": {"pid": None, "rss": None, "uptime": None},      # python serve.server
    "eng": {"pid": None, "rss": None, "cpu_time": None, "uptime": None},
    "health": {"ok": False, "loaded": False, "max_context": None, "model": ""},
    "requests": deque(maxlen=600),
    "req_seq": 0,               # 请求记录单调序号（deque 环绕后增量逻辑仍正确）
    "log_lines": deque(maxlen=4000),
    "log_seq": 0,
    "eng_log_offset": -1,       # -1 = 首次定位到文件末尾（不回放历史请求）
    "api_log_offset": 0,
    "last_error": "",
    "started_by_me": None,      # 本程序启动的 server PID
}


def st_set(k, v):
    with LOCK:
        STATE[k] = v


def st_get(k, default=None):
    with LOCK:
        return STATE.get(k, default)


def now_hms():
    return time.strftime("%H:%M:%S")


def bar(pct, width=22, filled="#", empty="."):
    if pct is None:
        return "[" + empty * width + "]"
    try:
        n = max(0, min(width, int(round(float(pct) / 100.0 * width))))
    except Exception:
        n = 0
    return "[" + filled * n + empty * (width - n) + "]"


def gib(nbytes):
    return "n/a" if nbytes is None else f"{nbytes / 1024 ** 3:.1f} GiB"


def fmt_duration(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h} 时 {m} 分 {s} 秒" if h else f"{m} 分 {s} 秒"


def http_get_json(url, timeout=HEALTH_TIMEOUT):
    with urlrequest.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def port_open(port, timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ============================================================================
def launcher_cfg(cfg):
    """strata.json 里的 launcher 私有块（服务端忽略未知键）。"""
    d = dict(LAUNCH_DEFAULTS)
    d.update(cfg.get("launcher") or {})
    return d


def find_server_proc():
    """找本部署的 python -m serve.server 进程（按 cwd 归一到 ROOT，防误杀同构部署）；
    psutil 缺失时返回 None。"""
    if not psutil:
        return None
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower().startswith("python") and \
               any("serve.server" in (c or "") for c in (p.info["cmdline"] or [])):
                try:
                    if Path(p.cwd()).resolve() != ROOT.resolve():
                        continue
                except Exception:
                    pass
                return p
        except Exception:
            continue
    return None


def find_engine_proc():
    if not psutil:
        return None
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == "strata.exe":
                return p
        except Exception:
            continue
    return None


def gpu_poller():
    cmd = ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
           "temperature.gpu,power.draw", "--format=csv,noheader,nounits"]
    while True:
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                          creationflags=flags, timeout=8)
            parts = [x.strip() for x in
                     out.decode("utf-8", "replace").strip().splitlines()[0].split(",")]
            if len(parts) >= 6:
                def num(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                st_set("gpu", {"ok": True, "name": parts[0], "util": num(parts[1]),
                               "mem_used": num(parts[2]) * 1024 ** 2,
                               "mem_total": num(parts[3]) * 1024 ** 2,
                               "temp": num(parts[4]), "power": num(parts[5])})
        except Exception as e:
            st_set("gpu", {"ok": False})
            st_set("last_error", f"nvidia-smi: {e}")
        time.sleep(1.0)


def sys_poller():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            cpu_name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        cpu_name = "n/a"
    st_set("sys.cpu_name" if False else "sys", dict(st_get("sys") or {}, cpu_name=cpu_name))
    while True:
        try:
            if psutil:
                vm = psutil.virtual_memory()
                st_set("sys", {"cpu_name": cpu_name,
                               "cpu_total": psutil.cpu_percent(interval=None),
                               "mem_pct": vm.percent,
                               "mem_used": vm.used, "mem_total": vm.total})
        except Exception as e:
            st_set("last_error", f"psutil: {e}")
        time.sleep(1.0)


def svc_poller():
    """服务(python) + 引擎(strata) 进程与 /health 状态。health 探测不依赖 psutil，
    端口每轮从 STATE 读（参数页改端口后即时生效）。"""
    while True:
        port = st_get("port") or DEFAULT_PORT
        hp = {}
        try:
            hp = http_get_json(f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT)
            hp = hp if isinstance(hp, dict) else {}
            hp["ok"] = True
        except Exception:
            hp = {"ok": False}
        st_set("health", {"ok": hp.get("ok", False),
                          "loaded": bool(hp.get("loaded")),
                          "max_context": hp.get("max_context"),
                          "model": hp.get("model", "")})
        if psutil:
            sp = find_server_proc()
            if sp:
                try:
                    with sp.oneshot():
                        st_set("svc", {"pid": sp.pid, "rss": sp.memory_info().rss,
                                       "uptime": time.time() - sp.create_time()})
                except Exception:
                    pass
            else:
                st_set("svc", {"pid": None, "rss": None, "uptime": None})
            ep = find_engine_proc()
            if ep:
                try:
                    with ep.oneshot():
                        st_set("eng", {"pid": ep.pid, "rss": ep.memory_info().rss,
                                       "cpu_time": sum(ep.cpu_times()[:2]),
                                       "uptime": time.time() - ep.create_time()})
                except Exception:
                    pass
            else:
                st_set("eng", {"pid": None, "rss": None, "cpu_time": None,
                               "uptime": None})
        time.sleep(1.0)


def tail_text(path, key_offset):
    p = Path(path)
    try:
        if not p.exists():
            return []
        size = p.stat().st_size
        with LOCK:
            off = STATE.get(key_offset, -1)
        if off < 0:                       # 首次：定位到文件末尾，不回放历史
            off = size
        if size < off:
            off = size
        if size == off:
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(off)
            data = f.read()
            # 只消费到最后一个完整行为止，半行留给下次（防请求行被拆两段）
            cut = data.rfind("\n")
            if cut < 0:
                return []
            data = data[:cut + 1]
            with LOCK:
                STATE[key_offset] = off + cut + 1
        return data.splitlines()
    except Exception:
        return []


def log_poller():
    """tail 引擎日志：喂日志页 + 抽请求记录。"""
    while True:
        lines = tail_text(ENGINE_LOG, "eng_log_offset")
        if lines:
            with LOCK:
                for ln in lines:
                    STATE["log_lines"].append(ln)
                    STATE["log_seq"] += 1
            for req in parse_request_lines(lines):
                with LOCK:
                    STATE["req_seq"] = STATE.get("req_seq", 0) + 1
                    req["seq"] = STATE["req_seq"]
                    STATE["requests"].append(req)
        time.sleep(0.5)


# ============================================================================
# 测速语料（沿用教程 §0.2 的“数数字”，读数可与 3.8_27b 控制台横向比）
# ============================================================================
COUNTING_INSTR_TOKENS = 40
LONG_CTX_SAFETY = 0.90
LONG_CTX_MAX_OUT = 256


def _counting_digits(n):
    total, lo, hi, d = 0, 1, 9, 1
    while lo <= n:
        top = min(hi, n)
        total += (top - lo + 1) * d
        lo *= 10
        hi = lo * 10 - 1
        d += 1
    return total


def estimate_counting_tokens(n):
    return (_counting_digits(n) + n + COUNTING_INSTR_TOKENS) if n > 0 else 0


def long_prompt_numbers(target_tokens):
    lo, hi = 300, 600
    if estimate_counting_tokens(lo) >= target_tokens:
        return lo
    while estimate_counting_tokens(hi) < target_tokens and hi < 100_000_000:
        hi *= 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_counting_tokens(mid) <= target_tokens:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_counting_prompt(n):
    return (" ".join(str(i) for i in range(1, n + 1)) +
            "\n\nContinue the same number sequence from the next number. "
            "Output numbers only, in the same space-separated format, and "
            "keep going until the length limit.")


def counting_verdict(content, expected_next):
    nums = [int(m) for m in re.findall(r"\d+", content or "")]
    if not nums:
        return "无法判定", "回答中没有整数（可能被截断或思考占满）"
    start = nums[0]
    for i in range(len(nums) - 3):
        if (nums[i + 1] == nums[i] + 1 and nums[i + 2] == nums[i] + 2
                and nums[i + 3] == nums[i] + 3):
            start = nums[i]
            break
    head = " ".join(str(x) for x in nums[nums.index(start):][:6])
    if start == expected_next:
        return "正确", f"从 {head} 接着数（期望 {expected_next}）"
    if start < expected_next:
        return "疑似静默答错", f"回答从 {head} 起，期望 {expected_next} 起"
    return "可疑（跳号）", f"回答从 {head} 起，期望 {expected_next} 起"


# ============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("strata 控制台 — Cyber-Tiel-Coder-35B-A3B · strata serve")
        self.geometry("1010x760")
        self.minsize(920, 680)

        self.cfg = blank_config()
        self._load_config()

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        self.nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_dash, self.tab_param = ttk.Frame(nb), ttk.Frame(nb)
        self.tab_log, self.tab_speed, self.tab_api = ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb)
        nb.add(self.tab_dash, text="实时看板")
        nb.add(self.tab_param, text="参数")
        nb.add(self.tab_log, text="日志")
        nb.add(self.tab_speed, text="一键测速")
        nb.add(self.tab_api, text="接入信息")

        self.vals = {}
        self._build_dash()
        self._build_param()
        self._build_log()
        self._build_speed()
        self._build_api()

        self.after(600, self._refresh)
        self.after(1500, self._refresh_slow)

    # ---------------- 配置 ----------------
    def _load_config(self):
        disk = load_json(CONFIG_FILE, None)
        self.cfg = disk if disk else blank_config()
        if not isinstance(self.cfg.get("args"), list):
            self.cfg["args"] = blank_config()["args"]
        self.cfg.setdefault("sampling", blank_config()["sampling"])
        self.cfg.setdefault("launcher", dict(LAUNCH_DEFAULTS))

    def _port(self):
        try:
            port = int(launcher_cfg(self.cfg)["port"])
        except Exception:
            port = DEFAULT_PORT
        if not (1 <= port <= 65535):
            port = DEFAULT_PORT
        st_set("port", port)      # svc_poller 每轮从这里读（线程安全）
        return port

    # ---------------- 实时看板 ----------------
    def _row(self, parent, r, label, labelw=8):
        tk.Label(parent, text=label, width=labelw, anchor="w", font=FONT_UI,
                 fg="#222").grid(row=r, column=0, sticky="w", padx=(3, 4), pady=1)
        w = tk.Label(parent, text="", anchor="w", font=FONT_MONO)
        w.grid(row=r, column=1, sticky="we", padx=(0, 4), pady=1)
        return w

    def _frame(self, parent, title, r, c):
        f = ttk.LabelFrame(parent, text=title)
        f.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
        f.columnconfigure(1, weight=1)
        return f

    def _build_dash(self):
        root = self.tab_dash
        root.columnconfigure(0, weight=1, uniform="c")
        root.columnconfigure(1, weight=1, uniform="c")

        g = self._frame(root, "GPU", 0, 0)
        self.vals["gpu_name"] = self._row(g, 0, "型号")
        self.vals["gpu_util"] = self._row(g, 1, "利用率")
        self.vals["gpu_mem"] = self._row(g, 2, "显存")
        self.vals["gpu_temp"] = self._row(g, 3, "温度℃/功耗")
        for r in range(4):
            g.grid_rowconfigure(r, pad=1)

        c = self._frame(root, "CPU / 内存", 0, 1)
        self.vals["cpu_name"] = self._row(c, 0, "型号")
        self.vals["cpu_total"] = self._row(c, 1, "总占用")
        self.vals["mem"] = self._row(c, 2, "内存")

        e = self._frame(root, "服务（python serve.server）", 1, 0)
        self.vals["svc_state"] = self._row(e, 0, "状态")
        self.vals["svc_pid"] = self._row(e, 1, "进程号/内存")
        self.vals["svc_uptime"] = self._row(e, 2, "运行时长")
        self.vals["health"] = self._row(e, 3, "/health")

        x = self._frame(root, "引擎（strata.exe）", 1, 1)
        self.vals["eng_state"] = self._row(x, 0, "状态")
        self.vals["eng_pid"] = self._row(x, 1, "进程号/内存")
        self.vals["eng_cpu"] = self._row(x, 2, "CPU 时间")
        self.vals["eng_uptime"] = self._row(x, 3, "运行时长")

        t = ttk.LabelFrame(root, text="实测吞吐（解析 serve-engine.log 请求行）")
        t.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        t.columnconfigure(1, weight=1)
        self.vals["thr_median"] = self._row(t, 0, "中位数")
        self.vals["thr_count"] = self._row(t, 1, "请求计数")
        wrap = ttk.Frame(t)
        wrap.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=6, pady=(4, 4))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        tbl = ttk.Treeview(wrap, columns=("t", "p", "d", "h", "pt", "ct"),
                           show="headings", height=7)
        for col, txt, w in (("t", "时间", 90), ("p", "prompt", 80), ("d", "输出", 80),
                            ("h", "命中率%", 90), ("pt", "prefill t/s", 95),
                            ("ct", "decode t/s", 95)):
            tbl.heading(col, text=txt)
            tbl.column(col, width=w, minwidth=w, anchor="center", stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tbl.yview)
        tbl.configure(yscrollcommand=vsb.set)
        tbl.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        root.rowconfigure(2, weight=1)
        self.tbl = tbl
        self._req_seen = 0

        btns = ttk.Frame(root)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))
        ttk.Button(btns, text="启动服务", width=12, command=self.on_start).pack(side="left", padx=3)
        ttk.Button(btns, text="停止服务", width=12, command=self.on_stop).pack(side="left", padx=3)
        ttk.Button(btns, text="打开日志目录", width=14, command=self.on_open_logs).pack(side="left", padx=3)
        ttk.Label(btns, text="停止会结束 serve.server 与它拉起的 strata.exe 整个进程树",
                  font=FONT_UI, foreground="#888").pack(side="left", padx=(12, 0))

    # ---------------- 参数页 ----------------
    def _build_param(self):
        f = self.tab_param
        top = ttk.Frame(f)
        top.pack(side="top", fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="strata.json 参数（只含引擎/服务层真实存在的键）",
                  font=FONT_TITLE).pack(side="left")
        ttk.Button(top, text="保存到 strata.json", width=18,
                   command=self.on_save_config).pack(side="right")
        ttk.Button(top, text="从磁盘重读", width=12,
                   command=self.on_reload_config).pack(side="right", padx=(0, 6))
        self.cfg_status = tk.Label(f, text="", font=FONT_UI, fg="#0a0")
        self.cfg_status.pack(side="bottom", anchor="w", padx=10, pady=(0, 2))

        wrap = ttk.Frame(f)
        wrap.pack(side="top", fill="both", expand=True, padx=6, pady=2)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.vars = {}
        meta = {f[0]: f for f in ARG_FIELDS}
        pairs = {f: v for f, v in parse_args(self.cfg.get("args") or [])}
        argkey = {("--" + f[0]) for f in ARG_FIELDS if f[0] != "__placeholder"}

        groups = [
            ("服务层（strata.json 顶层，serve/server.py 读取）",
             [("model_name", "模型名", "str", "/v1/models 显示的名字"),
              ("fit_max_tokens", "输出超窗自动收缩", "bool", "#545：max_tokens 超窗口时收缩而非 400"),
              ("engine_silence_s", "引擎静默看门狗 s", "int", "0 = 无限等（长 prefill 防误杀）"),
              ("reasoning_budget_tokens", "思考预算 token", "int", "#123：0 = 不限；到达即注入收尾强制作答"),
              ("effort_position", "思考档注入位置", "choice", TOP_CHOICES["effort_position"],
               "#458：end 时非默认档插在回答前，prompt 可复用"),
              ("lazy_load", "懒加载", "bool", "HTTP 先起、首请求才载模型"),
              ("host", "监听地址", "str", "127.0.0.1 = 仅本机"),
              ("tokenizer", "tokenizer 目录", "path", ""),
              ("log", "引擎日志路径", "str", "")]),
        ]
        r = 0
        for gtitle, keys in groups:
            lf = ttk.LabelFrame(inner, text=gtitle)
            lf.grid(row=r, column=0, sticky="we", padx=8, pady=4)
            lf.columnconfigure(1, weight=1)
            rr = 0
            for item in keys:
                key, label, typ = item[0], item[1], item[2]
                hint = item[-1]
                choices = item[3] if typ == "choice" else None
                tk.Label(lf, text=label, anchor="w", font=FONT_UI, width=16).grid(
                    row=rr, column=0, sticky="w", padx=(4, 6), pady=2)
                var = tk.StringVar()
                if typ == "bool":
                    var.set("1" if self.cfg.get(key) else "0")
                    w = ttk.Checkbutton(lf, variable=var, onvalue="1", offvalue="0")
                else:
                    var.set("" if self.cfg.get(key) is None else str(self.cfg.get(key)))
                    if typ == "choice":
                        w = ttk.Combobox(lf, textvariable=var, width=12,
                                         values=choices or [])
                    else:
                        w = tk.Entry(lf, textvariable=var, font=FONT_MONO)
                w.grid(row=rr, column=1, sticky="we", padx=(0, 6), pady=2)
                if typ == "path":
                    ttk.Button(lf, text="浏览", width=6,
                               command=lambda k=key: self._browse_top(k)).grid(
                        row=rr, column=2, padx=(0, 6))
                tk.Label(lf, text=hint, anchor="w", fg="#888", font=FONT_UI).grid(
                    row=rr, column=3, sticky="w")
                self.vars[key] = var
                rr += 1
            r += 1

        lf2 = ttk.LabelFrame(inner, text="采样（strata.json 的 sampling 块）")
        lf2.grid(row=r, column=0, sticky="we", padx=8, pady=4)
        lf2.columnconfigure(1, weight=1)
        for rr, key in enumerate(SAMPLING_KEYS):
            tk.Label(lf2, text=key, anchor="w", font=FONT_UI, width=16).grid(
                row=rr, column=0, sticky="w", padx=(4, 6), pady=2)
            var = tk.StringVar(value=str(self.cfg["sampling"].get(key, "")))
            tk.Entry(lf2, textvariable=var, font=FONT_MONO).grid(
                row=rr, column=1, sticky="we", padx=(0, 6), pady=2)
            self.vars["sampling." + key] = var
        r += 1

        lf3 = ttk.LabelFrame(inner, text="引擎参数（strata.json 的 args；未列出的元素原样保留）")
        lf3.grid(row=r, column=0, sticky="we", padx=8, pady=4)
        lf3.columnconfigure(1, weight=1)
        for rr, item in enumerate(ARG_FIELDS):
            key, label, typ = item[0], item[1], item[2]
            hint = item[-1]
            choices = item[3] if typ == "choice" else None
            flag = "--" + key if key != "__placeholder" else "__placeholder"
            tk.Label(lf3, text=label, anchor="w", font=FONT_UI, width=16).grid(
                row=rr, column=0, sticky="w", padx=(4, 6), pady=2)
            var = tk.StringVar()
            if typ == "bool":
                var.set("1" if pairs.get(flag) else "0")
                ttk.Checkbutton(lf3, variable=var, onvalue="1", offvalue="0").grid(
                    row=rr, column=1, sticky="w", padx=(0, 6), pady=2)
            elif typ == "choice":
                # 初始值一律从现 args 反查；绝不用提示文案当默认值（P0 教训）
                if key == "__placeholder":
                    cur = ("--resident-experts" if "--resident-experts" in pairs else
                           "--mmap-experts" if "--mmap-experts" in pairs else "")
                else:
                    cur = pairs.get(flag, "") or ""
                var.set(cur)
                ttk.Combobox(lf3, textvariable=var, width=14,
                             values=choices or []).grid(
                    row=rr, column=1, sticky="we", padx=(0, 6), pady=2)
            else:
                var.set(pairs.get(flag, "") or "")
                tk.Entry(lf3, textvariable=var, font=FONT_MONO).grid(
                    row=rr, column=1, sticky="we", padx=(0, 6), pady=2)
            tk.Label(lf3, text=hint, anchor="w", fg="#888", font=FONT_UI).grid(
                row=rr, column=2, columnspan=2, sticky="w")
            self.vars["arg." + key] = var
        r += 1

        lf4 = ttk.LabelFrame(inner, text="启动器（launcher 块；控制台私有，服务端忽略）")
        lf4.grid(row=r, column=0, sticky="we", padx=8, pady=4)
        lf4.columnconfigure(1, weight=1)
        lc = launcher_cfg(self.cfg)
        for rr, (key, label, hint) in enumerate([
                ("port", "服务端口", "启动/停止/健康探测都用它"),
                ("idle_unload_s", "空闲卸载 s", "0 = 常驻；>0 = 无请求 N 秒后卸模型腾显存")]):
            tk.Label(lf4, text=label, anchor="w", font=FONT_UI, width=16).grid(
                row=rr, column=0, sticky="w", padx=(4, 6), pady=2)
            var = tk.StringVar(value=str(lc.get(key, "")))
            tk.Entry(lf4, textvariable=var, font=FONT_MONO).grid(
                row=rr, column=1, sticky="we", padx=(0, 6), pady=2)
            tk.Label(lf4, text=hint, anchor="w", fg="#888", font=FONT_UI).grid(
                row=rr, column=2, sticky="w")
            self.vars["launcher." + key] = var
        tk.Label(lf4, text="请求监视页", anchor="w", font=FONT_UI, width=16).grid(
            row=2, column=0, sticky="w", padx=(4, 6), pady=2)
        lv = tk.StringVar(value="1" if lc.get("api_monitor") else "0")
        ttk.Checkbutton(lf4, variable=lv, onvalue="1", offvalue="0").grid(
            row=2, column=1, sticky="w", padx=(0, 6), pady=2)
        tk.Label(lf4, text="开启后 server 提供 /api-monitor", anchor="w", fg="#888",
                 font=FONT_UI).grid(row=2, column=2, sticky="w")
        self.vars["launcher.api_monitor"] = lv

        pv = ttk.LabelFrame(f, text="启动命令行预览（python -m serve.server …）")
        pv.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        pv.columnconfigure(0, weight=1)
        self.argv_preview = tk.Text(pv, height=3, wrap="word", font=FONT_MONO, bg="#f6f6f6")
        self.argv_preview.grid(row=0, column=0, sticky="we", padx=4, pady=4)
        self._refresh_argv()

    def _browse_top(self, key):
        cur = self.vars[key].get()
        init = str(Path(cur).parent) if cur else str(ROOT)
        if key == "tokenizer":
            p = filedialog.askdirectory(title="选择 tokenizer 目录", initialdir=init)
        else:
            p = filedialog.askopenfilename(title="选择文件", initialdir=init,
                                           filetypes=[("全部", "*.*")])
        if p:
            self.vars[key].set(p.replace("/", "\\"))

    def _collect(self):
        """读表单 -> (新 cfg dict, errors)。非法值回退现值并报告。"""
        cfg = json.loads(json.dumps(self.cfg))       # 深拷贝
        errors = []
        ints_top = {"engine_silence_s", "reasoning_budget_tokens", "port"}
        for key in TOP_KEYS:
            if key not in self.vars:
                continue
            raw = self.vars[key].get().strip()
            if key in ints_top:
                try:
                    cfg[key] = int(raw) if raw else 0
                except ValueError:
                    errors.append(f"{key} 需为整数：{raw!r}（已保留原值）")
                    cfg[key] = self.cfg.get(key)
            elif key == "fit_max_tokens" or key == "lazy_load":
                cfg[key] = (raw == "1")
            else:
                cfg[key] = raw
        samp = dict(self.cfg.get("sampling") or {})
        # 范围与 server.py 的校验一致：top_p (0,1]、top_k 1..64、min_p [0,1]、temperature >= 0。
        # 空值/非法值一律保留原值——清空写出 0 会让 server 直接拒绝启动（top_p=0 / top_k=0）。
        limits = {"temperature": (0.0, None), "top_p": (0.0 + 1e-9, 1.0),
                  "top_k": (1, 64), "min_p": (0.0, 1.0)}
        for key in SAMPLING_KEYS:
            raw = self.vars.get("sampling." + key, tk.StringVar()).get().strip()
            cur = self.cfg["sampling"].get(key, 0)
            if raw == "":
                samp[key] = cur
                continue
            try:
                val = float(raw)
            except ValueError:
                errors.append(f"sampling.{key} 需为数字：{raw!r}（已保留原值 {cur}）")
                samp[key] = cur
                continue
            lo, hi = limits[key]
            if val < lo or (hi is not None and val > hi):
                errors.append(f"sampling.{key}={val} 超出允许范围 [{lo}, {hi or '∞'}]"
                              f"（已保留原值 {cur}）")
                samp[key] = cur
                continue
            samp[key] = val
        cfg["sampling"] = samp

        form_vals = {}
        pairs_now = {f: v for f, v in parse_args(self.cfg.get("args") or [])}
        # 空值=删 flag 只对"无值也合法"的参数成立；核心数值参数空值视为保留原值，
        # 防止 --max-context 被清空后引擎静默回落 4096。
        PRESERVE_EMPTY = {"--max-context", "--vram-reserve-mib", "--kv", "--prefill",
                          "--pool-workers", "--pool-affinity"}
        for item in ARG_FIELDS:
            key = item[0]
            typ = item[2]
            raw = self.vars.get("arg." + key, tk.StringVar()).get().strip()
            flag = "--" + key if key != "__placeholder" else "__placeholder"
            if typ == "bool":
                form_vals[key] = (raw == "1")
            elif typ == "int":
                if raw == "":
                    if flag in PRESERVE_EMPTY and pairs_now.get(flag) not in (None, ""):
                        form_vals[key] = pairs_now[flag]     # 保留原值，不删 flag
                        errors.append(f"引擎参数 {key} 为空：已保留原值 {pairs_now[flag]}")
                    else:
                        form_vals[key] = ""                  # prompt-cache/batch 等可省略
                else:
                    try:
                        form_vals[key] = int(raw)
                    except ValueError:
                        errors.append(f"引擎参数 {key} 需为整数：{raw!r}（已保留原值）")
                        form_vals[key] = pairs_now.get(flag, "")
            else:
                form_vals[key] = raw
        cfg["args"] = args_from_form(cfg, form_vals)

        lc = {}
        for key in ("port", "idle_unload_s"):
            raw = self.vars.get("launcher." + key, tk.StringVar()).get().strip()
            try:
                lc[key] = int(float(raw)) if raw else 0
            except ValueError:
                errors.append(f"launcher.{key} 需为数字：{raw!r}")
                lc[key] = launcher_cfg(self.cfg).get(key, 0)
        if not (1 <= lc.get("port", 0) <= 65535):
            errors.append(f"launcher.port 非法：{lc.get('port')}（已保留原值）")
            lc["port"] = launcher_cfg(self.cfg).get("port", DEFAULT_PORT)
        lc["api_monitor"] = (self.vars.get("launcher.api_monitor",
                                           tk.StringVar()).get() == "1")
        cfg["launcher"] = lc
        return cfg, errors

    def _server_argv(self, cfg):
        lc = launcher_cfg(cfg)
        argv = [sys.executable, "-m", "serve.server", "--engine", "strata",
                "--config", "strata.json",
                "--host", str(cfg.get("host") or "127.0.0.1"),
                "--port", str(lc.get("port") or DEFAULT_PORT)]
        if lc.get("idle_unload_s"):
            argv += ["--idle-unload", str(lc["idle_unload_s"])]
        if lc.get("api_monitor"):
            argv.append("--api-monitor")
        return argv

    def _refresh_argv(self):
        try:
            cfg, _ = self._collect()
        except Exception:
            cfg = self.cfg
        self.argv_preview.configure(state="normal")
        self.argv_preview.delete("1.0", "end")
        self.argv_preview.insert("1.0", " ".join(self._server_argv(cfg)))
        self.argv_preview.configure(state="disabled")

    def on_save_config(self):
        cfg, errors = self._collect()
        if errors:
            messagebox.showerror("参数有误（未保存）", "\n".join(errors))
            return
        try:
            save_json(CONFIG_FILE, cfg)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.cfg = cfg
        self._refresh_argv()
        self.cfg_status.configure(
            text=f"已保存 strata.json（{now_hms()}）——改动需停止后重新启动服务才生效",
            fg="#0a0")

    def on_reload_config(self):
        if not messagebox.askyesno("重读配置", "放弃表单修改，从 strata.json 重读？"):
            return
        self._load_config()
        for w in self.tab_param.winfo_children():
            w.destroy()
        self.vars = {}
        self._build_param()
        self.cfg_status.configure(text=f"已从 strata.json 重读（{now_hms()}）", fg="#06c")

    # ---------------- 启动 / 停止 ----------------
    def on_start(self):
        self._port()
        sp = find_server_proc()
        if sp is not None:
            messagebox.showinfo(
                "启动服务",
                f"服务进程已在运行（PID {sp.pid}）——可能正在加载模型。\n"
                "健康探测未通时请耐心等 /health 就绪，不要重复启动。")
            return
        if st_get("health", {}).get("ok") or port_open(self._port()):
            messagebox.showinfo("启动服务", f"端口 {self._port()} 已有服务在响应。")
            return
        cfg, errors = self._collect()
        if errors:
            messagebox.showerror("参数有误", "\n".join(errors) + "\n\n请到「参数」页修正后再启动。")
            return
        model = next((pairs_v for f, pairs_v in parse_args(cfg["args"])
                      if f == "--native"), "")
        checks = [(ENGINE_EXE, "引擎"), (ROOT / str(model or "x"), "模型"),
                  (ROOT / str(cfg.get("tokenizer") or "x"), "tokenizer")]
        missing = [f"{label}: {p}" for p, label in checks if not p.exists()]
        if missing:
            messagebox.showerror("启动预检失败", "缺少文件：\n" + "\n".join(missing))
            return
        if port_open(self._port()):
            messagebox.showerror("端口被占用",
                                 f"127.0.0.1:{self._port()} 已有监听。\n"
                                 "可能是服务已运行（健康探测未通）或别的程序占用。")
            return
        if psutil:
            avail = psutil.virtual_memory().available / 1024 ** 3
            if avail < MEM_WARN_GIB:
                if not messagebox.askyesno(
                        "内存偏紧",
                        f"当前可用内存 {avail:.1f} GiB < {MEM_WARN_GIB:.0f} GiB。\n"
                        "模型文件页缓存约需 21 GiB，紧张时加载可能失败。\n\n仍要启动？"):
                    return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            fh = open(API_LOG, "ab", buffering=0)
            subprocess.Popen(self._server_argv(cfg), cwd=str(ROOT), stdout=fh,
                             stderr=subprocess.STDOUT, creationflags=flags)
            fh.close()
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            return
        messagebox.showinfo(
            "启动服务", f"已启动（端口 {self._port()}）。\n"
            "首次/冷启动加载约 5–8 分钟（模型在外接 SSD），热启动约 2 分钟。\n"
            "看板「/health」出现 loaded=true 即就绪。日志见 logs\\serve-api.log。")

    def _pid_by_port(self, port):
        """psutil 缺失时的兜底：netstat 找监听该端口的 PID。"""
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 creationflags=flags, timeout=15).stdout.decode(
                "utf-8", "replace")
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[1] == f"127.0.0.1:{port}" and \
                        parts[3] == "LISTENING":
                    return int(parts[4])
        except Exception:
            pass
        return None

    def on_stop(self):
        self._port()
        sp = find_server_proc()
        pid = sp.pid if sp else self._pid_by_port(self._port())
        if pid is None and not port_open(self._port()):
            messagebox.showinfo("停止服务", "服务未在运行。")
            return
        if not messagebox.askyesno(
                "停止服务", "将结束 serve.server 与它拉起的 strata.exe（整个进程树）。\n"
                "自学习档案在服务被强杀时不会保存（定时周期内已保存的部分仍在）。\n\n确定停止？"):
            return
        try:
            if pid:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, creationflags=flags, timeout=15)
                if r.returncode != 0:
                    messagebox.showerror(
                        "停止失败",
                        f"taskkill 返回 {r.returncode}：\n"
                        + r.stderr.decode("utf-8", "replace")[:300]
                        + "\n\n可手动在任务管理器结束 python/strata 进程。")
                    return
            else:
                messagebox.showwarning("停止服务", "按端口也找不到进程，未执行停止。")
                return
        except Exception as e:
            messagebox.showerror("停止失败", str(e))
            return
        messagebox.showinfo("停止服务", "已发送停止命令。")

    def on_open_logs(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(LOG_DIR))        # noqa: 仅 Windows
        except Exception as e:
            messagebox.showerror("打开日志目录", str(e))

    # ---------------- 日志页 ----------------
    def _build_log(self):
        f = self.tab_log
        top = ttk.Frame(f)
        top.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(top, text=f"引擎日志：{ENGINE_LOG}（只读增量）",
                  font=FONT_UI, foreground="#555").pack(side="left")
        self.log_text = tk.Text(f, wrap="none", font=FONT_MONO, bg="#111", fg="#ddd")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._log_seen = 0

    # ---------------- 一键测速 ----------------
    def _build_speed(self):
        f = self.tab_speed
        top = ttk.Frame(f)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="发送测速请求（1000 token 数数字）",
                   command=self.on_speed_test).pack(side="left")
        ttk.Button(top, text="超长上下文测速（32k 冒烟档）",
                   command=self.on_long_speed_test).pack(side="left", padx=(8, 0))
        ttk.Label(top, text="  读数 = HTTP usage + 引擎日志完成行",
                  font=FONT_UI, foreground="#888").pack(side="left")
        self.speed_text = tk.Text(f, wrap="word", font=FONT_MONO, bg="#fbfbfb")
        self.speed_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.speed_text.insert(
            "1.0",
            "“数数字”题面与 3.8_27b 控制台同源，读数可横向比。\n"
            "  · 快测：max_tokens=1000、reasoning_effort=none、temperature=0；\n"
            "    tok/s 取自引擎日志完成行（与命令行形态同口径）。\n"
            "  · 超长：按 /health 实际上限 ×90% − 256 输出收缩语料，不会撑爆窗口；\n"
            "    引擎按“题面+输出”合计算上限，mmap 形态冷预填随长度平方增长。\n"
            "  · 服务未启动时先到「实时看板」点【启动服务】。\n"
            "  · 审查约定：本页只在人手点按钮时发请求，自动化验证不发长负载。\n")
        self.speed_text.configure(state="disabled")
        self._speed_busy = False

    def _speed_print(self, s):
        def do():
            self.speed_text.configure(state="normal")
            self.speed_text.insert("end", s)
            self.speed_text.see("end")
            self.speed_text.configure(state="disabled")
        self.after(0, do)

    def _log_snapshot(self):
        with LOCK:
            return len(STATE["log_lines"])

    def _new_engine_records(self, seen_seq):
        """取 seq 大于 seen_seq 的新请求记录（deque 环绕后仍正确）。"""
        reqs = list(st_get("requests") or [])
        return [r for r in reqs if (r.get("seq") or 0) > seen_seq]

    def _speed_preflight(self):
        if self._speed_busy:
            messagebox.showinfo("一键测速", "已有测速在跑，等它结束。")
            return False
        if not st_get("health", {}).get("ok"):
            messagebox.showinfo("一键测速",
                                f"服务未运行（127.0.0.1:{self._port()}）。\n"
                                "先到「实时看板」点【启动服务】，/health 通了再测。")
            return False
        return True

    def _finish_report(self, wall, payload, seen_reqs, expected_next=None):
        try:
            usage = (payload.get("usage") or {}) if isinstance(payload, dict) else {}
            choice = (payload.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            msg = choice.get("message") or {}
            content = (msg.get("content") or "") if isinstance(msg, dict) else ""
            self._speed_print(f"[{now_hms()}] 完成，HTTP 200，墙钟 {wall:.1f} s\n")
            self._speed_print(f"  prompt_tokens     {usage.get('prompt_tokens')}\n")
            self._speed_print(f"  completion_tokens {usage.get('completion_tokens')}\n")
            self._speed_print(f"  finish_reason     {finish}\n")
            recs = self._new_engine_records(seen_reqs)
            if recs:
                rec = recs[-1]
                self._speed_print(f"  引擎日志读数      prefill {rec.get('prefill_tps')} tok/s · "
                                  f"decode {rec.get('decode_tps')} tok/s · "
                                  f"命中 {rec.get('hit')}%\n")
            if expected_next is not None:
                verdict, note = counting_verdict(content, expected_next)
                self._speed_print(f"  接数判定          {verdict} —— {note}\n")
                self._speed_print(f"  回答开头          {content.strip()[:80]}\n")
        except Exception as e:
            self._speed_print(f"[{now_hms()}] 结果整理出错：{e}\n")

    def on_speed_test(self):
        if not self._speed_preflight():
            return
        self._speed_busy = True          # 主线程先置位，双击不会双发
        threading.Thread(target=self._speed_worker, daemon=True).start()

    def _speed_worker(self):
        self._speed_busy = True
        try:
            port = self._port()
            body = json.dumps({
                "model": self.cfg.get("model_name") or "Cyber-Tiel-Coder-35B-A3B",
                "messages": [{"role": "user", "content": build_counting_prompt(300)}],
                "max_tokens": 1000, "temperature": 0,
                "reasoning_effort": "none",      # 纯续写：思考只会吃光输出额度
            }).encode("utf-8")
            seen_reqs = st_get("req_seq") or 0
            self._speed_print(f"[{now_hms()}] 发送快测 …（max_tokens=1000, temp=0, "
                              "思考关闭）\n")
            t0 = time.time()
            req = urlrequest.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urlrequest.urlopen(req, timeout=900) as r:
                    payload = json.loads(r.read().decode("utf-8", "replace"))
                self._finish_report(time.time() - t0, payload, seen_reqs, 301)
            except urlerror.HTTPError as e:
                self._speed_print(f"[{now_hms()}] 服务端拒绝：HTTP {e.code} —— "
                                  "本部署 fit_max_tokens 开启，通常不会 400；"
                                  "若发生请把详情发回。\n")
            except urlerror.URLError as e:
                self._speed_print(f"[{now_hms()}] 请求失败：{e}\n"
                                  "  → 加载中（loaded=false）也会这样，等 /health 就绪再测。\n")
        finally:
            self._speed_busy = False

    def on_long_speed_test(self):
        if not self._speed_preflight():
            return
        if not messagebox.askyesno(
                "超长上下文测速",
                "32k 冒烟档：题面约 3 万 token，mmap 形态冷预填可能需要 10 分钟以上，\n"
                "期间显卡与内存会满负荷。\n\n确定开始？"):
            return
        self._speed_busy = True          # 主线程先置位
        threading.Thread(target=self._long_worker, daemon=True).start()

    def _long_worker(self):
        self._speed_busy = True
        try:
            port = self._port()
            max_ctx = int(st_get("health", {}).get("max_context") or 32768)
            budget = int(max_ctx * LONG_CTX_SAFETY) - LONG_CTX_MAX_OUT
            n = long_prompt_numbers(budget)
            est = estimate_counting_tokens(n)
            if est > budget:
                self._speed_print(f"[{now_hms()}] 上限 {max_ctx:,} 装不下最短语料，"
                                  "请调大 --max-context。\n")
                return
            self._speed_print(f"[{now_hms()}] 超长测速：引擎上限 {max_ctx:,}，"
                              f"题面预算 {budget:,}，语料 1..{n}（估 {est:,} token）\n"
                              f"  prefill 完成前不会有首字；引擎日志行会先于本页出现。\n")
            body = json.dumps({
                "model": self.cfg.get("model_name") or "Cyber-Tiel-Coder-35B-A3B",
                "messages": [{"role": "user", "content": build_counting_prompt(n)}],
                "max_tokens": LONG_CTX_MAX_OUT, "temperature": 0,
                "reasoning_effort": "none",
            }).encode("utf-8")
            seen_reqs = st_get("req_seq") or 0
            t0 = time.time()
            req = urlrequest.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urlrequest.urlopen(req, timeout=7200) as r:
                    payload = json.loads(r.read().decode("utf-8", "replace"))
                self._finish_report(time.time() - t0, payload, seen_reqs, n + 1)
            except urlerror.HTTPError as e:
                self._speed_print(f"[{now_hms()}] 服务端拒绝：HTTP {e.code}\n")
            except urlerror.URLError as e:
                self._speed_print(f"[{now_hms()}] 请求失败：{e}\n"
                                  "  → 长预填可能被传输层断开（引擎通常仍在跑，"
                                  "看「日志」页确认，别误判为崩溃）。\n")
        finally:
            self._speed_busy = False

    # ---------------- 接入信息 ----------------
    def _build_api(self):
        f = self.tab_api
        self.api_text = tk.Text(f, wrap="word", font=FONT_MONO, bg="#fbfbfb")
        self.api_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._refresh_api_text()

    def _refresh_api_text(self):
        port = self._port()
        base = f"http://127.0.0.1:{port}"
        self.api_text.configure(state="normal")
        self.api_text.delete("1.0", "end")
        self.api_text.insert("1.0", "\n".join([
            "OpenAI 兼容接口（serve.server，strata serve 子进程常驻）",
            f"  base_url   {base}/v1",
            f"  chat       POST {base}/v1/chat/completions   (流式/非流式)",
            f"  models     GET  {base}/v1/models",
            f"  health     GET  {base}/health   (loaded / max_context / model)",
            "",
            "Anthropic Messages",
            f"  messages   POST {base}/v1/messages   (流式/非流式)",
            "",
            "DSH（DeepSeek Harness 桌面端）接入",
            "  设置 → Our Free Model → 聚合渠道 → LLaMA.cpp 卡片：",
            f"    Base URL 覆盖 = {base}/v1   （写 127.0.0.1，不要写 localhost）",
            "    测试 → 发现模型 → 启用 Cyber-Tiel-Coder-35B-A3B",
            "  选择器里即出现 llamacpp/Cyber-Tiel-Coder-35B-A3B。",
            "",
            "curl 示例（PowerShell 转义麻烦，可用文件体）",
            f'  curl {base}/v1/chat/completions -H "Content-Type: application/json" \\',
            '    --data-binary @req.json',
            "",
            "注意",
            "  · /health 返回 200 不代表可服务；loaded=true 才算就绪。",
            "  · 上下文窗口见看板 /health 行（strata.json 的 --max-context）。",
            "  · fit_max_tokens 开启：max_tokens 超窗自动收缩，不再 400。",
        ]))
        self.api_text.configure(state="disabled")

    # ---------------- 刷新 ----------------
    def _refresh(self):
        try:
            self._refresh_gpu()
            self._refresh_sys()
            self._refresh_svc()
            self._refresh_throughput()
        except Exception:
            pass
        self.after(REFRESH_MS, self._refresh)

    def _refresh_gpu(self):
        g = st_get("gpu") or {}
        v = self.vals
        if not g.get("ok"):
            v["gpu_name"].configure(text="nvidia-smi 不可用")
            for k in ("gpu_util", "gpu_mem", "gpu_temp"):
                v[k].configure(text="")
            return
        v["gpu_name"].configure(text=g.get("name") or "n/a")
        util = g.get("util")
        v["gpu_util"].configure(text=f"{bar(util)} {util:.1f}%" if util is not None else bar(None))
        mu, mt = g.get("mem_used"), g.get("mem_total")
        pct = (mu / mt * 100) if (mu is not None and mt) else None
        v["gpu_mem"].configure(
            text=f"{bar(pct)} {pct:5.1f}%  ({mu/1024**2:.0f}/{mt/1024**2:.0f} MiB)"
            if pct is not None else "")
        temp, pw = g.get("temp"), g.get("power")
        v["gpu_temp"].configure(
            text=(f"{temp:.0f} ℃ / {pw:.0f} W"
                  if temp is not None and pw is not None else "n/a"))

    def _refresh_sys(self):
        s = st_get("sys") or {}
        v = self.vals
        v["cpu_name"].configure(text=s.get("cpu_name") or "n/a")
        ct = s.get("cpu_total")
        v["cpu_total"].configure(text=f"{bar(ct)} {ct:5.1f}%" if ct is not None else bar(None))
        used, total, mp = s.get("mem_used"), s.get("mem_total"), s.get("mem_pct")
        v["mem"].configure(
            text=f"{bar(mp)} {mp:5.1f}%  {used/1024**3:.1f} / {total/1024**3:.1f} GiB"
            if mp is not None else ("（psutil 缺失，pip install psutil 可补全）"))

    def _refresh_svc(self):
        v = self.vals
        h = st_get("health") or {}
        s = st_get("svc") or {}
        e = st_get("eng") or {}
        if h.get("ok"):
            hv = f"loaded={'是' if h.get('loaded') else '否'}  ctx={h.get('max_context')}"
        else:
            hv = "不可达（未启动或加载中）"
        v["health"].configure(text=hv)
        v["svc_state"].configure(
            text=("运行中" if s.get("pid") else
                  ("端口有监听但进程不可见（psutil 缺失？）" if h.get("ok") else "未运行")))
        v["svc_pid"].configure(
            text=f"{s.get('pid') or '—'}  {gib(s.get('rss'))}")
        v["svc_uptime"].configure(text=fmt_duration(s.get("uptime")))
        v["eng_state"].configure(
            text=("运行中" if e.get("pid") else
                  ("加载中/未起" if h.get("ok") else "未运行")))
        v["eng_pid"].configure(text=f"{e.get('pid') or '—'}  {gib(e.get('rss'))}")
        v["eng_cpu"].configure(
            text=f"{e['cpu_time']:.0f} s" if e.get("cpu_time") else "—")
        v["eng_uptime"].configure(text=fmt_duration(e.get("uptime")))

    def _refresh_throughput(self):
        v = self.vals
        reqs = list(st_get("requests") or [])
        d = [r["decode_tps"] for r in reqs[-MEDIAN_WINDOW:] if r.get("decode_tps")]
        p = [r["prefill_tps"] for r in reqs[-MEDIAN_WINDOW:] if r.get("prefill_tps")]
        md = statistics.median(d) if d else None
        mp = statistics.median(p) if p else None
        v["thr_median"].configure(
            text=(f"decode {md:.1f} t/s · prefill {mp:.1f} t/s" if md is not None
                  else "（等待引擎日志请求行…）"))
        v["thr_count"].configure(text=f"已捕获 {len(reqs)} 条完成记录")
        new = [r for r in reqs if (r.get("seq") or 0) > self._req_seen]
        for r in new:
            self.tbl.insert("", 0, values=(
                r.get("ts", ""),
                f"{r.get('prompt'):,}" if r.get("prompt") is not None else "—",
                f"{r.get('completion'):,}" if r.get("completion") is not None else "—",
                f"{r.get('hit')}" if r.get("hit") is not None else "—",
                f"{r.get('prefill_tps'):.1f}" if r.get("prefill_tps") else "—",
                f"{r.get('decode_tps'):.1f}" if r.get("decode_tps") else "—"))
        if new:
            self._req_seen = max((r.get("seq") or 0) for r in new)
            kids = self.tbl.get_children()
            for k in kids[REQ_MAX:]:
                self.tbl.delete(k)

    def _refresh_slow(self):
        try:
            total = int(st_get("log_seq") or 0)
            if total > self._log_seen:
                buf = list(st_get("log_lines") or [])
                n = total - self._log_seen
                new_lines = buf[-n:] if n <= len(buf) else buf
                if new_lines:
                    self.log_text.insert("end", "\n".join(new_lines) + "\n")
                    over = int(self.log_text.index("end-1c").split(".")[0]) - LOG_VIEW_MAX
                    if over > 0:
                        self.log_text.delete("1.0", f"{over + 1}.0")
                    self.log_text.see("end")
                self._log_seen = total
            try:
                cur = self.nb.select()
                on_api = bool(cur) and self.nametowidget(cur) is self.tab_api
            except Exception:
                on_api = False
            if on_api:
                self._refresh_api_text()
        except Exception:
            pass
        finally:
            self.after(1500, self._refresh_slow)


# ============================================================================
def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    st_set("port", launcher_cfg(load_json(CONFIG_FILE, blank_config())).get("port")
           or DEFAULT_PORT)
    threads = [threading.Thread(target=gpu_poller, daemon=True),
               threading.Thread(target=sys_poller, daemon=True)]
    threads.append(threading.Thread(target=svc_poller, daemon=True))
    threads.append(threading.Thread(target=log_poller, daemon=True))
    for t in threads:
        t.start()
    App().mainloop()


if __name__ == "__main__":
    main()
