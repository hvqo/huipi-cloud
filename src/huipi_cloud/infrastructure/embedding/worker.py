"""Private stdio subprocess, run with an isolated torch/transformers interpreter.

Only trusted local snapshot loading; bounded JSON input/output, no network downloads.
Stdout is the private IPC channel, not a public log. Errors never include source text.
"""

import json
import os
import sys


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    import torch.nn.functional as functional
    from transformers import AutoModel, AutoTokenizer

    path, device = sys.argv[1:3]
    torch.set_num_threads(4)
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            path, padding_side="left", local_files_only=True, trust_remote_code=False
        )
        model = (
            AutoModel.from_pretrained(
                path, local_files_only=True, trust_remote_code=False, torch_dtype=torch.float32
            )
            .to(device)
            .eval()
        )
    except torch.cuda.OutOfMemoryError:
        print(json.dumps({"error": "embedding_out_of_memory"}), flush=True)
        return 2
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"error": "embedding_model_unavailable"}), flush=True)
        return 2
    print(json.dumps({"ready": True, "dimension": model.config.hidden_size}), flush=True)
    for line in sys.stdin:
        try:
            if len(line.encode()) > 32768:
                raise ValueError("embedding_input_limit")
            payload = json.loads(line)
            texts, maximum = payload["texts"], payload["max_tokens"]
            if not 1 <= len(texts) <= 8 or any(len(t) > 1000 for t in texts):
                raise ValueError("embedding_input_limit")
            batch = tokenizer(texts, padding=True, truncation=False, return_tensors="pt")
            if batch["input_ids"].shape[1] > maximum:
                raise ValueError("embedding_token_limit")
            with torch.inference_mode():
                batch = batch.to(device)
                states = model(**batch).last_hidden_state[:, -1]
                vectors = functional.normalize(states.float(), p=2, dim=1).cpu().tolist()
            print(json.dumps({"vectors": vectors}, allow_nan=False), flush=True)
        except torch.cuda.OutOfMemoryError:
            print(json.dumps({"error": "embedding_out_of_memory"}), flush=True)
            return 2
        except ValueError as error:
            code = str(error)
            allowed = {"embedding_input_limit", "embedding_token_limit"}
            print(
                json.dumps({"error": code if code in allowed else "embedding_invalid_output"}),
                flush=True,
            )
        # Unexpected runtime failures exit; the parent maps EOF, never exposes a stack.
    return 0


if __name__ == "__main__":
    sys.exit(main())
