#!/usr/bin/env python3
"""短周期帧异常检测原型（OpenHarmony RenderService 帧时间戳）。单文件，仅标准库，Python 3.6+。

默认在 PC 上经 hdc 调用设备 hidumper；直接在设备上运行时加 --local。输出全为 ASCII

  python3 fad.py selftest                  合成数据自检，应输出 "selftest ok"
  python3 fad.py probe                     画面持续动画时运行，探测缓冲容量/刷新率/时钟/调用耗时
  python3 fad.py run -s game -d 60         按场景连续判定 60 s，结束时输出报告
  python3 fad.py run -s vid -t <surface名>  改为采集指定 surface（名字可用 hidumper -s RenderService -a surface 查）

probe 报告：
  BUF   cap 缓冲槽位数  fill 已填充数  span 缓冲覆盖时长  ts_max 建议 T_s 上限（span 的一半）
  HZ    est 估计刷新率  gap_ms 缓冲内帧间隔分位数
  CLK   最新帧时间戳减 /proc/uptime，接近 0 说明同为开机单调时钟
  LAT   单次调用耗时（经 hdc 时含传输）
run 报告：
  STATE    各状态窗口数，未知原因 read 读取失败 / ovf 缓冲覆盖 / few 有效帧太少
  TIMELINE 每字符一个短周期：. 正常  a 异常  S 严重  D 漂移  - 未知
           下一行标记完整采样：^ 长周期到期  ! 提前触发
  GAPS     全部帧间隔按 VSync 倍数分档计数；idle 为判为静止而不计入的长空档
  HTR      卡顿时间占比 ms/s；M 最大间隔倍数（相对窗口节奏）
  FPS      有效窗口帧率分位数；cadence 节奏分布（VSync 倍数:窗口数）；fast>0 说明 --hz 偏低
  FULL     完整采样次数：adaptive 本方法（含 early 提前触发）对比固定周期 t0 与固定每个 T_s；TL 长周期范围
  LAT      调用耗时分位数
"""
import argparse
import re
import shlex
import subprocess
import sys
import time

STD_HZ = (30, 48, 60, 72, 90, 120, 144)
HEADER = re.compile(r"screen \[Id:(\d+)\]|surface \[|window \[")
SYM = {"normal": ".", "abnormal": "a", "severe": "S", "drift": "D", "unknown": "-"}


def parse_dump(text, screen_id=0):
    """解析 fps dump，返回一个块的全部槽位（含未填充的 0）；composer 按 screen_id 选块。"""
    blocks = []
    for line in text.splitlines():
        m = HEADER.search(line)
        if m:
            blocks.append((m.group(1), []))
            line = line.rsplit(":", 1)[-1]
        elif not blocks:
            continue
        toks = line.split()
        if toks and all(t.isdigit() for t in toks):
            blocks[-1][1].extend(int(t) for t in toks)
    for sid, vals in blocks:
        if sid == str(screen_id) and vals:
            return vals
    return blocks[0][1] if blocks and blocks[0][1] else None


def busy_gaps(slots, idle_gap=200e6):
    ts = sorted({t for t in slots if t > 0})
    return sorted(b - a for a, b in zip(ts, ts[1:]) if b - a <= idle_gap)


