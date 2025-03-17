"""SRT: SGLang Runtime"""

import asyncio
import dataclasses
import json
import logging
import multiprocessing as mp
import os
import sys
import threading
import time
from typing import List, Optional, Union

# Fix a bug of Python threading
setattr(threading, "_register_atexit", lambda *args, **kwargs: None)

import aiohttp
import psutil
import requests
import uvicorn
import uvloop
import glog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from sglang.backend.runtime_endpoint import RuntimeEndpoint
from sglang.srt.constrained import disable_cache
from sglang.srt.hf_transformers_utils import get_tokenizer
from sglang.srt.managers.detokenizer_manager import start_detokenizer_process
from sglang.srt.managers.io_struct import GenerateReqInput
from sglang.srt.managers.router.manager import start_router_process
from sglang.srt.managers.tokenizer_manager import TokenizerManager
from sglang.srt.openai_api_adapter import (
    load_chat_template_for_openai_api,
    v1_chat_completions,
    v1_completions,
)
from sglang.srt.server_args import PortArgs, ServerArgs
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sglang.srt.managers.router.model_runner import GPUConfig
from sglang.srt.utils import (
    API_KEY_HEADER_NAME,
    APIKeyValidatorMiddleware,
    allocate_init_ports,
    assert_pkg_version,
    enable_show_time_cost,
    get_exception_traceback,
)
import numpy as np 
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
import glog
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = logging.getLogger('server')


app = FastAPI()
tokenizer_manager = None

'''
TODO:

add "getstats" endpoint to get the stats of the server

'''

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

@dataclass    
class MetricForSchedule:
    output_throughput: float = 0
    output_throughput_retokenized: float = 0
    p99_ttft_ms: float = 0
    p99_tpot_ms: float = 0
    p99_itl_ms: float = 0
    p99_e2e_latency_ms: float = 0
    waiting_queue_len: int = 0
    gpu_power: list = field(default_factory=list)
    gpu_mem_used: list = field(default_factory=list)
    gpu_utils: list = field(default_factory=list)
    
    def to_dict(self):
        return {
            "output_throughput": self.output_throughput,
            "output_throughput_retokenized": self.output_throughput_retokenized,
            "p99_ttft_ms": self.p99_ttft_ms,
            "p99_tpot_ms": self.p99_tpot_ms,
            "p99_itl_ms": self.p99_itl_ms,
            "p99_e2e_latency_ms": self.p99_e2e_latency_ms,
            "waiting_queue_len": self.waiting_queue_len,
            "gpu_power": self.gpu_power,
            "gpu_mem_used": self.gpu_mem_used,
            "gpu_utils": self.gpu_utils,
        }
