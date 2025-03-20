# Adapted from https://github.com/vllm-project/vllm/blob/6366efc67b0aedd2c1721c14385370e50b297fb3/benchmarks/backend_request_func.py
# Adapted from https://github.com/vllm-project/vllm/blob/6366efc67b0aedd2c1721c14385370e50b297fb3/benchmarks/benchmark_serving.py

"""
Benchmark online serving with dynamic requests.

Usage:
python3 bench_serving_v2.py --backend sglang --num-prompt 3000 -c configs/2_tiers_config.yaml --request-rate 10 \
    --model meta-llama/Meta-Llama-3.2-1B --port 8081 --window 60

python3 -m sglang.bench_serving --backend sglang --dataset-name random --num-prompts 3000 --random-input 1024 --random-output 1024 --random-range-ratio 0.5
python3 -m sglang.bench_serving --backend sglang --dataset-name random --request-rate-range 1,2,4,8,16,32 --random-input 4096 --random-output 1024 --random-range-ratio 0.125 --multi
"""
import argparse
import asyncio
import json
import os
import random
import resource
import subprocess
import sys
import time
import traceback
import uuid
import warnings
from argparse import ArgumentParser
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

import aiohttp
import numpy as np
import pandas as pd
import requests
import yaml
from dataset_loader import DatasetsTypes
from rich.console import Console
from rich.table import Table
from tqdm.asyncio import tqdm
from transformers import (
    AutoTokenizer,
    PreTrainedTokenizer,
    PreTrainedTokenizerBase,
    PreTrainedTokenizerFast,
)

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=6 * 60 * 60)

global args


@dataclass
class RequestFuncInput:
    prompt: str
    api_url: str
    prompt_len: int
    output_len: int
    model: str
    extra_request_body: Dict[str, Any]
    max_priority: int = 1
    rid: str = ""


@dataclass
class RequestFuncOutput:
    rid: str = ""
    generated_text: str = ""
    success: bool = False
    cancelled: bool = False
    latency: float = 0.0
    ttft: float = 0.0  # Time to first token
    itl: List[float] = field(default_factory=list)  # List of inter-token latencies
    prompt_len: int = 0
    error: str = ""
    output_len: int = 0
    retok_output_len: int = 0

    queueing_before_local_node: float = 0.0
    waiting_queue_time: float = 0.0
    total_prefill_time: float = 0.0
    total_decode_time: float = 0.0
    local_scheduling_time: float = 0.0
    detok_time: float = 0.0
    token_time: float = 0.0
    overall_node_time: float = 0.0

    node_measured_ttft: float = (
        None  # Measured by local scheduler. Possibly to get server latency
    )
    node_measured_tpots: List[float] = field(default_factory=list)
    sent_time: float = 0.0
    input_rid: str = ""
    avg_recieving_time: float = 0.0

@dataclass
class Workload:
    requests: List[Tuple[str, int, int]]
    timestamps: List[float] | np.ndarray
    ttft_slo_ms: Optional[float] = None
    tpot_slo_ms: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)
    priority: Optional[int] = None
    request_rate: float = 1.0


