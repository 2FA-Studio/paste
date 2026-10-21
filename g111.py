"""HDC帧呈现间隔记录器，仅Python标准库，不修改设备，不触发完整资源采集。"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


TZ = timezone(timedelta(hours=8))
FRAME_FIELDS = ["poll_seq", "screen_id", "epoch", "device_timestamp_ns",
                "device_relative_s", "previous_timestamp_ns", "interval_ms",
                "baseline_ms", "threshold_ms", "detected_host_time", "read_latency_ms"]
EVENT_FIELDS = ["event_id", "poll_seq", "screen_id", "epoch", "event_type",
                "severity", "device_start_ns", "device_end_ns", "device_start_s",
                "device_end_s", "interval_ms", "span_ms", "baseline_ms", "threshold_ms",
                "reference_excess_ms", "observed_median_ms", "ratio",
                "detected_host_time", "read_latency_ms", "interpretation"]


def wall_time():
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def parse_composer(text, screen_id):
    """只解析明确匹配的屏幕段；不将其他屏幕数字或错误信息当作时间戳。"""
    selected = False
    found = False
    points = []
    for line in text.splitlines():
        header = re.search(r"The fps of screen\s*\[\s*Id\s*:\s*(\d+)\s*\]\s*is", line, re.I)
        if header:
            selected = int(header.group(1)) == screen_id
            found = found or selected
        elif selected and re.fullmatch(r"\s*\d+\s*", line):
            value = int(line.strip())
            if 0 < value < 2**63 - 1:
                points.append(value)
        elif selected and re.search(r"\b(?:surface|window)\s*\[", line, re.I):
            selected = False
    if not found:
        raise ValueError(f"没有找到 screen [Id:{screen_id}] 的composer时间戳段；检查raw_dumps.jsonl")
    return sorted(set(points))


class Recorder:
    """固定会话参考，连续记录候选长间隔；不模拟完整资源核验或自动重建参考。"""
    def __init__(self, screen_id=0, warmup=16):
        self.screen_id = screen_id
        self.warmup = warmup
        self.last = None
        self.origin = None
        self.baseline = None
        self.scale = None
        self.history = []
        self.block = []
        self.direction = 0
        self.shift_count = 0
        self.shift_start = None
        self.epoch = 0
        self.event_count = 0
        self.frame_count = 0

    def prime(self, points):
        self.last = self.origin = points[-1]
        self.baseline = self.scale = None
        self.history.clear()
        self.block.clear()
        self.direction = self.shift_count = 0
        self.shift_start = None

    def observe(self, points, poll_seq, detected, latency):
        points = sorted(set(t for t in points if t > 0))
        if not points:
            return [], [], "NO_NEW_FRAME"
        if self.last is None:
            self.prime(points)
            return [], [], "PRIMED_IGNORE_PRESTART_HISTORY"
        if points[-1] < self.last:
            self.epoch += 1
            self.prime(points)
            return [], [], "UNKNOWN_TIME_RESET"
        fresh = [t for t in points if t > self.last]
        if not fresh:
            return [], [], "NO_NEW_FRAME"
        if self.last not in points:
            self.epoch += 1
            self.prime(points)
            return [], [], "UNKNOWN_COVERAGE_GAP"
        frames, events = [], []
        for t in fresh:
            previous = self.last
            self.last = t
            interval = (t - previous) / 1e6
            self.history = (self.history + [interval])[-self.warmup:]
            threshold = None
            if self.baseline is None and len(self.history) >= self.warmup:
                b = median(self.history)
                mad = median(abs(x - b) for x in self.history)
                if mad / b <= 0.15 and max(self.history) < 2.5 * b:
                    self.baseline = b
                    self.scale = max(1.4826 * mad, 0.05 * b)
            if self.baseline is not None:
                threshold = max(2.5 * self.baseline, self.baseline + 6 * self.scale)
            common = {"poll_seq": poll_seq, "screen_id": self.screen_id, "epoch": self.epoch,
                      "baseline_ms": self.baseline, "threshold_ms": threshold,
                      "detected_host_time": detected, "read_latency_ms": round(latency, 3)}
            frames.append({**common, "device_timestamp_ns": t,
                           "device_relative_s": (t - self.origin) / 1e9,
                           "previous_timestamp_ns": previous, "interval_ms": interval})
            self.frame_count += 1
            if self.baseline is None:
                continue

            def event(kind, start, end, **extra):
                self.event_count += 1
                events.append({**common, "event_id": self.event_count, "event_type": kind,
                               "device_start_ns": start, "device_end_ns": end,
                               "device_start_s": (start - self.origin) / 1e9,
                               "device_end_s": (end - self.origin) / 1e9,
                               "span_ms": (end - start) / 1e6, **extra})

            if interval >= threshold:
                severe = interval >= max(6 * self.baseline, 100.0)
                event("LONG_FRAME_GAP_CANDIDATE", previous, t,
                      severity="severe" if severe else "candidate", interval_ms=interval,
                      reference_excess_ms=max(0.0, interval - self.baseline),
                      interpretation="长呈现间隔；可能为卡顿、静止恢复或主动节奏变化，未判定原因")

            self.block.append((previous, t, interval))
            while self.block and sum(v[2] for v in self.block) > 500:
                self.block.pop(0)
                self.direction = self.shift_count = 0
                self.shift_start = None
            if len(self.block) >= 8:
                bvalues = [v[2] for v in self.block]
                ratio = median(bvalues) / self.baseline
                direction = 1 if ratio >= 1.5 else (-1 if ratio <= 1 / 1.5 else 0)
                if direction:
                    if direction == self.direction:
                        self.shift_count += 1
                    else:
                        self.shift_count = 1
                        self.shift_start = self.block[0][0]
                    self.direction = direction
                    if self.shift_count == 2:
                        event("CADENCE_CHANGE", self.shift_start, t, severity="state_change",
                              observed_median_ms=median(bvalues), ratio=ratio,
                              interpretation="持续节奏变化，需要重新核验；不等同于卡顿")
                else:
                    self.direction = self.shift_count = 0
                    self.shift_start = None
                self.block.clear()
        return frames, events, "VALID" if self.baseline is not None else "WARMING_UP"


def hdc_call(prefix, args, timeout):
    result = subprocess.run(prefix + args, capture_output=True, timeout=timeout)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"hdc退出码{result.returncode}: {output.strip()[:400]}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", default="hdc", help="hdc可执行文件路径")
    parser.add_argument("--device", help="hdc list targets显示的设备标识")
    parser.add_argument("--screen", type=int, default=0)
    parser.add_argument("--duration", type=float, default=60, help="采集秒数，0表示直到Ctrl+C")
    parser.add_argument("--poll-ms", type=float, default=200)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--out", type=Path, help="新的输出目录，不能覆盖已有目录")
    args = parser.parse_args()
    if args.poll_ms < 20 or args.duration < 0 or args.timeout <= 0 or args.screen < 0:
        parser.error("poll-ms须>=20，duration须>=0，timeout须>0，screen须>=0")
    executable = shutil.which(args.hdc)
    if executable is None:
        parser.exit(2, "找不到hdc。请先安装HarmonyOS命令行工具，或传入 --hdc /绝对路径/hdc\n")
    prefix = [executable]
    if args.device:
        prefix += ["-t", args.device]
    else:
        listing = hdc_call(prefix, ["list", "targets"], args.timeout)
        targets = [line.strip().split()[0] for line in listing.splitlines()
                   if line.strip() and not line.lstrip().startswith("[")]
        if len(targets) != 1:
            parser.exit(2, f"请确认连接一台设备，或使用 --device 指定。hdc返回：\n{listing}\n")
        args.device = targets[0]
        prefix += ["-t", args.device]
    # HDC传递完整远端命令，保留-a参数内的空格；不使用本地shell执行。
    remote = "hidumper -s RenderService -a 'composer fps'"
    probe = hdc_call(prefix, ["shell", remote], args.timeout)
    parse_composer(probe, args.screen)
    out = args.out or Path(__file__).parent / "runs" / datetime.now(TZ).strftime("run-%Y%m%d-%H%M%S-%f")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    session = {"started_host_time": wall_time(), "device": args.device, "hdc": executable,
               "screen_id": args.screen, "poll_ms": args.poll_ms, "duration_s": args.duration,
               "timestamp_kind": "device_present_fence_ns", "baseline_policy": "fixed_per_epoch",
               "full_resource_collection": False, "confirmed_jank": False,
               "command": prefix + ["shell", remote]}
    recorder = Recorder(args.screen)
    # 丢弃启动前缓存；只有启动后新增的帧参与学习和事件记录。
    recorder.observe(parse_composer(probe, args.screen), 0, wall_time(), 0)
    session["initial_anchor_ns"] = recorder.origin
    session["warmup_intervals"] = recorder.warmup
    start = time.monotonic()
    polls = errors = consecutive_errors = 0
    exit_code = 0
    print(f"输出目录：{out}\n请在真机持续滑动或播放动画约2秒建立参考，随后复现卡顿。", flush=True)
    print("这是候选卡顿区间记录器；Ctrl+C结束并保存。", flush=True)
    try:
        with (out / "frames.csv").open("w", newline="", encoding="utf-8-sig") as ff, \
             (out / "events.csv").open("w", newline="", encoding="utf-8-sig") as ef, \
             (out / "raw_dumps.jsonl").open("w", encoding="utf-8") as rf:
            frames_writer = csv.DictWriter(ff, fieldnames=FRAME_FIELDS)
            events_writer = csv.DictWriter(ef, fieldnames=EVENT_FIELDS)
            frames_writer.writeheader()
            events_writer.writeheader()
            while args.duration == 0 or time.monotonic() - start < args.duration:
                polls += 1
                tick = time.monotonic()
                requested = wall_time()
                text = ""
                try:
                    text = hdc_call(prefix, ["shell", remote], args.timeout)
                    latency = (time.monotonic() - tick) * 1000
                    detected = wall_time()
                    old_baseline = recorder.baseline
                    frames, events, quality = recorder.observe(
                        parse_composer(text, args.screen), polls, detected, latency)
                    consecutive_errors = 0
                    frames_writer.writerows(frames)
                    events_writer.writerows(events)
                    if old_baseline is None and recorder.baseline is not None:
                        print(f"参考已建立：{recorder.baseline:.3f} ms；开始检测新增间隔。", flush=True)
                    for row in events:
                        detail = (f"间隔{row['interval_ms']:.2f}ms" if "interval_ms" in row
                                  else f"节奏比{row['ratio']:.2f}")
                        print(f"[{detected}] {row['event_type']} {detail}，设备相对区间"
                              f"{row['device_start_s']:.3f}–{row['device_end_s']:.3f}s", flush=True)
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                    errors += 1
                    consecutive_errors += 1
                    quality = f"UNKNOWN_COLLECTION: {exc}"
                    print(quality, file=sys.stderr, flush=True)
                rf.write(json.dumps({"poll_seq": polls, "requested_host_time": requested,
                                     "completed_host_time": wall_time(), "quality": quality,
                                     "raw": text}, ensure_ascii=False) + "\n")
                ff.flush()
                ef.flush()
                rf.flush()
                if consecutive_errors >= 5:
                    exit_code = 2
                    print("连续5次读取失败，停止采集；已保留此前记录。", file=sys.stderr)
                    break
                remaining = args.poll_ms / 1000 - (time.monotonic() - tick)
                if args.duration:
                    remaining = min(remaining, args.duration - (time.monotonic() - start))
                time.sleep(max(0, remaining))
    except KeyboardInterrupt:
        print("\n采集结束。", flush=True)
    finally:
        session.update({"finished_host_time": wall_time(), "elapsed_s": time.monotonic() - start,
                        "poll_count": polls, "error_count": errors,
                        "new_interval_count": recorder.frame_count,
                        "event_count": recorder.event_count, "baseline_ms": recorder.baseline,
                        "exit_code": exit_code})
        (out / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n")
        print(f"已保存：{recorder.frame_count}个间隔，{recorder.event_count}个候选事件。\n{out}", flush=True)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        sys.exit(2)