class MetricCollector:
    def __init__(self):
        self.ts_list = []
        self.status_list = []
        self.ttft_list = []
        self.tpot_list = []
        self.itl_list = []
        self.latency_list = []
        self.output_len_list = []
        
    def cal_metrics(self, window_sec=10):
        if not self.ts_list:
            glog.warning("No requests have been processed yet.", stacklevel=2)
            return None
        
        last_ts = self.ts_list[-1]
        target_ts = last_ts - window_sec
        
        # 使用bisect_left进行二分查找
        from bisect import bisect_left
        window_start = bisect_left(self.ts_list, target_ts)
            
        if window_start >= len(self.ts_list):
            glog.info('ts_list: {}'.format(self.ts_list))
            glog.warning("No requests have been processed yet.", stacklevel=2)
            return None
        ts_list = self.ts_list[window_start:]
        ttft_list = self.ttft_list[window_start:]
        tpot_list = self.tpot_list[window_start:]
        itl_list = self.itl_list[window_start:]
        itl_list = [item for sublist in itl_list for item in sublist]
        latency_list = self.latency_list[window_start:]
        status_list = self.status_list[window_start:]
        output_len_list = self.output_len_list[window_start:]
        if len(ttft_list) == 0:
            glog.warning("No requests have been processed yet.", stacklevel=2)
            return None
        
        metric = MetricForSchedule()
        metric.output_throughput = np.sum(output_len_list) / np.sum(latency_list)
        metric.output_throughput_retokenized = np.sum(output_len_list) / np.sum(latency_list)
        metric.p99_ttft_ms = np.percentile(ttft_list, 99) * 1000
        metric.p99_tpot_ms = np.percentile(tpot_list, 99) * 1000
        metric.p99_itl_ms = np.percentile(itl_list, 99) * 1000
        metric.p99_e2e_latency_ms = np.percentile(latency_list, 99) * 1000
        metric.gpu_power = []
        metric.gpu_mem_used = []
        metric.gpu_utils = []
        return metric

    # def calculate_metrics(self, window_sec=20)->Tuple[BenchmarkMetrics, List[int]]:

    #     if len(self.output_list) < window:
    #         window = len(self.output_list)
        
    #     if len(self.output_list) == 0:
    #         glog.warning("No requests have been processed yet.", stacklevel=2)
    #         return None, None

    #     window = len(self.output_list)
    #     dur_s = 0
    #     window_start = len(self.dur_list) - 1
    #     while window_start >= 0 and dur_s < window_sec:
    #         dur_s += self.dur_list[window_start]
    #         window_start -= 1
    #     window = len(self.dur_list) - window_start - 1     
    #     if window > len(self.output_list):
    #         window = len(self.output_list)
        
    #     if window <= 0:
    #         glog.warning("No requests have been processed yet.", stacklevel=2)
    #         return None, None

    #     output_list = self.output_list[window_start:]
    #     input_request_list = self.input_request_list[window_start:]
    #     dur_s = np.sum(self.dur_list[window_start:]) 

    #     if (dur_s < 1e-6):
    #         glog.info(f"len of dur_list: {len(self.dur_list)}")
    #         glog.warning("Duration is less than 1e-6. ", stacklevel=2)
    #         return None, None

    #     output_lens: List[int] = []
    #     retokenized_output_lens: List[int] = []
    #     total_input = 0
    #     completed = 0
    #     itls: List[float] = []
    #     tpots: List[float] = []
    #     ttfts: List[float] = []
    #     e2e_latencies: List[float] = []
    #     for i in range(len(output_list)):
    #         if output_list[i].success:
    #             output_len = output_list[i].output_len
    #             output_lens.append(output_len)
    #             retokenized_output_len = len(
    #                 self.tokenizer.encode(output_list[i].generated_text, add_special_tokens=False)
    #             )
    #             retokenized_output_lens.append(retokenized_output_len)
    #             total_input += input_request_list[i][1]
    #             if output_len > 1:
    #                 tpots.append((output_list[i].request_latency - output_list[i].ttft) / (output_len - 1))
    #             itls += output_list[i].itl
    #             ttfts.append(output_list[i].ttft)

    #             e2e_latencies.append(output_list[i].request_latency)
    #             completed += 1
    #         else:
    #             output_lens.append(0)
    #             retokenized_output_lens.append(0)
    #     if completed == 0:
    #         glog.warning(
    #             "All requests failed. This is likely due to a misconfiguration "
    #             "on the benchmark arguments.",
    #             stacklevel=2,
    #         )
    #     metrics = BenchmarkMetrics(
    #         completed=completed,
    #         total_input=total_input,
    #         total_output=sum(output_lens),
    #         total_output_retokenized=sum(retokenized_output_lens),
    #         request_throughput=completed / dur_s,
    #         input_throughput=total_input / dur_s,
    #         output_throughput=sum(output_lens) / dur_s,
    #         output_throughput_retokenized=sum(retokenized_output_lens) / dur_s,
    #         mean_ttft_ms=np.mean(ttfts or 0)
    #         * 1000,  # ttfts is empty if streaming is not supported by backend
    #         median_ttft_ms=np.median(ttfts or 0) * 1000,
    #         std_ttft_ms=np.std(ttfts or 0) * 1000,
    #         p99_ttft_ms=np.percentile(ttfts or 0, 99) * 1000,
    #         mean_tpot_ms=np.mean(tpots or 0) * 1000,
    #         median_tpot_ms=np.median(tpots or 0) * 1000,
    #         std_tpot_ms=np.std(tpots or 0) * 1000,
    #         p99_tpot_ms=np.percentile(tpots or 0, 99) * 1000,
    #         mean_itl_ms=np.mean(itls or 0) * 1000,
    #         median_itl_ms=np.median(itls or 0) * 1000,
    #         std_itl_ms=np.std(itls or 0) * 1000,
    #         p99_itl_ms=np.percentile(itls or 0, 99) * 1000,
    #         mean_e2e_latency_ms=np.mean(e2e_latencies) * 1000,
    #         median_e2e_latency_ms=np.median(e2e_latencies) * 1000,
    #     )

    #     glog.info(f"Metrics: {metrics}")
    #     glog.info(f"Output lens: {output_lens}")
    #     return metrics, output_lens

