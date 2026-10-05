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
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from urllib.parse import urljoin

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


_CTX = None


def _ctx():
    global _CTX
    if _CTX is None:
        _CTX = ssl.create_default_context()
        _CTX.check_hostname = False
        _CTX.verify_mode = ssl.CERT_NONE
    return _CTX


def fetch_manifest(url, timeout, ua):
    """返回清单文本；失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            return resp.read(8192).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def probe(url, timeout, ua):
    body = fetch_manifest(url, timeout, ua)
    return bool(body) and "#EXTM3U" in body


def expected_tokens(name):
    """
    从台名推导「分片路径里应该出现」的标识符。
    只对 CCTV 这类命名规范的台有效；推导不出来就返回空集（表示不做这项检查）。
    """
    toks = set()
    if re.search(r"CCTV[-\s]?4K\b", name, re.IGNORECASE):
        toks.add("cctv4k")
    if re.search(r"CCTV[-\s]?8K\b", name, re.IGNORECASE):
        toks.add("cctv8k")
    m = re.search(r"CCTV[-\s]?(\d{1,2})", name, re.IGNORECASE)
    if m:
        n = m.group(1)
        toks.add(f"cctv{n}")
        if "+" in name:
            toks.add(f"cctv{n}p")
    return toks


def path_matches_channel(url, name, timeout, ua):
    """
    拉清单看「真实分片路径」，判断它是不是真的在播这个频道。

    这一项专门抓那种「入口地址写着 cctvN，实际喂别的内容」的线路 ——
    实测抓到的例子：入口 cdnlive/cctv16 实际是 cdnlive/mkt/（marketing），
    入口 ?id=cctv3hd 实际是 /gslb/yss/，入口 cdnlive/cctv5 实际是 /cdnlive/byt/。

    返回 (是否通过, 实际分片路径)。不适用时一律通过。
    """
    toks = expected_tokens(name)
    if not toks:
        return True, ""
    body = fetch_manifest(url, timeout, ua)
    if not body:
        return False, "(清单拉取失败)"
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = re.sub(r"\?.*$", "", urljoin(url, line)).lower()
            if any(t in seg for t in toks):
                return True, seg
            return False, seg
    return False, "(清单无分片)"


# ---------------------------------------------------------------------------
# 台标解析
#   源里的 tvg-logo 质量参差，其中 imgur / fanmingming 在 Apple TV 上（不走代理）
#   根本拉不到，显示出来就是破图或空白。这里改成主动解析：
#     按优先级探测两套国内可达的图集，命中即用，并缓存结果避免每次重探。
# ---------------------------------------------------------------------------
LOGO_PROVIDERS = [
    # 优先 gitee：分辨率最高（CCTV 普遍 640x320），国内直连快
    "https://gitee.com/suxuang/logo/raw/master/mylogo/{key}.png",
    "https://www.xn--rgv465a.top/tvlogo/{key}.png",
]
LOGO_BLOCKED = ("imgur.com", "fanmingming")     # Apple TV 上取不到，一律不用

# 台名 -> 图集里的文件名（两套命名规则不同，实测出来的特例）
LOGO_ALIASES = {
    "CCTV-4 Asia": ["CCTV-4", "CCTV4"],
    "CCTV-5+": ["CCTV-5+", "CCTV5+"],
    "CCTV-8K": ["CCTV-8K", "CCTV8K"],
    "CCTV-4K": ["CCTV-4K", "CCTV4K"],
    "福建海峡卫视": ["海峡卫视", "福建海峡卫视"],
    "黑龙江卫视": ["黑龙江卫视", "黑龙卫视"],
}
# 前缀兜底：如 CGTN Documentary / CGTN French 都退回通用 CGTN 台标
LOGO_PREFIX_FALLBACK = [("CGTN", "CGTN"), ("CCTV", None)]


def logo_keys(name):
    # 台名里带了画质后缀（"CCTV-1 高清"），图集里没有，先剥掉
    base = BP.LABEL_TAIL_RE.sub("", name).strip() or name
    keys = {name, base, base.replace("-", ""), name.replace("-", "")}
    for extra in LOGO_ALIASES.get(base, []) + LOGO_ALIASES.get(name, []):
        keys.add(extra)
        keys.add(extra.replace("-", ""))
    for pref, fallback in LOGO_PREFIX_FALLBACK:
        if fallback and base.upper().startswith(pref):
            keys.add(fallback)
    return [k for k in keys if k]


def logo_ok(url, timeout, ua):
    u = urllib.parse.quote(url, safe=":/?&=#+")
    try:
        req = urllib.request.Request(u, headers={"User-Agent": ua, "Range": "bytes=0-64"})
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            head = resp.read(16)
            return head[:8] == b"\x89PNG\r\n\x1a\n" or resp.status in (200, 206)
    except Exception:  # noqa: BLE001
        return False


def resolve_logos(names, source_logos, cache, timeout, ua, workers):
    """给每个台名解析一个可用台标；结果写入 cache 复用。返回 {台名: url}"""
    todo = [n for n in names if n not in cache]
    if todo:
        print(f"[info] 解析台标（{len(todo)} 个台待探，其余用缓存）…", file=sys.stderr)
        jobs = []
        for n in todo:
            for tpl in LOGO_PROVIDERS:
                for key in logo_keys(n):
                    jobs.append((n, tpl.format(key=key)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            hits = list(pool.map(lambda nu: (nu[0], nu[1], logo_ok(nu[1], timeout, ua)), jobs))
        for n in todo:
            cache[n] = ""            # 先置空，保证每个台都有条目
        for n, url, good in hits:
            # jobs 按 provider 顺序生成，先命中的优先级更高，不被覆盖
            if good and not cache.get(n):
                cache[n] = url
        found = sum(1 for n in todo if cache.get(n))
        print(f"[info] 台标命中 {found}/{len(todo)}", file=sys.stderr)

    out = {}
    for n in names:
        url = cache.get(n) or ""
        if not url:
            sl = source_logos.get(n, "")
            if sl and not any(h in sl for h in LOGO_BLOCKED):
                url = sl
        out[n] = url
    return out


def fetch_text(url, timeout, ua, limit=4 << 20):
    """下载较大的文本（EPG 有 1MB+，不能用 fetch_manifest 的 8KB 上限）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            return resp.read(limit).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def norm_key(s):
    return re.sub(r"[\s\-_·]", "", s or "").lower()


