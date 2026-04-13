"""Benchmark worker — calibrage des performances d'inference."""

from __future__ import annotations

import logging
import time

from .engine import InferenceEngine

log = logging.getLogger("iamine.benchmark")

# Prompts de bench (variés pour tester différents cas)
BENCH_PROMPTS = [
    {"role": "user", "content": "Count from 1 to 20."},
    {"role": "user", "content": "Write a short poem about the sea."},
    {"role": "user", "content": "Explain what a CPU does in one paragraph."},
    {"role": "user", "content": "List 5 programming languages and their main use."},
    {"role": "user", "content": "What is distributed computing?"},
]


def run_benchmark(
    engine: InferenceEngine,
    rounds: int = 5,
    tokens_per_round: int = 64,
) -> dict:
    """Lance un benchmark complet et retourne les résultats.

    Measures tokens/sec over multiple rounds,
    computes avg/min/max/stability,
    and returns a config recommendation.

    Retourne :
    {
        "avg_tps": float,
        "min_tps": float,
        "max_tps": float,
        "stability_pct": float,     # 100% = parfaitement stable
        "avg_ttft_ms": float,       # time to first token
        "avg_duration_sec": float,
        "total_tokens": int,
        "rounds": int,
        "results": [...]
    }
    """
    if not engine.loaded:
        raise RuntimeError("Modèle non chargé")

    print()
    print(f" * BENCHMARK   STARTING ({rounds} rounds, {tokens_per_round} tokens/round)")
    print()

    results = []

    for i in range(rounds):
        prompt = [BENCH_PROMPTS[i % len(BENCH_PROMPTS)]]
        prompt_text = prompt[0]["content"]

        print(f"   Round {i + 1}/{rounds} — \"{prompt_text[:40]}...\"", end="", flush=True)

        t0 = time.perf_counter()
        result = engine.generate(prompt, max_tokens=tokens_per_round)
        t_total = time.perf_counter() - t0

        # Time to first token (approximation : durée totale - tokens * temps/token)
        if result.tokens_generated > 1:
            time_per_token = result.duration_sec / result.tokens_generated
            ttft = t_total - (result.tokens_generated * time_per_token)
        else:
            ttft = t_total

        results.append({
            "round": i + 1,
            "tokens": result.tokens_generated,
            "tps": result.tokens_per_sec,
            "duration_sec": result.duration_sec,
            "ttft_ms": round(max(0, ttft) * 1000, 1),
        })

        print(f" → {result.tokens_generated} tok, {result.tokens_per_sec} t/s, {result.duration_sec}s")

    # Calculs agrégés
    tps_values = [r["tps"] for r in results]
    avg_tps = sum(tps_values) / len(tps_values)
    min_tps = min(tps_values)
    max_tps = max(tps_values)

    # Stabilité = 1 - (écart-type / moyenne)
    if avg_tps > 0:
        variance = sum((v - avg_tps) ** 2 for v in tps_values) / len(tps_values)
        stddev = variance ** 0.5
        stability = max(0, (1 - stddev / avg_tps)) * 100
    else:
        stability = 0

    avg_ttft = sum(r["ttft_ms"] for r in results) / len(results)
    avg_duration = sum(r["duration_sec"] for r in results) / len(results)
    total_tokens = sum(r["tokens"] for r in results)

    bench_result = {
        "avg_tps": round(avg_tps, 2),
        "min_tps": round(min_tps, 2),
        "max_tps": round(max_tps, 2),
        "stability_pct": round(stability, 1),
        "avg_ttft_ms": round(avg_ttft, 1),
        "avg_duration_sec": round(avg_duration, 3),
        "total_tokens": total_tokens,
        "rounds": rounds,
        "model": engine.model_name,
        "results": results,
    }

    # Affichage des resultats
    print()
    print(f" * BENCHMARK   COMPLETE")
    print(f" * SPEED       avg={avg_tps:.1f} min={min_tps:.1f} max={max_tps:.1f} t/s")
    print(f" * STABILITY   {stability:.0f}%")
    print(f" * LATENCY     {avg_ttft:.0f}ms (time to first token)")
    print(f" * TOTAL       {total_tokens} tokens in {sum(r['duration_sec'] for r in results):.1f}s")
    print()

    return bench_result
