# Adapted from https://github.com/vllm-project/vllm/blob/6366efc67b0aedd2c1721c14385370e50b297fb3/benchmarks/backend_request_func.py
# Adapted from https://github.com/vllm-project/vllm/blob/6366efc67b0aedd2c1721c14385370e50b297fb3/benchmarks/benchmark_serving.py

"""
Benchmark online serving with dynamic requests.

Usage:
python3 bench_request.py --backend vllm --num-prompts 3000 -c configs/2_tiers_config.yaml --request-rate 2 \
    --model meta-llama/Llama-3.2-1B --port 30000 --window 60

python3 -m sglang.bench_request --backend sglang --dataset-name random --num-prompts 3000 --random-input 1024 --random-output 1024 --random-range-ratio 0.5
python3 -m sglang.bench_request --backend sglang --dataset-name random --request-rate-range 1,2,4,8,16,32 --random-input 4096 --random-output 1024 --random-range-ratio 0.125 --multi
"""

import argparse
import asyncio
import json
import os
import random
import resource
import sys
import time
import traceback
import warnings
from argparse import ArgumentParser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
import subprocess
import glog 
import aiohttp
import numpy as np
import pandas as pd
import requests
from tqdm.asyncio import tqdm
from transformers import (
    AutoTokenizer,
    PreTrainedTokenizer,
    PreTrainedTokenizerBase,
    PreTrainedTokenizerFast,
)
import yaml

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=6 * 60 * 60)

global args


@dataclass
class RequestFuncInput:
    prompt: str
    api_url: str
    prompt_len: int
    output_len: int
    extra_request_body: Dict[str, Any]


@dataclass
class RequestFuncOutput:
    generated_text: str = ""
    success: bool = False
    cancelled: bool = False
    latency: float = 0.0
    ttft: float = 0.0  # Time to first token
    itl: List[float] = field(default_factory=list)  # List of inter-token latencies
    prompt_len: int = 0
    error: str = ""
    output_len: int = 0


@dataclass
class Workload:
    requests: List[Tuple[str, int, int]]
    timestamps: Union[List[float], np.ndarray]
    ttft_slo_ms: Optional[float] = None
    tpot_slo_ms: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)


