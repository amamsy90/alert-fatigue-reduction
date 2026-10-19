"""Optional LoRA adapter for shadow notes. Off unless ``LLM_LORA`` is set.

Training pairs are the decision-time prompt ``explain`` already builds, and a
template note filled from those same fields. Fault labels and later rows are
not inputs. The adapter is safetensors plus JSON with a SHA-256 sidecar.

Loading it is optional. ``explain`` uses the adapter when the pin matches,
then the pinned GGUF, then no note. Notes stay beside ``would_have_paged``.
They do not page.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

from .weight_io import load_bytes

log = logging.getLogger(__name__)

_ON = frozenset({"1", "true", "yes", "on"})
FORMAT = "oncall-llm-lora-v1"
WEIGHTS_NAME = "adapter.safetensors"
META_NAME = "adapter.json"
PIN_PATH = Path(__file__).resolve().parents[1] / "product" / "llm_lora_pin.json"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
_outcome = "off"


def lora_enabled() -> bool:
    return os.environ.get("LLM_LORA", "").strip().lower() in _ON


def lora_directory() -> Path | None:
    raw = os.environ.get("LLM_LORA_DIR", "").strip()
    return Path(raw) if raw else None


def lora_outcome() -> str:
    return _outcome


def remember_adapter_status() -> str:
    """Inspect the adapter and remember the result for the shadow record."""
    status = inspect_adapter()
    _set_outcome(status)
    return status


def _set_outcome(status: str) -> None:
    global _outcome
    _outcome = status


def load_base_pin() -> dict[str, object]:
    """Hugging Face name, revision, and the safetensors SHA-256. Weights are not committed."""
    raw = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("lora pin is not an object")
    for field in ("name", "repo", "revision", "filename", "sha256", "size_bytes"):
        if field not in raw:
            raise ValueError(f"lora pin is missing {field}")
    digest = str(raw["sha256"]).strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("lora pin sha256 is not 64 hex characters")
    raw["sha256"] = digest
    return raw


def reference_note(context: Mapping[str, object]) -> str:
    """A note filled only from the decision-time context. The log text is not copied."""
    from product.llm import _count, _num

    kind = context.get("kind", "")
    service = context.get("service", "")
    severity = context.get("severity", "")
    parts: list[str] = []
    signals = context.get("top_signals")
    if isinstance(signals, list):
        for signal in signals:
            if isinstance(signal, Mapping):
                parts.append(f"{signal.get('name', '')} {_num(signal.get('value', 0))}")
    signal_text = ", ".join(parts) if parts else "none"
    counts = context.get("template_counts")
    if not isinstance(counts, Mapping):
        counts = {}
    return (
        f"{kind} on {service} ({severity}). "
        f"Top signals: {signal_text}. "
        f"Earlier copies: 5m {_count(counts.get('count_5m', 0))}, "
        f"15m {_count(counts.get('count_15m', 0))}, "
        f"60m {_count(counts.get('count_60m', 0))}."
    )


def note_examples(alerts: list, spikes: list, *, until=None, model: object | None = None) -> list[dict[str, str]]:
    """One prompt/reference pair per item at or before ``until``.

    Each prompt is ``render_prompt(decision_context(...))`` on the prefix of
    rows known at that item. A later row is not in the prefix.
    """
    from product.llm import decision_context, render_prompt
    from product.pagerduty import spike_key

    rows: list[dict[str, str]] = []
    ordered_alerts = sorted(alerts, key=lambda alert: (alert.timestamp, alert.alert_id))
    for index, alert in enumerate(ordered_alerts):
        if until is not None and alert.timestamp >= until:
            continue
        prefix = ordered_alerts[: index + 1]
        past_spikes = [spike for spike in spikes if spike.shown_at <= alert.timestamp]
        item = {
            "kind": "alert",
            "dedup_key": alert.alert_id[:255],
            "service": alert.source,
            "severity": alert.severity,
        }
        context = decision_context(item, prefix, past_spikes, model)
        rows.append(_pair(context, render_prompt))
    ordered_spikes = sorted(spikes, key=lambda spike: (spike.shown_at, spike_key(spike)))
    for index, spike in enumerate(ordered_spikes):
        if until is not None and spike.shown_at >= until:
            continue
        prefix = ordered_spikes[: index + 1]
        item = {"kind": spike.kind, "dedup_key": spike_key(spike), "service": spike.service}
        context = decision_context(item, [], prefix, model)
        rows.append(_pair(context, render_prompt))
    return rows


def _pair(context: dict, render_prompt) -> dict[str, str]:
    prompt = render_prompt(context)
    if "fault_id" in prompt or "\nlabel:" in prompt:
        raise ValueError("decision-time prompt contains a label field")
    return {"prompt": prompt, "reference": reference_note(context)}


def _numbers(text: str) -> set[str]:
    return {match.group(0).lower() for match in _NUMBER.finditer(text)}


def _instruction_tokens(log_text: str) -> set[str]:
    from product.llm import _INSTRUCTION

    tokens: set[str] = set()
    for line in str(log_text).splitlines():
        if _INSTRUCTION.search(line):
            tokens.update(match.group(0).lower() for match in _WORD.finditer(line))
    return tokens


def _trusted_tokens(context: Mapping[str, object], prompt: str) -> set[str]:
    """Words that are allowed because they come from the decision-time fields."""
    kept = prompt
    start = kept.find("UNTRUSTED LOG TEXT START")
    end = kept.find("UNTRUSTED LOG TEXT END")
    if start != -1 and end != -1:
        kept = kept[:start] + kept[end:]
    tokens = {match.group(0).lower() for match in _WORD.finditer(kept)}
    for key in ("kind", "service", "severity"):
        tokens.add(str(context.get(key, "")).lower())
    signals = context.get("top_signals")
    if isinstance(signals, list):
        for signal in signals:
            if isinstance(signal, Mapping):
                tokens.add(str(signal.get("name", "")).lower())
    return tokens


def score_note(
    note: str,
    context: Mapping[str, object],
    *,
    injected_note: str | None = None,
    length_cap: int = 500,
) -> dict[str, object]:
    """Deterministic rubric. No model call.

    The note must name every top signal, contain only numbers that are already
    in the prompt, stay within the length cap, and must not repeat an
    instruction-only token from the untrusted log. When a second note is
    given, the signal names and the kind/service line must match.
    """
    from product.llm import render_prompt

    prompt = render_prompt(context)
    signals = []
    raw_signals = context.get("top_signals")
    if isinstance(raw_signals, list):
        for signal in raw_signals:
            if isinstance(signal, Mapping) and signal.get("name"):
                signals.append(str(signal["name"]))
    mentions = all(name in note for name in signals) if signals else True
    invented = sorted(_numbers(note) - _numbers(prompt))
    numbers_ok = not invented
    length_ok = len(note) <= length_cap
    banned = _instruction_tokens(str(context.get("log_text") or "")) - _trusted_tokens(context, prompt)
    followed = sorted(token for token in banned if token in note.lower())
    injection_ok = not followed
    stable = True
    if injected_note is not None:
        stable = _same_structure(note, injected_note, signals, context) and not any(
            token in injected_note.lower() for token in banned
        )
    passed = bool(mentions and numbers_ok and length_ok and injection_ok and stable)
    return {
        "passed": passed,
        "mentions_top_signals": mentions,
        "no_invented_numbers": numbers_ok,
        "invented_numbers": invented,
        "length_ok": length_ok,
        "length": len(note),
        "no_instruction_following": injection_ok,
        "instruction_tokens": followed,
        "structure_stable": stable,
    }


def _same_structure(left: str, right: str, signals: list[str], context: Mapping[str, object]) -> bool:
    def _has(text: str) -> tuple[bool, ...]:
        return tuple(name in text for name in signals) + (
            str(context.get("kind", "")) in text,
            str(context.get("service", "")) in text,
        )

    return _has(left) == _has(right)


def inspect_adapter(directory: Path | None = None) -> str:
    """``ok`` or a refusal. Does not import torch."""
    if not lora_enabled() and directory is None:
        return "off"
    folder = directory if directory is not None else lora_directory()
    if folder is None or not folder.is_dir():
        return "missing"
    meta_bytes, meta_status = load_bytes(folder / META_NAME)
    if meta_bytes is None:
        return meta_status
    try:
        payload = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "bad-format"
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("pages") is not False:
        return "bad-format"
    pinned = os.environ.get("LLM_LORA_SHA256", "")
    _blob, status = load_bytes(folder / WEIGHTS_NAME, payload.get("weights_sha256"))
    if status != "ok":
        return status
    actual = str(payload.get("weights_sha256", ""))
    if pinned and pinned.strip().lower() != actual.lower():
        return "hash-mismatch"
    return "ok"


def generate_with_adapter(prompt: str) -> str | None:
    """The adapter's note, or None so the caller uses the base GGUF.

    A missing file, a bad hash, or a missing library returns None. It does
    not raise, and it does not page.
    """
    status = inspect_adapter()
    if status != "ok":
        _set_outcome(status)
        return None
    try:
        text = _peft_generate(prompt)
    except ImportError:
        _set_outcome("no-backend")
        return None
    except Exception:
        log.warning("lora adapter did not generate; falling back to the base model", exc_info=True)
        _set_outcome("fallback")
        return None
    if not str(text).strip():
        _set_outcome("fallback")
        return None
    _set_outcome("applied")
    return str(text)


def _peft_generate(prompt: str) -> str:
    """Local Hugging Face generate with the pinned base and the adapter. No network when the files are local."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    pin = load_base_pin()
    base = os.environ.get("LLM_LORA_BASE", "").strip() or str(pin["repo"])
    revision = str(pin["revision"])
    _verify_local_base(base, str(pin["sha256"]), str(pin["filename"]))
    folder = lora_directory()
    if folder is None:
        raise FileNotFoundError("lora directory is unset")
    staged = _stage_adapter(folder)
    tokenizer = AutoTokenizer.from_pretrained(base, revision=revision, local_files_only=_is_dir(base))
    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        base,
        revision=revision,
        dtype=dtype,
        local_files_only=_is_dir(base),
    )
    model = PeftModel.from_pretrained(model, staged)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)
    model.eval()
    tokens = tokenizer(prompt, return_tensors="pt")
    input_ids = tokens["input_ids"].to(device)
    attention = tokens.get("attention_mask")
    if attention is not None:
        attention = attention.to(device)
    from product.llm import generation_settings

    settings = generation_settings()
    with torch.no_grad():
        output = model.generate(
            input_ids=input_ids,
            attention_mask=attention,
            max_new_tokens=int(settings["max_tokens"]),
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    new_tokens = output[0, input_ids.shape[1] :]
    return str(tokenizer.decode(new_tokens, skip_special_tokens=True))


def _stage_adapter(folder: Path) -> str:
    """Copy the hash-checked weights to the filename peft loads. The other file is not read."""
    import tempfile

    config_path = folder / "adapter_config.json"
    if not config_path.is_file():
        raise FileNotFoundError("adapter_config.json is missing")
    stage = Path(tempfile.mkdtemp(prefix="oncall-lora-"))
    (stage / "adapter_model.safetensors").write_bytes((folder / WEIGHTS_NAME).read_bytes())
    (stage / "adapter_config.json").write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    return str(stage)


def _is_dir(path: str) -> bool:
    return Path(path).is_dir()


def _verify_local_base(base: str, expected: str, filename: str) -> None:
    path = Path(base) / filename
    if not path.is_file():
        return
    from product.llm import sha256_file

    digest = sha256_file(path)
    if digest != expected:
        raise ValueError("base model hash mismatch")
