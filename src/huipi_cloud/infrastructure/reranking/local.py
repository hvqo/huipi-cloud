"""Bounded process lifecycle, serial GPU gate, strict candidate identity contract."""

import asyncio
import json
import os
import signal
import time
from pathlib import Path

from huipi_cloud.infrastructure.reranking.protocol import (
    MODEL_REVISION,
    RerankError,
    RerankIdentity,
    RerankReply,
    validate_candidates,
    validate_reply,
)


class LocalRerankProvider:
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
        if device not in {"cpu", "cuda"} or not 0 < timeout <= 120:
            raise ValueError("rerank_invalid_runtime_config")
        self.python, self.model_path, self.device = python, model_path, device
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.identity = RerankIdentity()
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
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                pass  # OS-uninterruptible I/O is not forcefully recoverable in Python.

    async def aclose(self):
        async with self.lock:
            await self._stop()

    async def _read(self, timeout):
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            data = json.loads(line)
        except (ValueError, TypeError):
            raise RerankError("rerank_process_failed") from None
        if not isinstance(data, dict):
            raise RerankError("rerank_invalid_output")
        code = data.get("error")
        if code:
            allowed = {
                "rerank_model_unavailable",
                "rerank_out_of_memory",
                "rerank_token_limit",
                "rerank_input_limit",
                "rerank_invalid_output",
            }
            raise RerankError(code if code in allowed else "rerank_process_failed")
        return data

    async def _start(self):
        path = Path(self.model_path).expanduser()
        if (
            path.name != MODEL_REVISION
            or not (path / "model.safetensors").is_file()
            or not Path(self.python).is_file()
        ):
            raise RerankError("rerank_model_unavailable")
        self.process = await asyncio.create_subprocess_exec(
            self.python,
            str(Path(__file__).with_name("worker.py")),
            str(path),
            self.device,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=65536,
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
        if ready != {"ready": True, "hidden_size": self.identity.hidden_size}:
            raise RerankError("rerank_model_shape_mismatch")

    async def warmup(self):
        async with self.inference_gate, self.lock:
            try:
                if self.process is None:
                    await self._start()
            except asyncio.CancelledError:
                await self._stop()
                raise
            except TimeoutError:
                await self._stop()
                raise RerankError("rerank_timeout") from None
            except OSError:
                await self._stop()
                raise RerankError("rerank_model_unavailable") from None
            except RerankError:
                await self._stop()
                raise

    async def rerank(self, query, candidates):
        checked = validate_candidates(query, candidates)
        identity, tick = self.identity, time.perf_counter()
        scores, total_tokens, allocated, peak = {}, 0, 0, 0
        async with self.inference_gate, self.lock:
            try:
                if checked and self.process is None:
                    await self._start()
                async with asyncio.timeout(self.timeout):
                    for offset in range(0, len(checked), identity.batch_size):
                        batch = checked[offset : offset + identity.batch_size]
                        message = dict(
                            query=query,
                            documents=[c.excerpt for c in batch],
                            max_tokens=identity.max_tokens,
                            instruction=identity.instruction,
                        )
                        self.process.stdin.write(json.dumps(message).encode() + b"\n")
                        await self.process.stdin.drain()
                        data = await self._read(self.timeout)
                        values = data.get("scores")
                        if not isinstance(values, list) or len(values) != len(batch):
                            raise RerankError("rerank_candidate_mismatch")
                        scores.update({c.chunk_id: score for c, score in zip(batch, values)})
                        for name in (
                            "input_tokens",
                            "cuda_allocated_bytes",
                            "cuda_peak_allocated_bytes",
                        ):
                            if type(data.get(name)) is not int or data[name] < 0:
                                raise RerankError("rerank_invalid_output")
                        total_tokens += data["input_tokens"]
                        allocated = max(allocated, data["cuda_allocated_bytes"])
                        peak = max(peak, data["cuda_peak_allocated_bytes"])
                if self.identity != identity:
                    raise RerankError("rerank_model_mismatch")
                reply = RerankReply(
                    scores,
                    identity.model_id,
                    identity.model_revision,
                    identity.digest,
                    len(checked),
                    round((time.perf_counter() - tick) * 1000),
                    total_tokens,
                    allocated,
                    peak,
                )
                return validate_reply(reply, checked, identity)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except TimeoutError:
                await self._stop()
                raise RerankError("rerank_timeout") from None
            except (OSError, ConnectionError):
                await self._stop()
                raise RerankError("rerank_model_unavailable") from None
            except RerankError:
                await self._stop()
                raise