def estimate_hz(slots):
    gaps = busy_gaps(slots)
    if len(gaps) < 8:
        return None
    g = gaps[len(gaps) // 10]
    return min(STD_HZ, key=lambda h: abs(1e9 / h - g))


def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0


class FrameAnomalyDetector:
    """无目标帧率的短周期异常判定：以窗口自身节奏为基准判卡顿，以节奏相对参考值的变化判负载漂移。"""

    def __init__(self, idle_gap_ms=200, tol=0.5, n_min=8,
                 htr_a=10.0, htr_s=50.0, m_a=3.0, m_s=6.0, drift_k=2):
        self.idle_gap = idle_gap_ms * 1e6      # 静止阈值，取自 HgmIdleDetector 的 200 ms
        self.tol = tol                          # 抖动容差，单位为 VSync 周期
        self.n_min = n_min
        self.htr_a, self.htr_s = htr_a, htr_s   # 卡顿时间占比阈值，ms/s
        self.m_a, self.m_s = m_a, m_s           # 最大间隔倍数阈值
        self.drift_k = drift_k
        self.last_ts = None
        self.prev_on_beat = None                # 上一窗口最后一个间隔是否按节奏
        self.c_ref = None                       # 上次完整采样时的节奏
        self.c_last = None
        self.drift_cnt = 0

    def prime(self, slots):
        """丢弃已有历史，从当前最新一帧开始判定。"""
        ts = [t for t in slots if t > 0]
        self.last_ts = max(ts) if ts else None

    def on_full_sample(self):
        """完整采样后调用：以当前节奏为新的参考。"""
        self.c_ref = self.c_last
        self.drift_cnt = 0

    def judge(self, slots, P, touch_active=False):
        """slots: 一次 dump 的全部槽位；P: VSync 周期(ns)。返回 (状态, 指标)。"""
        if slots is None:
            return "unknown", {"reason": "read"}
        ts = sorted({t for t in slots if t > 0})
        new = ts if self.last_ts is None else [t for t in ts if t > self.last_ts]
        overflow = (self.last_ts is not None and len(ts) == len(slots)
                    and ts[0] > self.last_ts)
        seq = ([self.last_ts] if self.last_ts is not None else []) + new
        if new:
            self.last_ts = new[-1]
        if overflow:                            # 缓冲已被覆盖，缺口不可信
            self.prev_on_beat = None
            return "unknown", {"reason": "ovf"}

        gaps = [b - a for a, b in zip(seq, seq[1:])]
        raw = sorted(g for g in gaps if g <= self.idle_gap)
        if len(raw) < self.n_min:
            if gaps:
                self.prev_on_beat = gaps[-1] <= self.idle_gap
            return "unknown", {"reason": "few"}

        busy = sorted(max(1, round(g / P)) for g in raw)
        c = busy[len(busy) // 2]                # 窗口节奏：以 VSync 为单位的帧间隔中位数
        T = c * P
        on_beat = [abs(g - T) <= self.tol * P for g in gaps]
        n, active, hitch, max_m = 0, 0.0, 0.0, 0.0
        h = [0] * 6                             # 间隔分档：1, 2, 3, 4-5, 6+ 个 VSync，静止空档
        for j, g in enumerate(gaps):
            if g > self.idle_gap:
                before = on_beat[j - 1] if j > 0 else self.prev_on_beat
                after = on_beat[j + 1] if j + 1 < len(gaps) else None
                if not (touch_active or (before and after)):
                    h[5] += 1
                    continue                    # 静止空档，不计入
            k = max(1, round(g / P))
            h[k - 1 if k <= 3 else (3 if k <= 5 else 4)] += 1
            n += 1
            active += g
            if g > T + self.tol * P:
                hitch += g - T
            max_m = max(max_m, g / T)
        self.prev_on_beat = on_beat[-1]
        self.c_last = c

        htr = (hitch / 1e6) / (active / 1e9)    # 卡顿时间占比，ms/s
        info = {"n": n, "c": c, "htr": round(htr, 1), "m": round(max_m, 2),
                "fps": round(n / (active / 1e9), 1), "h": h,
                "fast": raw[len(raw) // 10] < 0.75 * P}
        if htr >= self.htr_s or max_m >= self.m_s:
            return "severe", info
        if htr >= self.htr_a or max_m >= self.m_a:
            return "abnormal", info
        if self.c_ref is not None and c != self.c_ref:
            self.drift_cnt += 1
            if self.drift_cnt >= self.drift_k:
                return "drift", info
        else:
            self.drift_cnt = 0
        return "normal", info


class LongCycleSim:
    """按稿子 3.5/3.6 节规则，由短周期状态推演长周期与完整采样时刻（不实际采集）。"""

    def __init__(self, ts, t0=5.0, tmin=2.0, tmax=20.0, gamma=2.0,
                 v_min=3, r_low=0.1, r_high=0.3, k=2, w=3):
        self.ts, self.TL = ts, t0
        self.tmin, self.tmax, self.gamma = tmin, tmax, gamma
        self.v_min, self.r_low, self.r_high = v_min, r_low, r_high
        self.k, self.w = k, w
        self.full, self.early = 1, 0            # 启动时的一次完整采样计入
        self.tl_hist = [t0]
        self._reset()

    def _reset(self):
        self.elapsed, self.V, self.A, self.recent = 0.0, 0, 0, []

    def _full(self):
        self.full += 1
        self.tl_hist.append(self.TL)
        self._reset()

    def step(self, st):
        """输入一个短周期状态；返回 '' / '^'（长周期到期完整采样）/ '!'（提前触发）。"""
        self.elapsed += self.ts
        if st != "unknown":                     # 未知窗口不计入统计，也不进入触发窗口
            self.V += 1
            self.A += st in ("abnormal", "severe")
            self.recent = (self.recent + [st])[-self.w:]
            if st == "drift" or self.recent.count("severe") >= self.k:
                self.TL = max(self.TL / self.gamma, self.tmin)
                self.early += 1
                self._full()
                return "!"
        if self.elapsed >= self.TL - 1e-9:
            r = self.A / self.V if self.V else 0.0
            if self.V >= self.v_min and r <= self.r_low:
                self.TL = min(self.TL * self.gamma, self.tmax)
            elif self.V > 0 and r >= self.r_high:
                self.TL = max(self.TL / self.gamma, self.tmin)
            self._full()
            return "^"
        return ""


class Source:
    def __init__(self, a):
        arg = "composer fps" if a.target is None else a.target + " fps"
        self.dump = "hidumper -s RenderService -a " + shlex.quote(arg)
        self.prefix = None if a.local else shlex.split(a.hdc) + ["shell"]
        self.screen = a.screen

    def read(self, with_uptime=False):
        """返回 (槽位列表或 None, uptime 秒或 None, 调用耗时 ms)。"""
        sh = ("cat /proc/uptime; " if with_uptime else "") + self.dump
        argv = ["sh", "-c", sh] if self.prefix is None else self.prefix + [sh]
        t0 = time.time()
        try:
            out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=5).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            return None, None, (time.time() - t0) * 1e3
        lat = (time.time() - t0) * 1e3
        up = None
        if with_uptime:
            try:
                up = float(out.split()[0])
            except (IndexError, ValueError):
                pass
        return parse_dump(out, self.screen), up, lat


def src_name(a):
    return "composer" if a.target is None else "surface:" + a.target


def cmd_probe(a):
    slots, up, lat = Source(a).read(with_uptime=True)
    if not slots:
        print("E nodata: hidumper returned no timestamps (lat=%dms)" % lat)
        return 1
    ts = sorted(t for t in slots if t > 0)
    span = (ts[-1] - ts[0]) / 1e6 if len(ts) > 1 else 0
    g = [x / 1e6 for x in busy_gaps(slots)]
    clk = "%+dms" % (ts[-1] / 1e6 - up * 1e3) if (ts and up) else "na"
    print("\n".join([
        "=== FAD probe src=%s ===" % src_name(a),
        "BUF cap=%d fill=%d span=%dms ts_max=%.1fs" % (len(slots), len(ts), span, span / 2e3),
        "HZ  est=%s gap_ms p10=%.2f p50=%.2f p90=%.2f max=%.1f"
        % (estimate_hz(slots), pct(g, .1), pct(g, .5), pct(g, .9), max(g or [0])),
        "CLK last-uptime=%s" % clk,
        "LAT call=%dms" % lat,
    ]))
    return 0


def report(a, hz, states, marks, infos, why, lats, sim):
    cnt = {k: states.count(v) for k, v in SYM.items()}
    h = [sum(i["h"][j] for i in infos) for j in range(6)]
    cad = {}
    for i in infos:
        cad[i["c"]] = cad.get(i["c"], 0) + 1
    htr = [i["htr"] for i in infos]
    mm = [i["m"] for i in infos]
    fps = [i["fps"] for i in infos]
    dur = len(states) * a.ts
    L = ["=== FAD run scene=%s src=%s hz=%d ts=%.2f win=%d ===" % (a.scene, src_name(a), hz, a.ts, len(states)),
         "STATE N=%d A=%d S=%d D=%d U=%d (read=%d ovf=%d few=%d)"
         % (cnt["normal"], cnt["abnormal"], cnt["severe"], cnt["drift"], cnt["unknown"],
            why["read"], why["ovf"], why["few"]),
         "TIMELINE .=N a=A S=S D=D -=U / ^=full !=early"]
    for i in range(0, len(states), 50):
        L.append("%4d %s" % (i, "".join(states[i:i + 50])))
        L.append("     %s" % "".join(marks[i:i + 50]))
    L += ["GAPS(xVSync) 1:%d 2:%d 3:%d 4-5:%d 6+:%d idle:%d" % tuple(h),
          "HTR ms/s p50=%.1f p90=%.1f max=%.1f | M p90=%.1f max=%.1f"
          % (pct(htr, .5), pct(htr, .9), max(htr or [0]), pct(mm, .9), max(mm or [0])),
          "FPS p10=%.0f p50=%.0f p90=%.0f | cadence %s | fast=%d"
          % (pct(fps, .1), pct(fps, .5), pct(fps, .9),
             " ".join("%d:%d" % kv for kv in sorted(cad.items())) or "-", sum(i["fast"] for i in infos)),
          "FULL adaptive=%d (early=%d) | fixed%gs=%d | fixedTs=%d | TL %.1f..%.1f last=%.1f"
          % (sim.full, sim.early, a.t0, int(dur // a.t0) + 1, len(states) + 1,
             min(sim.tl_hist), max(sim.tl_hist), sim.TL),
          "LAT ms p50=%d p90=%d max=%d" % (pct(lats, .5), pct(lats, .9), max(lats or [0]))]
    print("\n".join(L))


def cmd_run(a):
    src = Source(a)
    slots, _, _ = src.read()
    if not slots:
        print("E nodata: hidumper returned no timestamps")
        return 1
    hz = a.hz or estimate_hz(slots)
    if not hz:
        print("E hz_unknown: screen idle? add --hz")
        return 1
    P = 1e9 / hz
    det = FrameAnomalyDetector()
    det.prime(slots)
    sim = LongCycleSim(a.ts, a.t0, a.tmin, a.tmax)
    states, marks, infos, lats = [], [], [], []
    why = {"read": 0, "ovf": 0, "few": 0}
    t_end = time.time() + a.dur
    nxt = time.time() + a.ts
    try:
        while nxt <= t_end:
            time.sleep(max(0.0, nxt - time.time()))
            nxt += a.ts
            slots, _, lat = src.read()
            lats.append(lat)
            st, info = det.judge(slots, P)
            if st == "unknown":
                why[info["reason"]] += 1
            else:
                infos.append(info)
                if det.c_ref is None:           # 首个有效窗口建立参考节奏
                    det.on_full_sample()
            mk = sim.step(st)
            if mk:
                det.on_full_sample()
            states.append(SYM[st])
            marks.append(mk or " ")
            if a.verbose:
                print("W %d %s %s%s lat=%d" % (len(states), st, info, mk, lat))
    except KeyboardInterrupt:
        pass
    report(a, hz, states, marks, infos, why, lats, sim)
    return 0


def cmd_selftest(_):
    P, CAP = 1e9 / 60, 384

    class Screen:
        def __init__(self):
            self.t, self.buf = 10 ** 12, []

        def emit(self, gaps):
            for k in gaps:
                self.t += int(k * P)
                self.buf.append(self.t)
            self.buf = self.buf[-CAP:]

        def slots(self):
            return [0] * (CAP - len(self.buf)) + self.buf

    s, d, bad = Screen(), FrameAnomalyDetector(), []
    cases = [
        ("steady", [1] * 30, False, "normal"),
        ("miss1", [1] * 14 + [2] + [1] * 14, False, "abnormal"),
        ("bursts", [1] * 8 + [3] + [1] * 8 + [2] + [1] * 8 + [3], False, "severe"),
        ("hitch100ms", [1] * 14 + [6] + [1] * 10, False, "severe"),
        ("anim_end", [1] * 20 + [30], False, "normal"),
        ("static", [], False, "unknown"),
        ("resume", [1] * 30, False, "normal"),
        ("30fps#1", [2] * 15, False, "normal"),
        ("30fps#2", [2] * 15, False, "drift"),
        ("resample", None, False, None),
        ("30fps_ok", [2] * 15, False, "normal"),
        ("touch_stall", [2] * 8 + [12] + [2] * 8, True, "severe"),
        ("overflow", "ovf", False, "unknown"),
    ]
    s.emit([1] * 30)
    d.judge(s.slots(), P)
    d.on_full_sample()
    for name, gaps, touch, want in cases:
        if gaps is None:
            d.on_full_sample()
            continue
        if gaps == "ovf":
            s.emit([1] * 400)
            gaps = [1] * 5
        s.emit(gaps)
        got = d.judge(s.slots(), P, touch)[0]
        if got != want:
            bad.append("%s:%s!=%s" % (name, got, want))

    flat = "-- The recently fps records info of screens:\n The fps of screen [Id:0] is: 0 0 5 6\n"
    lines = "The fps of screen [Id:1] is:\n9\nThe fps of screen [Id:0] is:\n0\n7\n8\n"
    surf = "\n surface [game] Id[12]:\n0\n3\n4\n"
    for name, txt, want in (("flat", flat, [0, 0, 5, 6]), ("lines", lines, [0, 7, 8]),
                            ("surface", surf, [0, 3, 4])):
        if parse_dump(txt) != want:
            bad.append("parse_" + name)
    if estimate_hz([0] * 10 + [int(i * 1e9 / 120) + 1 for i in range(50)]) != 120:
        bad.append("hz")

    sim = LongCycleSim(1.0)                     # 稳定：5 s 到期后延长，再 10 s 到期延长至上限
    marks = "".join(sim.step("normal") for _ in range(15))
    if marks != "^^" or sim.TL != 20.0 or sim.full != 3:
        bad.append("sim_stable:%s/%.0f" % (marks, sim.TL))
    sim = LongCycleSim(1.0)                     # 连续严重：第 2 个严重窗口提前触发并缩短
    if [sim.step(x) for x in ("severe", "unknown", "severe")] != ["", "", "!"] or sim.TL != 2.5:
        bad.append("sim_early")
    sim = LongCycleSim(1.0)                     # 全未知：到期只保持，不延长
    for _ in range(5):
        sim.step("unknown")
    if sim.TL != 5.0 or sim.full != 2:
        bad.append("sim_unknown")

    total = len([c for c in cases if c[1] is not None]) + 7
    print("selftest ok %d/%d" % (total, total) if not bad else "selftest FAIL " + " ".join(bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="短周期帧异常检测原型")
    ap.add_argument("cmd", choices=("selftest", "probe", "run"))
    ap.add_argument("-s", "--scene", default="x", help="场景标签，原样写入报告")
    ap.add_argument("-d", "--dur", type=float, default=60, help="运行时长 s")
    ap.add_argument("--ts", type=float, default=1.0, help="短周期 T_s，s；需小于 probe 给出的 ts_max")
    ap.add_argument("--hz", type=int, default=0, help="屏幕刷新率；0 为自动估计")
    ap.add_argument("--t0", type=float, default=5.0, help="基础长周期 s，同时作为固定周期对照")
    ap.add_argument("--tmin", type=float, default=2.0)
    ap.add_argument("--tmax", type=float, default=20.0)
    ap.add_argument("-t", "--target", default=None, help="surface/窗口名；缺省为 composer（全局）")
    ap.add_argument("--screen", type=int, default=0)
    ap.add_argument("--hdc", default="hdc", help='hdc 命令，如 "hdc -t <序列号>"')
    ap.add_argument("--local", action="store_true", help="在设备上直接运行")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐窗口输出")
    a = ap.parse_args()
    return {"selftest": cmd_selftest, "probe": cmd_probe, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
