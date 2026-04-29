#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_MODEL  = "Qwen/Qwen2.5-7B-Instruct"
MODELS_DIR  = Path(__file__).parent / "models"
LLAMA_CPP   = Path.home() / "detroit-pulse" / "llama.cpp"


def check_prerequisites():
    errors = []

    convert_script = LLAMA_CPP / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        errors.append(f"llama.cpp not found at {LLAMA_CPP}")
        errors.append("  Fix: cd ~/detroit-pulse && git clone https://github.com/ggerganov/llama.cpp")

    quantize_bin = LLAMA_CPP / "llama-quantize"
    if not quantize_bin.exists():
        errors.append(f"llama-quantize binary not built at {quantize_bin}")
        errors.append("  Fix: cd ~/detroit-pulse/llama.cpp && make -j$(nproc) llama-quantize")

    try:
        subprocess.run(["ollama", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        errors.append("ollama not found in PATH")

    if errors:
        print("Prerequisites not met:\n")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)


def merge_adapter(version: str, adapter_dir: Path, merged_dir: Path):
    """Merge LoRA weights into base model on CPU."""
    print(f"\n── Step 1: Merging LoRA adapter into base model ──")
    print(f"  Adapter: {adapter_dir}")
    print(f"  Output:  {merged_dir}")
    print(f"  (This loads Qwen2.5-7B in fp16 on CPU — needs ~14GB RAM, ~3 min)\n")

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)

    print("  Loading base model in fp16...")
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        torch_dtype       = torch.float16,
        device_map        = "cpu",
        trust_remote_code = True,
    )

    print("  Applying LoRA adapter...")
    model = PeftModel.from_pretrained(base, str(adapter_dir))

    print("  Merging and unloading...")
    model = model.merge_and_unload()

    print(f"  Saving merged model to {merged_dir}...")
    merged_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(merged_dir))
    tokenizer.save_pretrained(str(merged_dir))
    print("  ✓ Merge complete")


def convert_to_gguf(merged_dir: Path, gguf_path: Path, quant: str):
    """Convert HF model to quantized GGUF using llama.cpp."""
    print(f"\n── Step 2: Converting to GGUF ({quant}) ──")

    # First convert to fp16 GGUF
    fp16_path = gguf_path.with_suffix(".fp16.gguf")

    convert_cmd = [
        sys.executable,
        str(LLAMA_CPP / "convert_hf_to_gguf.py"),
        str(merged_dir),
        "--outtype", "f16",
        "--outfile", str(fp16_path),
    ]
    print(f"  Converting to fp16 GGUF...")
    subprocess.run(convert_cmd, check=True)
    print(f"  ✓ fp16 GGUF: {fp16_path}")

    # Quantize to target format
    quant_cmd = [
        str(LLAMA_CPP / "llama-quantize"),
        str(fp16_path),
        str(gguf_path),
        quant.upper(),
    ]
    print(f"  Quantizing to {quant.upper()}...")
    subprocess.run(quant_cmd, check=True)
    fp16_path.unlink()  # remove intermediate fp16
    print(f"  ✓ Quantized GGUF: {gguf_path} ({gguf_path.stat().st_size // 1024 // 1024}MB)")


def register_with_ollama(version: str, gguf_path: Path, modelfile_path: Path) -> str:
    """Create Ollama Modelfile and register the model."""
    print(f"\n── Step 3: Registering with Ollama ──")

    ollama_name = f"detroit-{version}"

    modelfile_content = f"""FROM {gguf_path}

TEMPLATE \"\"\"<|im_start|>system
{{{{ .System }}}}<|im_end|>
<|im_start|>user
{{{{ .Prompt }}}}<|im_end|>
<|im_start|>assistant
\"\"\"

SYSTEM \"\"\"You are a Detroit metro area dispatch address normalizer.
Convert dispatch shorthand into full, geocodable location strings.
If no address is detectable, return exactly: NO_LOCATION
Return ONLY the address string. No explanation.\"\"\"

PARAMETER temperature 0
PARAMETER num_predict 64
PARAMETER stop "<|im_end|>"
"""

    modelfile_path.write_text(modelfile_content)
    print(f"  Modelfile: {modelfile_path}")

    create_cmd = ["ollama", "create", ollama_name, "-f", str(modelfile_path)]
    print(f"  Running: {' '.join(create_cmd)}")
    subprocess.run(create_cmd, check=True)
    print(f"  ✓ Registered as: {ollama_name}")

    return ollama_name