def build_epg_index(epg_url, timeout, ua):
    """
    51zmt 这类 EPG 用的是数字频道 ID：
        <channel id="1"><display-name>CCTV1</display-name></channel>
    列表如果不带 tvg-id，节目单就永远是空的。这里把 display-name -> id 建索引。
    """
    body = fetch_text(epg_url, max(timeout * 4, 30), ua)
    if not body:
        return {}
    idx = {}
    for cid, block in re.findall(r"<channel\s+id=\"([^\"]+)\"[^>]*>(.*?)</channel>", body, re.S):
        for dn in re.findall(r"<display-name[^>]*>([^<]*)</display-name>", block):
            idx.setdefault(norm_key(dn), cid)
    return idx


def match_epg_id(name, index):
    """把台名对到 EPG 的频道 ID。"""
    if not index:
        return ""
    base = BP.LABEL_TAIL_RE.sub("", name).strip() or name
    cands = [base, base.replace("-", ""), name, name.replace("-", "")]
    cands += LOGO_ALIASES.get(base, [])          # 复用别名表（如 福建海峡卫视 -> 海峡卫视）
    for c in cands:
        cid = index.get(norm_key(c))
        if cid:
            return cid
    return ""


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
    ap.add_argument("--no-path-check", action="store_true",
                    help="关闭「真实分片路径是否匹配频道名」这项检查。"
                         "默认开启：它会剔除入口写着 cctvN、实际喂广告/别的频道的线路")
    ap.add_argument("--list", action="store_true", help="打印存活清单")
    ap.add_argument("--min-channels", type=int, default=10)
    ap.add_argument("--no-epg-id", action="store_true",
                    help="不匹配节目单 ID（tvg-id）")
    ap.add_argument("--no-logo", action="store_true", help="不解析台标，输出不带 tvg-logo")
    ap.add_argument("--logo-cache", default="output/.logo-cache.json",
                    help="台标解析缓存，避免每次重探（默认 output/.logo-cache.json）")
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
    src_logos = {}                             # 台名 -> 源里带的 tvg-logo
    ok_src = 0
    for spec in specs:
        try:
            text = BP.load_source(spec, 60, args.insecure)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 源失败，跳过: {spec} ({str(exc)[:60]})", file=sys.stderr)
            continue
        got = 0
        for attrs, raw_name, url in BP.parse_m3u(text):
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
            if name not in src_logos:
                m = re.search(r'tvg-logo="([^"]+)"', attrs or "")
                if m:
                    src_logos[name] = m.group(1)
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

    # ---- 2b. 频道一致性检查：入口写着 cctvN，实际是不是真在播 cctvN --------
    if not args.no_path_check:
        print("[info] 校验「真实分片路径」是否匹配频道名（剔除挂羊头卖狗肉的线路）…",
              file=sys.stderr)
        checked = []
        dropped = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            verdicts = list(pool.map(
                lambda nu: path_matches_channel(nu[1], nu[0], args.timeout, args.user_agent),
                alive))
        for (name, url), (ok, seg) in zip(alive, verdicts):
            if ok:
                checked.append((name, url))
            else:
                dropped.append((name, url, seg))
        print(f"[info] 分片路径不匹配、已剔除 {len(dropped)} 条", file=sys.stderr)
        for name, url, seg in dropped[:12]:
            print(f"       {name:<18} 实际 -> {seg[:60]}", file=sys.stderr)
        if len(dropped) > 12:
            print(f"       … 另有 {len(dropped) - 12} 条", file=sys.stderr)
        alive = checked

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

    # ---- 3b. 台标解析 ----------------------------------------------------
    kept_names = list(dict.fromkeys(it["name"] for it in kept))
    if args.no_logo:
        logo_map = {n: "" for n in kept_names}
    else:
        cache = {}
        if args.logo_cache and os.path.exists(args.logo_cache):
            try:
                with open(args.logo_cache, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except Exception:  # noqa: BLE001
                cache = {}
        logo_map = resolve_logos(kept_names, src_logos, cache, args.timeout,
                                 args.user_agent, args.workers)
        if args.logo_cache:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.logo_cache)), exist_ok=True)
                with open(args.logo_cache, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, ensure_ascii=False, indent=0)
            except Exception:  # noqa: BLE001
                pass
        print(f"[info] 最终带台标 {sum(1 for v in logo_map.values() if v)}/{len(kept_names)} 个台",
              file=sys.stderr)
    for it in kept:
        it["logo"] = logo_map.get(it["name"], "")

    # ---- 3c. 节目单 ID（tvg-id）------------------------------------------
    # 51zmt 的 EPG 用数字 ID，列表不带 tvg-id 的话节目单永远空着。
    if args.no_epg_id or not args.epg:
        epg_index = {}
    else:
        epg_index = build_epg_index(args.epg, args.timeout, args.user_agent)
        print(f"[info] EPG 索引 {len(epg_index)} 个频道", file=sys.stderr)
    for it in kept:
        it["epg_id"] = match_epg_id(it["name"], epg_index)
        # tvg-name 用去掉画质后缀的干净台名，兼容那些按名字匹配节目单的播放器
        it["clean"] = BP.LABEL_TAIL_RE.sub("", it["name"]).strip() or it["name"]
    if epg_index:
        got = sum(1 for it in kept if it["epg_id"])
        print(f"[info] 匹配到节目单 {got}/{len(kept)} 条", file=sys.stderr)

    # ---- 4. 输出 ---------------------------------------------------------
    lines = [f'#EXTM3U url-tvg="{args.epg}"' if args.epg else "#EXTM3U"]
    for it in kept:
        parts = ["#EXTINF:-1"]
        if it.get("epg_id"):
            parts.append(f'tvg-id="{it["epg_id"]}"')
        parts.append(f'tvg-name="{it["clean"]}"')
        if it.get("logo"):
            parts.append(f'tvg-logo="{it["logo"]}"')
        parts.append(f'group-title="{it["group"]}"')
        # 逗号后是显示名：保留「高清」等信息
        lines.append(" ".join(parts) + f',{it["name"]}')
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
