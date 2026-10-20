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
import random
import tempfile
from pathlib import Path

import pandas as pd

from src.config import Config
from src.experiment import load_gaia
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults
from src.llm_lora import (
    FORMAT,
    META_NAME,
    WEIGHTS_NAME,
    dedupe_note_rows,
    example_tensors,
    load_base_pin,
    note_diversity,
    split_note_examples,
)
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
    # Held-out is every alert on or after the split, not one day. One pass.
    train, held = split_note_examples(alerts, [], boundary=boundary)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoRA fine-tune of the shadow-note model.")
    parser.add_argument("--data", choices=("synthetic", "gaia"), default="synthetic")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--base", default=None, help="Local snapshot directory. Defaults to the pinned repo id.")
    parser.add_argument("--out", default="results/llm_lora")
    parser.add_argument(
        "--held-out-limit",
        type=int,
        default=500,
        help="Earliest held-out rows scored for the loss. 0 scores every held-out row.",
    )
    parser.add_argument(
        "--held-out-batch",
        type=int,
        default=2,
        help="Rows per held-out loss batch. The training update stays one row per step.",
    )
    return parser


def select_held_out_rows(rows: list, limit: int) -> list:
    """Earliest held-out rows. Zero keeps every row."""
    if limit <= 0:
        return list(rows)
    return list(rows[:limit])


def random_held_out_sample(rows: list, limit: int, seed: int) -> list:
    """A random cap of the held-out rows. Zero keeps every row.

    The same seed returns the same rows. They stay in time order.
    """
    if limit <= 0 or limit >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    picked = sorted(rng.sample(range(len(rows)), limit))
    return [rows[index] for index in picked]


def held_out_loss_sample(rows: list, limit: int) -> list:
    """One earliest row per note skeleton, then the cap.

    Repeated template targets are not scored again. ``limit`` 0 keeps every
    unique skeleton. ``held_out_rows`` stays the count before this cut.
    """
    return select_held_out_rows(dedupe_note_rows(rows), limit)


def held_out_batches(rows: list, batch_size: int) -> list[list]:
    size = max(1, batch_size)
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def pad_token_batch(rows: list[tuple[list[int], list[int]]], pad_id: int) -> dict[str, list[list[int]]]:
    """Pad a loss batch. Prompt labels stay ``-100``. Padding labels are ``-100`` too."""
    if not rows:
        return {"input_ids": [], "labels": [], "attention_mask": []}
    width = max(len(ids) for ids, _labels in rows)
    input_ids: list[list[int]] = []
    labels: list[list[int]] = []
    mask: list[list[int]] = []
    for ids, labs in rows:
        extra = width - len(ids)
        input_ids.append(list(ids) + [pad_id] * extra)
        labels.append(list(labs) + [-100] * extra)
        mask.append([1] * len(ids) + [0] * extra)
    return {"input_ids": input_ids, "labels": labels, "attention_mask": mask}


def main() -> None:
    args = build_parser().parse_args()
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
    from src.torch_determinism import apply_cuda_determinism

    # Seed before the adapter is initialised. LoRA init draws weights, and a
    # seed applied afterwards leaves that draw unseeded. CUDA can still write
    # a different hash; see docs/ML_MODELS.md.
    print(f"lora determinism {' '.join(apply_cuda_determinism(torch, device, args.seed))}", flush=True)
    train_rows, held_rows = _examples(args.data, args.seed)
    diversity = {
        "train": note_diversity([row["reference"] for row in train_rows]),
        "held_out": note_diversity([row["reference"] for row in held_rows]),
    }
    print(json.dumps({"target_diversity": diversity}), flush=True)
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
    apply_cuda_determinism(torch, device, args.seed)
    optim = torch.optim.AdamW((param for param in model.parameters() if param.requires_grad), lr=1e-4)
    train_loss = None
    held_out_loss = None
    held_out_loss_rows = 0
    held_out_loss_tokens = 0
    held_out_loss_random = None
    held_out_loss_random_rows = 0
    held_out_loss_random_tokens = 0
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
        losses = _held_out_loss(
            model,
            tokenizer,
            held_rows,
            batch_size=args.held_out_batch,
            limit=args.held_out_limit,
            seed=args.seed,
        )
        held_out_loss = losses["skeleton_loss"]
        held_out_loss_rows = losses["skeleton_rows"]
        held_out_loss_tokens = losses["skeleton_tokens"]
        held_out_loss_random = losses["random_loss"]
        held_out_loss_random_rows = losses["random_rows"]
        held_out_loss_random_tokens = losses["random_tokens"]
        print(
            f"epoch={epoch + 1} train_loss={train_loss} "
            f"held_out_loss={held_out_loss} held_out_loss_rows={held_out_loss_rows} "
            f"held_out_loss_random={held_out_loss_random} "
            f"held_out_loss_random_rows={held_out_loss_random_rows} "
            f"held_out_rows={len(held_rows)}"
        )
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
        "held_out_loss_rows": held_out_loss_rows,
        "held_out_loss_tokens": held_out_loss_tokens,
        "held_out_loss_random": held_out_loss_random,
        "held_out_loss_random_rows": held_out_loss_random_rows,
        "held_out_loss_random_tokens": held_out_loss_random_tokens,
        "held_out_limit": args.held_out_limit,
        "held_out_batch": args.held_out_batch,
        "held_out_loss_reduction": (
            "mean over supervised tokens; skeleton-deduped and a separate random cap, "
            "both honour --held-out-limit"
        ),
        "target_diversity": diversity,
        "train_loss": train_loss,
        "held_out_loss": held_out_loss,
        "device": device,
    }
    save_bytes(out / META_NAME, json.dumps(payload, sort_keys=True).encode("utf-8"))
    print(
        f"rows={len(train_rows)} held_out_rows={len(held_rows)} "
        f"held_out_loss_rows={held_out_loss_rows} held_out_loss_tokens={held_out_loss_tokens} "
        f"held_out_loss_random={held_out_loss_random} held_out_loss_random_rows={held_out_loss_random_rows} "
        f"train_loss={train_loss} held_out_loss={held_out_loss} "
        f"qlora={qlora} device={device} sha256={digest} pages=false"
    )


