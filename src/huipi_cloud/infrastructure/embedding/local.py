"""One supervised local embedding process; timeout/cancellation kills its process group."""

import asyncio
import json
import os
import signal
from pathlib import Path

from huipi_cloud.infrastructure.embedding.protocol import (
    MODEL_ID,
    MODEL_REVISION,
    EmbeddingError,
    EmbeddingIdentity,
    normalize,
)


class LocalEmbeddingProvider:
    def __init__(
        self,
        python,
        model_path,
        *,
        device="cpu",
        timeout=30,
        startup_timeout=60,
        inference_gate=None,
    ):
        self.python, self.model_path = python, model_path
        if device not in {"cpu", "cuda"} or not 0 < timeout <= 120:
            raise ValueError("embedding_invalid_runtime_config")
        self.device, self.timeout, self.startup_timeout = device, timeout, startup_timeout
        self.identity = EmbeddingIdentity()
        self.process = None
        self.lock = asyncio.Lock()
        self.inference_gate = inference_gate or asyncio.Semaphore(1)

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                # OS may retain uninterruptible processes; do not wait forever.
                pass

    async def aclose(self):
        async with self.lock:
            await self._stop()

    async def _read(self, timeout):
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line or len(line) > 256 * 1024:
                raise EmbeddingError("embedding_process_failed")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise EmbeddingError("embedding_invalid_output")
            return payload
        except (ValueError, TypeError):
            raise EmbeddingError("embedding_invalid_output") from None

    async def _start(self):
        path = Path(self.model_path).expanduser()
        if (
            path.name != MODEL_REVISION
            or not (path / "model.safetensors").is_file()
            or not Path(self.python).is_file()
        ):
            raise EmbeddingError("embedding_model_unavailable")
        if self.identity.model_id != MODEL_ID:
            raise EmbeddingError("embedding_model_unavailable")
        self.process = await asyncio.create_subprocess_exec(
            self.python,
            str(Path(__file__).with_name("worker.py")),
            str(path),
            self.device,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=256 * 1024,
            env={
                **{
                    k: v
                    for k, v in os.environ.items()
                    if k in {"HOME", "PATH", "LANG", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES"}
                },
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
        )
        ready = await self._read(self.startup_timeout)
        if ready.get("error") in {"embedding_out_of_memory", "embedding_model_unavailable"}:
            raise EmbeddingError(ready["error"])
        if ready != {"ready": True, "dimension": self.identity.dimension}:
            raise EmbeddingError("embedding_dimension_mismatch")

    async def _embed(self, texts):
        if (
            not isinstance(texts, list)
            or not 1 <= len(texts) <= self.identity.batch_size
            or any(not isinstance(t, str) or not t.strip() or len(t) > 1000 for t in texts)
        ):
            raise EmbeddingError("embedding_input_limit")
        async with self.inference_gate, self.lock:
            try:
                if self.process is None:
                    await self._start()
                data = json.dumps({"texts": texts, "max_tokens": self.identity.max_tokens}).encode()
                self.process.stdin.write(data + b"\n")
                await asyncio.wait_for(self.process.stdin.drain(), self.timeout)
                payload = await self._read(self.timeout)
                if "error" in payload:
                    allowed = {
                        "embedding_out_of_memory",
                        "embedding_token_limit",
                        "embedding_input_limit",
                        "embedding_invalid_output",
                    }
                    raise EmbeddingError(
                        payload["error"]
                        if payload["error"] in allowed
                        else "embedding_process_failed"
                    )
                vectors = payload.get("vectors")
                if not isinstance(vectors, list) or len(vectors) != len(texts):
                    raise EmbeddingError("embedding_invalid_output")
                return [normalize(v, self.identity.dimension) for v in vectors]
            except asyncio.CancelledError:
                await self._stop()
                raise
            except TimeoutError:
                await self._stop()
                raise EmbeddingError("embedding_timeout") from None
            except (OSError, ConnectionError):
                await self._stop()
                raise EmbeddingError("embedding_model_unavailable") from None
            except EmbeddingError:
                await self._stop()
                raise

    async def embed_documents(self, texts):
        return await self._embed(texts)

    async def embed_query(self, text):
        prompt = f"Instruct: {self.identity.query_instruction}\nQuery:{text}"
        return (await self._embed([prompt]))[0]
