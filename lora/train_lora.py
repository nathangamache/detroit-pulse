#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODELS_DIR, exist_ok=True)

BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
VERSION    = "normalize-v3"
OUTPUT_DIR = os.path.join(MODELS_DIR, VERSION)

# ── Verify data ───────────────────────────────────────────────────────────────

train_path = os.path.join(DATA_DIR, "normalize_train.jsonl")
eval_path  = os.path.join(DATA_DIR, "normalize_eval.jsonl")
stats_path = os.path.join(DATA_DIR, "export_stats.json")

for p in [train_path, eval_path]:
    if not os.path.exists(p):
        print(f"Missing: {p}")
        print("Run lora/export_dataset.py first.")
        raise SystemExit(1)

with open(stats_path) as f:
    stats = json.load(f)

if not stats.get("all_gates_pass"):
    print("Data quality gates did not all pass.")
    print("Check lora/data/export_stats.json and collect more labels.")
    print("Run anyway? [y/N]", end=" ")
    if input().strip().lower() != 'y':
        raise SystemExit(1)

# ── Imports ───────────────────────────────────────────────────────────────────

import torch
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)
from peft import (
    LoraConfig,
    TaskType,
    get_peft_model,
    prepare_model_for_kbit_training,
)
from trl import SFTTrainer, SFTConfig

# ── GPU profile detection ─────────────────────────────────────────────────────

def detect_gpu_profile() -> str:
    """
    Detect GPU and return a profile name.
    Override with GPU_PROFILE env var: GPU_PROFILE=a100 python lora/train_lora.py
    """
    override = os.getenv("GPU_PROFILE", "").lower()
    if override in ("a100", "4070", "local"):
        return override

    if not torch.cuda.is_available():
        print("WARNING: No CUDA GPU detected — training on CPU will be extremely slow.")
        return "cpu"

    gpu_name = torch.cuda.get_device_name(0).upper()
    vram_gb  = torch.cuda.get_device_properties(0).total_memory / 1024**3

    if "A100" in gpu_name:
        return "a100"
    if "4070" in gpu_name or "4080" in gpu_name or "4090" in gpu_name:
        return "4070"
    if vram_gb >= 40:
        return "a100"   # treat any 40GB+ card as A100-class
    return "4070"       # default to conservative settings for unknown GPUs


GPU_PROFILES = {
    # ── A100 80GB ─────────────────────────────────────────────────────────────
    # 80GB headroom — larger batches, higher LoRA rank, full saturation.
    # r=64 for v3 — more expressive adapter for harder disambiguation cases.
    "a100": {
        "lora_r":                    64,    # was 32 in v1, targeting better halluc reduction
        "lora_alpha":                128,   # keep alpha = 2×r
        "per_device_train_batch_size": 8,
        "per_device_eval_batch_size":  8,
        "gradient_accumulation_steps": 4,   # effective batch = 32
        "dataloader_num_workers":      4,
        "estimated_time":            "~15-20 minutes",
    },
    # ── RTX 4070 Ti Super 16GB ────────────────────────────────────────────────
    # 16GB — small batches, accumulate to effective 16, conservative rank.
    # Leaves ~1.5GB headroom so the GPU doesn't OOM mid-epoch.
    "4070": {
        "lora_r":                    16,
        "lora_alpha":                32,
        "per_device_train_batch_size": 2,
        "per_device_eval_batch_size":  2,
        "gradient_accumulation_steps": 8,   # effective batch = 16
        "dataloader_num_workers":      0,   # WSL2 doesn't handle workers well
        "estimated_time":            "~60-90 minutes",
    },
    # ── CPU fallback ──────────────────────────────────────────────────────────
    "cpu": {
        "lora_r":                    8,
        "lora_alpha":                16,
        "per_device_train_batch_size": 1,
        "per_device_eval_batch_size":  1,
        "gradient_accumulation_steps": 16,
        "dataloader_num_workers":      0,
        "estimated_time":            "many hours — use a GPU",
    },
}

profile_name = detect_gpu_profile()
profile      = GPU_PROFILES[profile_name]

gpu_name_display = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
vram_display     = (
    f"{torch.cuda.get_device_properties(0).total_memory / 1024**3:.0f}GB"
    if torch.cuda.is_available() else "N/A"
)

print(f"\n── Training config ──")
print(f"  Base model:  {BASE_MODEL}")
print(f"  Version:     {VERSION}")
print(f"  GPU:         {gpu_name_display} ({vram_display})")
print(f"  Profile:     {profile_name.upper()}")
print(f"  LoRA rank:   r={profile['lora_r']}  alpha={profile['lora_alpha']}")
print(f"  Batch size:  {profile['per_device_train_batch_size']} × "
      f"{profile['gradient_accumulation_steps']} accum = "
      f"{profile['per_device_train_batch_size'] * profile['gradient_accumulation_steps']} effective")
