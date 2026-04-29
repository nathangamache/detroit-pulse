#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key
from sqlalchemy import create_engine, text

load_dotenv()

ENV_FILE   = Path.home() / "detroit-pulse" / ".env"
MODELS_DIR = Path(__file__).parent / "models"
engine     = create_engine(os.getenv("DATABASE_URL"))

OLLAMA_URL  = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
BASE_MODEL  = os.getenv("LORA_BASE_MODEL", "qwen2.5:7b-instruct")


# ── Registry helpers ──────────────────────────────────────────────────────────

def register_model(version: str, ollama_name: str, meta: dict):
    """Insert or update lora_model_registry."""
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO lora_model_registry (
                version, task, adapter_path, ollama_model_name,
                train_examples, human_reviewed,
                eval_exact_match, eval_no_location_f1, eval_hallucination_rate,
                status
            ) VALUES (
                :version, :task, :adapter_path, :ollama_model_name,
                :train_examples, :human_reviewed,
                :eval_exact_match, :eval_no_location_f1, :eval_hallucination_rate,
                'shadow'
            )
            ON CONFLICT (version) DO UPDATE SET
                ollama_model_name       = EXCLUDED.ollama_model_name,
                train_examples          = EXCLUDED.train_examples,
                human_reviewed          = EXCLUDED.human_reviewed,
                eval_exact_match        = EXCLUDED.eval_exact_match,
                eval_no_location_f1     = EXCLUDED.eval_no_location_f1,
                eval_hallucination_rate = EXCLUDED.eval_hallucination_rate
        """), {
            "version":               version,
            "task":                  "normalize",
            "adapter_path":          str(MODELS_DIR / version),
            "ollama_model_name":     ollama_name,
            "train_examples":        meta.get("train_examples"),
            "human_reviewed":        meta.get("human_reviewed"),
            "eval_exact_match":      meta.get("eval_exact_match"),
            "eval_no_location_f1":   meta.get("eval_no_location_f1"),
            "eval_hallucination_rate": meta.get("eval_hallucination_rate"),
        })
        conn.commit()


def set_status(version: str, status: str):
    with engine.connect() as conn:
        deployed_clause = ", deployed_at = NOW()" if status == "production" else ""
        retired_clause  = ", retired_at = NOW()"  if status == "retired"    else ""
        conn.execute(text(f"""
            UPDATE lora_model_registry
            SET status = :status{deployed_clause}{retired_clause}
            WHERE version = :version
        """), {"version": version, "status": status})
        conn.commit()


def retire_current_production():
    """Mark any currently production model as retired."""
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE lora_model_registry
            SET status = 'retired', retired_at = NOW()
            WHERE status = 'production'
        """))
        conn.commit()


