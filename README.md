# 自建 IPTV 订阅（Apple TV / APTV 用）

用 GitHub Actions 每天自动跑一次：拉取多个公开源 → 筛选频道 → 去重 → 生成干净 m3u，
提交回仓库。你只要在 Apple TV 上订阅那个 URL，就有一个**不会因为别人删库而消失**的播放列表。

> 分三组：**央视** / **卫视** / **港澳台**，已剔除购物/付费/测试频道。
> 这些公开源绝大多数没有官方授权，仅限个人自用，请勿公开传播或商用。
>
> ⚠️ **港澳台组（凤凰、翡翠台、明珠台、无线新闻、TVBS、三立、东森、靖天等约 20 个台）的可用性明显低于国内台**：
> 多为境外信号、常被 geo-block、清晰度和在线率波动大。列表里留了就说明当前能拉到流，
> 但随时可能失效——不需要这一组的话，把 `DEFAULT_INCLUDE` 末尾的 `HKMO_TW_PATTERN` 去掉即可。

## 目录结构

```
.
├── .github/workflows/update-iptv.yml   # 每日自动更新
├── sources.txt                         # 源清单（你要维护的就这一个文件）
├── scripts/build-playlist.py           # 构建脚本（纯标准库，无第三方依赖）
└── output/result.m3u                   # 产物，由 Actions 自动生成
```

## 部署步骤

**1. 新建一个 public 仓库**
必须是 **public**——private 仓库的 raw 地址需要 token，APTV 订阅不了。
（内容是公开源的合集，不涉及隐私。）

