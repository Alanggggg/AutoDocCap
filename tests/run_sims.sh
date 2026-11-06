#!/bin/zsh
# 并行跑 6 个远程卡顿场景的回归测试（约 3 分钟），每个场景上限 6 分钟。
cd "${0:A:h}"
PY=../.venv/bin/python
scenarios=("" "--noise" "--lag 1.5" "--noise --spike 2.5" "--noise --lag 0.8 --spike 3.5" "--noise --spike 6")
pids=(); i=0
for a in "${scenarios[@]}"; do
  i=$((i+1))
  ( $PY simulate.py ${=a} --out _sim$i > /tmp/doccap_sim_$i.log 2>&1 ) &
  pids+=$!
done
( sleep 360; kill ${pids[@]} 2>/dev/null ) & guard=$!
for p in ${pids[@]}; do wait $p; done
kill $guard 2>/dev/null
fail=0; i=0
for a in "${scenarios[@]}"; do
  i=$((i+1)); r=$(grep "结论" /tmp/doccap_sim_$i.log)
  echo "场景 ${a:-默认}: ${r:-（无结论，见 /tmp/doccap_sim_$i.log）}"
  [[ "$r" == *✅* ]] || fail=1
done
rm -rf ../output/_sim?
exit $fail
