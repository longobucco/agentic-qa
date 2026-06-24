#!/usr/bin/env bash
# Clone Alumnium + its WebVoyager fork and install dependencies for re-running the benchmark.
# Credential-independent: this only fetches code and installs tools. The actual run needs
# a provider key for Alumnium's internal model (see README.md / the key question).
set -euo pipefail
cd "$(dirname "$0")"

VENDOR="./vendor"
ALUMNIUM_DIR="$VENDOR/alumnium"

mkdir -p "$VENDOR"

if [ ! -d "$ALUMNIUM_DIR/.git" ]; then
  echo "==> Cloning alumnium-hq/alumnium (with WebVoyager submodule)"
  git clone --recurse-submodules https://github.com/alumnium-hq/alumnium.git "$ALUMNIUM_DIR"
else
  echo "==> Updating existing clone"
  git -C "$ALUMNIUM_DIR" pull --recurse-submodules
  git -C "$ALUMNIUM_DIR" submodule update --init --recursive
fi

WV="$ALUMNIUM_DIR/benchmarks/webvoyager"
echo "==> WebVoyager fork at: $WV"
ls "$WV"/run_claude_code.py "$WV"/evaluation/auto_eval.py >/dev/null && echo "    harness scripts present."

echo "==> Installing WebVoyager evaluator deps (openai, selenium, pillow) into a uv venv"
uv venv "$WV/.venv" >/dev/null
uv pip install --python "$WV/.venv/bin/python" -r "$WV/requirements.txt" >/dev/null
echo "    done."

echo "==> Checking Alumnium MCP entrypoint (uvx alumnium)"
uvx alumnium --help >/dev/null 2>&1 && echo "    uvx alumnium OK" || echo "    NOTE: 'uvx alumnium' will fetch on first MCP launch."

cat <<'EOF'

==> NEXT: credentials (the run itself needs ONE provider key)
  - Orchestrator      : Claude Code (`claude`) on your subscription  -> no key
  - Alumnium do/check : needs a provider key (their run used GPT-5 Nano via Azure OpenAI;
                        plain OpenAI works too): export OPENAI_API_KEY=...
  - Judge             : either OpenAI gpt-5-chat (their auto_eval) OR our subscription
                        Claude judge (../webvoyager/evaluate.py) -> no key

Recommended (single key): use ../webvoyager/alumnium_run.py with OPENAI_API_KEY set, then
  python extract_failures.py ../webvoyager/results/alumnium

For exact fidelity, run their own scripts under "$WV" (run_claude_code.py + auto_eval.py), then
  python extract_failures.py "$WV/results/claude-code"
EOF
