#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取所有源 → 保留【全部】候选线路 → 逐条实测 → 只输出确认能播的频道。

为什么必须在本地跑：
  免费公开源里 60%~85% 的线路是死链。而按"每台留 2 条 + 按画质排序"的常规
  构建方式，死线路会把同一频道里能播的那条挤掉 —— 所以必须先把【全部】线路
  都留着，在你自己网络上逐条探测，再筛出活的。
  GitHub Actions 跑在境外，探测中文直播源的结果没有参考价值，所以这步不能进 CI。

用法：
  python3 scripts/scan-and-verify.py sources.txt -o output/result.alive.m3u
  python3 scripts/scan-and-verify.py sources.txt -o out.m3u --insecure --workers 24
  python3 scripts/scan-and-verify.py sources.txt -o out.m3u --max-per-channel 1
  python3 scripts/scan-and-verify.py sources.txt -o out.m3u --timeout 8 --list
"""

import argparse
import collections
import concurrent.futures
import importlib.util
import os
import re
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "VLC/3.0.20 LibVLC/3.0.20"


def load_builder():
    """复用 build-playlist.py 的台名清洗 / 归并 / 筛选规则，保证两份列表口径一致。"""
    path = os.path.join(HERE, "build-playlist.py")
    spec = importlib.util.spec_from_file_location("build_playlist", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BP = load_builder()


def probe(url, timeout, ua):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return "#EXTM3U" in resp.read(4096).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="逐条实测所有源，只输出能播的频道")
    ap.add_argument("sources", help="源清单文件")
    ap.add_argument("-o", "--output", default="output/result.alive.m3u")
    ap.add_argument("--epg", default="https://epg.51zmt.top:8001/e.xml")
    ap.add_argument("--include", default=None, help="保留频道名正则（默认同 build-playlist）")
    ap.add_argument("--exclude", default=None, help="丢弃频道名正则（默认同 build-playlist）")
    ap.add_argument("--exclude-groups", default=None,
                    help="按分组整体排除，逗号分隔，可选：央视/卫视/港澳台/其他。"
                         "例如 --exclude-groups 港澳台")
    ap.add_argument("--max-per-channel", type=int, default=2,
                    help="每台最多保留几条实测存活的线路，默认 2")
    ap.add_argument("--family", choices=["any", "ipv4", "ipv6"], default="any")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--user-agent", default=UA)
    ap.add_argument("--insecure", action="store_true",
                    help="拉源时跳过 SSL 校验（本机有 HTTPS 中间人代理时需要）")
    ap.add_argument("--list", action="store_true", help="打印存活清单")
    ap.add_argument("--min-channels", type=int, default=10)
    args = ap.parse_args()

    inc = re.compile(args.include or BP.DEFAULT_INCLUDE)
    exc = re.compile(args.exclude or BP.DEFAULT_EXCLUDE)
    skip_groups = {g.strip() for g in (args.exclude_groups or "").split(",") if g.strip()}

    with open(args.sources, encoding="utf-8") as fh:
        specs = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
    if not specs:
        print("[error] 源清单为空", file=sys.stderr)
        return 2

    # ---- 1. 拉源并保留【全部】候选线路 -----------------------------------
    cands = collections.OrderedDict()          # url -> display_name
    ok_src = 0
    for spec in specs:
        try:
            text = BP.load_source(spec, 60, args.insecure)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 源失败，跳过: {spec} ({str(exc)[:60]})", file=sys.stderr)
            continue
        got = 0
        for _attrs, raw_name, url in BP.parse_m3u(text):
            name = BP.display_name(raw_name)
            if not name or not inc.search(name) or exc.search(name):
                continue
            if skip_groups and BP.group_of(name) in skip_groups:
                continue
            fam = BP.address_family(url)
            if args.family != "any" and fam != "unknown" and fam != args.family:
                continue
            if url not in cands:
                cands[url] = name
                got += 1
        ok_src += 1
        print(f"[ok] {spec} -> 候选 {got} 条", file=sys.stderr)

    print(f"\n[info] 可用源 {ok_src}/{len(specs)}；候选线路合计 {len(cands)} 条（无每台上限）",
          file=sys.stderr)
    if not cands:
        print("[error] 没有候选线路", file=sys.stderr)
        return 2

    # ---- 2. 逐条实测 -----------------------------------------------------
    print(f"[info] 逐条探测中（{args.workers} 并发，超时 {args.timeout}s）…", file=sys.stderr)
    urls = list(cands)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda u: probe(u, args.timeout, args.user_agent), urls))

    alive = [(cands[u], u) for u, good in zip(urls, results) if good]
    rate = len(alive) / len(urls) * 100
    print(f"[info] 实测存活 {len(alive)}/{len(urls)} = {rate:.0f}%", file=sys.stderr)

    # ---- 3. 按台归并，每台留前 N 条 --------------------------------------
    by_ch = collections.OrderedDict()
    for name, url in alive:
        key = BP.channel_key(name)
        by_ch.setdefault(key, {"name": name, "urls": []})
        if len(by_ch[key]["urls"]) < max(1, args.max_per_channel):
            by_ch[key]["urls"].append(url)

    kept = []
    for v in by_ch.values():
        for url in v["urls"]:
            kept.append({
                "name": v["name"], "url": url,
                "group": BP.group_of(v["name"]), "qrank": BP.quality_rank(v["name"]),
            })

    def final_key(it):
        gorder = {"央视": 0, "卫视": 1, "港澳台": 2, "其他": 3}.get(it["group"], 4)
        m = BP.CCTV_RE.search(it["name"])
        if m:
            return (gorder, 0, int(m.group(1)), it["qrank"], it["name"])
        if BP.CCTV_SPECIAL_RE.search(it["name"]):
            return (gorder, 0, 900, it["qrank"], it["name"])
        if it["group"] == "央视":
            return (gorder, 0, 800, it["qrank"], it["name"])
        return (gorder, 1, 0, it["qrank"], it["name"])

    kept.sort(key=final_key)

    # ---- 4. 输出 ---------------------------------------------------------
    lines = [f'#EXTM3U url-tvg="{args.epg}"' if args.epg else "#EXTM3U"]
    for it in kept:
        lines.append(f'#EXTINF:-1 tvg-name="{it["name"]}" group-title="{it["group"]}",{it["name"]}')
        lines.append(it["url"])

    outdir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(outdir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    groups = collections.Counter(it["group"] for it in kept)
    uniq = len(by_ch)
    print(f"[ok] 输出 {len(kept)} 条线路 / {uniq} 个台"
          f"（央视 {groups['央视']} / 卫视 {groups['卫视']} / "
          f"港澳台 {groups['港澳台']} / 其他 {groups['其他']}）-> {args.output}",
          file=sys.stderr)

    if args.list:
        for it in kept:
            print(f"    {it['group']}\t{it['name']}", file=sys.stderr)

    if uniq < args.min_channels:
        print(f"[error] 台数 {uniq} 低于阈值 {args.min_channels}，未写入可信结果", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
