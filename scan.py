#!/usr/bin/env python3
"""IPTV 高清源自动扫描器 (cao9ip 同款思路, 自写实现).

拉取上游开源直播源, 用 ffprobe 逐条探测真实分辨率, 只保留 1080P
(>=1920x1080) 与 4K (>=3840x2160), 按频道分组, 每频道最多保留 3 条,
输出 live.m3u / live.txt / stats.json, 供 TVBox / APTV / TiviMate 订阅.

上游 (公开开源源):
  - iptv-org streams/cn.m3u
  - fanmingming/live tv/m3u/ipv6.m3u
  - YueChan/Live IPTV.m3u

环境变量:
  OUT_DIR         输出目录, 默认 ./out
  PROBE_TIMEOUT   单源探测超时秒数, 默认 7
  MAX_PER_CHANNEL 每频道最多保留链接数, 默认 3
  WORKERS         并发探测数, 默认 20
  LIMIT           只测前 N 条 (冒烟测试用, 默认不限制)
  PROXY_PREFIX    上游拉取代理前缀, 默认空
"""

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

UPSTREAMS = [
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u",
    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
    "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
]

OUT_DIR = os.environ.get("OUT_DIR", "./out")
TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "7"))
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "3"))
WORKERS = int(os.environ.get("WORKERS", "20"))
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
PROXY_PREFIX = os.environ.get("PROXY_PREFIX", "")


def fetch(url):
    full = PROXY_PREFIX + url if PROXY_PREFIX else url
    req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_m3u(text):
    """返回 [(name, group, url)]。"""
    entries = []
    name, group = None, ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            group = m.group(1) if m else ""
            name = line.split(",", 1)[1].strip() if "," in line else ""
            name = re.sub(r"\s*\((1080p|720p|480p|4K|UHD)\)\s*$", "", name, flags=re.I)
            name = re.sub(r"\s*\[(1080P|720P|4K|UHD)\]\s*$", "", name)
        elif line and not line.startswith("#"):
            if name:
                entries.append((name, group, line))
            name, group = None, ""
    return entries


def clean_name(name):
    return re.sub(r"\s+", " ", name).strip()


def group_of(name, upstream_group):
    if upstream_group:
        return upstream_group
    if name.startswith("CCTV"):
        return "央视"
    if "卫视" in name:
        return "卫视"
    if "4K" in name:
        return "4K"
    return "其他"


def probe(url):
    """返回 (width, height) 或 None。只探测视频流, 超时即放弃。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "json", url],
            capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        return (w, h) if w and h else None
    except Exception:
        return None


def main():
    started = time.time()
    seen_urls = set()
    channels = []  # (name, group, url)
    for upstream in UPSTREAMS:
        try:
            entries = parse_m3u(fetch(upstream))
            print(f"upstream {upstream}: {len(entries)} entries", flush=True)
        except Exception as exc:
            print(f"upstream {upstream} FAILED: {exc}", flush=True)
            continue
        for name, grp, url in entries:
            url = url.strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            channels.append((clean_name(name), grp, url))
    print(f"unique streams to probe: {len(channels)}", flush=True)
    if LIMIT:
        channels = channels[:LIMIT]

    results = {}

    def work(item):
        name, grp, url = item
        res = probe(url)
        return (name, grp, url, res)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, grp, url, res in pool.map(work, channels):
            if res is None:
                continue
            w, h = res
            if w >= 3840 and h >= 2160:
                label = "4K"
            elif w >= 1920 and h >= 1080:
                label = "1080P"
            else:
                continue
            key = name
            results.setdefault(key, {"group": group_of(name, grp), "links": []})
            results[key]["links"].append((label, url, w, h))

    kept = {}
    for name, info in results.items():
        links = sorted(info["links"], key=lambda x: (0 if x[0] == "4K" else 1, x[1]))
        seen = set()
        uniq = []
        for label, url, w, h in links:
            if url in seen:
                continue
            seen.add(url)
            uniq.append((label, url, w, h))
        kept[name] = {"group": info["group"], "links": uniq[:MAX_PER_CHANNEL]}

    n4k = sum(1 for v in kept.values() for l in v["links"] if l[0] == "4K")
    n1080 = sum(1 for v in kept.values() for l in v["links"] if l[0] == "1080P")
    elapsed = round(time.time() - started, 1)
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(OUT_DIR, exist_ok=True)
    groups = {}
    for name, info in sorted(kept.items()):
        groups.setdefault(info["group"], []).append((name, info["links"]))

    with open(os.path.join(OUT_DIR, "live.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for grp in sorted(groups):
            for name, links in groups[grp]:
                for label, url, w, h in links:
                    f.write(f'#EXTINF:-1 group-title="{grp}",{name} [{label}]\n{url}\n')

    with open(os.path.join(OUT_DIR, "live.txt"), "w", encoding="utf-8") as f:
        for grp in sorted(groups):
            f.write(f"{grp},#genre#\n")
            for name, links in groups[grp]:
                for label, url, w, h in links:
                    f.write(f"{name} [{label}],{url}\n")

    stats = {
        "generated": generated,
        "elapsed_seconds": elapsed,
        "channels": len(kept),
        "links_4k": n4k,
        "links_1080p": n1080,
        "links_total": n4k + n1080,
        "upstreams": UPSTREAMS,
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
