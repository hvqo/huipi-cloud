"""Private offline process: yes/no logits, never generate(), no raw-content logs."""

import json
import os
import sys

PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on "
    'the Query and the Instruct provided. Note that the answer can only be "yes" '
    'or "no".<|im_end|>\n<|im_start|>user\n'
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def main():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(4)
    path, device = sys.argv[1:3]
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            path, padding_side="left", local_files_only=True, trust_remote_code=False
        )
        model = (
            AutoModelForCausalLM.from_pretrained(
                path, local_files_only=True, trust_remote_code=False, torch_dtype=torch.float32
            )
            .to(device)
            .eval()
        )
        prefix = tokenizer.encode(PREFIX, add_special_tokens=False)
        suffix = tokenizer.encode(SUFFIX, add_special_tokens=False)
        no, yes = [tokenizer.convert_tokens_to_ids(t) for t in ("no", "yes")]
        if no == yes or min(no, yes) < 0:
            raise ValueError
    except torch.cuda.OutOfMemoryError:
        print(json.dumps({"error": "rerank_out_of_memory"}), flush=True)
        return 2
    except (ValueError, OSError, RuntimeError):
        print(json.dumps({"error": "rerank_model_unavailable"}), flush=True)
        return 2
    print(json.dumps({"ready": True, "hidden_size": model.config.hidden_size}), flush=True)
    for line in sys.stdin:
        try:
            if len(line.encode()) > 32768:
                raise ValueError("rerank_input_limit")
            data = json.loads(line)
            if not 1 <= len(data["documents"]) <= 4:
                raise ValueError("rerank_input_limit")
            pairs = [
                f"<Instruct>: {data['instruction']}\n<Query>: {data['query']}\n<Document>: {d}"
                for d in data["documents"]
            ]
            tokens = [
                prefix + tokenizer.encode(p, add_special_tokens=False) + suffix for p in pairs
            ]
            if any(len(t) > data["max_tokens"] for t in tokens):
                raise ValueError("rerank_token_limit")
            batch = tokenizer.pad({"input_ids": tokens}, padding=True, return_tensors="pt").to(
                device
            )
            with torch.inference_mode():
                logits = model(**batch, use_cache=False, logits_to_keep=1).logits[:, -1, :]
                scores = torch.softmax(logits[:, [no, yes]].float(), dim=1)[:, 1].cpu().tolist()
            cuda = device == "cuda"
            print(
                json.dumps(
                    {
                        "scores": scores,
                        "input_tokens": sum(map(len, tokens)),
                        "cuda_allocated_bytes": torch.cuda.memory_allocated() if cuda else 0,
                        "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated()
                        if cuda
                        else 0,
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
        except torch.cuda.OutOfMemoryError:
            print(json.dumps({"error": "rerank_out_of_memory"}), flush=True)
            return 2
        except ValueError as error:
            code = str(error)
            print(
                json.dumps(
                    {
                        "error": code
                        if code in {"rerank_token_limit", "rerank_input_limit"}
                        else "rerank_invalid_output"
                    }
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