metric_collector = None

@app.get("/health")
async def health() -> Response:
    """Health check."""
    return Response(status_code=200)


@app.get("/get_model_info")
async def get_model_info():
    result = {
        "model_path": tokenizer_manager.model_path,
    }
    return result


@app.get("/get_server_args")
async def get_server_args():
    return dataclasses.asdict(tokenizer_manager.server_args)


@app.get("/flush_cache")
async def flush_cache():
    await tokenizer_manager.flush_cache()
    return Response(
        content="Cache flushed.\nPlease check backend logs for more details. "
        "(When there are running or waiting requests, the operation will not be performed.)\n",
        status_code=200,
    )

_last_generate_request_result = []

@app.post("/get_stats")
async def get_stats():
    global _last_generate_request_result
    global metric_collector
    import pynvml
    try:
        pynvml.nvmlInit()
        deviceCount = pynvml.nvmlDeviceGetCount()
        gpu_utils = []
        gpu_mem_used = []
        gpu_power = []
        for i in range(deviceCount):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            power_usage = pynvml.nvmlDeviceGetPowerUsage(handle)
            gpu_utils.append(utilization.gpu)
            gpu_mem_used.append(memory_info.used)
            gpu_power.append(power_usage)
        pynvml.nvmlShutdown()
        if metric_collector is not None:
            metric = metric_collector.cal_metrics()
            if metric is None:
                return {"error": "No requests have been processed yet."}
            metric.gpu_utils = gpu_utils
            metric.gpu_mem_used = gpu_mem_used
            metric.gpu_power = gpu_power
        
        stats = metric.to_dict()
        glog.info(f"stats: {stats}")

    except ImportError:
        return {
            "error": "pynvml is not installed. Please install it with `pip install pynvml`"
        }
    
    return stats
    pass

@app.post("/generate")
async def generate_request(obj: GenerateReqInput):
    global metric_collector
    if metric_collector is None:
        metric_collector = MetricCollector()
    if obj.text is None and obj.input_ids is None:
        return JSONResponse(
            {"error": "Either text or input_ids should be provided"}, status_code=400
        )
    obj.post_init()
    logger.debug(f"{obj.text[:20]} ...")
    if obj.stream:
        import glog 
       # glog.warning('streaming\'s get stat is not supported yet')
        ts = time.perf_counter()
        
        async def stream_results():
            ttft = 0
            tpot = 0 
            
            itl = []
            output_len = 0
            most_recent_ts = ts
            success = False
            async for out in tokenizer_manager.generate_request(obj):
                if ttft == 0:
                    ttft = time.perf_counter() - ts
                    glog.info(f"out: {out}")
                    output_len += out['meta_info']['completion_tokens']
                else:
                    itl.append(time.perf_counter() - most_recent_ts)
                    most_recent_ts = time.perf_counter()
                yield f"data: {json.dumps(out, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            success = True
            request_latency = time.perf_counter() - ts
            tpot = (request_latency - ttft) / max(1, output_len)
            
            if metric_collector is not None:
                metric_collector.ttft_list.append(ttft)
                metric_collector.tpot_list.append(tpot)
                metric_collector.itl_list.append(itl)
                metric_collector.latency_list.append(request_latency)
                metric_collector.status_list.append(success)
                metric_collector.ts_list.append(ts)
                metric_collector.output_len_list.append(output_len)
                
            
            await get_stats()
        
        stream_result = stream_results()
       # glog.info(StreamingResponse(stream_result, media_type="text/event-stream"))
        
        return StreamingResponse(stream_result, media_type="text/event-stream")

    try:
        
        
        tokenizer = tokenizer_manager.tokenizer
        if tokenizer is None:
            raise ValueError("Tokenizer not initialized")
        ret = await tokenizer_manager.generate_request(obj).__anext__()
        
     #  generated_text = ret.generated_text
     #  metric_collector.input_request_list.append((text, len(input_ids), len(generated_text)))
     #  metric_collector.dur_list.append(obj.dur)
     #  metric_collector.output_list.append(ret)
        
        return ret
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# Just add request wihout expecting result
@app.post("/add_request")
async def add_request(obj: GenerateReqInput):
    obj.post_init()
    await tokenizer_manager.add_request_to_queue(obj)
    return Response(status_code=200)