def _held_out_loss(
    model,
    tokenizer,
    rows: list[dict[str, str]],
    *,
    batch_size: int = 2,
    limit: int = 500,
    seed: int = 42,
) -> dict[str, float | int | None]:
    """Per-token loss on two capped held-out samples. Neither updates weights.

    The skeleton sample is the earliest row of each note skeleton. The random
    sample is ``limit`` held-out rows (``0`` keeps every row). Both losses are
    the sum of supervised-token cross-entropy divided by the token count.
    """
    skeleton = held_out_loss_sample(rows, limit)
    random_rows = random_held_out_sample(rows, limit, seed)
    skeleton_loss, skeleton_rows, skeleton_tokens = _token_loss(
        model, tokenizer, skeleton, batch_size=batch_size, label="skeleton"
    )
    random_loss, random_scored, random_tokens = _token_loss(
        model, tokenizer, random_rows, batch_size=batch_size, label="random"
    )
    return {
        "skeleton_loss": skeleton_loss,
        "skeleton_rows": skeleton_rows,
        "skeleton_tokens": skeleton_tokens,
        "random_loss": random_loss,
        "random_rows": random_scored,
        "random_tokens": random_tokens,
    }


def _token_loss(
    model,
    tokenizer,
    rows: list[dict[str, str]],
    *,
    batch_size: int,
    label: str,
) -> tuple[float | None, int, int]:
    """Mean loss per supervised token. Logits are dropped after each batch."""
    import torch
    import torch.nn.functional as functional

    if not rows:
        return None, 0, 0
    was_training = model.training
    model.eval()
    total = 0.0
    tokens = 0
    seen = 0
    n_rows = len(rows)
    pad_id = int(tokenizer.pad_token_id or 0)
    print(
        f"held-out {label} loss scoring {n_rows} rows batch={max(1, batch_size)}",
        flush=True,
    )
    try:
        with torch.no_grad():
            for batch in held_out_batches(rows, batch_size):
                encoded: list[tuple[list[int], list[int]]] = []
                for row in batch:
                    input_ids, labels = example_tensors(tokenizer, row["prompt"], row["reference"])
                    encoded.append((input_ids.view(-1).tolist(), labels.view(-1).tolist()))
                padded = pad_token_batch(encoded, pad_id)
                labels_t = torch.tensor(padded["labels"], dtype=torch.long, device=model.device)
                logits = model(
                    input_ids=torch.tensor(padded["input_ids"], dtype=torch.long, device=model.device),
                    attention_mask=torch.tensor(padded["attention_mask"], dtype=torch.long, device=model.device),
                ).logits
                shift_logits = logits[:, :-1, :]
                shift_labels = labels_t[:, 1:]
                step_tokens = int((shift_labels != -100).sum().item())
                if step_tokens:
                    total += float(
                        functional.cross_entropy(
                            shift_logits.reshape(-1, shift_logits.size(-1)),
                            shift_labels.reshape(-1),
                            reduction="sum",
                            ignore_index=-100,
                        ).detach()
                    )
                    tokens += step_tokens
                del logits, shift_logits, shift_labels, labels_t
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                seen += len(batch)
                running = total / tokens if tokens else 0.0
                print(
                    f"held-out {label} loss {seen}/{n_rows} tokens={tokens} running={running:.4f}",
                    flush=True,
                )
    finally:
        model.train(was_training)
    return (total / tokens if tokens else None), seen, tokens


if __name__ == "__main__":
    main()