**2. 把本目录的文件原样传上去**
注意 `.github/` 是隐藏目录，网页上传时容易漏，用命令行更保险：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
cp -R <本目录>/* .        # 确认 .github 也一起复制过去
git add -A && git commit -m "init" && git push
```

**3. 打开工作流写权限**
仓库 → Settings → Actions → General → Workflow permissions → 选 **Read and write permissions** → Save。
不设这一步，Actions 跑完无法把结果提交回来。

**4. 手动跑一次**
仓库 → Actions → 左侧「更新 IPTV 播放列表」→ Run workflow。
跑完检查两个地方：
- 日志里 `[ok] 输出 xx 条频道`，以及 `[probe] 可用源 x/y`
- 仓库里出现 `output/result.m3u`

**5. 拿到订阅地址，填进 APTV**

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/result.m3u
```

## 在 Apple TV 上添加（obox / APTV / iPlayTV 通用）

tvOS 上敲几十个字符的 URL 很痛苦，所以这里同时给出**短链**（TinyURL，已实测跳转正确）：

| 用途 | 地址 | 长度 |
|---|---|---|
| 订阅（完整） | `https://raw.githubusercontent.com/zq720911/iptv-playlist/main/output/result.m3u` | 79 |
| 订阅（**短链**） | `https://tinyurl.com/28vb7e3y` | 28 |
| EPG（**短链**） | `https://tinyurl.com/27uyfc28` | 28 |

obox 的入口是「添加服务器上的 M3u 地址」，三个输入框：

1. **M3u 网址** → 填短链 `https://tinyurl.com/28vb7e3y`（**先清空框里预置的示例地址**）
2. **EPG 地址** → 填 `https://tinyurl.com/27uyfc28`（可留空，只是没有节目预告）
3. **文件名** → 随便填，例如 `iptv`
4. 点 **提交**，等它拉取完（97 个台，分央视 / 卫视 / 港澳台三组）

obox 界面上有个「Obox 支持直播格式」按钮，播不了时点它可以看到支持的流格式。

## 国内可达性：很重要

`raw.githubusercontent.com` 在国内经常被 DNS 污染或超时，Apple TV 拉不到就会一片空白。优先用 CDN 镜像：

```
https://cdn.jsdelivr.net/gh/<你的用户名>/<仓库名>@main/output/result.m3u
```

如果两个都不稳，退路是**不用订阅、改手动**：把 `output/result.m3u` 下载下来，
用 iPlayTV 的本地文件 / iCloud 导入（APTV 主要吃远程 URL），过一阵子再手动更新一次。

### 已知的兼容性风险

| 项 | 数量 | 说明 |
|---|---|---|
| 仅 IPv6 线路的台 | 14 | `CCTV-13`、`CCTV-14少儿`、`CCTV-15`、`CCTV-6电影`、`CCTV-5体育`、`中国教育1~4台`、`云南卫视` 等；播放器或 Apple TV 不支持 IPv6 时这些台播不了 |
| 带过期令牌的线路 | 33 | URL 含 `GuardEncType`/`accountinfo`（移动 CDN 鉴权串），会过期；工作流每天刷新 |
| FLV 线路 | 1 | tvOS 基本不支持 |

需要「保守版」列表（只留 IPv4/域名、去掉 FLV 和令牌地址）时，用 `--family ipv4` 加一个
`--url-exclude` 重新生成到 `output/result.lite.m3u`，在 obox 里加第二条订阅即可。

## 首次使用先做「源体检」

`sources.txt` 里预置的地址**没有在你的网络环境下验证过**，而且这类项目改名/删库很频繁。
先体检，把失败的整行删掉：

```bash
python3 scripts/build-playlist.py sources.txt --probe-only
```

### 已实测记录（2025，广西电信 IPv6 环境）

| 源 | 结果 |
|---|---|
| `iptv-org.github.io/iptv/countries/cn.m3u` | 可下载，145 条 → 筛出 **央视 21 / 卫视 11**；**0 条 IPv6 字面量**（72 条 IPv4 + 73 条域名） |

结论：**这一个源够用但不够好**——卫视覆盖偏薄（只有北京/江苏/浙江/山东/四川/广东/深圳/云南/延边/兵团等），
且完全没有 IPv6 线路。想要完整的省级卫视 + IPv6 高码率源，必须再补 1~2 个国内聚合项目的产物。

脚本已同时支持两套台名写法，国内源的「`CCTV-1 综合高清`」和海外源的
「`CCTV-1 HD (1080p) [Not 24/7]`」都会被清洗、归并、并自动把罗马字台名还原成中文
（`Beijing Satellite TV` → `北京卫视`）。所以中外源可以放心混着用。

或在 GitHub Actions 日志里看 `[probe] 可用源 x/y`。建议保留 3~5 个可用源互相补台。

## 自定义

筛选规则都走命令行参数，改 `.github/workflows/update-iptv.yml` 里的调用即可：

```bash
# 默认：央视 + 卫视 + 港澳台，每频道最多 2 条备用线路
python3 scripts/build-playlist.py sources.txt -o output/result.m3u \
    --epg "https://epg.51zmt.top:8001/e.xml" --max-per-channel 2

# 不要港澳台那一组（去掉 HKMO_TW_PATTERN 的等价写法）
--include 'CCTV[-\s]?\d{1,2}\+?|央视|CGTN|中国教育|[一-龥]{2,4}卫视|Satellite TV'

# 再加点别的台
--include '央视|卫视|凤凰|翡翠|澳视'

# 每频道只留 1 条（最干净）/ 3 条（最容错）
--max-per-channel 1

# 丢弃非 http 地址（rtsp/rtmp 等，Apple TV 播放器多半不支持）
--only-http

# 看最终清单
--list
```

常用环境变量（在 workflow 的 `env:` 里）：

| 变量 | 作用 |
|---|---|
| `EPG` | 节目单地址，写进 `#EXTM3U url-tvg=`；填错不影响播放，只是没节目单 |
| `MAX_PER_CHANNEL` | 同一频道保留几条线路 |
| `MAX_PER_FAMILY` | 每个地址族最多几条；设 `1` = IPv6 和 IPv4 各留一条 |
| `PREFER_FAMILY` | 同频道选线路时优先哪一族：`ipv6` / `ipv4` / `none` |
| `MIN_CHANNELS` | 低于该数量判定异常、**不提交**（避免把空列表覆盖上去） |

频道归并规则：`CCTV-1 综合高清` / `CCTV-1 备用线路` / `CCTV1标清` 会归一成同一个频道，
再按画质排序保留前 N 条，所以不会出现同一个台刷屏。

## IPv6（你有电信 IPv6 公网地址）

**好消息：源的选择面和画质都会明显变好。** 国内大量高清源是 IPv6 分发的，运营商 IPv6 出口
带宽充裕、不经 NAT，通常比 IPv4 源更稳、码率更高。很多聚合项目会单独输出 IPv6 版本
（路径常形如 `.../output/ipv6/result.m3u`），在 `sources.txt` 里补上。

### 三个参数怎么配

```bash
# A. 跨族冗余（推荐）：每个频道留 IPv6 和 IPv4 各一条，一族挂了另一族顶上
--max-per-channel 2 --max-per-family 1 --prefer-family ipv6

# B. 只要 IPv6：IPv4 字面量源全部丢弃（域名源会保留，因为无法静态判断）
--family ipv6

# C. 纯看画质，不看地址族
--prefer-family none --max-per-family 0
```

注意 `--prefer-family ipv6` 单独用时，同一频道的两条线路可能**都是 IPv6**，IPv6 一断就全断；
配上 `--max-per-family 1` 才有跨族兜底。这是默认配置的用意。

> 域名形式的源无法静态判断走 IPv4 还是 IPv6，脚本记为「域名」并在 `--family` 下始终保留。
> 输出日志里的 `[info] 地址族：IPv6 x / IPv4 y / 域名 z` 就是这个统计。

### ⚠️ 前提：Apple TV 必须真的能走 IPv6

有 IPv6 公网地址 **不等于** Apple TV 能用 IPv6。三件事都要成立：

**1. 电信有没有下发 IPv6 前缀**
RB5009 上执行，看是否拿到 `delegated prefix`（电信一般给 /60 或 /56）：

```
/ipv6 dhcp-client print detail
```

没有的话先加一个（`pppoe-out1` 换成你实际的宽带拨号接口名）：

```rsc
/ipv6 dhcp-client add interface=pppoe-out1 request=prefix \
    pool-name=ipv6-pool pool-prefix-length=64 add-default-route=yes disabled=no
```

**2. 前缀有没有下发到内网**（Apple TV 要能通过 RA 拿到地址）

```rsc
/ipv6 address add address=::1/64 from-pool=ipv6-pool interface=bridge advertise=yes
/ipv6 nd set [ find default=yes ] advertise-dns=yes
```

如果你的 LAN 叫别的名字，把 `bridge` 换掉。想看细节可以 `advertise=yes` 后确认
`/ipv6 nd prefix print` 里有前缀。

**3. Apple TV 实际拿到了 IPv6 地址**
Apple TV → 设置 → 网络 → 当前网络，看有没有 `IPv6 地址`（应该是以 `240e:` 开头的公网地址，
不是 `fe80:` 开头的链路本地地址）。

另外若你在 IPv6 上配了防火墙，记得放行 `forward` 链到内网客户端的出向流量，
否则会有 IPv6 地址但连不上。

**如果 Apple TV 拿不到 IPv6**：IPv6 源会全部播不了（表现为频道列表有、点了转圈）。
三种处理：修好路由器 IPv6 下发；或改用 `--family ipv4` 只要 IPv4 源；
或保留 `--max-per-family 1` 靠 IPv4 兜底线路顶着。

## 常见问题

**Actions 跑失败，报 `频道数 xx 低于阈值`**
说明所有源都失效了。看体检日志，把还能用的源补进 `sources.txt`。
失败时不会提交，仓库里上一次的 `result.m3u` 仍然可用。

**APTV 里列表是空的**
先确认订阅 URL 在手机浏览器里能打开且是 m3u 文本；再确认仓库是 public；再试 jsDelivr 地址。

**想彻底不看别人脸色**
把 [Guovin/iptv-api](https://github.com/Guovin/iptv-api) fork 一份自己跑（它自带采集+校验+测速的
Actions），或者直接用本仓库这套——本套的好处是不依赖任何第三方项目的配置格式，只依赖 m3u 本身。

**为什么不去用电信 IPTV 的本地台？**
那需要光猫 VLAN / IPTV 接入认证 / 组播转单播一整套，只有桂林本地台才有价值。
如果你以后改主意了，那套配置模板不在本目录里。