@app.post("/migrate_control")
async def migrate_request(migration_target_url: str):
    await tokenizer_manager.schedule_migration_request(migration_target_url)
    return Response(status_code=200)

@app.post("/scheduling_metrics")
async def scheduling_metrics(raw_request: Request):
    """
    Returns metrics that could be used by a global data parallel scheduler.
    The output format is:
    out_dict = {
        "waiting_queue_len": int,
        "running_req_len": int,
        "prefix_match_len": int,
        "token_kv_available_size": int,
        "evicatable_size": int,
        "tree_cache_metrics_hit": int,
        "tree_cache_metrics_total": int,
        "input_len": int
    }
    """
    start_time = time.time()
    request_json = await raw_request.json()
    request = request_json
    if not tokenizer_manager:
        return {
            "status": "error",
            "message": "Tokenizer manager not initialized"
        }
    text = request.get("prompt", None)
    if text is None:
        return {
            "status": "error",
            "message": "Prompt not found in request"
        }
    request_processing_time = time.time() - start_time
    ret = await tokenizer_manager.get_scheduling_metrics(text)
    ret["request_processing_time"] = request_processing_time
    ret["return_time"] = time.time() - ret["return_time"]
    ret["total_internal_request_time"] = time.time() - start_time
    return ret

@app.post("/dump_prefix_hit_trace")
async def dump_prefix_hit_trace(fpath: str):
    """
    Ask the runtime to log prefix hit trace to the provided file path    
    """
    await tokenizer_manager.dump_prefix_hit_trace(fpath)
    return Response(status_code=200)

# {
#     'windowed': recv_obj.windowed,
#     'hit_ratio': recv_obj.hit_ratio,
# }
@app.get('/windowed_prefix_hit_ratio')
async def windowed_prefix_hit_ratio():
    return await tokenizer_manager.handle_windowed_prefix_hit_ratio()

@app.post("/v1/completions")
async def openai_v1_completions(raw_request: Request):
    return await v1_completions(tokenizer_manager, raw_request)


@app.post("/v1/chat/completions")
async def openai_v1_chat_completions(raw_request: Request):
    return await v1_chat_completions(tokenizer_manager, raw_request)


    # Non-streaming response.
    ret = await generate_request(adapted_request)
    prompt_tokens = ret["meta_info"]["prompt_tokens"]
    completion_tokens = ret["meta_info"]["completion_tokens"]
    choice_data = ChatCompletionResponseChoice(
        index=0,
        message=ChatMessage(role="assistant", content=ret["text"]),
        finish_reason=None,  # TODO(comaniac): Add finish reason.
    )
    response = ChatCompletionResponse(
        id=ret["meta_info"]["id"],
        model=request.model,
        choices=[choice_data],
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )
    return response