class DCGMContext:

    def __init__(self, gpu_ids: int = [0], output_file: str = 'dgcm.csv') -> None:
        self.gpu_ids = gpu_ids
        self.process = None
        self.output_file = output_file
        self.fields = {
            'PROF_GR_ENGINE_ACTIVE': 1001,
            'PROF_SM_ACTIVE': 1002,
            'PROF_SM_OCCUPANCY': 1003,
            'PROF_PIPE_TENSOR_ACTIVE': 1004,
            'PROF_DRAM_ACTIVE': 1005,
        }

    def __enter__(self):
        # capture the output of the command
        fields = ','.join([str(v) for v in self.fields.values()])
        self.process = subprocess.Popen(["dcgmi", "dmon", "-e", fields, "-i", ','.join(map(str, self.gpu_ids)), '-d', '100'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.process.kill()
        # capture the output of the process
        stdout, stderr = self.process.communicate()
        lines = stdout.decode().split('\n')
        data = {k: [] for k in self.fields.keys()}
        data.update({'gpu_id': []})
        for line in lines[3:]: # skip the first line of data because they are all 0
            if line.startswith('GPU'):
                values = line.split()
                data['gpu_id'].append(values[1])
                for i, k in enumerate(self.fields.keys()):
                    data[k].append(values[i+2])  # first two strings are 'GPU' and '0'
        df = pd.DataFrame(data)
        df.to_csv(self.output_file, index=False)


class NvidiaSmiContext:

    def __init__(self, gpu_ids: int = [0], output_file: str = 'nvidia-smi.csv') -> None:
        self.gpu_ids = gpu_ids
        self.process = None
        self.output_file = output_file

    def __enter__(self):
        # with timestamp
        self.process = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,index,utilization.gpu,utilization.memory,memory.used", 
                                         "--format=csv,nounits", "-l", "1", "--id", ','.join(map(str, self.gpu_ids))],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.process.kill()
        # capture the output of the process
        stdout, stderr = self.process.communicate()
        with open(self.output_file, 'wb') as f:
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
async def async_request_openai_completions(
    request_func_input: RequestFuncInput,
    pbar: Optional[tqdm] = None,
) -> RequestFuncOutput:
    global icount
    try:
        start_time = time.perf_counter()
        # print('sending request for batch =', request_func_input.extra_request_body.get('batch', False))
        api_url = request_func_input.api_url
    

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
                            chunk = remove_prefix(chunk_bytes.decode("utf-8"), "data:")
                            latency = time.perf_counter() - st
                            if "[DONE]" in chunk:
                                pass
                            else:
                                data = json.loads(chunk)
                                # NOTE: Some completion API might have a last
                                # usage summary response without a token so we
                                # want to check a token was generated
                                if data["text"]:
                                    text = data["text"]
                                    generated_text += data["text"]

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
    "sglang": async_request_openai_completions,
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


SHAREGPT_URL = "https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json"


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


def sample_sharegpt_requests(
    dataset_path: str,
    num_requests: int,
    tokenizer: PreTrainedTokenizerBase,
    fixed_output_len: Optional[int] = None,
) -> List[Tuple[str, int, int]]:
    if fixed_output_len is not None and fixed_output_len < 4:
        raise ValueError("output_len too small")

    # Download sharegpt if necessary
    if not os.path.isfile(dataset_path):
        dataset_path = download_and_cache_file(SHAREGPT_URL)

    # Load the dataset.
    with open(dataset_path) as f:
        dataset = json.load(f)
    # Filter out the conversations with less than 2 turns.
    dataset = [data for data in dataset if len(data["conversations"]) >= 2]
    # Only keep the first two turns of each conversation.
    dataset = [
        (data["conversations"][0]["value"], data["conversations"][1]["value"])
        for data in dataset
    ]

    # Shuffle the dataset.
    random.shuffle(dataset)

    # Filter out sequences that are too long or too short
    filtered_dataset: List[Tuple[str, int, int]] = []
    for i in range(len(dataset)):
        if len(filtered_dataset) == num_requests:
            break

        # Tokenize the prompts and completions.
        prompt = dataset[i][0]
        prompt_token_ids = tokenizer.encode(prompt)
        completion = dataset[i][1]
        completion_token_ids = tokenizer.encode(completion)
        prompt_len = len(prompt_token_ids)
        output_len = (
            len(completion_token_ids) if fixed_output_len is None else fixed_output_len
        )
        if prompt_len < 4 or output_len < 4:
            # Prune too short sequences.
            continue
        if prompt_len > 1024 or (
            prompt_len + output_len > 2048 and fixed_output_len is None
        ):
            # Prune too long sequences.
            continue
        filtered_dataset.append((prompt, prompt_len, output_len))

    return filtered_dataset


def sample_random_requests(
    input_len: int,
    output_len: int,
    num_prompts: int,
    range_ratio: float,
    tokenizer: PreTrainedTokenizerBase,
    dataset_path: str,
) -> List[Tuple[str, int, int]]:

    input_lens = np.random.randint(
        max(int(input_len * range_ratio), 1),
        input_len + 1,
        size=num_prompts,
    )
    output_lens = np.random.randint(
        int(output_len * range_ratio),
        output_len + 1,
        size=num_prompts,
    )

    if True:
        # Sample token ids from ShareGPT and repeat/truncate them to satisfy the input_lens

        # Download sharegpt if necessary
        if not os.path.isfile(dataset_path):
            dataset_path = download_and_cache_file(SHAREGPT_URL)

        # Load the dataset.
        with open(dataset_path) as f:
            dataset = json.load(f)
        # Filter out the conversations with less than 2 turns.
        dataset = [data for data in dataset if len(data["conversations"]) >= 2]
        # Only keep the first two turns of each conversation.
        dataset = [
            (data["conversations"][0]["value"], data["conversations"][1]["value"])
            for data in dataset
        ]

        # Shuffle the dataset.
        random.shuffle(dataset)

        # Filter out sequences that are too long or too short
        input_requests: List[Tuple[str, int, int]] = []

        for i in range(num_prompts):
            # Tokenize the prompts and completions.
            prompt = dataset[i][0]
            prompt_token_ids = tokenizer.encode(prompt)
            prompt_len = len(prompt_token_ids)

            if prompt_len > input_lens[i]:
                input_ids = prompt_token_ids[: input_lens[i]]
            else:
                ratio = (input_lens[i] + prompt_len - 1) // prompt_len
                input_ids = (prompt_token_ids * ratio)[: input_lens[i]]
            prompt = tokenizer.decode(input_ids)
            input_requests.append((prompt, int(input_lens[i]), int(output_lens[i])))
        
        print(input_ids)
    else:
        # Sample token ids from random integers. This can cause some NaN issues.
        offsets = np.random.randint(0, tokenizer.vocab_size, size=num_prompts)
        input_requests = []
        for i in range(num_prompts):
            prompt = tokenizer.decode(
                [
                    (offsets[i] + i + j) % tokenizer.vocab_size
                    for j in range(input_lens[i])
                ]
            )
            input_requests.append((prompt, int(input_lens[i]), int(output_lens[i])))

    print(f"#Input tokens: {np.sum(input_lens)}")
    print(f"#Output tokens: {np.sum(output_lens)}")
    return input_requests


def sample_loogle_requests(
    tokenizer: PreTrainedTokenizerBase,
    total_num_requests: int,
):
    from .dataset_sglang import LooGLEDataset, LooGLEDatasetType
    loogle = LooGLEDataset(loogle_dataset_type=LooGLEDatasetType.LONG_QA, tokenizer=tokenizer, total_num_requests=total_num_requests, num_patterns=12)
    requests = loogle.generate_workload(max_length=32768 - 50)
    post_processed_requests = []
    for request in requests:
        post_processed_requests.append((request["text"], len(request["input_ids"]), request['sampling_params']["max_new_tokens"]))
    return post_processed_requests[:total_num_requests]


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
        if outputs[i].success:
            output_len = outputs[i].output_len
            output_lens.append(output_len)
            retokenized_output_len = len(
                tokenizer.encode(outputs[i].generated_text, add_special_tokens=False)
            )
            retokenized_output_lens.append(retokenized_output_len)
            total_input += input_requests[i][1]
            if output_len > 1:
                tpots.append((outputs[i].latency - outputs[i].ttft) / (output_len - 1))
            itls += outputs[i].itl
            ttfts.append(outputs[i].ttft)

            e2e_latencies.append(outputs[i].latency)

            completed += 1
        else:
            output_lens.append(0)
            retokenized_output_lens.append(0)

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
        ttft_slo_attainment=np.mean([ttft <= ttft_slo for ttft in ttfts]) if ttft_slo else None,
        tpot_slo_attainment=np.mean([tpot <= tpot_slo for tpot in tpots]) if tpot_slo else None,
    )

    return metrics, output_lens