def get_current_status() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT version, task, ollama_model_name, status,
                   eval_exact_match, eval_no_location_f1, eval_hallucination_rate,
                   deployed_at, retired_at, created_at
            FROM lora_model_registry
            ORDER BY created_at DESC
        """)).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Ollama verification ───────────────────────────────────────────────────────

def verify_ollama_model(model_name: str) -> bool:
    """Confirm Ollama can serve the model with a test inference."""
    print(f"  Verifying Ollama model: {model_name}...")
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":    model_name,
                "messages": [{"role": "user",
                              "content": "Transmission: Engine 30 respond to 14303 East Warren structure fire"}],
                "stream":  False,
                "options": {"temperature": 0, "num_predict": 32},
            },
            timeout=60,
        )
        output = resp.json()["message"]["content"].strip()
        ok     = "warren" in output.lower() or "14303" in output
        print(f"  Test output: {output}")
        print(f"  Contains expected content: {'yes' if ok else 'NO — check model'}")
        return ok
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


# ── .env manipulation ─────────────────────────────────────────────────────────

def update_env(lora_enabled: bool, shadow_mode: bool, model_name: str | None = None):
    """Update feature flags in .env file."""
    set_key(str(ENV_FILE), "LORA_NORMALIZE_ENABLED", "true" if lora_enabled else "false")
    set_key(str(ENV_FILE), "LORA_SHADOW_MODE",       "true" if shadow_mode  else "false")
    if model_name:
        set_key(str(ENV_FILE), "LORA_NORMALIZE_MODEL", model_name)
    print(f"  Updated {ENV_FILE}:")
    print(f"    LORA_NORMALIZE_ENABLED={str(lora_enabled).lower()}")
    print(f"    LORA_SHADOW_MODE={str(shadow_mode).lower()}")
    if model_name:
        print(f"    LORA_NORMALIZE_MODEL={model_name}")


# ── Trend verification before promotion ──────────────────────────────────────

def check_recent_metrics(hours: int = 48) -> dict:
    """Pull recent pipeline_metrics and compare model versions."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                model_version,
                SUM(normalize_total)                                          AS n,
                SUM(geocode_high)::FLOAT / NULLIF(SUM(normalize_total),0)    AS geo_high_rate,
                SUM(normalize_no_loc)::FLOAT / NULLIF(SUM(normalize_total),0) AS no_loc_rate,
                SUM(corr_update)::FLOAT / NULLIF(SUM(corr_new)+SUM(corr_update),0) AS merge_rate,
                SUM(norm_latency_ms_sum) / NULLIF(SUM(normalize_total),0)    AS avg_ms
            FROM pipeline_metrics
            WHERE bucket >= NOW() - INTERVAL '{hours} hours'
            GROUP BY model_version
        """)).fetchall()
    return {r.model_version: dict(r._mapping) for r in rows}


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_status():
    models = get_current_status()
    if not models:
        print("No models in registry. Train and export a model first.")
        return

    print(f"\n{'─'*65}")
    print(f"  LoRA Model Registry")
    print(f"{'─'*65}")
    for m in models:
        status_color = {
            "production": "\033[92m",
            "shadow":     "\033[93m",
            "trained":    "\033[94m",
            "retired":    "\033[90m",
        }.get(m["status"], "")
        reset = "\033[0m"
        print(f"\n  Version:     {m['version']}")
        print(f"  Status:      {status_color}{m['status']}{reset}")
        print(f"  Ollama:      {m['ollama_model_name'] or 'not exported'}")
        if m["eval_exact_match"]:
            print(f"  Eval:        exact={m['eval_exact_match']:.1%}  "
                  f"no_loc_f1={m['eval_no_location_f1']:.1%}  "
                  f"halluc={m['eval_hallucination_rate']:.1%}")
        if m["deployed_at"]:
            print(f"  Deployed:    {str(m['deployed_at'])[:19]}")

    print(f"\n  Active flags (.env):")
    print(f"    LORA_NORMALIZE_ENABLED = {os.getenv('LORA_NORMALIZE_ENABLED','false')}")
    print(f"    LORA_SHADOW_MODE       = {os.getenv('LORA_SHADOW_MODE','false')}")
    print(f"    LORA_NORMALIZE_MODEL   = {os.getenv('LORA_NORMALIZE_MODEL','(not set)')}")

    metrics = check_recent_metrics(48)
    if metrics:
        print(f"\n  Pipeline metrics (last 48h):")
        print(f"  {'Model':<30} {'Chunks':>7} {'GeoHigh':>9} {'NoLoc':>7} {'Merge':>7} {'Latency':>9}")
        for mv, m in metrics.items():
            print(f"  {mv:<30} {int(m['n'] or 0):>7} "
                  f"{(m['geo_high_rate'] or 0):.1%}   "
                  f"{(m['no_loc_rate'] or 0):.1%} "
                  f"{(m['merge_rate'] or 0):.1%} "
                  f"{int(m['avg_ms'] or 0):>7}ms")
    print()


def cmd_promote(version: str):
    print(f"\n── Promoting {version} to production ──\n")

    adapter_dir = MODELS_DIR / version
    meta_path   = adapter_dir / "meta.json"

    if not adapter_dir.exists():
        print(f"Adapter not found: {adapter_dir}")
        print("Run train_lora.py and export_gguf.py first.")
        sys.exit(1)

    meta = {}
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)

    ollama_name = meta.get("ollama_model_name")
    if not ollama_name:
        print("No ollama_model_name in meta.json. Run export_gguf.py first.")
        sys.exit(1)

    # Verify Ollama can serve it
    if not verify_ollama_model(ollama_name):
        print("\nOllama verification failed. Aborting promotion.")
        sys.exit(1)

    # Check shadow analysis report
    shadow_reports = sorted(
        Path(MODELS_DIR).glob("data/shadow_report_*.json")
    )
    if not shadow_reports:
        print("No shadow_report_*.json found in lora/data/.")
        print("Run lora/scripts/shadow_analysis.py first.")
        resp = input("Promote anyway? [y/N] ").strip().lower()
        if resp != 'y':
            sys.exit(1)
    else:
        latest = shadow_reports[-1]
        with open(latest) as f:
            shadow = json.load(f)
        if not shadow.get("all_gates_pass"):
            print(f"Shadow report {latest.name} did NOT pass all gates.")
            resp = input("Promote anyway? [y/N] ").strip().lower()
            if resp != 'y':
                sys.exit(1)
        else:
            print(f"  Shadow report gates: PASSED ({latest.name})")

    # Register in DB
    register_model(version, ollama_name, meta)

    # Retire any existing production model
    retire_current_production()

    # Mark new model as production
    set_status(version, "production")

    # Update .env flags
    update_env(lora_enabled=True, shadow_mode=False, model_name=ollama_name)

    print(f"\n  ✓ {version} promoted to production")
    print(f"\n  Restart the pipeline to apply:")
    print(f"    cd ~/detroit-pulse && ./stop.sh && ./start.sh")
    print(f"\n  Monitor for 48h:")
    print(f"    python lora/scripts/promote_model.py --status")
    print(f"\n  Emergency rollback:")
    print(f"    python lora/scripts/promote_model.py --rollback")


def cmd_rollback():
    print(f"\n── Rolling back to base model ──\n")

    # Retire production model
    retire_current_production()

    # Disable LoRA and shadow mode
    update_env(lora_enabled=False, shadow_mode=False)

    print(f"\n  ✓ Rolled back to base model ({BASE_MODEL})")
    print(f"\n  Restart the pipeline:")
    print(f"    cd ~/detroit-pulse && ./stop.sh && ./start.sh")


def main():
    parser = argparse.ArgumentParser()
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version",  help="Promote this version to production")
    group.add_argument("--rollback", action="store_true", help="Roll back to base model")
    group.add_argument("--status",   action="store_true", help="Show current status")
    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.rollback:
        cmd_rollback()
    elif args.version:
        cmd_promote(args.version)


if __name__ == "__main__":
    main()