def launch_server(server_args: ServerArgs, pipe_finish_writer, gpu_config, model_overide_args=None):
    global tokenizer_manager
    global chat_template_name
    logging.basicConfig(
        level=os.environ.get('LOGLEVEL', 'INFO').upper()
    )

    if server_args.cuda_devices:
        os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(d) for d in server_args.cuda_devices)
        logger.info(f"Set CUDA_VISIBLE_DEVICES to {os.environ['CUDA_VISIBLE_DEVICES']}")

    logging.basicConfig(
        level=getattr(logging, server_args.log_level.upper()),
        format="%(message)s",
    )

    # Set global environments
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    if server_args.show_time_cost:
        enable_show_time_cost()
    if server_args.disable_disk_cache:
        disable_cache()
    if server_args.enable_flashinfer:
        assert_pkg_version("flashinfer", "0.0.4")
    if server_args.chat_template:
        # TODO: replace this with huggingface transformers template
        load_chat_template_for_openai_api(server_args.chat_template)

    # Allocate ports
    server_args.port, server_args.additional_ports = allocate_init_ports(
        server_args.port, server_args.additional_ports, server_args.tp_size
    )
    port_args = PortArgs(
        tokenizer_port=server_args.additional_ports[0],
        router_port=server_args.additional_ports[1],
        detokenizer_port=server_args.additional_ports[2],
        nccl_port=server_args.additional_ports[3],
        migrate_port=server_args.additional_ports[4],
        model_rpc_ports=server_args.additional_ports[5:],
    )
    logger.info(f'{server_args.url()}, ports: {port_args}')

    # Launch processes
    tokenizer_manager = TokenizerManager(server_args, port_args, model_overide_args)
    pipe_router_reader, pipe_router_writer = mp.Pipe(duplex=False)
    pipe_detoken_reader, pipe_detoken_writer = mp.Pipe(duplex=False)

    proc_router = mp.Process(
        target=start_router_process,
        args=(
            server_args,
            port_args,
            pipe_router_writer,
            model_overide_args,
            gpu_config,
        ),
    )
    proc_router.start()
    proc_detoken = mp.Process(
        target=start_detokenizer_process,
        args=(
            server_args,
            port_args,
            pipe_detoken_writer,
        ),
    )
    proc_detoken.start()

    # Wait for the model to finish loading
    router_init_state = pipe_router_reader.recv()
    detoken_init_state = pipe_detoken_reader.recv()

    if router_init_state != "init ok" or detoken_init_state != "init ok":
        proc_router.kill()
        proc_detoken.kill()
        print(
            f"Initialization failed. router_init_state: {router_init_state}", flush=True
        )
        print(
            f"Initialization failed. detoken_init_state: {detoken_init_state}",
            flush=True,
        )
        sys.exit(1)
    assert proc_router.is_alive() and proc_detoken.is_alive()

    if server_args.api_key and server_args.api_key != "":
        app.add_middleware(APIKeyValidatorMiddleware, api_key=server_args.api_key)

    print(f"Server is on port {server_args.port} on host {server_args.host} on pid {os.getpid()}")
    def _wait_and_warmup():
        headers = {}
        url = server_args.url()
        if server_args.api_key:
            headers[API_KEY_HEADER_NAME] = server_args.api_key

        # Wait until the server is launched
        for _ in range(120):
            time.sleep(0.5)
            try:
                requests.get(url + "/get_model_info", timeout=5, headers=headers)
                success = True  # Set flag to True if request succeeds
                break
            except requests.exceptions.RequestException as e:
                pass

        # Send a warmup request
        try:
            res = requests.post(
                url + "/generate",
                json={
                    "text": "Say this is a warmup request.",
                    "sampling_params": {
                        "temperature": 0,
                        "max_new_tokens": 16,
                    },
                },
                headers=headers,
                timeout=600,
            )
            assert res.status_code == 200
        except Exception as e:
            if pipe_finish_writer is not None:
                pipe_finish_writer.send(get_exception_traceback())
            print(f"Initialization failed. warmup error: {e}")
            raise e

        if pipe_finish_writer is not None:
            pipe_finish_writer.send("init ok")
            
    t = threading.Thread(target=_wait_and_warmup)
    t.start()
    try:
        uvicorn.run(
            app,
            host=server_args.host,
            port=server_args.port,
            log_level=server_args.log_level,
            timeout_keep_alive=5,
            loop="uvloop",
        )
    finally:
        t.join()


