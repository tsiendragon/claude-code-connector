#!/usr/bin/env bash
# run-tests.sh — automated ccc test runner
# Runs all test groups against real Claude sessions and writes results.
set -uo pipefail

CCC="$HOME/.local/bin/ccc"
RESULTS_DIR="$(dirname "$0")/results"
TS=$(date +%s)
SA="test-a-$TS"
SB="test-b-$TS"
SC="test-c-$TS"
# Use ccc-ts dir as cwd — already trusted by Claude Code (avoids trust dialog)
TEST_CWD="$(cd "$(dirname "$0")/.." && pwd)"

PASS=0
FAIL=0
FAILURES=()

mkdir -p "$RESULTS_DIR"
RESULTS_FILE="$RESULTS_DIR/run-$TS.json"
echo "[]" > "$RESULTS_FILE"

now_ms() { python3 -c "import time; print(int(time.time() * 1000))"; }

record() {
  local name="$1" passed="$2" expected="$3" actual="$4" error="$5" ms="$6"
  local entry
  entry=$(jq -n \
    --arg n "$name" --argjson p "$passed" \
    --arg e "$expected" --arg a "$actual" \
    --arg err "$error" --argjson ms "$ms" \
    '{name:$n,passed:$p,expected:$e,actual:$a,error:$err,duration_ms:$ms}')
  local tmp; tmp=$(mktemp)
  jq --argjson entry "$entry" '. + [$entry]' "$RESULTS_FILE" > "$tmp"
  mv "$tmp" "$RESULTS_FILE"
  if [ "$passed" = "true" ]; then
    echo "  ✓ $name"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $name — $error"
    FAIL=$((FAIL + 1))
    FAILURES+=("$name")
  fi
}

cleanup() {
  for s in "$SA" "$SB" "$SC"; do
    $CCC kill "$s" 2>/dev/null || true
  done
}
trap cleanup EXIT

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Group A: Session Lifecycle ═══"

