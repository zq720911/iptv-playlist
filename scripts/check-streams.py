#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐条实测 m3u 里的直播流是否真的能播，并统计编码格式。

判断逻辑：
  HTTP 200 + 正文含 #EXTM3U        -> 有效 HLS
  HTTP 200 + 正文不含 #EXTM3U      -> 可能是 TS 裸流（播放器需要支持）
  HTTP 4xx/5xx                     -> 失效
  连接失败/超时                    -> 失效（或被墙、或仅特定网络可达）

用法：
  python3 check-streams.py output/result.m3u
  python3 check-streams.py output/result.m3u --workers 24 --timeout 6
  python3 check-streams.py output/result.m3u --dead-out dead.txt --by-group
  python3 check-streams.py output/result.m3u --only-ipv6      # 只测 IPv6 线路
"""

import argparse
import collections
import concurrent.futures
import re
import ssl
import sys
import urllib.error
import urllib.request

UA_DEFAULT = "Mozilla/5.0 (AppleTV; tvOS 17.0) AppleWebKit/605.1.15 VLC/3.0.20"
CODEC_RE = re.compile(r'CODECS="([^"]+)"', re.IGNORECASE)
RES_RE = re.compile(r"RESOLUTION=(\d+x\d+)", re.IGNORECASE)


def parse_m3u(path):
    entries, name, group = [], "", ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n").strip()
            if line.startswith("#EXTINF"):
                name = line.split(",", 1)[1].strip() if "," in line else ""
                m = re.search(r'group-title="([^"]*)"', line)
                group = m.group(1) if m else ""
            elif line.startswith(("http://", "https://", "rtsp://", "rtmp://")):
                entries.append({"name": name, "group": group, "url": line})
                name, group = "", ""
    return entries


def family_of(url):
    if re.match(r"^[a-z]+://\[", url, re.I):
        return "ipv6"
    if re.match(r"^[a-z]+://\d{1,3}(?:\.\d{1,3}){3}", url, re.I):
        return "ipv4"
    return "域名"


def probe(entry, timeout, ua):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(entry["url"], headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(4096)
            status = resp.status
    except urllib.error.HTTPError as exc:
        entry.update(state="http错误", detail=f"HTTP {exc.code}")
        return entry
    except Exception as exc:  # noqa: BLE001
        entry.update(state="连接失败", detail=str(exc)[:60])
        return entry

    text = body.decode("utf-8", "replace")
    if "#EXTM3U" in text:
        entry["state"] = "有效HLS"
        codecs = CODEC_RE.findall(text)
        entry["codec"] = ",".join(sorted({c.upper() for c in codecs})) if codecs else ""
        res = RES_RE.findall(text)
        entry["res"] = res[0] if res else ""
    else:
        entry["state"] = "非HLS裸流"
        entry["detail"] = f"HTTP {status}, {len(body)}B"
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="实测 m3u 直播流可用性并统计编码")
    ap.add_argument("playlist")
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--user-agent", default=UA_DEFAULT)
    ap.add_argument("--dead-out", help="把失效频道写到该文件")
    ap.add_argument("--alive-out",
                    help="把【实测存活】的频道写到该文件（可直接给播放器用）")
    ap.add_argument("--by-group", action="store_true", help="按分组给出存活率")
    ap.add_argument("--only-ipv6", action="store_true", help="只测 IPv6 线路")
    ap.add_argument("--only-ipv4", action="store_true", help="只测 IPv4 线路")
    args = ap.parse_args()

    entries = parse_m3u(args.playlist)
    if args.only_ipv6:
        entries = [e for e in entries if family_of(e["url"]) == "ipv6"]
    if args.only_ipv4:
        entries = [e for e in entries if family_of(e["url"]) == "ipv4"]
    for e in entries:
        e["family"] = family_of(e["url"])

    if not entries:
        print("[error] 没有解析到任何流地址", file=sys.stderr)
        return 2

    print(f"[info] 开始探测 {len(entries)} 条线路（{args.workers} 并发，超时 {args.timeout}s）…",
          file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda e: probe(e, args.timeout, args.user_agent), entries))

    states = collections.Counter(r["state"] for r in results)
    total = len(results)
    ok = states.get("有效HLS", 0)

    print(f"\n═══ 总览（{total} 条）═══")
    for st in ("有效HLS", "非HLS裸流", "http错误", "连接失败"):
        n = states.get(st, 0)
        bar = "█" * round(n / total * 40) if total else ""
        print(f"  {st:<10} {n:>4}  {n/total*100:5.1f}%  {bar}")

    fams = collections.Counter(r["family"] for r in results)
    fam_ok = collections.Counter(r["family"] for r in results if r["state"] == "有效HLS")
    print("\n═══ 按地址族 ═══")
    for fam in ("ipv6", "ipv4", "域名"):
        if fams.get(fam):
            print(f"  {fam:<5} 存活 {fam_ok.get(fam,0):>3}/{fams[fam]:<3} "
                  f"({fam_ok.get(fam,0)/fams[fam]*100:.0f}%)")

    if args.by_group:
        groups = collections.defaultdict(lambda: [0, 0])
        for r in results:
            groups[r["group"] or "(无分组)"][0] += 1
            if r["state"] == "有效HLS":
                groups[r["group"] or "(无分组)"][1] += 1
        print("\n═══ 按分组 ═══")
        for g, (t, o) in sorted(groups.items(), key=lambda kv: -kv[1][0]):
            print(f"  {g:<8} 存活 {o:>3}/{t:<3} ({o/t*100:.0f}%)")

    codecs = collections.Counter(r.get("codec", "") for r in results if r.get("codec"))
    if codecs:
        print("\n═══ 存活流的编码（CODECS 属性）═══")
        for c, n in codecs.most_common(8):
            print(f"  {n:>4}  {c}")

    dead = [r for r in results if r["state"] != "有效HLS"]
    if dead:
        print(f"\n═══ 异常线路（{len(dead)} 条，最多列 25）═══")
        for r in dead[:25]:
            print(f"  [{r['state']}] {r['name'][:22]:<24} {r['detail'][:52]}")

    if args.dead_out:
        with open(args.dead_out, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n")
            for r in dead:
                fh.write(f'#EXTINF:-1 group-title="{r["group"]}",{r["name"]}\n{r["url"]}\n')
        print(f"\n[ok] 异常线路已写入 {args.dead_out}", file=sys.stderr)

    if args.alive_out:
        alive = [r for r in results if r["state"] == "有效HLS"]
        with open(args.alive_out, "w", encoding="utf-8") as fh:
            fh.write('#EXTM3U url-tvg="https://epg.51zmt.top:8001/e.xml"\n')
            for r in alive:
                fh.write(f'#EXTINF:-1 group-title="{r["group"]}",{r["name"]}\n{r["url"]}\n')
        print(f"[ok] 实测存活 {len(alive)} 条已写入 {args.alive_out}", file=sys.stderr)

    print(f"\n[ok] 可用率 {ok}/{total} = {ok/total*100:.1f}%", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