async def benchmark(
    backend: str,
    api_url: str,
    model_id: str,
    tokenizer: PreTrainedTokenizerBase,
    workloads: List[Workload],
    window: float,
    disable_tqdm: bool,
    extra_request_body: Dict[str, Any],
):
    if backend in ASYNC_REQUEST_FUNCS:
        request_func = ASYNC_REQUEST_FUNCS[backend]
    else:
        raise ValueError(f"Unknown backend: {backend}")

    print("Starting initial single prompt test run...")
    print()
    for key, val in extra_request_body.items():
        print(f"extra_request_body info: {key}:{val}")
    test_prompt, test_prompt_len, test_output_len = workloads[0].requests[0]
    test_input = RequestFuncInput(
        prompt=test_prompt,
        api_url=api_url,
        prompt_len=test_prompt_len,
        output_len=test_output_len,
        extra_request_body=extra_request_body,
    )
    print(f"Test prompt: {test_prompt}")
    print()
    if extra_request_body.get('scheduler_mode', 'DP_MODE') == 'TP_MODE':
        update_scheduler_mode_url = api_url.replace('v1/completions', 'update_scheduler_mode')
        res = requests.post(update_scheduler_mode_url, json={"scheduler_mode": "TP"})
        assert res.content.decode('utf-8') == "Updated scheduler to: TP"
    test_output = await request_func(request_func_input=test_input)
    print(f"test output: {test_output.generated_text}")
    print()
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
    for i, workload in enumerate(workloads):
        for req, ts in zip(workload.requests, workload.timestamps):
            request_timestamps.append((req, ts, i))
    request_timestamps.sort(key=lambda x: x[1])

    last_sent_time = request_timestamps[-1][1]
    if last_sent_time < window:
        print(f"WARNING: The benchmark window is longer than the last request sent time ({last_sent_time}).")

    benchmark_start_time = time.perf_counter()
    with NullContext(gpu_ids=[1], output_file=None):
        for request in request_timestamps:
            (prompt, prompt_len, output_len), ts, i = request
            request_func_input = RequestFuncInput(
                prompt=prompt,
                api_url=api_url,
                prompt_len=prompt_len,
                output_len=output_len,
                extra_request_body={
                    **extra_request_body,
                    "priority": i,
                },
            )
            while time.perf_counter() - benchmark_start_time < ts:
                await asyncio.sleep(0.001)
            if time.perf_counter() - benchmark_start_time > window:
                break
            
            tasks[i].append(
                asyncio.create_task(
                    request_func(request_func_input=request_func_input, pbar=pbar)
                )
            )

            another_request_func_input = RequestFuncInput(
                prompt=prompt,
                api_url=api_url,
                prompt_len=prompt_len,
                output_len=output_len,
                extra_request_body={
                    **extra_request_body,
                    "priority": i,
                    "batch": True
                },
            )

        # wait for the window
        while time.perf_counter() - benchmark_start_time < window:
            await asyncio.sleep(0.01)
    
    # cancel all unfinished tasks
    for jobs in tasks:
        for job in jobs:
            job.cancel()
    outputs_per_workload: List[List[RequestFuncOutput]] = [await asyncio.gather(*jobs) for jobs in tasks]
    if pbar is not None:
        pbar.close()

    benchmark_duration = time.perf_counter() - benchmark_start_time

    def print_metrics(requests, outputs, benchmark_duration, request_rate, ttft_slo_ms=None, tpot_slo_ms=None):
        metrics, output_lens = calculate_metrics(
            input_requests=requests,
            outputs=outputs,
            dur_s=benchmark_duration,
            tokenizer=tokenizer,
            backend=backend,
            ttft_slo=ttft_slo_ms / 1000,
            tpot_slo=tpot_slo_ms / 1000,
        )
        print('=' * 60)
        print("{:<40} {:<10}".format("Successful requests:", metrics.completed))
        print("{:<40} {:<10.2f}".format("Benchmark duration (s):", benchmark_duration))
        print("{:<40} {:<10.2f}".format("Request rate (rps):", request_rate))
        print("{:<40} {:<10}".format("Total input tokens:", metrics.total_input))
        print("{:<40} {:<10}".format("Total generated tokens:", metrics.total_output))
        print(
            "{:<40} {:<10}".format(
                "Total generated tokens (retokenized):", metrics.total_output_retokenized
            )
        )
        print(
            "{:<40} {:<10.2f}".format(
                "Request throughput (req/s):", metrics.request_throughput
            )
        )
        print(
            "{:<40} {:<10.2f}".format(
                "Input token throughput (tok/s):", metrics.input_throughput
            )
        )
        print(
            "{:<40} {:<10.2f}".format(
                "Output token throughput (tok/s):", metrics.output_throughput
            )
        )
        print("{s:{c}^{n}}".format(s="End-to-End Latency", n=50, c="-"))
        print(
            "{:<40} {:<10.2f}".format("Mean E2E Latency (ms):", metrics.mean_e2e_latency_ms)
        )
        print(
            "{:<40} {:<10.2f}".format(
                "Median E2E Latency (ms):", metrics.median_e2e_latency_ms
            )
        )
        print("{s:{c}^{n}}".format(s="Time to First Token", n=50, c="-"))
        print("{:<40} {:<10.2f}".format("Mean TTFT (ms):", metrics.mean_ttft_ms))
        print("{:<40} {:<10.2f}".format("Median TTFT (ms):", metrics.median_ttft_ms))
        print("{:<40} {:<10.2f}".format("P99 TTFT (ms):", metrics.p99_ttft_ms))
        if ttft_slo_ms:
            print("{:<40} {:<10.2f}".format(f"TTFT SLO ({ttft_slo_ms:2f}):", metrics.ttft_slo_attainment))
        print(
            "{s:{c}^{n}}".format(s="Time per Output Token (excl. 1st token)", n=50, c="-")
        )
        print("{:<40} {:<10.2f}".format("Mean TPOT (ms):", metrics.mean_tpot_ms))
        print("{:<40} {:<10.2f}".format("Median TPOT (ms):", metrics.median_tpot_ms))
        print("{:<40} {:<10.2f}".format("P99 TPOT (ms):", metrics.p99_tpot_ms))
        if tpot_slo_ms:
            print("{:<40} {:<10.2f}".format(f"TPOT SLO ({tpot_slo_ms:2f}):", metrics.tpot_slo_attainment))
        print("{s:{c}^{n}}".format(s="Inter-token Latency", n=50, c="-"))
        print("{:<40} {:<10.2f}".format("Mean ITL (ms):", metrics.mean_itl_ms))
        print("{:<40} {:<10.2f}".format("Median ITL (ms):", metrics.median_itl_ms))
        print("{:<40} {:<10.2f}".format("P99 ITL (ms):", metrics.p99_itl_ms))
        print("=" * 50)

        if (
            metrics.median_ttft_ms is not None
            and metrics.mean_itl_ms is not None
            and metrics.output_throughput is not None
        ):
            result = {
                "backend": args.backend,
                "window": window,
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
                "input_throughput": metrics.input_throughput,
                "output_throughput": metrics.output_throughput,
                "duration": benchmark_duration,
                "completed": metrics.completed,
                "ttft_slo_attainment": metrics.ttft_slo_attainment,
                "tpot_slo_attainment": metrics.tpot_slo_attainment,
                "ttft_slo_ms": ttft_slo_ms,
                "tpot_slo_ms": tpot_slo_ms,
                "config": workload.config,
            }
        else:
            print(f"Error running benchmark")
            print("-" * 30)
        return result

    print("\n{s:{c}^{n}}".format(s=" Serving Benchmark Result ", n=50, c="="))
    print("{:<40} {:<10}".format("Backend:", backend))
    print("{:<40} {:<10}".format("Benchmarking Window:", window))

    results = []
    for workload, outputs in zip(workloads, outputs_per_workload):
        res = print_metrics(
            workload.requests, 
            outputs, 
            benchmark_duration, 
            len(workload.timestamps) / workload.timestamps[-1],
            workload.ttft_slo_ms, 
            workload.tpot_slo_ms)
        results.append(res)

    # Determine output file name
    if args.output_file:
        output_file_name = args.output_file
    else:
        now = datetime.now().strftime("%m%d")
        if args.dataset_name == "random":
            output_file_name = f"{args.backend}_{now}_{args.num_prompts}_{args.random_input_len}_{args.random_output_len}.jsonl"
        else:
            output_file_name = f"{args.backend}_{now}_{args.num_prompts}_sharegpt.jsonl"

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
    if tag == 'code':
        trace_file = download_and_cache_file(
            'https://raw.githubusercontent.com/Azure/AzurePublicDataset/refs/heads/master/data/AzureLLMInferenceTrace_code.csv'
        )
    elif tag == 'conversation':
        trace_file = download_and_cache_file(
            'https://raw.githubusercontent.com/Azure/AzurePublicDataset/refs/heads/master/data/AzureLLMInferenceTrace_conv.csv'
        )
    else:
        raise ValueError(f"Unknown tag for azure trace: {tag}. Only `code` and `conversation` are supported.")
    df = pd.read_csv(trace_file)
    timestamps = pd.to_datetime(df['TIMESTAMP'])
    timestamps = (timestamps - timestamps[0]).dt.total_seconds().values
    timestamps = timestamps[:n_requests]
    timestamps = timestamps * total_time / timestamps[-1]
    return timestamps
    

