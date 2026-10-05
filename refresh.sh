#!/bin/sh
# 一键刷新「实测存活」播放列表。
#
# 做三件事：拉全部源 -> 逐条探测 -> 只留能播的 -> 提交并推送。
# 以后源失效了、想加频道了，直接跑这个脚本，然后在 obox 里刷新订阅即可。
#
# 约需 3~5 分钟。
set -e
cd "$(dirname "$0")"

# 不要的分组写在这里（逗号分隔，可选：央视/卫视/港澳台/其他）
EXCLUDE_GROUPS="港澳台"

echo "==> 开始扫描（排除分组：$EXCLUDE_GROUPS）"
python3 scripts/scan-and-verify.py sources.txt \
  -o output/result.alive.m3u \
  --insecure \
  --exclude-groups "$EXCLUDE_GROUPS" \
  --workers 24 \
  --timeout 6 \
  --max-per-channel 1 \
  --min-channels 20

# 说明：
#   --exclude-groups  : 不想要的分组
#   --max-per-channel : 每个台保留几条线路。1 = 列表最干净（每个台一个标签）；
#                       2 = 多一条备用镜像（会看到同名两个标签）
#   默认开启的“分片路径校验”会剔除【入口写着 cctvN、实际喂广告/别的频道】的线路，
#   实测抓到过 cdnlive/mkt/（marketing）、/gslb/yss/、/cdnlive/byt/ 这些。
#   想关掉加 --no-path-check

echo
echo "==> 提交结果"
git add output/result.alive.m3u
if git diff --staged --quiet; then
  echo "内容无变化，跳过提交"
else
  git commit -m "刷新存活列表 $(date +%F)"
  git pull --rebase --autostash origin main
  git push
  echo "已推送。到 obox 里刷新那条订阅即可。"
fi
