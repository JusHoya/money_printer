#!/usr/bin/env bash
# ============================================================================
# PREPARED, NOT AUTO-RUN. Run ONLY after side-by-side validation of the small
# model against the current one (tool-calling through the Hermes mp_* plugin,
# not just chat). This DESTROYS and recreates the mp-vllm container.
# ============================================================================
#
# Swaps alcyone's Hermes serving model:
#   FROM nvidia/Qwen3.6-35B-A3B-NVFP4   (~45GB unified RAM consumed)
#   TO   ykarout/Qwen3.5-9B-NVFP4       (~10-12GB)
# freeing ~35GB of the Spark's 121GB for lab/backtest work.
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
# (Rolling back also means reverting the ~/.hermes config: model.default and
# model.context_length back to nvidia/Qwen3.6-35B-A3B-NVFP4 / 65536.)
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
  --gpu-memory-utilization 0.12 \
  --max-model-len 32768 \
  --max-num-seqs 4 \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3

echo
echo "== Container started. REMAINING MANUAL STEPS (Hermes config, ~/.hermes):"
echo "   1) model.default        = ykarout/Qwen3.5-9B-NVFP4"
echo "   2) model.context_length = 32768"
echo "   3) KEEP model.max_tokens = 8192 (unset, Hermes sends max_tokens ="
echo "      context_length and vLLM 400s every tool-bearing request)"
echo "   4) KEEP the placeholder model.api_key (Hermes rejects empty keys)"
echo "   Then restart Hermes and watch: docker logs -f mp-vllm"
echo
echo "   Rollback: docker rm -f mp-vllm, then the verbatim command in this"
echo "   script's header (and revert the Hermes config)."
