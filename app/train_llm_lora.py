"""LoRA / QLoRA fine-tune of Qwen2.5-1.5B-Instruct for shadow notes.

The dataset is built by ``note_examples``: the same decision-time prompt as
``explain``, paired with a template note. Labels and later rows are not
inputs. The adapter is saved as safetensors with a SHA-256 sidecar. This
script does not set ``LLM_LORA`` and it does not page.

QLoRA (4-bit) is used when CUDA and bitsandbytes import. Otherwise the script
trains LoRA in fp16 on CUDA or fp32 on CPU.

    python train_llm_lora.py --data synthetic --device cuda --epochs 1 --out results/llm_lora
    python train_llm_lora.py --data gaia --device cuda --epochs 1 --out results/llm_lora
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import load_gaia
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults
from src.llm_lora import FORMAT, META_NAME, WEIGHTS_NAME, example_tensors, load_base_pin, note_examples
from src.sample_data import generate
from src.weight_io import save_bytes


def _examples(which: str, seed: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    config = Config()
    if which == "gaia":
        if not (Path("data/gaia_raw").exists() and Path("data/gaia_metrics").exists()):
            raise SystemExit(
                "GAIA folders are not in this run. Nothing was trained. "
                "On the laptop: python train_llm_lora.py --data gaia --device cuda"
            )
        data = load_gaia()
        alerts, _faults = data.alerts, data.faults
    else:
        folder = Path(tempfile.mkdtemp(prefix="lora-train-"))
        generate(folder, seed=seed, days=14)
        alerts, _faults = align_alerts_to_faults(load_alerts(folder / "alerts.csv"), load_faults(folder / "faults.csv"))
    origin = min(alert.timestamp for alert in alerts)
    boundary = origin + pd.Timedelta(days=config.split_day)
    train = note_examples(alerts, [], until=boundary)
    held = note_examples(alerts, [], since=boundary)
    for row in train + held:
        if "fault_id" in row["prompt"] or "\nlabel:" in row["prompt"]:
            raise SystemExit("refusing a training row whose prompt contains a label")
    if not train:
        raise SystemExit("no decision-time rows in the training window")
    return train, held


def _load_model(base: str, revision: str, device: str):
    import torch
    from transformers import AutoModelForCausalLM

    quant = None
    if device.startswith("cuda"):
        try:
            import bitsandbytes  # noqa: F401
            from transformers import BitsAndBytesConfig

            quant = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
            )
        except ImportError:
            quant = None
    kwargs: dict = {"revision": revision}
    if quant is not None:
        kwargs["quantization_config"] = quant
        kwargs["device_map"] = "auto"
    else:
        kwargs["dtype"] = torch.float16 if device.startswith("cuda") else torch.float32
    model = AutoModelForCausalLM.from_pretrained(base, **kwargs)
    if quant is None:
        model = model.to(device)
    return model, quant is not None


def _verify_base(base: str, filename: str, expected: str) -> None:
    path = Path(base) / filename
    if not path.is_file():
        return
    from product.llm import sha256_file

    digest = sha256_file(path)
    if digest != expected:
        raise SystemExit(f"base model sha256 {digest} does not match the pin {expected}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LoRA fine-tune of the shadow-note model.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), default="synthetic")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--base", default=None, help="Local snapshot directory. Defaults to the pinned repo id.")
    parser.add_argument("--out", default="results/llm_lora")
    args = parser.parse_args()
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "torch, transformers and peft are required. They are not installed in CPU CI. "
            "See docs/ML_MODELS.md for the cu130 install."
        ) from exc
    pin = load_base_pin()
    base = args.base or str(pin["repo"])
    revision = str(pin["revision"])
    _verify_base(base, str(pin["filename"]), str(pin["sha256"]))
    device = args.device
    if device.startswith("cuda") and not torch.cuda.is_available():
        device = "cpu"
    train_rows, held_rows = _examples(args.data, args.seed)
    tokenizer = AutoTokenizer.from_pretrained(base, revision=revision, local_files_only=Path(base).is_dir())
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model, qlora = _load_model(base, revision, device)
    model = get_peft_model(
        model,
        LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "v_proj"],
        ),
    )
    torch.manual_seed(args.seed)
    optim = torch.optim.AdamW((param for param in model.parameters() if param.requires_grad), lr=1e-4)
    train_loss = None
    held_out_loss = None
    for epoch in range(max(1, args.epochs)):
        model.train()
        total = 0.0
        seen = 0
        for row in train_rows:
            input_ids, labels = example_tensors(tokenizer, row["prompt"], row["reference"])
            input_ids = input_ids.to(model.device)
            labels = labels.to(model.device)
            optim.zero_grad(set_to_none=True)
            loss = model(input_ids=input_ids, labels=labels).loss
            loss.backward()
            optim.step()
            total += float(loss.detach())
            seen += 1
        train_loss = total / seen if seen else None
        held_out_loss = _held_out_loss(model, tokenizer, held_rows)
        print(f"epoch={epoch + 1} train_loss={train_loss} held_out_loss={held_out_loss} held_out_rows={len(held_rows)}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    peft_weights = out / "adapter_model.safetensors"
    if not peft_weights.is_file():
        raise SystemExit("peft did not write adapter_model.safetensors")
    digest = save_bytes(out / WEIGHTS_NAME, peft_weights.read_bytes())
    payload = {
        "format": FORMAT,
        "pages": False,
        "base_repo": pin["repo"],
        "base_revision": revision,
        "base_sha256": pin["sha256"],
        "weights_sha256": digest,
        "qlora": qlora,
        "rows": len(train_rows),
        "held_out_rows": len(held_rows),
        "train_loss": train_loss,
        "held_out_loss": held_out_loss,
        "device": device,
    }
    save_bytes(out / META_NAME, json.dumps(payload, sort_keys=True).encode("utf-8"))
    print(
        f"rows={len(train_rows)} held_out_rows={len(held_rows)} "
        f"train_loss={train_loss} held_out_loss={held_out_loss} "
        f"qlora={qlora} device={device} sha256={digest} pages=false"
    )


def _held_out_loss(model, tokenizer, rows: list[dict[str, str]]) -> float | None:
    """Mean loss on later decision-time rows. Those rows do not update weights."""
    import torch

    if not rows:
        return None
    was_training = model.training
    model.eval()
    total = 0.0
    try:
        with torch.no_grad():
            for row in rows:
                input_ids, labels = example_tensors(tokenizer, row["prompt"], row["reference"])
                loss = model(input_ids=input_ids.to(model.device), labels=labels.to(model.device)).loss
                total += float(loss.detach())
    finally:
        model.train(was_training)
    return total / len(rows)


if __name__ == "__main__":
    main()