class Runtime:
    def __init__(
        self,
        model_path: str,
        gpu_config: GPUConfig,
        tokenizer_path: Optional[str] = None,
        load_format: str = "auto",
        tokenizer_mode: str = "auto",
        trust_remote_code: bool = True,
        mem_fraction_static: float = ServerArgs.mem_fraction_static,
        max_prefill_num_token: int = ServerArgs.max_prefill_num_token,
        context_length: int = ServerArgs.context_length,
        host: str = '0.0.0.0',
        tp_size: int = 1,
        schedule_heuristic: str = "lpm",
        attention_reduce_in_fp32: bool = False,
        random_seed: int = 42,
        log_level: str = "error",
        disable_radix_cache: bool = False,
        enable_flashinfer: bool = False,
        disable_regex_jump_forward: bool = False,
        disable_disk_cache: bool = False,
        api_key: str = "",
        port: Optional[int] = None,
        additional_ports: Optional[Union[List[int], int]] = None,
        cuda_devices: Optional[List[int]] = None,
        freeze: bool = False,
        log_prefix_hit: bool = False,
        chunk_prefill_budget: int = 0,
        hit_trace_window_size: int = 30,
        report_hit_ratio: bool = True,
        enable_iterative_eviction: bool = False,
        enable_partial_eviction: bool = False,
        model_overide_args: Optional[dict] = None,
        **kwargs,   # additional args not specific to sglang
    ):
        """See the arguments in server_args.py::ServerArgs"""
        self.server_args = ServerArgs(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            host=host,
            port=port,
            additional_ports=additional_ports,
            load_format=load_format,
            tokenizer_mode=tokenizer_mode,
            trust_remote_code=trust_remote_code,
            mem_fraction_static=mem_fraction_static,
            max_prefill_num_token=max_prefill_num_token,
            context_length=context_length,
            tp_size=tp_size,
            schedule_heuristic=schedule_heuristic,
            attention_reduce_in_fp32=attention_reduce_in_fp32,
            random_seed=random_seed,
            log_level=log_level,
            cuda_devices=cuda_devices,
            freeze=freeze,
            log_prefix_hit=log_prefix_hit,
            disable_radix_cache=disable_radix_cache,
            enable_flashinfer=enable_flashinfer,
            disable_regex_jump_forward=disable_regex_jump_forward,
            disable_disk_cache=disable_disk_cache,
            api_key=api_key,
            chunk_prefill_budget=chunk_prefill_budget,
            hit_trace_window_size=hit_trace_window_size,
            report_hit_ratio=report_hit_ratio,
            enable_iterative_eviction=enable_iterative_eviction,
            enable_partial_eviction=enable_partial_eviction,
            **kwargs,
        )

        # Pre-allocate ports
        self.server_args.port, self.server_args.additional_ports = allocate_init_ports(
            self.server_args.port,
            self.server_args.additional_ports,
            self.server_args.tp_size,
        )

        self.url = self.server_args.url()
        self.generate_url = (
            f"http://{self.server_args.host}:{self.server_args.port}/generate"
        )
        self.hit_ratio_url = (
            f"http://{self.server_args.host}:{self.server_args.port}/windowed_prefix_hit_ratio" 
        )

        self.pid = None
        pipe_reader, pipe_writer = mp.Pipe(duplex=False)
        proc = mp.Process(
            target=launch_server,
            args=(self.server_args, pipe_writer, gpu_config, model_overide_args),
        )
        proc.start()
        pipe_writer.close()
        self.pid = proc.pid

        try:
            init_state = pipe_reader.recv()
        except EOFError:
            init_state = ""

        if init_state != "init ok":
            self.shutdown()
            raise RuntimeError(
                "Initialization failed. Please see the error messages above."
            )

        self.endpoint = RuntimeEndpoint(self.url)

    def shutdown(self):
        if self.pid is not None:
            try:
                parent = psutil.Process(self.pid)
            except psutil.NoSuchProcess:
                return
            children = parent.children(recursive=True)
            for child in children:
                child.kill()
            psutil.wait_procs(children, timeout=5)
            parent.kill()
            parent.wait(timeout=5)
            self.pid = None

    def get_tokenizer(self):
        return get_tokenizer(
            self.server_args.tokenizer_path,
            tokenizer_mode=self.server_args.tokenizer_mode,
            trust_remote_code=self.server_args.trust_remote_code,
        )

    async def add_request(
        self,
        prompt: str,
        sampling_params,
    ):
        json_data = {
            "text": prompt,
            "sampling_params": sampling_params,
            "stream": True,
        }
        pos = 0

        timeout = aiohttp.ClientTimeout(total=3 * 3600)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(self.generate_url, json=json_data) as response:
                async for chunk, _ in response.content.iter_chunks():
                    chunk = chunk.decode("utf-8")
                    if chunk and chunk.startswith("data:"):
                        if chunk == "data: [DONE]\n\n":
                            break
                        data = json.loads(chunk[5:].strip("\n"))
                        cur = data["text"][pos:]
                        if cur:
                            yield cur
                        pos += len(cur)
    
    async def add_request_await(
        self,
        prompt: str,
        sampling_params,
    ) -> None:
        json_data = {
            "text": prompt,
            "sampling_params": sampling_params,
            "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=3 * 3600)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(self.generate_url, json=json_data) as response:
                return await response.json()

    def __del__(self):
        self.shutdown()