print(f"  Train set:   {stats['train']} examples")
print(f"  Eval set:    {stats['eval']} examples")
print(f"  Est. time:   {profile['estimated_time']}")
print(f"  Output:      {OUTPUT_DIR}")
print()

# ── Load data ─────────────────────────────────────────────────────────────────

def load_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f]

train_ds = Dataset.from_list([{"text": d["text"]} for d in load_jsonl(train_path)])
eval_ds  = Dataset.from_list([{"text": d["text"]} for d in load_jsonl(eval_path)])

# ── Quantization ─────────────────────────────────────────────────────────────

bnb_config = BitsAndBytesConfig(
    load_in_4bit              = True,
    bnb_4bit_quant_type       = "nf4",
    bnb_4bit_compute_dtype    = torch.bfloat16,
    bnb_4bit_use_double_quant = True,
)

# ── Load base model ───────────────────────────────────────────────────────────

print("Loading base model...")
tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    quantization_config = bnb_config,
    device_map          = "auto",
    trust_remote_code   = True,
)
model.config.use_cache      = False
model.config.pretraining_tp = 1

# ── Prepare for kbit training ─────────────────────────────────────────────────
# Must happen BEFORE get_peft_model.
# use_gradient_checkpointing=False + enable_input_require_grads() is the correct
# pattern — avoids "inputs have no requires_grad" RuntimeError and warning.

model = prepare_model_for_kbit_training(
    model,
    use_gradient_checkpointing=False,
)
model.enable_input_require_grads()

# ── LoRA config (profile-driven) ──────────────────────────────────────────────

lora_config = LoraConfig(
    r              = profile["lora_r"],
    lora_alpha     = profile["lora_alpha"],
    lora_dropout   = 0.05,
    bias           = "none",
    task_type      = TaskType.CAUSAL_LM,
    target_modules = [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ],
)

model = get_peft_model(model, lora_config)
model.print_trainable_parameters()

# ── Training config (profile-driven) ─────────────────────────────────────────

NUM_EPOCHS = 3
LR         = 2e-4

training_args = SFTConfig(
    output_dir                  = OUTPUT_DIR,
    num_train_epochs            = NUM_EPOCHS,
    per_device_train_batch_size = profile["per_device_train_batch_size"],
    per_device_eval_batch_size  = profile["per_device_eval_batch_size"],
    gradient_accumulation_steps = profile["gradient_accumulation_steps"],
    gradient_checkpointing      = False,   # handled by prepare_model_for_kbit_training
    learning_rate               = LR,
    lr_scheduler_type           = "cosine",
    warmup_ratio                = 0.05,
    bf16                        = True,
    logging_steps               = 10,
    eval_strategy               = "steps",
    eval_steps                  = 50,
    save_strategy               = "steps",
    save_steps                  = 50,
    save_total_limit            = 3,
    load_best_model_at_end      = True,
    metric_for_best_model       = "eval_loss",
    greater_is_better           = False,
    report_to                   = "none",
    dataloader_num_workers      = profile["dataloader_num_workers"],
    optim                       = "paged_adamw_32bit",
    dataset_text_field          = "text",
    max_seq_length              = 512,
    packing                     = False,
)

# ── Train ─────────────────────────────────────────────────────────────────────

trainer = SFTTrainer(
    model            = model,
    processing_class = tokenizer,
    train_dataset    = train_ds,
    eval_dataset     = eval_ds,
    args             = training_args,
)

print(f"Starting training at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n")

trainer.train()

# ── Save ──────────────────────────────────────────────────────────────────────

trainer.save_model(OUTPUT_DIR)
tokenizer.save_pretrained(OUTPUT_DIR)

meta = {
    "version":        VERSION,
    "base_model":     BASE_MODEL,
    "trained_at":     datetime.now(timezone.utc).isoformat(),
    "gpu":            gpu_name_display,
    "gpu_profile":    profile_name,
    "train_examples": stats["train"],
    "human_reviewed": stats.get("human", 0),
    "lora_r":         lora_config.r,
    "lora_alpha":     lora_config.lora_alpha,
    "epochs":         NUM_EPOCHS,
    "learning_rate":  LR,
    "effective_batch":profile["per_device_train_batch_size"] * profile["gradient_accumulation_steps"],
    "status":         "trained",
}
with open(os.path.join(OUTPUT_DIR, "meta.json"), "w") as f:
    json.dump(meta, f, indent=2)

print(f"\n✓ Adapter saved to {OUTPUT_DIR}")
print(f"Next steps:")
print(f"  1. python lora/export_gguf.py  (convert for Ollama)")
print(f"  2. python lora/eval_baseline.py --lora-model detroit-{VERSION}")
print(f"  3. If eval gates pass: set LORA_SHADOW_MODE=true in .env")