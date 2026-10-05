#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 播放列表构建器：合并多个公开源 → 筛选频道 → 去重 → 输出干净 m3u

配合 .github/workflows/update-iptv.yml 使用，可以每天自动更新，
给自己一个稳定、可控、只含想看频道的订阅地址。

用法：
  # 默认只留央视 + 卫视，每频道最多 2 条备用线路
  python3 build-playlist.py sources.txt -o output/result.m3u \
      --epg "https://epg.51zmt.top:8001/e.xml"

  # 自定义筛选（正则，作用于频道名）
  python3 build-playlist.py sources.txt -o result.m3u \
      --include '央视|卫视|凤凰|翡翠' --exclude '购物|付费|测试'

  # 每频道只留 1 条（列表最干净）/ 留 3 条（容错最强）
  python3 build-playlist.py sources.txt -o result.m3u --max-per-channel 1

sources.txt 格式：每行一个 URL 或本地路径，`#` 开头为注释。
"""

import argparse
import ipaddress
import re
import sys
import urllib.request
from collections import OrderedDict, defaultdict

# ---------------------------------------------------------------------------
# 默认筛选规则
# ---------------------------------------------------------------------------

# 保留：央视（只认带编号的 CCTV，避免收进 CCTV-Storm Music 这类付费/海外服务）
DEFAULT_INCLUDE = (
    r"CCTV[-\s]?\d{1,2}\+?(?![\dKk])"              # CCTV-1 .. CCTV-17、CCTV-5+
    r"|CCTV[-\s]?(?:4K|8K)"                        # CCTV-4K / CCTV-8K
    r"|央视|中央电视|CGTN|中国教育"
    r"|[\u4e00-\u9fff]{2,4}卫视|Satellite\s*TV"     # 中文台名 + 海外源的罗马字台名
)

# 丢弃：购物频道、付费/加密频道、测试条目，以及央视的付费子频道（中英文两种写法）
DEFAULT_EXCLUDE = (
    r"购物|付费|测试|加密|推广|导视|暂无|停播|内部|样片|VIP|未测试|🔒|❌"
    r"|台球|高尔夫|网球|风云|怀旧|老故事|发现之旅|精品|女性时尚"
    r"|Billiards|Golf|Storm|Nostalgia|Culture of Quality"
)

# ---------------------------------------------------------------------------
# 频道名清洗
#   国内源写 "CCTV-1 综合高清"，海外源写 "CCTV-1 HD (1080p) [Not 24/7]"。
#   统一成「干净台名 + 画质后缀」，两种写法才能正确筛选和归并。
# ---------------------------------------------------------------------------
BRACKET_RE = re.compile(r"[\[\(（【][^\]\)）】]*[\]\)）】]")      # (1080p) [Not 24/7]
QUALITY_WORD_RE = re.compile(r"\b(?:FHD|UHD|HD|SD|H265|HEVC|AVC)\b", re.IGNORECASE)
RESOLUTION_RE = re.compile(r"\b\d{3,4}[pi]\b", re.IGNORECASE)
CJK_QUALITY_RE = re.compile(r"(高清|超清|蓝光|标清|流畅|高码|原画|备用线路?\d*)")
SEP_RE = re.compile(r"[\s·_–—]+")          # 注意：不吃连字符，否则 CCTV-1 会变成 CCTV 1
MULTISPACE_RE = re.compile(r"\s{2,}")

CCTV_RE = re.compile(r"CCTV[-\s]?(\d{1,2})(\+)?(?![\dKk])", re.IGNORECASE)
CCTV_SPECIAL_RE = re.compile(r"CCTV[-\s]?(4K|8K|中视购物)", re.IGNORECASE)
SATELLITE_RE = re.compile(r"[\u4e00-\u9fff]{2,4}卫视|Satellite\s*TV", re.IGNORECASE)
CCTV_GROUP_RE = re.compile(r"CCTV|央视|中央电视|CGTN|中国教育", re.IGNORECASE)

QUALITY_RANK = [("8k", 0), ("4k", 0), ("uhd", 0), ("超清", 1), ("蓝光", 1),
                ("1080", 2), ("高清", 2), ("高码", 2), ("hd", 2), ("fhd", 2),
                ("720", 3), ("576", 4), ("标清", 4), ("sd", 4)]

# 罗马字台名 -> 中文（iptv-org 这类海外源用的是罗马字）
ALIASES = {
    "beijing satellite tv": "北京卫视",
    "hunan satellite tv": "湖南卫视",
    "zhejiang satellite tv": "浙江卫视",
    "jiangsu satellite tv": "江苏卫视",
    "anhui satellite tv": "安徽卫视",
    "shandong satellite tv": "山东卫视",
    "guangdong satellite tv": "广东卫视",
    "shenzhen satellite tv": "深圳卫视",
    "sichuan satellite tv": "四川卫视",
    "hubei satellite tv": "湖北卫视",
    "henan satellite tv": "河南卫视",
    "liaoning satellite tv": "辽宁卫视",
    "heilongjiang satellite tv": "黑龙江卫视",
    "jilin satellite tv": "吉林卫视",
    "tianjin satellite tv": "天津卫视",
    "chongqing satellite tv": "重庆卫视",
    "yunnan satellite tv": "云南卫视",
    "guizhou satellite tv": "贵州卫视",
    "guangxi satellite tv": "广西卫视",
    "jiangxi satellite tv": "江西卫视",
    "shanxi satellite tv": "山西卫视",
    "shaanxi satellite tv": "陕西卫视",
    "gansu satellite tv": "甘肃卫视",
    "ningxia satellite tv": "宁夏卫视",
    "qinghai satellite tv": "青海卫视",
    "xinjiang satellite tv": "新疆卫视",
    "tibet satellite tv": "西藏卫视",
    "xizang satellite tv": "西藏卫视",
    "inner mongolia satellite tv": "内蒙古卫视",
    "hainan satellite tv": "海南卫视",
    "fujian satellite tv": "东南卫视",
    "southeast satellite tv": "东南卫视",
    "hebei satellite tv": "河北卫视",
    "shanghai satellite tv": "东方卫视",
    "dragon satellite tv": "东方卫视",
    "bingtuan satellite tv": "兵团卫视",
    "yanbian satellite tv": "延边卫视",
    "xiamen satellite tv": "厦门卫视",
}


def clean_name(raw: str) -> str:
    """去掉画质词、分辨率和方括号标记，得到干净台名。"""
    s = BRACKET_RE.sub(" ", raw)
    s = QUALITY_WORD_RE.sub(" ", s)
    s = RESOLUTION_RE.sub(" ", s)
    s = CJK_QUALITY_RE.sub(" ", s)
    s = SEP_RE.sub(" ", s)
    s = MULTISPACE_RE.sub(" ", s)
    return s.strip()


def quality_label(raw: str) -> str:
    """从原始名提取画质标签（清洗后这信息就丢了，必须在清洗前取）。"""
    s = raw.lower()
    if "8k" in s:
        return "8K"
    if "4k" in s or "uhd" in s:
        return "4K"
    if "超清" in raw or "蓝光" in raw or "1080" in s or "fhd" in s or re.search(r"\bhd\b", s):
        return "高清"
    if "标清" in raw or "576" in s or re.search(r"\bsd\b", s):
        return "标清"
    if "720" in s or "高清" in raw or "高码" in raw:
        return "高清"
    return ""


def display_name(raw: str) -> str:
    """清洗 → 罗马字转中文 → 补回画质后缀。用于展示、筛选和归并。"""
    base = clean_name(raw)
    base = ALIASES.get(base.lower(), base)
    label = quality_label(raw)
    if label and label.lower() not in base.lower():
        return f"{base} {label}"
    return base

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 IPTV-Builder/1.0"

# ---------------------------------------------------------------------------
# 地址族识别
#   字面量 IPv6 形如 http://[240e:398:1:2::1]:8080/live/cctv1.m3u8
#   域名无法静态判断走 IPv4 还是 IPv6，记为 unknown（运行期由 DNS/播放器决定）
# ---------------------------------------------------------------------------
IPV6_LITERAL_RE = re.compile(r"^(?:https?|rtsp|rtmp)://\[[0-9a-fA-F:.]+\]", re.IGNORECASE)
IPV4_LITERAL_RE = re.compile(
    r"^(?:https?|rtsp|rtmp)://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/|$)", re.IGNORECASE
)

# 数值越小越优先
FAMILY_RANK = {"ipv6": 0, "unknown": 1, "ipv4": 2}
FAMILY_LABEL = {"ipv6": "IPv6", "ipv4": "IPv4", "unknown": "域名"}


def address_family(url: str) -> str:
    """返回 'ipv6' / 'ipv4' / 'unknown'"""
    if IPV6_LITERAL_RE.match(url) or url.startswith("["):
        return "ipv6"
    if IPV4_LITERAL_RE.match(url):
        return "ipv4"
    return "unknown"


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", "replace")


def load_source(spec: str, timeout: float) -> str:
    if re.match(r"^https?://", spec, re.IGNORECASE):
        req = urllib.request.Request(spec, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return decode_text(resp.read())
    with open(spec, "rb") as fh:
        return decode_text(fh.read())


def parse_m3u(text: str):
    """yield (attrs_line, name, url)"""
    attrs = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs = line
            continue
        if line.startswith("#"):
            continue
        name = ""
        if attrs and "," in attrs:
            name = attrs.split(",", 1)[1].strip()
        if not name:
            name = line.rsplit("/", 1)[-1]
        yield attrs or "#EXTINF:-1", name, line
        attrs = None


def channel_key(name: str) -> str:
    """
    把同一频道的不同叫法归一，避免"备用线路""综合"等后缀被当成不同频道：
      "CCTV-1 综合高清" / "CCTV-1 备用线路" / "CCTV1标清"  ->  cctv1
      "CCTV-5+"                                            ->  cctv5plus
      "湖南卫视高清" / "湖南卫视"                            ->  湖南卫视
    """
    m = CCTV_RE.search(name)
    if m:
        key = f"cctv{int(m.group(1))}"
        if m.group(2):
            key += "plus"
        for tag in ("欧洲", "美洲", "亚洲", "北美", "非洲"):
            if tag in name:            # CCTV-4 中文国际的各版本是不同频道
                key += tag
                break
        return key

    m = CCTV_SPECIAL_RE.search(name)
    if m:
        return "cctv-" + m.group(1).lower()

    m = SATELLITE_RE.search(name)
    if m:
        return m.group(0)              # 例如 湖南卫视

    return SEP_RE.sub("", name).lower()


def quality_rank(name: str) -> int:
    for marker, rank in QUALITY_RANK:
        if marker.lower() in name.lower():
            return rank
    return 5


def group_of(name: str) -> str:
    if CCTV_GROUP_RE.search(name):
        return "央视"
    if SATELLITE_RE.search(name):
        return "卫视"
    return "其他"


def sort_key(item):
    """已废弃：排序统一由 main() 内的 final_key 处理，保留以免外部引用报错。"""
    return (item.get("group", ""), item.get("name", ""))


def extract_logo(attrs: str) -> str:
    m = re.search(r'tvg-logo="([^"]*)"', attrs, re.IGNORECASE)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="合并/筛选/去重 IPTV 源，生成干净 m3u")
    ap.add_argument("sources", help="源清单文件（每行一个 URL 或本地路径，# 为注释）")
    ap.add_argument("-o", "--output", default="result.m3u", help="输出 m3u 路径")
    ap.add_argument("--epg", default="", help="EPG 地址，写入 #EXTM3U url-tvg=")
    ap.add_argument("--include", default=DEFAULT_INCLUDE, help="保留频道名正则")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="丢弃频道名正则")
    ap.add_argument("--max-per-channel", type=int, default=2,
                    help="同一频道最多保留几条线路，默认 2")
    ap.add_argument("--min-channels", type=int, default=10,
                    help="低于该数量视为失败并返回非 0，默认 10")
    ap.add_argument("--only-http", action="store_true",
                    help="丢弃非 http/https 的地址（rtsp/rtmp 等）")
    ap.add_argument("--family", choices=["any", "ipv4", "ipv6"], default="any",
                    help="只保留该地址族的线路；域名（unknown）始终保留。"
                         "有 IPv6 公网时建议 ipv6，可滤掉大量失效的 IPv4 字面量源")
    ap.add_argument("--prefer-family", choices=["ipv6", "ipv4", "none"], default="ipv6",
                    help="同一频道有多条线路时优先保留哪一族，默认 ipv6；"
                         "想优先保画质可设为 none")
    ap.add_argument("--max-per-family", type=int, default=0,
                    help="同一频道每个地址族最多保留几条（0=不限制）。"
                         "设为 1 可得到 IPv6+IPv4 跨族冗余，建议配合 --max-per-channel 2")
    ap.add_argument("--timeout", type=float, default=60.0, help="单个源下载超时秒数")
    ap.add_argument("--list", action="store_true", help="打印最终频道清单到 stderr")
    ap.add_argument("--probe-only", action="store_true",
                    help="只逐个测试源是否可用并统计条数，不生成文件")
    args = ap.parse_args()

    # ---- 读取源清单 --------------------------------------------------------
    try:
        with open(args.sources, "r", encoding="utf-8") as fh:
            specs = [ln.strip() for ln in fh
                     if ln.strip() and not ln.strip().startswith("#")]
    except OSError as exc:
        print(f"[error] 读取源清单失败: {exc}", file=sys.stderr)
        return 2

    if not specs:
        print("[error] 源清单为空", file=sys.stderr)
        return 2

    # ---- 拉取并解析 --------------------------------------------------------
    raw_entries = []
    ok_sources = 0
    for spec in specs:
        try:
            text = load_source(spec, args.timeout)
        except Exception as exc:  # noqa: BLE001 - 单个源失败不应中断整体
            print(f"[warn] 源获取失败，已跳过: {spec} ({str(exc)[:80]})", file=sys.stderr)
            continue
        got = list(parse_m3u(text))
        if not got:
            print(f"[warn] 源内无有效频道，已跳过: {spec}", file=sys.stderr)
            continue
        ok_sources += 1
        raw_entries.extend(got)
        print(f"[ok] {spec} -> {len(got)} 条", file=sys.stderr)

    if args.probe_only:
        print(f"[probe] 可用源 {ok_sources}/{len(specs)}"
              f"（失败的行请从 {args.sources} 里删掉）", file=sys.stderr)
        return 0 if ok_sources else 2

    if not raw_entries:
        print("[error] 所有源都失败了，终止（保留上一次的产物）", file=sys.stderr)
        return 2

    # ---- 筛选 --------------------------------------------------------------
    inc = re.compile(args.include)
    exc = re.compile(args.exclude)
    seen_urls = set()
    candidates = []
    stat = defaultdict(int)

    for attrs, raw_name, url in raw_entries:
        stat["总计"] += 1
        name = display_name(raw_name)
        if not name:
            stat["台名为空"] += 1
            continue
        if not inc.search(name):
            stat["不在保留范围"] += 1
            continue
        if exc.search(name):
            stat["命中排除词"] += 1
            continue
        if args.only_http and not re.match(r"^https?://", url, re.IGNORECASE):
            stat["非 http 地址"] += 1
            continue
        fam = address_family(url)
        if args.family != "any" and fam != "unknown" and fam != args.family:
            stat[f"非 {FAMILY_LABEL[args.family]} 地址"] += 1
            continue
        key = url.strip().lower()
        if key in seen_urls:
            stat["地址重复"] += 1
            continue
        seen_urls.add(key)
        candidates.append({
            "name": name,
            "url": url,
            "logo": extract_logo(attrs),
            "group": group_of(name),
            "ckey": channel_key(name),
            "family": fam,
            "qrank": quality_rank(raw_name),
        })

    # ---- 每频道限量（先按地址族偏好，再按画质）-----------------------------
    by_channel = OrderedDict()
    for item in candidates:
        by_channel.setdefault(item["ckey"], []).append(item)

    pref_order = {"ipv6": {"ipv6": 0, "unknown": 1, "ipv4": 2},
                  "ipv4": {"ipv4": 0, "unknown": 1, "ipv6": 2},
                  "none": {"ipv6": 0, "ipv4": 0, "unknown": 0}}[args.prefer_family]

    def line_rank(it):
        return (pref_order.get(it["family"], 1), it["qrank"], len(it["url"]))

    kept = []
    for items in by_channel.values():
        keep_n = max(1, args.max_per_channel)

        if args.max_per_family > 0:
            # 每个地址族各留最多 N 条，保证跨族冗余：
            # 例如 IPv6 与 IPv4 各一条，IPv6 挂了还有 IPv4 兜底。
            per_fam = defaultdict(list)
            for it in sorted(items, key=line_rank):
                per_fam[it["family"]].append(it)
            picked = []
            for lst in per_fam.values():
                picked.extend(lst[: args.max_per_family])
            picked.sort(key=line_rank)
        else:
            picked = sorted(items, key=line_rank)

        kept.extend(picked[:keep_n])
        stat["因超出线路上限丢弃"] += max(0, len(items) - min(len(picked), keep_n))

    # ---- 排序 + 输出 -------------------------------------------------------
    def final_key(it):
        gorder = {"央视": 0, "卫视": 1, "其他": 2}.get(it["group"], 3)
        name = it["name"]
        m = CCTV_RE.search(name)
        if m:
            return (gorder, 0, int(m.group(1)), it["qrank"], name)
        if CCTV_SPECIAL_RE.search(name):
            return (gorder, 0, 900, it["qrank"], name)
        if it["group"] == "央视":
            return (gorder, 0, 800, it["qrank"], name)
        return (gorder, 1, 0, it["qrank"], name)

    kept.sort(key=final_key)

    lines = []
    header = "#EXTM3U"
    if args.epg:
        header += f' url-tvg="{args.epg}"'
    lines.append(header)

    for it in kept:
        attrs = f'#EXTINF:-1 tvg-name="{it["name"]}"'
        if it["logo"]:
            attrs += f' tvg-logo="{it["logo"]}"'
        attrs += f' group-title="{it["group"]}",{it["name"]}'
        lines.append(attrs)
        lines.append(it["url"])

    import os
    outdir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(outdir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # ---- 报告 --------------------------------------------------------------
    groups = defaultdict(int)
    fams = defaultdict(int)
    for it in kept:
        groups[it["group"]] += 1
        fams[it["family"]] += 1

    fam_filter_note = ""
    if args.family != "any":
        fam_filter_note = f"、非 {FAMILY_LABEL[args.family]} 地址 {stat[f'非 {FAMILY_LABEL[args.family]} 地址']}"

    print(f"[info] 成功源 {ok_sources}/{len(specs)}", file=sys.stderr)
    print(f"[info] 原始 {stat['总计']} 条；"
          f"不在保留范围 {stat['不在保留范围']}、命中排除词 {stat['命中排除词']}、"
          f"地址重复 {stat['地址重复']}、超线路上限 {stat['因超出线路上限丢弃']}"
          + (f"、非 http {stat['非 http 地址']}" if args.only_http else "")
          + fam_filter_note,
          file=sys.stderr)
    print(f"[info] 地址族：IPv6 {fams['ipv6']} / IPv4 {fams['ipv4']} / 域名 {fams['unknown']}"
          f"（--family {args.family}，--prefer-family {args.prefer_family}）", file=sys.stderr)
    print(f"[ok] 输出 {len(kept)} 条频道（央视 {groups['央视']} / "
          f"卫视 {groups['卫视']} / 其他 {groups['其他']}）-> {args.output}", file=sys.stderr)

    if args.list:
        for it in kept:
            print(f"    {it['group']}\t{it['name']}", file=sys.stderr)

    if len(kept) < args.min_channels:
        print(f"[error] 频道数 {len(kept)} 低于阈值 {args.min_channels}，"
              f"判定为异常，返回非 0（工作流将不提交本次结果）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
