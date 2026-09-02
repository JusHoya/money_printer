#!/usr/bin/env bash
# ============================================================================
# PREPARED, NOT AUTO-RUN. Run ONLY after side-by-side validation of the small
# model against the current one (tool-calling through the Hermes mp_* plugin,
# not just chat). This DESTROYS and recreates the mp-vllm container.
# ============================================================================
#
# Swaps alcyone's Hermes serving model:
#   FROM nvidia/Qwen3.6-35B-A3B-NVFP4   (51.1GB reserved / ~45GB consumed)
#   TO   ykarout/Qwen3.5-9B-NVFP4       (21.9GB reserved at 0.18 util)
# freeing ~29GB of the Spark's 121GB for lab/backtest work.
# 0.18 is measured, not guessed: at 0.12 the load FAILS ("No available memory
# for the cache blocks") — the checkpoint's weights are 11.2GB (vision tower +
# BF16 attention), leaving negative KV budget at 14.6GB. Validated 2026-09-01
# side-by-side on this box: 5.59GB KV available, tool-call smoke test passed.
#
# --max-model-len 65536 is a FLOOR, not a tuning knob: Hermes Agent refuses
# any model whose context_length is below 64K (agent_init raises on every
# chat turn — the 2026-09-01 swap first shipped 32768 and every Discord
# message failed with "context window of 32,768 tokens ... below the minimum
# 64,000"). The 9B's native window is 262144, and at 0.18 util the KV pool
# (164,789 tokens) still gives 2.5x concurrency at 64K.
#
# ROLLBACK — the exact command the current container was started with; rerun
# it verbatim (after docker rm -f mp-vllm) to restore the 35B model:
#
#   docker run -d --name mp-vllm --restart unless-stopped --gpus all \
#     -v /home/jushoya/.cache/huggingface:/root/.cache/huggingface \
#     -p 127.0.0.1:8000:8000 \
#     vllm/vllm-openai:latest \
#     nvidia/Qwen3.6-35B-A3B-NVFP4 \
#     --host 0.0.0.0 --port 8000 \
#     --tensor-parallel-size 1 \
#     --trust-remote-code \
#     --kv-cache-dtype fp8 \
#     --attention-backend flashinfer \
#     --moe-backend marlin \
#     --gpu-memory-utilization 0.42 \
#     --max-model-len 65536 \
#     --max-num-seqs 4 \
#     --max-num-batched-tokens 8192 \
#     --enable-chunked-prefill \
#     --async-scheduling \
#     --enable-prefix-caching \
#     --load-format fastsafetensors \
#     --reasoning-parser qwen3 \
#     --tool-call-parser qwen3_xml \
#     --enable-auto-tool-choice
#
# (Rolling back also means reverting the ~/.hermes config: model.default back
# to nvidia/Qwen3.6-35B-A3B-NVFP4; model.context_length stays 65536 — and
# re-pinning any cron job with `hermes cron edit <id> --provider custom
# --model <model>`, or it drift-skips.)
set -euo pipefail

if [ "${1:-}" != "--yes" ]; then
  echo "This replaces the mp-vllm serving container (35B -> 9B model)."
  echo "Run only after side-by-side validation. Confirm with:"
  echo "  bash deploy/spark/hermes_model_swap.sh --yes"
  exit 1
fi

echo "== Removing the current mp-vllm container"
docker rm -f mp-vllm

echo "== Starting mp-vllm on ykarout/Qwen3.5-9B-NVFP4"
docker run -d --name mp-vllm --restart unless-stopped --gpus all \
  -v /home/jushoya/.cache/huggingface:/root/.cache/huggingface \
  -p 127.0.0.1:8000:8000 \
  vllm/vllm-openai:latest \
  ykarout/Qwen3.5-9B-NVFP4 \
  --host 0.0.0.0 --port 8000 \
  --trust-remote-code \
  --gpu-memory-utilization 0.18 \
  --max-model-len 65536 \
  --max-num-seqs 4 \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3

echo
echo "== Container started. REMAINING MANUAL STEPS (Hermes config, ~/.hermes):"
echo "   1) model.default        = ykarout/Qwen3.5-9B-NVFP4"
echo "   2) model.context_length = 65536 (Hermes refuses anything under 64K)"
echo "   3) KEEP model.max_tokens = 8192 (unset, Hermes sends max_tokens ="
echo "      context_length and vLLM 400s every tool-bearing request)"
echo "   4) KEEP the placeholder model.api_key (Hermes rejects empty keys)"
echo "   5) Re-pin every cron job to the new model, or each one drift-skips:"
echo "      hermes cron edit <job-id> --provider custom --model ykarout/Qwen3.5-9B-NVFP4"
echo "   Then restart Hermes and watch: docker logs -f mp-vllm"
echo
echo "   Rollback: docker rm -f mp-vllm, then the verbatim command in this"
echo "   script's header (and revert the Hermes config)."
