"""Fine-tune an open-source LLM on one UniVerse subject, on Modal cloud GPUs.

This is the only part of UniVerse that leaves your machine, and only when you
run it. It:

1. builds a training set locally from a subject's transcripts, notes and the
   quizzes you imported from Claude (``~/.universe_data/Subjects/<Subject>``),
2. uploads that JSONL to a Modal Volume,
3. LoRA-fine-tunes an instruction model on a cloud GPU,
4. leaves the adapter in the Volume for you to download and run locally
   (e.g. merge + convert to GGUF/MLX).

Usage::

    pip install modal && modal setup
    modal run modal_training/train_model.py --subject "Corporate Finance"
    modal run modal_training/train_model.py --subject "Corporate Finance" --base-model Qwen/Qwen2.5-3B-Instruct --epochs 3
    modal volume get universe-training "Corporate Finance/adapter" ./adapter   # download the result

Preview the dataset without touching the cloud::

    python modal_training/dataset.py --subject "Corporate Finance"
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import modal

APP_NAME = "universe-trainer"
VOLUME_NAME = "universe-training"
VOLUME_ROOT = "/vol"
DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "torch==2.5.1",
    "transformers==4.46.3",
    "datasets==3.1.0",
    "accelerate==1.1.1",
    "peft==0.13.2",
    "trl==0.12.2",
    "sentencepiece",
)


# --- training (runs on Modal) -----------------------------------------------


@app.function(image=image, gpu="A10G", timeout=3 * 60 * 60, volumes={VOLUME_ROOT: volume})
def train(subject: str, base_model: str, epochs: int, learning_rate: float, lora_rank: int) -> str:
    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    workdir = Path(VOLUME_ROOT) / subject
    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dataset = load_dataset("json", data_files=str(workdir / "dataset.jsonl"), split="train")
    dataset = dataset.map(lambda ex: {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False)})

    model = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype=torch.bfloat16, device_map="auto")
    peft_config = LoraConfig(
        r=lora_rank,
        lora_alpha=lora_rank * 2,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        task_type="CAUSAL_LM",
    )
    args = SFTConfig(
        output_dir=str(workdir / "checkpoints"),
        num_train_epochs=epochs,
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        learning_rate=learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=10,
        save_strategy="epoch",
        bf16=True,
        max_seq_length=2048,
        dataset_text_field="text",
        report_to="none",
    )
    trainer = SFTTrainer(
        model=model,
        args=args,
        train_dataset=dataset,
        peft_config=peft_config,
        processing_class=tokenizer,
    )
    trainer.train()

    adapter_dir = workdir / "adapter"
    trainer.model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    (adapter_dir / "universe_meta.json").write_text(
        json.dumps({"subject": subject, "base_model": base_model, "examples": len(dataset), "epochs": epochs}, indent=2)
    )
    volume.commit()
    return str(adapter_dir)


@app.local_entrypoint()
def main(
    subject: str,
    base_model: str = DEFAULT_BASE_MODEL,
    epochs: int = 2,
    learning_rate: float = 2e-4,
    lora_rank: int = 16,
):
    # Imported here, not at module level: the GPU container never needs it.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dataset import build_dataset, write_jsonl

    examples = build_dataset(subject)
    if len(examples) < 20:
        raise SystemExit(f"Only {len(examples)} examples for '{subject}' — transcribe more lectures or import quizzes first.")
    print(f"Built {len(examples)} examples for '{subject}'. Uploading…")

    with tempfile.TemporaryDirectory() as tmp:
        local = write_jsonl(examples, Path(tmp) / "dataset.jsonl")
        with volume.batch_upload(force=True) as batch:
            batch.put_file(str(local), f"/{subject}/dataset.jsonl")

    adapter = train.remote(subject, base_model, epochs, learning_rate, lora_rank)
    print(f"Done. Adapter saved in volume '{VOLUME_NAME}' at {adapter.removeprefix(VOLUME_ROOT)}")
    print(f'Download with: modal volume get {VOLUME_NAME} "{subject}/adapter" ./adapter')