## T-A1: run
echo "→ T-A1: ccc run"
t0=$(now_ms)
actual=$($CCC run "$SA" --cwd "$TEST_CWD" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && echo "$actual" | grep -q "started"; then
  record "T-A1: run 创建 session" true "exit 0 + 含 started" "$actual" "null" $ms
else
  record "T-A1: run 创建 session" false "exit 0 + 含 started" "$actual" "exit=$ec" $ms
fi

## T-A2: tmux 确认
echo "→ T-A2: tmux 确认 session 存在"
t0=$(now_ms)
actual=$(tmux list-sessions 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if echo "$actual" | grep -q "ccc-$SA"; then
  record "T-A2: tmux 有对应 session" true "tmux 中存在 ccc-$SA" "$actual" "null" $ms
else
  record "T-A2: tmux 有对应 session" false "tmux 中存在 ccc-$SA" "$actual" "未找到" $ms
fi

## T-A3: ps
echo "→ T-A3: ccc ps"
t0=$(now_ms)
actual=$($CCC ps 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && echo "$actual" | grep -q "$SA"; then
  record "T-A3: ps 列出 session" true "包含 session 名" "$actual" "null" $ms
else
  record "T-A3: ps 列出 session" false "包含 session 名" "$actual" "exit=$ec" $ms
fi

## T-A4: ps --json
echo "→ T-A4: ccc ps --json"
t0=$(now_ms)
actual=$($CCC ps --json 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
valid_json=$(echo "$actual" | jq 'type == "array"' 2>/dev/null || echo "false")
has_session=$(echo "$actual" | jq --arg n "$SA" 'any(.[]; .name == $n)' 2>/dev/null || echo "false")
if [ $ec -eq 0 ] && [ "$valid_json" = "true" ] && [ "$has_session" = "true" ]; then
  record "T-A4: ps --json 合法结构" true "合法JSON数组，含session" "${actual:0:300}" "null" $ms
else
  record "T-A4: ps --json 合法结构" false "合法JSON数组，含session" "${actual:0:300}" "valid=$valid_json has=$has_session" $ms
fi

## T-A5: kill
echo "→ T-A5: ccc kill"
t0=$(now_ms)
actual=$($CCC kill "$SA" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
tmux_gone=$(tmux list-sessions 2>&1 | grep -c "ccc-$SA" || true)
if [ $ec -eq 0 ] && [ "$tmux_gone" = "0" ]; then
  record "T-A5: kill 删除 session" true "exit 0，tmux session 消失" "$actual" "null" $ms
else
  record "T-A5: kill 删除 session" false "exit 0，tmux session 消失" "$actual" "exit=$ec tmux_still=$tmux_gone" $ms
fi

## T-A6: clean
echo "→ T-A6: ccc clean（dead session）"
$CCC run "test-dead-$TS" --cwd "$TEST_CWD" 2>/dev/null || true
sleep 1
tmux kill-session -t "ccc-test-dead-$TS" 2>/dev/null || true
t0=$(now_ms)
actual=$($CCC clean --yes 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ]; then
  record "T-A6: clean 清理 dead session" true "exit 0" "$actual" "null" $ms
else
  record "T-A6: clean 清理 dead session" false "exit 0" "$actual" "exit=$ec" $ms
fi

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Group B: State Reading (等待 Claude 就绪...) ═══"

$CCC run "$SB" --cwd "$TEST_CWD" 2>&1 || true

## T-B1: wait ready
echo "→ T-B1: wait ready (最多 60s)"
t0=$(now_ms)
actual=$($CCC wait "$SB" ready --timeout 60 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ]; then
  record "T-B1: wait ready 成功" true "60s内就绪" "${actual:0:200}" "null" $ms
else
  record "T-B1: wait ready 成功" false "60s内就绪" "${actual:0:200}" "exit=$ec 超时或错误" $ms
fi

## T-B2: read --json
echo "→ T-B2: read --json"
t0=$(now_ms)
actual=$($CCC read "$SB" --json 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
has_state=$(echo "$actual" | jq 'has("state")' 2>/dev/null || echo "false")
has_lines=$(echo "$actual" | jq 'has("lines")' 2>/dev/null || echo "false")
if [ $ec -eq 0 ] && [ "$has_state" = "true" ] && [ "$has_lines" = "true" ]; then
  record "T-B2: read --json 含 state/lines" true "合法JSON含state和lines" "${actual:0:200}" "null" $ms
else
  record "T-B2: read --json 含 state/lines" false "合法JSON含state和lines" "${actual:0:300}" "has_state=$has_state has_lines=$has_lines" $ms
fi

## T-B3: status
echo "→ T-B3: status"
t0=$(now_ms)
actual=$($CCC status "$SB" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && echo "$actual" | grep -q "state:"; then
  record "T-B3: status 含 state:" true "含 state: 行" "$actual" "null" $ms
else
  record "T-B3: status 含 state:" false "含 state: 行" "$actual" "exit=$ec" $ms
fi

## T-B4: tail
echo "→ T-B4: tail"
t0=$(now_ms)
actual=$($CCC tail "$SB" --lines 20 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && [ -n "$actual" ]; then
  record "T-B4: tail 有输出" true "非空输出" "${actual:0:200}" "null" $ms
else
  record "T-B4: tail 有输出" false "非空输出" "${actual:0:200}" "exit=$ec" $ms
fi

## T-B5: last（无消息时）
echo "→ T-B5: last（无消息，不崩溃）"
t0=$(now_ms)
actual=$($CCC last "$SB" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ]; then
  record "T-B5: last 不崩溃" true "exit 0" "$actual" "null" $ms
else
  record "T-B5: last 不崩溃" false "exit 0" "$actual" "exit=$ec" $ms
fi

## T-B6: read dead session
echo "→ T-B6: read dead session"
$CCC kill "$SB" 2>/dev/null || true
t0=$(now_ms)
actual=$($CCC read "$SB" --json 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
is_dead=$(echo "$actual" | jq '.state == "dead"' 2>/dev/null || echo "false")
if [ "$is_dead" = "true" ] || [ $ec -ne 0 ]; then
  record "T-B6: dead session 状态正确" true "state=dead 或 error" "${actual:0:200}" "null" $ms
else
  record "T-B6: dead session 状态正确" false "state=dead 或 error" "${actual:0:200}" "exit=$ec" $ms
fi

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Group C: Send & Interaction (真实 Claude) ═══"

$CCC run "$SC" --cwd "$TEST_CWD" 2>&1 || true
echo "→ 等待 Claude 就绪..."
$CCC wait "$SC" ready --timeout 60 2>&1 || true

## T-C1: send PONG
echo "→ T-C1: send PONG (最多 120s)"
t0=$(now_ms)
actual=$($CCC send "$SC" "reply with exactly one word: PONG" --timeout 120 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && echo "$actual" | grep -qi "pong"; then
  record "T-C1: send 响应含 PONG" true "响应含PONG" "${actual:0:300}" "null" $ms
else
  record "T-C1: send 响应含 PONG" false "响应含PONG" "${actual:0:300}" "exit=$ec" $ms
fi

## T-C2: last 提取
echo "→ T-C2: last 提取响应"
t0=$(now_ms)
actual=$($CCC last "$SC" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
if [ $ec -eq 0 ] && [ -n "$actual" ]; then
  record "T-C2: last 有内容" true "非空" "${actual:0:300}" "null" $ms
else
  record "T-C2: last 有内容" false "非空" "${actual:0:300}" "exit=$ec" $ms
fi

## T-C3: input --no-enter
echo "→ T-C3: input --no-enter"
t0=$(now_ms)
actual=$($CCC input "$SC" "hello-test" --no-enter 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
$CCC key "$SC" C-c 2>/dev/null || true
sleep 0.5
if [ $ec -eq 0 ]; then
  record "T-C3: input --no-enter 不崩溃" true "exit 0" "$actual" "null" $ms
else
  record "T-C3: input --no-enter 不崩溃" false "exit 0" "$actual" "exit=$ec" $ms
fi

## T-C4: interrupt
echo "→ T-C4: interrupt"
$CCC send "$SC" "count from 1 to 1000 one per line" --no-wait 2>&1 || true
sleep 2
t0=$(now_ms)
actual=$($CCC interrupt "$SC" 2>&1) && ec=0 || ec=$?
ms=$(( $(now_ms) - t0 ))
$CCC wait "$SC" ready --timeout 20 2>/dev/null || true
if [ $ec -eq 0 ]; then
  record "T-C4: interrupt 不崩溃" true "exit 0" "$actual" "null" $ms
else
  record "T-C4: interrupt 不崩溃" false "exit 0" "$actual" "exit=$ec" $ms
fi

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "═══ 测试完成 ═══"
echo "✓ 通过: $PASS"
echo "✗ 失败: $FAIL"
echo ""
echo "结果: $RESULTS_FILE"

if [ ${#FAILURES[@]} -gt 0 ]; then
  echo ""
  echo "失败项："
  printf '  - %s\n' "${FAILURES[@]}"
fi

[ $FAIL -eq 0 ]