def test_ollama_model(model_name: str) -> bool:
    """Smoke test — send a known transcript and check the output looks right."""
    import requests

    print(f"\n── Step 4: Smoke test ──")
    test_cases = [
        ("Engine 30 respond to 14303 East Warren structure fire", "14303"),
        ("Units responding multiple gunshots fired no address given", "NO_LOCATION"),
        ("Medic 5 to 9 Mile and Woodward medical emergency", "9 Mile"),
    ]

    passed = 0
    for transcript, expected_fragment in test_cases:
        try:
            resp = requests.post(
                "http://localhost:11434/api/chat",
                json={
                    "model":    model_name,
                    "messages": [{"role": "user", "content": f"Transmission: {transcript}"}],
                    "stream":   False,
                    "options":  {"temperature": 0, "num_predict": 64},
                },
                timeout=30,
            )
            output = resp.json()["message"]["content"].strip()
            ok = expected_fragment.lower() in output.lower()
            symbol = "✓" if ok else "✗"
            print(f"  [{symbol}] {transcript[:50]}")
            print(f"       Expected fragment: '{expected_fragment}'  Got: '{output}'")
            if ok:
                passed += 1
        except Exception as e:
            print(f"  [✗] Request failed: {e}")

    print(f"\n  Smoke test: {passed}/{len(test_cases)} passed")
    return passed >= 2


def update_meta(adapter_dir: Path, ollama_name: str, gguf_path: Path):
    """Update the adapter's meta.json with export info."""
    meta_path = adapter_dir / "meta.json"
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
    else:
        meta = {}

    meta.update({
        "ollama_model_name": ollama_name,
        "gguf_path":         str(gguf_path),
        "exported_at":       datetime.now(timezone.utc).isoformat(),
        "status":            "exported",
    })

    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True,
                        help="Adapter version, e.g. normalize-v1")
    parser.add_argument("--quant", default="q4_k_m",
                        choices=["q4_k_m", "q5_k_m", "q3_k_m", "q8_0"],
                        help="GGUF quantization format (default: q4_k_m)")
    args = parser.parse_args()

    check_prerequisites()

    adapter_dir  = MODELS_DIR / args.version
    merged_dir   = MODELS_DIR / f"{args.version}-merged"
    gguf_path    = MODELS_DIR / f"{args.version}.gguf"
    modelfile    = MODELS_DIR / f"Modelfile.{args.version}"

    if not adapter_dir.exists():
        print(f"Adapter not found: {adapter_dir}")
        print("Run lora/scripts/train_lora.py first.")
        sys.exit(1)

    # Skip merge if already done
    if merged_dir.exists():
        print(f"Merged model already exists at {merged_dir}, skipping merge.")
    else:
        merge_adapter(args.version, adapter_dir, merged_dir)

    # Skip GGUF conversion if already done
    if gguf_path.exists():
        print(f"GGUF already exists at {gguf_path}, skipping conversion.")
    else:
        convert_to_gguf(merged_dir, gguf_path, args.quant)

    ollama_name = register_with_ollama(args.version, gguf_path, modelfile)
    smoke_ok    = test_ollama_model(ollama_name)
    update_meta(adapter_dir, ollama_name, gguf_path)

    print(f"\n{'='*55}")
    print(f"  Export complete: {ollama_name}")
    print(f"  Smoke test: {'PASSED' if smoke_ok else 'FAILED (check output above)'}")
    print(f"{'='*55}")
    print(f"\nNext steps:")
    print(f"  1. Run eval comparison:")
    print(f"       python lora/scripts/eval_baseline.py --lora-model {ollama_name}")
    print(f"  2. If gates pass, enable shadow mode in .env:")
    print(f"       LORA_SHADOW_MODE=true")
    print(f"       LORA_NORMALIZE_MODEL={ollama_name}")
    print(f"  3. Restart pipeline and monitor for 48h")


if __name__ == "__main__":
    main()