def prepare_workloads(config, tokenizer, num_prompts, request_rate) -> List[Workload]:
    total_percentage = sum([item["request_ratio"] for item in config["priorities"]])
    num_req_highest_priority = int(num_prompts * config["priorities"][0]["request_ratio"] / total_percentage)
    total_time = num_req_highest_priority / request_rate
    workloads = []
    for item in config["priorities"]:
        n_requests = int(num_prompts * item["request_ratio"] / total_percentage)
        dataset_config = item["dataset"].copy()
        dataset_name = dataset_config.pop("name")
        if dataset_name == "sharegpt":
            input_requests = sample_sharegpt_requests(
                dataset_path='',
                num_requests=n_requests,
                tokenizer=tokenizer,
                **dataset_config,
            )
        elif dataset_name == "random":
            input_requests = sample_random_requests(
                num_prompts=n_requests,
                tokenizer=tokenizer,
                dataset_path='',
                **dataset_config,
            )
        elif dataset_name == "loogle":
            input_requests = sample_loogle_requests(
                tokenizer=tokenizer,
                total_num_requests=n_requests,
            )
        else:
            raise ValueError(f"Unknown dataset: {dataset_name}")
        # prepare for timestamps
        arrival_config = item["arrival_pattern"].copy()
        arrival_pattern = arrival_config.pop("name")
        if arrival_pattern == "poisson":
            timestamps = generate_poisson_timestamps(n_requests, total_time, **arrival_config)
        elif arrival_pattern == "azure":
            timestamps = generate_azure_timestamps(n_requests, total_time, **arrival_config)
        else:
            raise ValueError(f"Unknown arrival pattern: {arrival_pattern}")
        workloads.append(
            Workload(
                requests=input_requests,
                timestamps=timestamps,
                ttft_slo_ms=item["slo_ms"]["ttft"],
                tpot_slo_ms=item["slo_ms"]["tpot"],
                config=item,
            )
        )
    return workloads


def run_benchmark(args_: argparse.Namespace):
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
            "sglang": 30000,
            "lmdeploy": 23333,
            "vllm": 8000,
            "trt": 8000,
            "gserver": 9988,
        }.get(args.backend, 30000)

    api_url = (
        f"{args.base_url}/generate"
        if args.base_url
        else f"http://{args.host}:{args.port}/generate"
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
    
    workloads = prepare_workloads(config, tokenizer, args.num_prompts, args.request_rate)

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
        "-c", "--config-file",
        type=str,
        help="The config file for the benchmark.",
    )
    args = parser.parse_args()
    run_benchmark(args)