class DCGMContext:

    def __init__(self, gpu_ids: int = [0], output_file: str = "dgcm.csv") -> None:
        self.gpu_ids = gpu_ids
        self.process = None
        self.output_file = output_file
        self.fields = {
            "PROF_GR_ENGINE_ACTIVE": 1001,
            "PROF_SM_ACTIVE": 1002,
            "PROF_SM_OCCUPANCY": 1003,
            "PROF_PIPE_TENSOR_ACTIVE": 1004,
            "PROF_DRAM_ACTIVE": 1005,
        }

    def __enter__(self):
        # capture the output of the command
        fields = ",".join([str(v) for v in self.fields.values()])
        self.process = subprocess.Popen(
            [
                "dcgmi",
                "dmon",
                "-e",
                fields,
                "-i",
                ",".join(map(str, self.gpu_ids)),
                "-d",
                "100",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.process.kill()
        # capture the output of the process
        stdout, stderr = self.process.communicate()
        lines = stdout.decode().split("\n")
        data = {k: [] for k in self.fields.keys()}
        data.update({"gpu_id": []})
        for line in lines[3:]:  # skip the first line of data because they are all 0
            if line.startswith("GPU"):
                values = line.split()
                data["gpu_id"].append(values[1])
                for i, k in enumerate(self.fields.keys()):
                    data[k].append(values[i + 2])  # first two strings are 'GPU' and '0'
        df = pd.DataFrame(data)
        df.to_csv(self.output_file, index=False)


class NvidiaSmiContext:

    def __init__(self, gpu_ids: int = [0], output_file: str = "nvidia-smi.csv") -> None:
        self.gpu_ids = gpu_ids
        self.process = None
        self.output_file = output_file

    def __enter__(self):
        # with timestamp
        self.process = subprocess.Popen(
            [
                "nvidia-smi",
                "--query-gpu=timestamp,index,utilization.gpu,utilization.memory,memory.used",
                "--format=csv,nounits",
                "-l",
                "1",
                "--id",
                ",".join(map(str, self.gpu_ids)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.process.kill()
        # capture the output of the process
        stdout, stderr = self.process.communicate()
        with open(self.output_file, "wb") as f:
            f.write(stdout)


class NullContext:

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def remove_prefix(text: str, prefix: str) -> str:
    return text[len(prefix) :] if text.startswith(prefix) else text


# trt llm not support ignore_eos
# https://github.com/triton-inference-server/tensorrtllm_backend/issues/505
async def async_request_trt_llm(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    api_url = request_func_input.api_url
    assert api_url.endswith("generate_stream")

    async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
        payload = {
            "accumulate_tokens": True,
            "text_input": request_func_input.prompt,
            "temperature": 0.000001,
            "top_p": 1.0,
            "max_tokens": request_func_input.output_len,
            "stream": True,
            "min_length": request_func_input.output_len,
            "end_id": 1048576,
            **request_func_input.extra_request_body,
        }
        if args.disable_ignore_eos:
            del payload["min_length"]
            del payload["end_id"]
        output = RequestFuncOutput()
        output.prompt_len = request_func_input.prompt_len

        ttft = 0.0
        st = time.perf_counter()
        most_recent_timestamp = st
        try:
            async with session.post(url=api_url, json=payload) as response:
                if response.status == 200:
                    async for chunk_bytes in response.content:
                        chunk_bytes = chunk_bytes.strip()
                        if not chunk_bytes:
                            continue

                        chunk = remove_prefix(chunk_bytes.decode("utf-8"), "data:")

                        data = json.loads(chunk)
                        output.generated_text += data["text_output"]
                        timestamp = time.perf_counter()
                        # First token
                        if ttft == 0.0:
                            ttft = time.perf_counter() - st
                            output.ttft = ttft

                        # Decoding phase
                        else:
                            output.itl.append(timestamp - most_recent_timestamp)

                        most_recent_timestamp = timestamp

                    output.latency = most_recent_timestamp - st
                    output.success = True
                    output.output_len = request_func_input.output_len

                else:
                    output.error = response.reason or ""
                    output.success = False
        except Exception:
            output.success = False
            exc_info = sys.exc_info()
            output.error = "".join(traceback.format_exception(*exc_info))

        if pbar:
            pbar.update(1)
        return output


# set ignore_eos True by default
async def async_request_dyserve_completions(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    try:
        # print('sending request for batch =', request_func_input.extra_request_body.get('batch', False))
        api_url = request_func_input.api_url
        assert api_url.endswith(
            "completions"
        ), "OpenAI Completions API URL must end with 'completions'."

        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            payload = {
                "model": request_func_input.model,
                "prompt": request_func_input.prompt,
                "temperature": 0.0,
                "best_of": 1,
                "max_tokens": request_func_input.output_len,
                "stream": not args.disable_stream,
                "ignore_eos": not args.disable_ignore_eos,
                **request_func_input.extra_request_body,
            }
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"}

            output = RequestFuncOutput()
            output.prompt_len = request_func_input.prompt_len

            if request_func_input.rid:
                output.input_rid = request_func_input.rid
        
            generated_text = ""
            ttft = 0.0
            st = time.perf_counter()
            most_recent_timestamp = st
            try:
                output.sent_time = st
                async with session.post(
                    url=api_url, json=payload, headers=headers
                ) as response:
                    if response.status == 200:
                        async for chunk_bytes in response.content:
                            chunk_bytes = chunk_bytes.strip()
                            if not chunk_bytes:
                                continue

                            chunk = remove_prefix(chunk_bytes.decode("utf-8"), "data: ")
                            latency = time.perf_counter() - st
                            if chunk == "[DONE]":
                                pass
                            else:
                                data = json.loads(chunk)

                                # NOTE: Some completion API might have a last
                                # usage summary response without a token so we
                                # want to check a token was generated
                                if data["seq_outputs"][0]:
                                    timestamp = time.perf_counter()
                                    # First token
                                    if ttft == 0.0:
                                        ttft = time.perf_counter() - st
                                        output.ttft = ttft
                                        output.node_measured_ttft = data['timing_metadata']['ttft_ms']
                                        output.queueing_before_local_node = data['time_spent_queue']
                                        output.total_decode_time = data['decoding_time']
                                        output.rid = data['request_id']
                                    # Decoding phase
                                    else:
                                        output.itl.append(
                                            timestamp - most_recent_timestamp
                                        )

                                    most_recent_timestamp = timestamp
                                    generated_text += data["seq_outputs"][0]
                                
                                if data["is_final"]:
                                    output.token_time = data["tokenization_time"]
                                    output.detok_time = data["decoding_time"]
                                    output.queueing_before_local_node = data["time_spent_queue"]
                                    output.node_measured_ttft = data["timing_metadata"]["ttft_ms"]
                                    output.node_measured_tpots = data["timing_metadata"]["tpot_ms"]
                        output.generated_text = generated_text
                        output.success = True
                        output.latency = latency
                        output.output_len = request_func_input.output_len
                    else:
                        output.error = response.reason or ""
                        output.success = False
                        print(output.error)
            except asyncio.CancelledError:
                output.cancelled = True
                # output.success = True if ttft > 0 else False
                output.generated_text = generated_text
                output.latency = time.perf_counter() - st
                output.output_len = request_func_input.output_len
            except Exception:
                output.success = False
                exc_info = sys.exc_info()
                output.error = "".join(traceback.format_exception(*exc_info))
                print(output.error)
        try:
            if pbar and not request_func_input.extra_request_body.get("batch", False):
                pbar.update(1)
        except asyncio.CancelledError:
            pass
        return output
    except asyncio.CancelledError:
        output = RequestFuncOutput()
        output.cancelled = True
        output.success = False
        exc_info = sys.exc_info()
        output.error = "".join(traceback.format_exception(*exc_info))
        return output


async def async_request_sglang_completions(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    try:
        start_time = time.perf_counter()
        # print('sending request for batch =', request_func_input.extra_request_body.get('batch', False))
        api_url = request_func_input.api_url
        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            payload = {
                "model": request_func_input.model,
                "text": request_func_input.prompt,
                "sampling_params": {
                    "temperature": 0.0,
                    "max_new_tokens": request_func_input.output_len,
                    "ignore_eos": not args.disable_ignore_eos,
                },
                "stream": not args.disable_stream,
            }
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"}

            output = RequestFuncOutput()
            output.prompt_len = request_func_input.prompt_len
            if request_func_input.rid:
                output.input_rid = request_func_input.rid

            generated_text = ""
            ttft = 0.0
            st = time.perf_counter()
            most_recent_timestamp = st
            try:
                output.sent_time = st
                async with session.post(
                    url=api_url, json=payload, headers=headers
                ) as response:
                    if response.status == 200:
                        async for chunk_bytes in response.content:
                            chunk_bytes = chunk_bytes.strip()
                            if not chunk_bytes:
                                continue

                            chunk = remove_prefix(chunk_bytes.decode("utf-8"), "data: ")
                            latency = time.perf_counter() - st
                            if chunk == "[DONE]":
                                pass
                            else:
                                data = json.loads(chunk)

                                # NOTE: Some completion API might have a last
                                # usage summary response without a token so we
                                # want to check a token was generated
                                if data["text"]:
                                    timestamp = time.perf_counter()
                                    # First token
                                    if ttft == 0.0:
                                        ttft = time.perf_counter() - st
                                        output.ttft = ttft
                                        output.node_measured_ttft = data['meta_info']['node_measured_ttft']
                                    # Decoding phase
                                    else:
                                        output.itl.append(
                                            timestamp - most_recent_timestamp
                                        )

                                    most_recent_timestamp = timestamp
                                    generated_text += data["text"]

                                if data.get("meta_info"):
                                    pass
                                    # Collect relevant metrics
                                    # output.queueing_before_local_node = data[
                                    #     "meta_info"
                                    # ]["queueing_before_local_node"]
                                    # output.waiting_queue_time = data["meta_info"][
                                    #     "waiting_queue_time"
                                    # ]
                                    # output.total_prefill_time = data["meta_info"][
                                    #     "total_prefill_time"
                                    # ]
                                    # output.total_decode_time = data["meta_info"][
                                    #     "total_decode_time"
                                    # ]
                                    # output.local_scheduling_time = (
                                    #     data["meta_info"]["local_scheduling_time"]
                                    #     - output.total_prefill_time
                                    #     - output.total_decode_time
                                    #     - output.waiting_queue_time
                                    # )
                                    # output.detok_time = data["meta_info"]["detok_time"]
                                    # output.token_time = data["meta_info"]["token_time"]

                                    # output.overall_node_time = data["meta_info"][
                                    #     "overall_node_time"
                                    # ]
                                    # output.node_measured_ttft = data["meta_info"][
                                    #     "node_measured_ttft"
                                    # ]
                                    # output.node_measured_tpots = data["meta_info"][
                                    #     "node_measured_tpot"
                                    # ]

                        output.generated_text = generated_text
                        output.success = True
                        output.latency = latency
                        output.output_len = request_func_input.output_len
                    else:
                        output.error = response.reason or ""
                        output.success = False
                        print(output.error)
            except asyncio.CancelledError:
                output.cancelled = True
                output.success = False

                # output.success = True if ttft > 0 else False
                # if not output.node_measured_ttft:
                #     output.node_measured_ttft = ttft
                # if not output.node_measured_tpots:
                #     output.node_measured_tpots = output.itl

                output.generated_text = generated_text
                output.latency = time.perf_counter() - st
                output.output_len = request_func_input.output_len
            except Exception:
                output.success = False
                exc_info = sys.exc_info()
                output.error = "".join(traceback.format_exception(*exc_info))
                print(output.error)
        try:
            if pbar and not request_func_input.extra_request_body.get("batch", False):
                pbar.update(1)
        except asyncio.CancelledError:
            pass
        return output
    except asyncio.CancelledError:
        output = RequestFuncOutput()
        output.cancelled = True
        output.success = False
        exc_info = sys.exc_info()
        output.error = "".join(traceback.format_exception(*exc_info))
        return output


async def async_request_openai_completions(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    try:
        start_time = time.perf_counter()
        # print('sending request for batch =', request_func_input.extra_request_body.get('batch', False))
        api_url = request_func_input.api_url
        assert api_url.endswith(
            "completions"
        ), "OpenAI Completions API URL must end with 'completions'."

        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            payload = {
                "model": request_func_input.model,
                "prompt": request_func_input.prompt,
                "temperature": 0.0,
                "best_of": 1,
                "max_tokens": request_func_input.output_len,
                "stream": not args.disable_stream,
                "ignore_eos": not args.disable_ignore_eos,
                **request_func_input.extra_request_body,
            }
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"}

            output = RequestFuncOutput()
            output.prompt_len = request_func_input.prompt_len
            if request_func_input.rid:
                output.input_rid = request_func_input.rid

            generated_text = ""
            ttft = 0.0
            st = time.perf_counter()
            most_recent_timestamp = st
            try:
                output.sent_time = st
                async with session.post(
                    url=api_url, json=payload, headers=headers
                ) as response:
                    if response.status == 200:
                        async for chunk_bytes in response.content:
                            chunk_bytes = chunk_bytes.strip()
                            if not chunk_bytes:
                                continue

                            chunk = remove_prefix(chunk_bytes.decode("utf-8"), "data: ")
                            latency = time.perf_counter() - st
                            if chunk == "[DONE]":
                                pass
                            else:
                                data = json.loads(chunk)

                                # NOTE: Some completion API might have a last
                                # usage summary response without a token so we
                                # want to check a token was generated
                                if data["choices"][0]["text"]:
                                    timestamp = time.perf_counter()
                                    # First token
                                    if ttft == 0.0:
                                        ttft = time.perf_counter() - st
                                        output.ttft = ttft
                                        # output.node_measured_ttft = data['meta_info']['node_measured_ttft']
                                    # Decoding phase
                                    else:
                                        output.itl.append(
                                            timestamp - most_recent_timestamp
                                        )

                                    most_recent_timestamp = timestamp
                                    generated_text += data["choices"][0]["text"]

                                if data.get("meta_info"):
                                    if data["meta_info"].get("queueing_before_local_node") is None:
                                        continue
                                    # Collect relevant metrics
                                    output.queueing_before_local_node = data[
                                        "meta_info"
                                    ]["queueing_before_local_node"]
                                    output.waiting_queue_time = data["meta_info"][
                                        "waiting_queue_time"
                                    ]
                                    output.total_prefill_time = data["meta_info"][
                                        "total_prefill_time"
                                    ]
                                    output.total_decode_time = data["meta_info"][
                                        "total_decode_time"
                                    ]
                                    output.local_scheduling_time = (
                                        data["meta_info"]["local_scheduling_time"]
                                        - output.total_prefill_time
                                        - output.total_decode_time
                                        - output.waiting_queue_time
                                    )
                                    output.detok_time = data["meta_info"]["detok_time"]
                                    output.token_time = data["meta_info"]["token_time"]
                                    output.overall_node_time = data["meta_info"][
                                        "overall_node_time"
                                    ]
                                    output.node_measured_ttft = data["meta_info"][
                                        "end_openai_completion_ttft"
                                    ]
                                    output.node_measured_tpots = data["meta_info"][
                                        "node_measured_tpot"
                                    ]
                                    output.avg_recieving_time = data["meta_info"][
                                        "avg_recieving_time"
                                    ]

                        output.generated_text = generated_text
                        output.success = True
                        output.latency = latency
                        output.output_len = request_func_input.output_len
                    else:
                        output.error = response.reason or ""
                        output.success = False
                        print(output.error)
            except asyncio.CancelledError:
                output.cancelled = True
                output.success = False

                # output.success = True if ttft > 0 else False
                # if not output.node_measured_ttft:
                #     output.node_measured_ttft = ttft
                # if not output.node_measured_tpots:
                #     output.node_measured_tpots = output.itl

                output.generated_text = generated_text
                output.latency = time.perf_counter() - st
                output.output_len = request_func_input.output_len
            except Exception:
                output.success = False
                exc_info = sys.exc_info()
                output.error = "".join(traceback.format_exception(*exc_info))
                print(output.error)
        try:
            if pbar and not request_func_input.extra_request_body.get("batch", False):
                pbar.update(1)
        except asyncio.CancelledError:
            pass
        return output
    except asyncio.CancelledError:
        output = RequestFuncOutput()
        output.cancelled = True
        output.success = False
        exc_info = sys.exc_info()
        output.error = "".join(traceback.format_exception(*exc_info))
        return output

async def async_request_llumnix_completions(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    global icount
    try:
        start_time = time.perf_counter()
        # print('sending request for batch =', request_func_input.extra_request_body.get('batch', False))
        api_url = request_func_input.api_url
        assert api_url.endswith(
            "completions"
        ), "OpenAI Completions API URL must end with 'completions'."

        # Inverting priority since vLLM uses a different priority system. We use 0 as highest. They use biggest number as highest.
        priority = request_func_input.max_priority - request_func_input.extra_request_body.get("priority", 0)
        request_func_input.extra_request_body["priority"] = priority

        request_func_input.extra_request_body.pop("ttft_slo", None)
        request_func_input.extra_request_body.pop("tpot_slo", None)
        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT, read_bufsize=4 * 1024 * 1024) as session:
            payload = {
                "prompt": request_func_input.prompt,
                "temperature": 0.0,
                "best_of": 1,
                "max_tokens": request_func_input.output_len,
                "stream": not args.disable_stream,
                "ignore_eos": not args.disable_ignore_eos,
                **request_func_input.extra_request_body,
            }
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"}

            output = RequestFuncOutput()
            output.prompt_len = request_func_input.prompt_len
            if request_func_input.rid:
                output.input_rid = request_func_input.rid

            generated_text = ""
            ttft = 0.0
            st = time.perf_counter()
            try:
                output.sent_time = st
                async with session.post(
                    url=api_url, json=payload, headers=headers
                ) as response:
                    if response.status == 200:
                        async for chunk_bytes in response.content:
                            chunk_bytes = chunk_bytes.rstrip(b"\r\n")
                            if not chunk_bytes:
                                continue
                            chunk = chunk_bytes.decode("utf-8")
                            latency = time.perf_counter() - st
                            if chunk == "[DONE]":
                                pass
                            else:
                                data = json.loads(chunk)
                                # NOTE: Some completion API might have a last
                                # usage summary response without a token so we
                                # want to check a token was generated
                                if data["text"]:
                                    # First token
                                    if ttft == 0.0 and "ttft" in data:
                                        ttft = data["ttft"]
                                        output.ttft = ttft
                                        output.node_measured_ttft = data['ttft']
                                    # Decoding phase
                                    else:
                                        output.itl.append(data["itl"])
                                    generated_text += data["text"]
                        if output.node_measured_ttft is None:
                            output.success = False
                            output.error = "No token received"
                        else:
                            output.generated_text = generated_text
                            output.success = True
                            output.latency = latency
                            output.output_len = request_func_input.output_len
                    else:
                        output.error = response.reason or ""
                        output.success = False
                        print(output.error)
            except asyncio.CancelledError:
                output.cancelled = True
                output.success = True if ttft > 0 else False
                output.generated_text = generated_text
                output.latency = time.perf_counter() - st
                output.output_len = request_func_input.output_len
            except Exception:
                output.success = False
                exc_info = sys.exc_info()
                output.error = "".join(traceback.format_exception(*exc_info))
                print(output.error)
        try:
            if pbar and not request_func_input.extra_request_body.get('batch', False):
                pbar.update(1)
        except asyncio.CancelledError:
            pass
        return output
    except asyncio.CancelledError:
        output = RequestFuncOutput()
        output.cancelled = True
        output.success = False
        exc_info = sys.exc_info()
        output.error = "".join(traceback.format_exception(*exc_info))
        return output

async def async_request_gserver(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    raise NotImplementedError()


def get_model(pretrained_model_name_or_path: str) -> str:
    if os.getenv("SGLANG_USE_MODELSCOPE", "False").lower() == "true":
        import huggingface_hub.constants
        from modelscope import snapshot_download

        model_path = snapshot_download(
            model_id=pretrained_model_name_or_path,
            local_files_only=huggingface_hub.constants.HF_HUB_OFFLINE,
            ignore_file_pattern=[".*.pt", ".*.safetensors", ".*.bin"],
        )

        return model_path
    return pretrained_model_name_or_path


def get_tokenizer(
    pretrained_model_name_or_path: str,
) -> Union[PreTrainedTokenizer, PreTrainedTokenizerFast]:
    if pretrained_model_name_or_path.endswith(
        ".json"
    ) or pretrained_model_name_or_path.endswith(".model"):
        from dyserve.srt.hf_transformers_utils import get_tokenizer

        return get_tokenizer(pretrained_model_name_or_path)

    if pretrained_model_name_or_path is not None and not os.path.exists(
        pretrained_model_name_or_path
    ):
        pretrained_model_name_or_path = get_model(pretrained_model_name_or_path)
    return AutoTokenizer.from_pretrained(
        pretrained_model_name_or_path, trust_remote_code=True
    )


ASYNC_REQUEST_FUNCS = {
    "dyserve": async_request_dyserve_completions,
    "sglang": async_request_openai_completions,
    "llumnix": async_request_llumnix_completions,
    "qlm": async_request_llumnix_completions, # LLumnix VLLM here use the same modified completions format
    "vllm": async_request_openai_completions,
    "lmdeploy": async_request_openai_completions,
    "trt": async_request_trt_llm,
    "gserver": async_request_gserver,
}


@dataclass
class BenchmarkMetrics:
    completed: int
    total_input: int
    total_output: int
    total_output_retokenized: int
    request_throughput: float
    input_throughput: float
    output_throughput: float
    output_throughput_retokenized: float
    mean_ttft_ms: float
    median_ttft_ms: float
    std_ttft_ms: float
    p99_ttft_ms: float
    mean_tpot_ms: float
    median_tpot_ms: float
    std_tpot_ms: float
    p99_tpot_ms: float
    mean_itl_ms: float
    median_itl_ms: float
    std_itl_ms: float
    p99_itl_ms: float
    mean_e2e_latency_ms: float
    median_e2e_latency_ms: float
    ttft_slo_attainment: Optional[float] = None
    tpot_slo_attainment: Optional[float] = None

    avg_local_scheduler_time_ignoring_queing: Optional[float] = None
    global_to_local_scheduling_time: Optional[float] = None
    overall_global_observed_time: Optional[float] = None

    avg_token_time: Optional[float] = None
    p99_token_time: Optional[float] = None
    avg_detok_time: Optional[float] = None

    avg_queuing_time: Optional[float] = None
    p99_queing_time: Optional[float] = None
    model_forwarding_time: Optional[float] = None
    prefill_times: Optional[float] = None
    decode_times: Optional[float] = None
    avg_node_measured_ttft: Optional[float] = None
    avg_node_measured_tpot: Optional[float] = None
    input_size_distribution: Optional[str] = None
    output_size_distribution: Optional[str] = None
    prompt_output_ratio: Optional[str] = None
    avg_request_recieving_time: Optional[float] = None

    tpots: Optional[List[float]] = None
    ttfts: Optional[List[float]] = None


def download_and_cache_file(url: str, filename: Optional[str] = None):
    """Read and cache a file from a url."""
    if filename is None:
        filename = os.path.join("/tmp", url.split("/")[-1])

    # Check if the cache file already exists
    if os.path.exists(filename):
        return filename

    print(f"Downloading from {url} to {filename}")

    # Stream the response to show the progress bar
    response = requests.get(url, stream=True)
    response.raise_for_status()  # Check for request errors

    # Total size of the file in bytes
    total_size = int(response.headers.get("content-length", 0))
    chunk_size = 1024  # Download in chunks of 1KB

    # Use tqdm to display the progress bar
    with open(filename, "wb") as f, tqdm(
        desc=filename,
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
    ) as bar:
        for chunk in response.iter_content(chunk_size=chunk_size):
            f.write(chunk)
            bar.update(len(chunk))

    return filename


async def get_request(
    input_requests: List[Tuple[str, int, int]],
    request_rate: float,
) -> AsyncGenerator[Tuple[str, int, int], None]:
    input_requests = iter(input_requests)
    for request in input_requests:
        yield request

        if request_rate == float("inf"):
            # If the request rate is infinity, then we don't need to wait.
            continue

        # Sample the request interval from the exponential distribution.
        interval = np.random.exponential(1.0 / request_rate)
        # The next request will be sent after the interval.
        await asyncio.sleep(interval)


def calculate_metrics(
    input_requests: List[Tuple[str, int, int]],
    outputs: List[RequestFuncOutput],
    dur_s: float,
    tokenizer: PreTrainedTokenizerBase,
    backend: str,
    ttft_slo: Optional[float] = None,
    tpot_slo: Optional[float] = None,
    requests_tracked: Optional[str] = None, # list of rids to check
) -> Tuple[BenchmarkMetrics, List[int]]:
    output_lens: List[int] = []
    retokenized_output_lens: List[int] = []
    total_input = 0
    completed = 0
    itls: List[float] = []
    tpots: List[float] = []
    ttfts: List[float] = []
    e2e_latencies: List[float] = []
    for i in range(len(outputs)):
        is_valid_metric = outputs[i].success
        if requests_tracked:
            is_valid_metric = outputs[i].input_rid in requests_tracked
        if is_valid_metric:
            output_len = outputs[i].output_len
            output_lens.append(output_len)
            retokenized_output_len = len(
                tokenizer.encode(outputs[i].generated_text, add_special_tokens=False)
            )
            outputs[i].retok_output_len = retokenized_output_len
            retokenized_output_lens.append(retokenized_output_len)
            total_input += input_requests[i][1]
            if retokenized_output_len > 1:
                tpots.append(
                    (outputs[i].latency - outputs[i].ttft)
                    / (retokenized_output_len - 1)
                )
            if outputs[i].node_measured_ttft:
                ttfts.append(outputs[i].node_measured_ttft)
            itls += outputs[i].itl

            e2e_latencies.append(outputs[i].latency)
            if not outputs[i].cancelled:
                completed += 1
        else:
            output_lens.append(0)
            retokenized_output_lens.append(0)

    # Collect Additional Matrics
    scheduling_time = []
    overall_node_times = []
    tokenization_times = []
    detokenization_times = []

    global_to_local_node_time = []
    queueing_time = []
    model_forwarding_time = []
    prefill_times = []
    decode_times = []
    input_sizes = []
    output_sizes = []
    avg_recieving_time = []
    pd_ratio = []
    node_measured_ttfts = []
    node_measured_tpots = []
    for output in outputs:
        is_valid_metric = output.success
        if requests_tracked:
            is_valid_metric = output.input_rid in requests_tracked
        if is_valid_metric:
            scheduling_time.append(output.local_scheduling_time)
            overall_node_times.append(output.overall_node_time)
            tokenization_times.append(output.token_time)
            detokenization_times.append(output.detok_time)
            global_to_local_node_time.append(output.queueing_before_local_node)
            queueing_time.append(output.waiting_queue_time)
            model_forwarding_time.append(
                output.total_prefill_time + output.total_decode_time
            )
            avg_recieving_time.append(output.avg_recieving_time)
            prefill_times.append(output.total_prefill_time)
            decode_times.append(output.total_decode_time)
            input_sizes.append(output.prompt_len)
            output_sizes.append(output.output_len)
            pd_ratio.append(output.prompt_len / (output.output_len or 1))
            if output.success:
                node_measured_ttfts.append(output.node_measured_ttft)
                if output.node_measured_tpots:
                    node_measured_tpots.append(np.mean(output.node_measured_tpots))

    input_size_distribution = f"(mean={np.mean(input_sizes or 0):.2f}, std={np.std(input_sizes or 0):.2f}, min={np.min(input_sizes or 0)}, max={np.max(input_sizes or 0)})"
    output_size_distribution = f"(mean={np.mean(output_sizes or 0):.2f}, std={np.std(output_sizes or 0):.2f}, min={np.min(output_sizes or 0)}, max={np.max(output_sizes or 0)})"
    prompt_output_ratio = f"(mean={np.mean(pd_ratio or 0):.2f}, std={np.std(pd_ratio or 0):.2f}, min={np.min(pd_ratio or 0)}, max={np.max(pd_ratio or 0)})"
    if completed == 0:
        warnings.warn(
            "All requests failed. This is likely due to a misconfiguration "
            "on the benchmark arguments.",
            stacklevel=2,
        )
    metrics = BenchmarkMetrics(
        completed=completed,
        total_input=total_input,
        total_output=sum(output_lens),
        total_output_retokenized=sum(retokenized_output_lens),
        request_throughput=completed / dur_s,
        input_throughput=total_input / dur_s,
        output_throughput=sum(output_lens) / dur_s,
        output_throughput_retokenized=sum(retokenized_output_lens) / dur_s,
        mean_ttft_ms=np.mean(ttfts or 0)
        * 1000,  # ttfts is empty if streaming is not supported by backend
        median_ttft_ms=np.median(ttfts or 0) * 1000,
        std_ttft_ms=np.std(ttfts or 0) * 1000,
        p99_ttft_ms=np.percentile(ttfts or 0, 99) * 1000,
        mean_tpot_ms=np.mean(tpots or 0) * 1000,
        median_tpot_ms=np.median(tpots or 0) * 1000,
        std_tpot_ms=np.std(tpots or 0) * 1000,
        p99_tpot_ms=np.percentile(tpots or 0, 99) * 1000,
        mean_itl_ms=np.mean(itls or 0) * 1000,
        median_itl_ms=np.median(itls or 0) * 1000,
        std_itl_ms=np.std(itls or 0) * 1000,
        p99_itl_ms=np.percentile(itls or 0, 99) * 1000,
        mean_e2e_latency_ms=np.mean(e2e_latencies) * 1000,
        median_e2e_latency_ms=np.median(e2e_latencies) * 1000,
        ttft_slo_attainment=(
            np.mean([ttft <= ttft_slo for ttft in ttfts]) if ttfts else None
        ),
        tpot_slo_attainment=(
            np.mean([tpot <= tpot_slo for tpot in tpots]) if tpot_slo else None
        ),
        avg_local_scheduler_time_ignoring_queing=np.mean(scheduling_time or 0) * 1000,
        avg_queuing_time=np.mean(queueing_time or 0) * 1000,
        global_to_local_scheduling_time=np.mean(global_to_local_node_time or 0) * 1000,
        overall_global_observed_time=np.mean(overall_node_times) * 1000,
        avg_token_time=np.mean(tokenization_times or 0) * 1000,
        p99_token_time=np.percentile(tokenization_times or 0, 99) * 1000,
        avg_detok_time=np.mean(detokenization_times or 0) * 1000,
        p99_queing_time=np.percentile(queueing_time or 0, 99) * 1000,
        model_forwarding_time=np.mean(model_forwarding_time or 0) * 1000,
        prefill_times=np.mean(prefill_times) * 1000,
        decode_times=np.mean(decode_times) * 1000,
        avg_node_measured_ttft=np.mean(node_measured_ttfts) * 1000,
        avg_node_measured_tpot=np.mean(node_measured_tpots or 0) * 1000,
        input_size_distribution=input_size_distribution,
        output_size_distribution=output_size_distribution,
        avg_request_recieving_time=np.mean(avg_recieving_time or 0) * 1000,
        prompt_output_ratio=prompt_output_ratio,
        tpots=tpots,
        ttfts=ttfts,
    )

    return metrics, output_lens


def print_section(
    console,
    title,
    metrics_dict,
    header_color="bold magenta",
    metric_color="cyan",
    value_color="bold",
):
    """
    Prints a formatted section with a table.

    Args:
        console (Console): Rich console instance.
        title (str): Section title.
        metrics_dict (dict): Dictionary of metric names and values.
        header_color (str): Color of the table header.
        metric_color (str): Color of the metric names.
        value_color (str): Color of the values.
    """
    console.print(f"[{header_color}]{title}[/{header_color}]")
    table = Table(show_header=True, header_style=header_color)
    table.add_column("Metric", style=metric_color)
    table.add_column("Value", style=value_color)

    for key, value in metrics_dict.items():
        table.add_row(key, str(value))

    console.print(table)


def highlight_metric(
    value, threshold=1.0, low_color="bold red", high_color="bold green"
):
    """Returns a formatted string with conditional coloring based on a threshold."""
    color = high_color if value >= threshold else low_color
    return f"[{color}]{value:.2%}[/{color}]"


def generate_performance_table(workloads, results):
    """Generates and prints a performance table with conditional highlights."""
    console = Console()
    table = Table(show_header=True, header_style="bold magenta")

    # Define columns
    table.add_column("Tier Name", style="cyan", no_wrap=True)
    table.add_column("Req/s", justify="right")
    table.add_column("Output Throughput", justify="right")
    table.add_column("TTFT Attainment", justify="right")
    table.add_column("TPOT Attainment", justify="right")
    table.add_column("Dataset Name", justify="right")
    table.add_column("Arrival Pattern", justify="right")

    # Populate table with data
    for workload, result in zip(workloads, results):
        tier_name = workload.config.get("tier_name", "N/A").lower()
        dataset_name = workload.config.get("dataset").get("name")
        arrival_pattern = workload.config.get("arrival_pattern").get("name")
        if dataset_name == "random":
            input_len = workload.config.get("dataset").get("input_len")
            output_len = workload.config.get("dataset").get("output_len")
            dataset_name += f"_{input_len}_{output_len}"
        # Conditional highlighting
        output_throughput = (
            f"[bold yellow]{result['output_throughput']:.2f}[/bold yellow]"
            if tier_name == "batch"
            else f"{result['output_throughput']:.2f}"
        )
        ttft_attainment = (
            highlight_metric(result["ttft_slo_attainment"])
            if tier_name == "realtime"
            else f"{result['ttft_slo_attainment']:.2%}"
        )
        tpot_attainment = (
            highlight_metric(result["tpot_slo_attainment"])
            if tier_name == "realtime"
            else f"{result['tpot_slo_attainment']:.2%}"
        )

        table.add_row(
            workload.config.get("tier_name", "N/A"),
            f"{result['request_throughput']:.2f}",
            output_throughput,
            ttft_attainment,
            tpot_attainment,
            dataset_name,
            arrival_pattern,
        )

    console.print(table)


async def benchmark(
    backend: str,
    api_url: str,
    model_id: str,
    tokenizer: PreTrainedTokenizerBase,
    workloads: List[Workload],
    window: float,
    disable_tqdm: bool,
    extra_request_body: Dict[str, Any],
    log_initial_run: bool,
    request_collection_window_start_time: float,
    request_collection_window: float,
):
    if backend in ASYNC_REQUEST_FUNCS:
        request_func = ASYNC_REQUEST_FUNCS[backend]
    else:
        raise ValueError(f"Unknown backend: {backend}")

    print("Starting initial single prompt test run...")
    test_prompt, test_prompt_len, test_output_len = workloads[0].requests[0]
    test_input = RequestFuncInput(
        model=model_id,
        prompt=test_prompt,
        api_url=api_url,
        prompt_len=test_prompt_len,
        output_len=test_output_len,
        extra_request_body={
            **extra_request_body,
            "priority": 0,
            "ttft_slo": 1000000,
            "tpot_slo": 1000000,
        },
        max_priority=0,
    )
    if extra_request_body.get("scheduler_mode", "DP_MODE") == "TP_MODE":
        update_scheduler_mode_url = api_url.replace(
            "v1/completions", "update_scheduler_mode"
        )
        res = requests.post(update_scheduler_mode_url, json={"scheduler_mode": "TP"})
        assert res.content.decode("utf-8") == "Updated scheduler to: TP"
    print("Test prompt len/test output len", test_prompt_len, test_output_len)
    test_output = await asyncio.gather(request_func(request_func_input=test_input), request_func(request_func_input=test_input))
    test_output = test_output[0]
    if log_initial_run:
        print(test_output.generated_text)
    print(test_output)
    if not test_output.success:
        raise ValueError(
            "Initial test run failed - Please make sure benchmark arguments "
            f"are correctly specified. Error: {test_output.error}"
        )
    else:
        print("Initial test run completed. Starting main benchmark run...")

    pbar = None if disable_tqdm else tqdm(total=sum(len(w.requests) for w in workloads))

    tasks: List[List[asyncio.Task]] = [[] for _ in range(len(workloads))]
    request_timestamps = []
    max_priority = 0
    for priority, workload in enumerate(workloads):
        ttft_slo_ms, tpot_slo_ms = workload.ttft_slo_ms, workload.tpot_slo_ms
        for req, ts in zip(workload.requests, workload.timestamps):
            request_timestamps.append((req, ts, priority, ttft_slo_ms, tpot_slo_ms))
        max_priority = max(max_priority, priority)
    request_timestamps.sort(key=lambda x: x[1])

    last_sent_time = request_timestamps[-1][1]
    if last_sent_time < window:
        print(
            f"WARNING: The benchmark window is longer than the last request sent time ({last_sent_time})."
        )

    # request collection window
    if request_collection_window is None:
        request_collection_window = float('inf')
    if request_collection_window_start_time is None:
        request_collection_window_start_time = 0.0
    num_requests_in_window = int(request_collection_window/(1/workloads[0].request_rate))
    num_requests_to_skip_before_collection = int(request_collection_window_start_time/(1/workloads[0].request_rate))
    requests_to_check_for_completion = []

    print(f"Request collection window: {request_collection_window} {num_requests_in_window} {num_requests_to_skip_before_collection}")
    benchmark_start_time = time.perf_counter()
    requests_tracked: List[str] = []
    with NullContext(gpu_ids=[], output_file=None):
        completed_all_tracked = False
        for request_num, request in enumerate(request_timestamps):
            (prompt, prompt_len, output_len), ts, priority, ttft_slo_ms, tpot_slo_ms = (
                request
            )
            input_rid = uuid.uuid4().hex
            request_func_input = RequestFuncInput(
                rid=input_rid,
                model=model_id,
                prompt=prompt,
                api_url=api_url,
                prompt_len=prompt_len,
                output_len=output_len,
                extra_request_body={
                    **extra_request_body,
                    "priority": priority,
                    "ttft_slo": ttft_slo_ms,
                    "tpot_slo": tpot_slo_ms,
                },
                max_priority=max_priority
            )
            while time.perf_counter() - benchmark_start_time < ts:
                await asyncio.sleep(0.001)

            if time.perf_counter() - benchmark_start_time > window:
                break

            if len(requests_tracked) == num_requests_in_window and all(task.done() for task in requests_to_check_for_completion):
                completed_all_tracked = True
                break

            future_task = asyncio.create_task(
                request_func(request_func_input=request_func_input, pbar=pbar)
            )
            if request_num >= num_requests_to_skip_before_collection and request_num < num_requests_in_window + num_requests_to_skip_before_collection:
                if priority == 0: # Only check realtime requests for completion
                    requests_to_check_for_completion.append(future_task)
                requests_tracked.append(input_rid)

            # if request_num % 20:
            #     print(f"{request_num} requests sent, {len(requests_tracked)} tracked, {len(requests_to_check_for_completion)} to check for completion {sum(task.done() for task in requests_to_check_for_completion)}")

            tasks[priority].append(
                future_task
            )
        # wait for the window
        while time.perf_counter() - benchmark_start_time < window:
            if requests_tracked and completed_all_tracked:
                break
            await asyncio.sleep(0.01)

    # cancel all unfinished tasks
    for jobs in tasks:
        for job in jobs:
            job.cancel()
    outputs_per_workload: List[List[RequestFuncOutput]] = [
        await asyncio.gather(*jobs) for jobs in tasks
    ]

    if pbar is not None:
        pbar.close()

    benchmark_duration = time.perf_counter() - benchmark_start_time

    def print_metrics(
        requests,
        outputs,
        benchmark_duration,
        request_rate,
        ttft_slo_ms=None,
        tpot_slo_ms=None,
        tier_name="",
        requests_tracked=[]
    ):
        console = Console()

        # Calculate metrics
        metrics, output_lens = calculate_metrics(
            input_requests=requests,
            outputs=outputs,
            dur_s=benchmark_duration,
            tokenizer=tokenizer,
            backend=backend,
            requests_tracked=requests_tracked,
            ttft_slo=ttft_slo_ms / 1000 if ttft_slo_ms else None,
            tpot_slo=tpot_slo_ms / 1000 if tpot_slo_ms else None,
        )

        # Print header
        console.print(f"[bold green]{'=' * 30}{tier_name}{'=' * 30}[/bold green]")

        # General Statistics
        print_section(
            console,
            "General Statistics",
            {
                "Successful requests": metrics.completed,
                "Benchmark duration (s)": f"{benchmark_duration:.2f}",
                "Request rate (rps)": f"{request_rate:.2f}",
                "Total input tokens": metrics.total_input,
                "Total generated tokens": metrics.total_output,
                "Total generated tokens (retokenized)": metrics.total_output_retokenized,
                "Request throughput (req/s)": f"{metrics.request_throughput:.2f}",
                "Input token throughput (tok/s)": f"{metrics.input_throughput:.2f}",
                "Output token throughput (tok/s)": f"{metrics.output_throughput:.2f}",
            },
        )

        # End-to-End Latency
        print_section(
            console,
            "End-to-End Latency",
            {
                "Mean E2E Latency (ms)": f"{metrics.mean_e2e_latency_ms:.2f}",
                "Median E2E Latency (ms)": f"{metrics.median_e2e_latency_ms:.2f}",
            },
            header_color="bold yellow",
        )

        # Time to First Token (TTFT)
        ttft_metrics = {
            "Mean TTFT (ms)": f"{metrics.mean_ttft_ms:.2f}",
            "Median TTFT (ms)": f"{metrics.median_ttft_ms:.2f}",
            "P99 TTFT (ms)": f"{metrics.p99_ttft_ms:.2f}",
        }
        if ttft_slo_ms:
            if metrics.ttft_slo_attainment is None:
                metrics.ttft_slo_attainment = float('inf')
            ttft_metrics[f"TTFT SLO ({ttft_slo_ms:.2f})"] = (
                f"{metrics.ttft_slo_attainment:.2f}"
            )
        print_section(
            console,
            "Time to First Token (TTFT)",
            ttft_metrics,
            header_color="bold blue",
        )

        # Time per Output Token (TPOT)
        tpot_metrics = {
            "Mean TPOT (ms)": f"{metrics.mean_tpot_ms:.2f}",
            "Median TPOT (ms)": f"{metrics.median_tpot_ms:.2f}",
            "P99 TPOT (ms)": f"{metrics.p99_tpot_ms:.2f}",
        }
        if tpot_slo_ms:
            if metrics.tpot_slo_attainment is None:
                metrics.tpot_slo_attainment = float('inf')
            tpot_metrics[f"TPOT SLO ({tpot_slo_ms:.2f})"] = (
                f"{metrics.tpot_slo_attainment:.2f}"
            )
        print_section(
            console,
            "Time per Output Token (TPOT)",
            tpot_metrics,
            header_color="bold blue",
        )

        print_section(
            console,
            "Inter-token Latency (ITL)",
            {
                "Mean ITL (ms)": f"{metrics.mean_itl_ms:.2f}",
                "Median ITL (ms)": f"{metrics.median_itl_ms:.2f}",
                "P99 ITL (ms)": f"{metrics.p99_itl_ms:.2f}",
            },
            header_color="bold yellow",
        )

        # Tokenizer Stats
        print_section(
            console,
            "Tokenizer Stats",
            {
                "Avg Token Time (ms)": f"{metrics.avg_token_time:.4f}",
                "P99 Token Time (ms)": f"{metrics.p99_token_time:.4f}",
                "Avg Detokenization Time (ms)": f"{metrics.avg_detok_time:.4f}",
            },
            header_color="bold cyan",
        )

        # Queueing Stats
        print_section(
            console,
            "Queueing Stats",
            {
                "Avg Waiting Queuing Time (ms)": f"{metrics.avg_queuing_time:.4f}",
                "P99 Queuing Time (ms)": f"{metrics.p99_queing_time:.4f}",
                "Avg Local Scheduler Scheduling Time Ignoring Queuing (ms)": f"{metrics.avg_local_scheduler_time_ignoring_queing:.4f}",
                "Avg Global to Local Scheduling Time (ms)": f"{metrics.global_to_local_scheduling_time:.4f}",
            },
            header_color="bold red",
        )

        print_section(
            console,
            "Overhead Analysis",
            {
                "Benchmarking Overhead (ms)": f"{metrics.mean_e2e_latency_ms - metrics.overall_global_observed_time:.4f}",
                "Avg Unrecorded Time (ms)": f"{metrics.overall_global_observed_time - metrics.global_to_local_scheduling_time - metrics.avg_queuing_time - metrics.avg_token_time - metrics.avg_detok_time - metrics.model_forwarding_time - metrics.avg_local_scheduler_time_ignoring_queing:.4f}",
                "% of Time not Model Forwarding": (
                    f"{(1 - metrics.model_forwarding_time/metrics.overall_global_observed_time) * 100:.4f}%"
                    if metrics.overall_global_observed_time != 0
                    else float("nan")
                ),
                "% of Time not Model Forwarding and not queueing": (
                    f"{(1 - (metrics.model_forwarding_time + metrics.avg_queuing_time)/metrics.overall_global_observed_time) * 100:.4f}%"
                    if metrics.overall_global_observed_time != 0
                    else float("nan")
                ),
                "Model Forwarding Time (ms)": f"{metrics.model_forwarding_time:.4f}",
                "Avg Prefill Time (ms)": f"{metrics.prefill_times:.4f}",
                "Avg Decode Time (ms)": f"{metrics.decode_times:.4f}",
                "Avg Node Measured TTFT (ms)": f"{metrics.avg_node_measured_ttft:.4f}",  # Sanity check if there's a significant difference from previous TTFT Metric
                "Avg Node Measured TPOT (ms)": f"{metrics.avg_node_measured_tpot:.4f}",
                "Avg Request Recieving Time (ms)": f"{metrics.avg_request_recieving_time:4f}"
            },
            header_color="bold red",
        )

        # Workload Stats
        print_section(
            console,
            "Workload Stats",
            {
                "Workload Input Distribution": str(metrics.input_size_distribution),
                "Workload Output Distribution": str(metrics.output_size_distribution),
                "Workload P/D Ratio Distribution": str(metrics.prompt_output_ratio),
            },
            header_color="bold green",
        )

        # Print footer
        console.print(f"[bold green]{'=' * 50}[/bold green]")

        if (
            metrics.median_ttft_ms is not None
            and metrics.mean_itl_ms is not None
            and metrics.output_throughput is not None
        ):
            result = {
                "run_id": str(uuid.uuid4()),
                "backend": args.backend,
                "window": window,
                "tier_name": tier_name,
                "request_rate": request_rate,
                "total_requests": len(requests),
                "total_input_tokens": metrics.total_input,
                "total_output_tokens": metrics.total_output,
                "total_output_tokens_retokenized": metrics.total_output_retokenized,
                "mean_e2e_latency_ms": metrics.mean_e2e_latency_ms,
                "median_e2e_latency_ms": metrics.median_e2e_latency_ms,
                "mean_ttft_ms": metrics.mean_ttft_ms,
                "median_ttft_ms": metrics.median_ttft_ms,
                "p99_ttft_ms": metrics.p99_ttft_ms,
                "mean_itl_ms": metrics.mean_itl_ms,
                "median_itl_ms": metrics.median_itl_ms,
                "p99_itl_ms": metrics.p99_itl_ms,
                "mean_tpot_ms": metrics.mean_tpot_ms,
                "median_tpot_ms": metrics.median_tpot_ms,
                "p99_tpot_ms": metrics.p99_tpot_ms,
                "request_throughput": metrics.request_throughput,
                "input_throughput": metrics.input_throughput,
                "output_throughput": metrics.output_throughput,
                "duration": benchmark_duration,
                "completed": metrics.completed,
                "ttft_slo_attainment": metrics.ttft_slo_attainment,
                "tpot_slo_attainment": metrics.tpot_slo_attainment,
                "ttft_slo_ms": ttft_slo_ms,
                "tpot_slo_ms": tpot_slo_ms,
                "avg_token_time": metrics.avg_token_time,
                "p99_token_time": metrics.p99_token_time,
                "detok_time": metrics.avg_detok_time,
                "avg_queuing_time": metrics.avg_queuing_time,
                "p99_queuing_time": metrics.p99_queing_time,
                "raw_itl": metrics.tpots,
                "raw_ttfts": metrics.ttfts,
                "percent_not_model_forwarding": 1
                - metrics.model_forwarding_time
                / (metrics.overall_global_observed_time or 1),
                "model_forwarding_time": metrics.model_forwarding_time,
                "input_distribution": metrics.input_size_distribution,
                "output_distribution": metrics.output_size_distribution,
                "prompt_output_ratio": metrics.prompt_output_ratio,
                "config": workload.config,
                "command_name": args.command_name if args.command_name else "",
            }
        else:
            print("Error running benchmark")
            print("-" * 30)
        return result

    print("\n{s:{c}^{n}}".format(s=" Serving Benchmark Result ", n=50, c="="))
    print("{:<40} {:<10}".format("Backend:", backend))
    print("{:<40} {:<10}".format("Benchmarking Window:", window))

    # Determine output file name
    if args.output_file:
        output_file_name = args.output_file
    else:
        now = datetime.now().strftime("%m%d")
        if args.dataset_name == "random":
            output_file_name = f"{args.backend}_{now}_{args.num_prompts}_{args.random_input_len}_{args.random_output_len}.jsonl"
        else:
            output_file_name = f"{args.backend}_{now}_{args.num_prompts}_sharegpt.jsonl"

    results: List[Dict] = []
    raw_trace_dir = os.path.join(os.path.dirname(output_file_name), "raw_traces")
    os.makedirs(raw_trace_dir, exist_ok=True)
    for workload, outputs in zip(workloads, outputs_per_workload):
        tier_name = workload.config.get("tier_name", "")
        res = print_metrics(
            workload.requests,
            outputs,
            benchmark_duration,
            len(workload.timestamps) / workload.timestamps[-1],
            workload.ttft_slo_ms,
            workload.tpot_slo_ms,
            tier_name=tier_name,
            requests_tracked=requests_tracked
        )
        results.append(res)
        # Save raw stats of each request
        run_id = res["run_id"]
        raw_trace_path = os.path.join(raw_trace_dir, f"{run_id}.json")
        with open(raw_trace_path, "w") as f:
            json.dump([o.__dict__ for o in outputs], f, indent=4)
    # Overall metrics, print the table SLO attainment,
    generate_performance_table(workloads, results)

    # Append results to a JSONL file
    with open(output_file_name, "a") as file:
        file.write(json.dumps(results) + "\n")

    # with open(f'trace_rps-{request_rate}.json', 'w') as f:
    #     json.dump([o.__dict__ for o in outputs], f, indent=4)


def parse_request_rate_range(request_rate_range):
    if len(request_rate_range.split(",")) == 3:
        start, stop, step = map(int, request_rate_range.split(","))
        return list(range(start, stop, step))
    else:
        return list(map(int, request_rate_range.split(",")))


def check_chat_template(model_path):
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        return "chat_template" in tokenizer.init_kwargs
    except Exception as e:
        print(f"Fail to load tokenizer config with error={e}")
        return False


def generate_poisson_timestamps(n_requests, total_time):
    timestamps = np.cumsum(np.random.exponential(1, n_requests))
    timestamps = timestamps * total_time / timestamps[-1]
    return timestamps


def generate_azure_timestamps(n_requests, total_time, tag: str):
    if tag == "code":
        trace_file = download_and_cache_file(
            "https://raw.githubusercontent.com/Azure/AzurePublicDataset/refs/heads/master/data/AzureLLMInferenceTrace_code.csv"
        )
    elif tag == "conversation":
        trace_file = download_and_cache_file(
            "https://raw.githubusercontent.com/Azure/AzurePublicDataset/refs/heads/master/data/AzureLLMInferenceTrace_conv.csv"
        )
    else:
        raise ValueError(
            f"Unknown tag for azure trace: {tag}. Only `code` and `conversation` are supported."
        )
    df = pd.read_csv(trace_file)
    timestamps = pd.to_datetime(df["TIMESTAMP"])
    timestamps = (timestamps - timestamps[0]).dt.total_seconds().values
    timestamps = timestamps[:n_requests]
    timestamps = timestamps * total_time / timestamps[-1]
    return timestamps


def prepare_workloads(config, tokenizer, num_prompts, request_rate) -> List[Workload]:
    total_percentage = sum([item["request_ratio"] for item in config["priorities"]])
    num_req_highest_priority = int(
        num_prompts * config["priorities"][0]["request_ratio"] / total_percentage
    )
    total_time = num_req_highest_priority / request_rate
    workloads = []
    for item in config["priorities"]:
        n_requests = int(num_prompts * item["request_ratio"] / total_percentage)
        dataset_config = item["dataset"].copy()
        dataset_name = dataset_config.pop("name")
        tier_name = item.get("tier_name", "N/A").lower()
        priority = item.get("priority", None)

        dataset_types = DatasetsTypes()  # Create an instance of DatasetsTypes
        if dataset_name not in dataset_types.dataset_name_to_class:
            raise ValueError(
                f"Unknown dataset: {dataset_name}. Supported datasets are: {dataset_types.dataset_name_to_class.keys()}"
            )
        print(f"Using dataset type: {dataset_name} for tier {tier_name}")
        input_requests = dataset_types.get_dataset_given_type(
            dataset_name, num_requests=n_requests, **dataset_config
        )

        # prepare for timestamps
        arrival_config = item["arrival_pattern"].copy()
        arrival_pattern = arrival_config.pop("name")
        if arrival_pattern == "poisson":
            timestamps = generate_poisson_timestamps(
                n_requests, total_time, **arrival_config
            )
        elif arrival_pattern == "azure":
            timestamps = generate_azure_timestamps(
                n_requests, total_time, **arrival_config
            )
        else:
            raise ValueError(f"Unknown arrival pattern: {arrival_pattern}")
        workloads.append(
            Workload(
                requests=input_requests,
                timestamps=timestamps,
                ttft_slo_ms=item["slo_ms"]["ttft"],
                tpot_slo_ms=item["slo_ms"]["tpot"],
                config=item,
                priority=priority,
                request_rate=request_rate
            )
        )
    return workloads


@dataclass
class BenchmarkingArgs:
    backend: str = "sglang"
    base_url: Optional[str] = None
    host: str = "0.0.0.0"
    port: Optional[int] = "8000"
    dataset_name: str = "sharegpt"
    dataset_path: Optional[str] = ""
    model: Optional[str] = None
    tokenizer: Optional[str] = None
    num_prompts: int = 1000
    request_rate: float = float("inf")
    seed: int = 1
    multi: bool = False
    output_file: Optional[str] = None
    disable_tqdm: bool = False
    disable_stream: bool = False
    disable_ignore_eos: bool = False
    log_initial_run: bool = False
    extra_request_body: Optional[str] = None
    window: float = 60
    config_file: Optional[str] = None
    command_name: Optional[str] = None
    request_collection_window_start_time: int = 0
    request_collection_window: Optional[int] = None

    @classmethod
    def from_cli_args(cls, args: argparse.Namespace):
        attrs = [attr.name for attr in fields(cls)]
        return cls(**{attr: getattr(args, attr) for attr in attrs})

    @classmethod
    def add_args(cls, parser):
        parser.add_argument(
            "--backend",
            type=str,
            choices=list(ASYNC_REQUEST_FUNCS.keys()),
            default="sglang",
            help="Must specify a backend, depending on the LLM Inference Engine.",
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help="Server or API base url if not using http host and port.",
        )
        parser.add_argument(
            "--host", type=str, default="0.0.0.0", help="Default host is 0.0.0.0."
        )
        parser.add_argument(
            "--port",
            type=int,
            help="If not set, the default port is configured according to its default value for different LLM Inference Engines.",
        )
        parser.add_argument(
            "--dataset-name",
            type=str,
            default="sharegpt",
            choices=["sharegpt", "random", "loogle"],
            help="Name of the dataset to benchmark on.",
        )
        parser.add_argument(
            "--dataset-path", type=str, default="", help="Path to the dataset."
        )
        parser.add_argument(
            "--model",
            type=str,
            default="meta-llama/Meta-Llama-3.2-1B",
            help="Name or path of the model. If not set, the default model will request /v1/models for conf.",
        )
        parser.add_argument(
            "--tokenizer",
            type=str,
            help="Name or path of the tokenizer. If not set, using the model conf.",
        )
        parser.add_argument(
            "--num-prompts",
            type=int,
            default=1000,
            help="Total number of prompts (all tiers) to process. Default is 1000.",
        )
        parser.add_argument(
            "--request-rate",
            type=float,
            default=float("inf"),
            help="Number of requests per second for the highest tier. If this is inf, then all the requests are sent at time 0. "
            "Default is inf.",
        )
        parser.add_argument("--seed", type=int, default=1, help="The random seed.")
        parser.add_argument(
            "--multi",
            action="store_true",
            help="Use request rate range rather than single value.",
        )
        parser.add_argument("--output-file", type=str, help="Output JSONL file name.")
        parser.add_argument(
            "--disable-tqdm",
            action="store_true",
            help="Specify to disable tqdm progress bar.",
        )
        parser.add_argument(
            "--disable-stream",
            action="store_true",
            help="Disable streaming mode.",
        )
        parser.add_argument(
            "--disable-ignore-eos",
            action="store_true",
            help="Disable ignoring EOS.",
        )
        parser.add_argument(
            "--log-initial-run",
            action="store_true",
            help="Sanity check logging initial run",
        )
        parser.add_argument(
            "--extra-request-body",
            metavar='{"key1": "value1", "key2": "value2"}',
            type=str,
            help="Append given JSON object to the request payload. You can use this to specify"
            "additional generate params like sampling params.",
        )
        parser.add_argument(
            "--window",
            type=float,
            default=60,
            help="Benchmarking window.",
        )
        parser.add_argument(
            "-c",
            "--config-file",
            type=str,
            help="The config file for the benchmark.",
        )
        parser.add_argument(
            "--command-name",
            type=str,
            help="The command name for the benchmark.",
            required=False,
            default=""
        )
        parser.add_argument(
            "--request-collection-window-start-time",
            type=int,
            default=BenchmarkingArgs.request_collection_window_start_time,
            help="The start time of the request collection window. Default is 0.",
        )
        parser.add_argument(
            "--request-collection-window",
            type=int,
            default=BenchmarkingArgs.request_collection_window,
            help="The duration of the request collection window. Default is 60 seconds.",
        )
        args = parser.parse_args()
        return cls.from_cli_args(args)


def run_benchmark(args_: BenchmarkingArgs):
    global args
    args = args_

    # Set global environments
    set_ulimit()
    random.seed(args.seed)
    np.random.seed(args.seed)

    extra_request_body = {}
    if args.extra_request_body:
        extra_request_body = json.loads(args.extra_request_body)

    # Set url
    if args.port is None:
        args.port = {
            "dyserve": 8081,
            "sglang": 30000,
            "lmdeploy": 23333,
            "vllm": 8000,
            "trt": 8000,
            "gserver": 9988,
        }.get(args.backend, 30000)

    api_url = (
        f"{args.base_url}/v1/completions"
        if args.base_url
        else f"http://{args.host}:{args.port}/v1/completions"
    )
    model_url = (
        f"{args.base_url}/v1/models"
        if args.base_url
        else f"http://{args.host}:{args.port}/v1/models"
    )

    if args.backend == "trt":
        api_url = (
            f"{args.base_url}/v2/models/ensemble/generate_stream"
            if args.base_url
            else f"http://{args.host}:{args.port}/v2/models/ensemble/generate_stream"
        )
        if args.model is None:
            print("Please provide a model using `--model` when using `trt` backend.")
            sys.exit(1)
    elif args.backend == "gserver":
        api_url = args.base_url if args.base_url else f"{args.host}:{args.port}"
        args.model = args.model or "default"
    # elif args.backend == "sglang":
    #     api_url = (
    #         f"{args.base_url}/generate"
    #         if args.base_url
    #         else f"http://{args.host}:{args.port}/generate"
    #     )

    # Get model name
    if args.model is None:
        try:
            response = requests.get(model_url)
            model_list = response.json().get("data", [])
            args.model = model_list[0]["id"] if model_list else None
        except Exception as e:
            print(f"Failed to fetch model from {model_url}. Error: {e}")
            print(
                "Please specify the correct host and port using `--host` and `--port`."
            )
            sys.exit(1)

    if args.model is None:
        print("No model specified or found. Please provide a model using `--model`.")
        sys.exit(1)

    if not check_chat_template(args.model):
        print(
            "\nWARNING It is recommended to use the `Chat` or `Instruct` model for benchmarking.\n"
            "Because when the tokenizer counts the output tokens, if there is gibberish, it might count incorrectly.\n"
        )

    print(f"{args}\n")

    # Read dataset
    backend = args.backend
    model_id = args.model
    tokenizer_id = args.tokenizer if args.tokenizer is not None else args.model

    tokenizer = get_tokenizer(tokenizer_id)

    with open(args.config_file) as f:
        config = yaml.safe_load(f)

    workloads = prepare_workloads(
        config, tokenizer, args.num_prompts, args.request_rate
    )

    if not args.multi:
        return asyncio.run(
            benchmark(
                backend=backend,
                api_url=api_url,
                model_id=model_id,
                tokenizer=tokenizer,
                workloads=workloads,
                window=args.window,
                disable_tqdm=args.disable_tqdm,
                extra_request_body=extra_request_body,
                log_initial_run=args.log_initial_run,
                request_collection_window_start_time=args.request_collection_window_start_time,
                request_collection_window=args.request_collection_window,
            )
        )
    else:
        # Benchmark multiple rps. TODO: use a fixed duration to compute num_prompts
        assert False, "Not implemented yet."


def set_ulimit(target_soft_limit=65535):
    resource_type = resource.RLIMIT_NOFILE
    current_soft, current_hard = resource.getrlimit(resource_type)

    if current_soft < target_soft_limit:
        try:
            resource.setrlimit(resource_type, (target_soft_limit, current_hard))
        except ValueError as e:
            print(f"Fail to set RLIMIT_NOFILE: {e}")


if __name__ == "__main__":
    parser = ArgumentParser(description="Benchmark the online serving throughput.")
    bench_args = BenchmarkingArgs.add_args(parser)
    run_benchmark(bench_args)