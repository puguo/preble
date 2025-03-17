from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Union
import json
import logging
import asyncio
import time
import numpy as np
import traceback
import sys
import aiohttp
import random
import os
import fire
import uuid
from transformers import AutoTokenizer
from typing import List, Optional
import uvicorn
from sglang.srt.managers.router.model_runner import GPUConfig
from pynvml import nvmlInit, nvmlDeviceGetHandleByIndex, nvmlDeviceGetUtilizationRates, nvmlDeviceGetMemoryInfo, nvmlShutdown
import random
from dataclasses import dataclass, field
import requests

random.seed(10)
np.random.seed(10)

# Add the parent directory of the 'src' directory to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_parallel_request_cache import DataParallelRequestRouter, CustomPolicyType, DataParallelRuntimeSelectionPolicy

from model_runtime_manager import remove_prefix
from preble.benchmarks.benchmark_utils import RequestFuncOutput
from global_scheduler_with_time import GlobalSchedulerWithTime
from multi_node_loader import MultiNodeLoader
from transformers import (
    AutoTokenizer,
    PreTrainedTokenizer,
    PreTrainedTokenizerBase,
    PreTrainedTokenizerFast,
)
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
import glog
logger = logging.getLogger(__name__)


util_list = []

# Defines parameters for sampling during text generation, 
# including token limits, temperature, penalties
class SamplingParams(BaseModel):
    max_new_tokens: int = 16
    stop: Optional[Union[str, List[str]]] = None
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = -1
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    ignore_eos: bool = False
    skip_special_tokens: bool = True
    dtype: Optional[str] = None
    regex: Optional[str] = None

class GenerateReqInput(BaseModel):
    text: str
    input_ids: Optional[List[int]]
    sampling_params: SamplingParams
    stream: bool = True

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
    

class MetricForSchedule:
    def __init__(self, tokenizer=None, backend=None, ttft_slo=None, tpot_slo=None):
        self.input_request_list = []
        self.output_list = []
        self.start_time = time.time()
        self.end_time = None
        self.dur_list = []
        self.dur_s = 0
        self.tokenizer = tokenizer
        self.backend = backend

    def calculate_metrics(self, window=20)->Tuple[BenchmarkMetrics, List[int]]:

        if len(self.output_list) < window:
            window = len(self.output_list)
        
        if len(self.output_list) == 0:
            glog.warning("No requests have been processed yet.", stacklevel=2)
            return None, None

        output_list = self.output_list[-window:]
        input_request_list = self.input_request_list[-window:]
        dur_s = np.sum(self.dur_list[-window:]) 

        if (dur_s < 1e-6):
            glog.info(f"len of dur_list: {len(self.dur_list)}")
            glog.warning("Duration is less than 1e-6. ", stacklevel=2)
            return None, None

        output_lens: List[int] = []
        retokenized_output_lens: List[int] = []
        total_input = 0
        completed = 0
        itls: List[float] = []
        tpots: List[float] = []
        ttfts: List[float] = []
        e2e_latencies: List[float] = []
        for i in range(len(output_list)):
            if output_list[i].success:
                output_len = output_list[i].output_len
                output_lens.append(output_len)
                retokenized_output_len = len(
                    self.tokenizer.encode(output_list[i].generated_text, add_special_tokens=False)
                )
                retokenized_output_lens.append(retokenized_output_len)
                total_input += input_request_list[i][1]
                if output_len > 1:
                    tpots.append((output_list[i].request_latency - output_list[i].ttft) / (output_len - 1))
                itls += output_list[i].itl
                ttfts.append(output_list[i].ttft)

                e2e_latencies.append(output_list[i].request_latency)
                completed += 1
            else:
                output_lens.append(0)
                retokenized_output_lens.append(0)
        if completed == 0:
            glog.warning(
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
        )

        glog.info(f"Metrics: {metrics}")
        glog.info(f"Output lens: {output_lens}")
        return metrics, output_lens



def calculate_metrics(
    input_requests: List[Tuple[str, int, int]],
    outputs: List[RequestFuncOutput],
    dur_s: float,
    tokenizer: PreTrainedTokenizerBase,
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
        glog.warning(
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
    )
    glog.info(f"Metrics: {metrics}")
    glog.info(f"Output lens: {output_lens}")

    return metrics, output_lens

def process_stream_output(chunk: dict, output: RequestFuncOutput, **kwargs):
    current_experiment_state_time = kwargs['current_experiment_state_time']
    output.generated_text += chunk["text"]
    output.output_len = chunk['meta_info']['completion_tokens']
    output.arrival_time = chunk['meta_info']['arrival_time'] - current_experiment_state_time
    output.append_to_queue_time = chunk['meta_info']['append_to_queue_time'] - current_experiment_state_time

async def async_send_request(
    text=None, input_ids=None, payload=None, runtime_id=None, runtime_url=None, rid=None
):
    start_time = time.time()
    st = time.perf_counter()
    scheduling_overhead = time.time() - start_time # Why is this here?
    api_url = runtime_url

    # Initialize the output object to store metrics and response data
    output = RequestFuncOutput()
    output.rid = rid
    output.prompt_text = text[:20]
    output.prompt_len = len(input_ids)
    output.runtime_selected = runtime_id
    timeout = aiohttp.ClientTimeout(total=3 * 3600)

    # Send the request to the runtime
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ttft = 0
        most_recent_timestamp = st
        try:
            async with session.post(url=api_url, json=payload) as response:
                if response.status == 200:
                    async for chunk in response.content:
                        yield chunk
                        chunk = chunk.strip()
                        if not chunk:
                            continue
                        
                        # process for streaming results
                        chunk = remove_prefix(chunk.decode("utf-8"), "data:").strip()
                        if chunk == "[DONE]":
                            output.success = True
                            break
                        else:
                            data = json.loads(chunk)
                            timestamp = time.perf_counter()
                            # First token
                            if ttft == 0:
                                ttft = time.perf_counter() - st
                                output.ttft = ttft

                            # Decoding phase
                            else:
                                output.itl.append(timestamp - most_recent_timestamp)

                            most_recent_timestamp = timestamp
                            process_stream_output(data, output, current_experiment_state_time=st)
                        output.request_latency = time.perf_counter() - st
                else:
                    output.error = response.reason
                    output.success = False
        except Exception:
            output.success = False
            exc_info = sys.exc_info()
            output.error = "".join(traceback.format_exception(*exc_info))

    #  throughput as token generated per second
    output.scheduling_overhead = scheduling_overhead
    if output.success:
        output.tpot = (output.request_latency - output.ttft) / max(1, output.output_len)
    yield output

# Coordinates request processing:
# Sends the request to a runtime selected by the router.
# Uses async_send_request() to handle communication with the runtime.
# Returns the response as a streaming output.
async def generate_request_helper(obj: GenerateReqInput):
    request_start = time.perf_counter()
    request_id = str(uuid.uuid4())
    runtime_events[request_id] = (asyncio.Event(), None)
    
    
    queue_start = time.perf_counter()
    await runtime_request_queue.put((obj, request_id))
    queue_time = time.perf_counter() - queue_start
    
    import glog 
    queue_items = await peek_queue(runtime_request_queue)
    await runtime_events[request_id][0].wait()

    runtime_id = runtime_events[request_id][1]
    runtime_events.pop(request_id)

    if runtime_id is None:
        raise HTTPException(status_code=500, detail="Runtime selection failed")
    
    url = runtimes[runtime_id]
    rid = str(uuid.uuid4())
    payload = {
        "text": obj.text,
        # "input_ids": obj.input_ids, TODO some systems support directly sending input ids
        "sampling_params": obj.sampling_params.dict(),
        "rid": rid,
        "stream": True
    }
    text = obj.text
    input_ids = obj.input_ids
   # glog.info(f"text: {text}")
   # glog.info(f"input_ids: {input_ids}")
    async def get_requests():
        processing_start = time.perf_counter()
        async for chunk in async_send_request(text, input_ids, payload, runtime_id, url, rid):
            if isinstance(chunk, RequestFuncOutput):
                break
            yield chunk
        output = chunk
        end_time = time.perf_counter()
   #     glog.info(f"Output: {output}")
        metric_collector_list[runtime_id].output_list.append(output)
        metric_collector_list[runtime_id].input_request_list.append((text, len(input_ids), len(output.generated_text)))
        metric_collector_list[runtime_id].dur_list.append(end_time - processing_start)
        await finished_requests_queue.put((output, text, input_ids))
       # metric_collector_list[runtime_id].calculate_metrics()

    
    return StreamingResponse(get_requests(), media_type="text/event-stream")
# The main API endpoint handler for incoming /generate requests
# Parses the request JSON.
# Tokenizes the input if needed.
# Delegates the request to generate_request_helper().
async def process_req(request: Request):
    
    try:
        obj = await request.json()
        text = obj.pop("prompt", "")
        sp = SamplingParams(**obj)
        stream = obj.pop("stream",True)
        generate_req_input = GenerateReqInput(text=text,input_ids=[],sampling_params=sp,stream=stream)
        # if text doesn't have tokenization/tokenize the input here
        if not generate_req_input.input_ids or len(generate_req_input.input_ids) == 0:
            generate_req_input.input_ids = tokenizer.encode(generate_req_input.text)
        return await generate_request_helper(generate_req_input)
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)

async def peek_queue(runtime_request_queue):
    import glog
  #  glog.info(f"Current gpu queue length:{runtime_request_queue.qsize()}")
    items = list(runtime_request_queue._queue)
    return items

#Picks a suitable runtime for each queued request using the request_router.
async def process_runtime_selection(global_scheduler=None):
    while True:
        obj: GenerateReqInput
        import glog 
        queue_items = await peek_queue(runtime_request_queue)
      #  glog.info(f"Queue items: {queue_items}")
        obj, request_id = await runtime_request_queue.get()
        text, input_ids, sampling_params = obj.text, obj.input_ids, obj.sampling_params
        
        

        sampling_params = sampling_params.dict()
        # hit_rates = [r.hit_ratio for r in runtimes] # 
        # hit_rates = [0 for _ in runtimes] # TODO handle hitrates
        # highest_idx = int(np.argmax(hit_rates))
        # if (highest_idx is not None) and (hit_rates[highest_idx] < 0.7):
        #     highest_idx = None
        highest_idx = None
        hit_rates = [0 for _ in runtimes] # TODO add hot/cold support
        
        loaded_gpu_list = global_scheduler.per_gpu_load.keys()
        try:
            runtime_id = request_router.select_runtime(text=text, experiment_id="1", input_ids=input_ids, request_id=request_id, sampling_params=sampling_params, runtime_id_with_highest_hit_rate=highest_idx, hit_rates=hit_rates, loaded_gpu_list=loaded_gpu_list)
            runtime_events[request_id] = (runtime_events[request_id][0], runtime_id)
        except Exception as e:
            logger.error(f"Error selecting runtime: {e}")
            runtime_events[request_id] = (runtime_events[request_id][0], None)
        finally:
            runtime_events[request_id][0].set()
            runtime_request_queue.task_done()

#Informs the request router when a request has finished.
async def process_cleanup_selection():
    while True:
        output_obj, text, input_ids = await finished_requests_queue.get()
    #    glog.info(f"Finished request: {output_obj}")
     #   glog.info(f"text: {text}")
     #   glog.info(f"input_ids: {input_ids}")
        request_router.finish_request(text=text, input_ids=input_ids, func_output=output_obj, experiment_id="exp_id", request_id="rid")


async def add_gpu_instance(global_scheduler):
    global model_details
    print(f"Current per_gpu_load: {global_scheduler.per_gpu_load}", flush=True)
    print(f"Current all_possible_gpus: {all_possible_gpus}", flush=True)
    available_gpus = [gpu_id for gpu_id in all_possible_gpus if gpu_id not in global_scheduler.per_gpu_load]
    if available_gpus:
        gpu_id = available_gpus[0]
        print(f"Adding GPU instance: GPU {gpu_id}", flush=True)
        loader.load_instance(
            model_path=model_details.model_path,
            gpu_id=gpu_id
        )
        global_scheduler.per_gpu_load[gpu_id] = 0
        print(f"Added GPU {gpu_id}, current GPU num:{global_scheduler.num_gpus}", flush=True)
    else:
        print("No available GPUs to scale up.", flush=True)

    

async def remove_gpu_instance(global_scheduler, gpu_id):
    loader.unload_instance(gpu_id)
    print(f"Removed GPU {gpu_id}", flush=True)
    global_scheduler.per_gpu_load.pop(gpu_id)

SCALE_OUT_THRESHOLD = 80
SCALE_IN_THRESHOLD = 20
MIN_GPU_INSTANCES = 2
overloaded_instances = set()
underloaded_instances = set()



async def test_monitor_and_autoscale(global_scheduler, initial_gpu_num=2, random_seed=12345):
    """
    Tests the autoscaling functionality by simulating GPU load and observing
    scale-up and scale-down behavior.

    Args:
        global_scheduler: The GlobalSchedulerWithTime instance to test.
        initial_gpu_num (int): The initial number of GPUs to simulate.
        target_utilization (int): The target GPU utilization percentage.
        duration (int): The duration of the test in seconds.
    """
    nvmlInit()
    random.seed(random_seed)

    sum = 0
    random_values = []
    list_length = 20  # Arbitrary length for the list
    for _ in range(list_length - 1):
        if sum == 0:
            random_values.append(random)
        elif sum == initial_gpu_num:
            random_values.append(-1)
        else:
            random_values.append(random.choice([1, -1]))
        sum += random_values[-1]
    print(f"Generated random list: {random_values}", flush=True)
    try:
        for step in len(random_values):

            current_devices = list(global_scheduler.per_gpu_load.keys())
            for gpu_id in current_devices:
                handle = nvmlDeviceGetHandleByIndex(gpu_id)
                utilization = nvmlDeviceGetUtilizationRates(handle)
                memory_info = nvmlDeviceGetMemoryInfo(handle)
                memory_used = self.get_available_gpu_memory(gpu_id)
                memory_used_percent = (memory_used / memory_info.total) * 100
                print(f"GPU {gpu_id}: Utilization: {utilization.gpu}% | Memory Used: {memory_used / (1024 ** 2):.2f} MB / {memory_info.total / (1024 ** 2):.2f} MB", flush=True)

                if random_values[step] == 1:
                    if gpu_id in overloaded_instances:
                        print(f"GPU {gpu_id} is consistently overloaded. Triggering scale-up.", flush=True)
                        await add_gpu_instance(global_scheduler)
                        overloaded_instances.discard(gpu_id)
                        break
                    else:
                        overloaded_instances.add(gpu_id)
                        underloaded_instances.discard(gpu_id)

                    
                elif random_values[step] == -1:
                    if gpu_id in underloaded_instances:
                        print(f"GPU {gpu_id} is consistently underloaded. Triggering scale-down.", flush=True)
                        await remove_gpu_instance(global_scheduler, gpu_id)
                        underloaded_instances.discard(gpu_id)
                        await asyncio.sleep(50)
                            
                        break
                            
                    else:
                        underloaded_instances.add(gpu_id)
                        overloaded_instances.discard(gpu_id)

        pass

    except Exception as e:
        print(f"Autoscaling test failed: {e}", flush=True)
        raise
    finally:
        print("Test complete.", flush=True)
        nvmlShutdown()

async def monitor_and_autoscale(global_scheduler,
                                keyword_list=['gpu_power'],
                                scaleup_period=5,
                                scaledown_period=3,
                                period_sec=4,
                                filename='stats.txt'
                                ):
    global util_list
    global stats_list
    global stats_keep_count
    try:
        while True:
            import glog 
            queue_items = await peek_queue(runtime_request_queue)
            current_devices = list(global_scheduler.per_gpu_load.keys())
            
            scaleup_flag = False 
            scaledown_flag = False
            stats = await get_stats()
            
            avg_stat = {}
            
            for gpu_id in current_devices:
                if gpu_id in global_scheduler.per_gpu_load:
                    if avg_stat == {}:
                        avg_stat = stats[gpu_id]
                    else:
                        for key in avg_stat.keys():
                            if key != 'error' and key in stats[gpu_id] and not isinstance(avg_stat[key], list):
                                avg_stat[key] += stats[gpu_id][key]
                                
            
            for key in avg_stat.keys():
                if key != 'error' and isinstance(avg_stat[key], float):
                    avg_stat[key] = avg_stat[key] / len(list(global_scheduler.per_gpu_load.keys()))
                elif isinstance(avg_stat[key], list):
                    val = np.mean([avg_stat[key][gpu_id] for gpu_id in current_devices])
                    avg_stat[key] = val
                   
                
                    

            if  avg_stat == {} or 'error' in avg_stat:
                import random 
             #   if random.randint(0, 10) < 5:
                    
                scaledown_flag = True
            glog.info(avg_stat)
            for keyword in keyword_list:
                if keyword in avg_stat:
                    if len(stats_list[keyword]) >= scaledown_period:
                        max_latency_ms = np.max(stats_list[keyword])
                        if max_latency_ms < avg_stat[keyword]:
                            scaleup_flag = True
                            stats_keep_count[keyword] = 0
                        else:
                            stats_keep_count[keyword] += 1
                            if stats_keep_count[keyword] > scaleup_period:
                                scaledown_flag = True
                                stats_keep_count[keyword] = 0
                    if len(stats_list[keyword] >= scaleup_period):
                        stats_list[keyword].pop(0)
                    stats_list[keyword].append(stats[keyword])
            
            print(f"stats: {stats}", file=open(filename, 'a'))
            if (scaleup_flag == True):
                print(f"Scale up triggered: {scaleup_flag}", file=open(filename, 'a'))
            if (scaledown_flag == True):
                print(f"Scale down triggered: {scaledown_flag}", file=open(filename, 'a'))

            
            for gpu_id in current_devices:
                '''
                handle = nvmlDeviceGetHandleByIndex(gpu_id)
                recent_window = util_list[-20:] if util_list else []
                
                if len(recent_window) == 0:
                    utilization = nvmlDeviceGetUtilizationRates(handle).gpu
                else:
                    utilization = 0
                    num = 0
                    for util in recent_window:
                        if gpu_id in util:
                            utilization += util[gpu_id]
                            num += 1
                    utilization = utilization/num
                del util_list[:20]
                memory_info = nvmlDeviceGetMemoryInfo(handle)
                memory_used_percent = (memory_info.used / memory_info.total) * 100
                print(f"GPU {gpu_id}: Utilization: {utilization}% | Memory Used: {memory_info.used / (1024 ** 2):.2f} MB / {memory_info.total / (1024 ** 2):.2f} MB", flush=True)
                '''
                if len(list(global_scheduler.per_gpu_load.keys())) < 1 or scaleup_flag:
                    if gpu_id in overloaded_instances:
                        print(f"GPU {gpu_id} is consistently overloaded. Triggering scale-up.", flush=True)
                        await add_gpu_instance(global_scheduler)
                        overloaded_instances.discard(gpu_id)
                        break
                    else:
                        overloaded_instances.add(gpu_id)
                        underloaded_instances.discard(gpu_id)

                # Check for underload condition
                # For 
                elif len(list(global_scheduler.per_gpu_load.keys())) > 1 and scaledown_flag:
                    if gpu_id in underloaded_instances:
                        print(f"GPU {gpu_id} is consistently underloaded. Triggering scale-down.", flush=True)
                        await remove_gpu_instance(global_scheduler, gpu_id)
                        underloaded_instances.discard(gpu_id)
                        break
                    else:
                        underloaded_instances.add(gpu_id)
                        overloaded_instances.discard(gpu_id)

                # If neither overloaded nor underloaded, remove from both sets
                else:
                    overloaded_instances.discard(gpu_id)
                    underloaded_instances.discard(gpu_id)

            await asyncio.sleep(period_sec)
    finally:
        nvmlShutdown()
app = FastAPI()

@app.post("/generate")
async def generate(request: Request):
    return await process_req(request)


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

def start_server(runtime_selection_policy="custom", runtime_urls="http://127.0.0.1:30000/generate", host='127.0.0.1', port=8000, model="mistralai/Mistral-7B-v0.1", mode='regular'):
    """
    Starts the server with the specified runtime selection policy, runtime URLs, and model.

    Args:
        runtime_selection_policy (str): The policy for selecting runtimes. Can be "round_robin", "lor", or "custom". Defaults to "custom".
        runtime_urls (str): A comma-separated list of runtime URLs. Defaults to "http://127.0.0.1:30000/generate".
        host (str): The host address for the server. Defaults to '127.0.0.1'.
        port (int): The port number for the server. Defaults to 8000.
        model (str): The model name or path to be used by the server. Defaults to "mistralai/Mistral-7B-v0.1".

    Example:
        preble start_server --runtime_urls="http://127.0.0.1:30000/generate,"http://127.0.0.1:30001/generate" 
    Raises:
        ValueError: If the runtime selection policy is not valid.
    """
    global request_router
    global tokenizer
    global runtimes
    global metric_collector_list

    
    # TODO check that these urls are valid

    tokenizer = AutoTokenizer.from_pretrained(model)
    print(f"Tokenizer loaded for model {model}", flush=True)

    # Split runtime URLs into a list for multi-node support
    runtimes = runtime_urls.split(',')
    num_nodes = len(runtimes)
    metric_collector_list = [MetricForSchedule(tokenizer=tokenizer, backend="mistral") for _ in range(len(runtimes))]
    # Select runtime policy based on provided input
    if runtime_selection_policy == "round_robin":
        runtime_selection_policy = DataParallelRuntimeSelectionPolicy.ROUND_ROBIN
    elif runtime_selection_policy == "lor":
        runtime_selection_policy = DataParallelRuntimeSelectionPolicy.LEAST_OUTSTANDING_REQUESTS
    elif runtime_selection_policy == "custom":
        runtime_selection_policy = DataParallelRuntimeSelectionPolicy.CUSTOM
    else:
        raise ValueError("Invalid runtime selection policy")

    # init scheduler and routers
    global_scheduler = GlobalSchedulerWithTime(num_nodes=num_nodes, enable_eviction=True)
    request_router = DataParallelRequestRouter(
        runtime_selection_policy, total_nodes=num_nodes
    )
    request_router.custom_selector = global_scheduler
    nvmlInit()

    # Define the main async loop to start background tasks and the server
    logger.info(f"Starting server... port {port}, host {host}")
    async def main():
        loop.create_task(process_runtime_selection(global_scheduler))
        loop.create_task(process_cleanup_selection())
        if mode == 'test':
            loop.create_task(test_monitor_and_autoscale(global_scheduler))
        else:
            loop.create_task(monitor_and_autoscale(global_scheduler))
        loop.create_task(record_gpu_metrics(global_scheduler))
        config = uvicorn.Config(app=app, loop="asyncio", host=host, port=port)
        server = uvicorn.Server(config)
        await server.serve()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

async def get_stats():
    global runtime_url_list
    result_json_list = []
    async with aiohttp.ClientSession() as session:
        for runtime_url in runtime_url_list:
            try:
                async with session.post(f"{runtime_url}/get_stats") as response:
                    if response.status == 200:
                        result_json_list.append(await response.json())
                    else:
                        print(f"Error: {response.status} for {runtime_url}", flush=True)
                        result_json_list.append({"error": f"Error: {response.status} for {runtime_url}"})
            except aiohttp.ClientConnectionError as e:
                print(f"Connection error for {runtime_url}: {e}", flush=True)
                result_json_list.append({"error": f"Connection error for {runtime_url}: {e}"})
            except Exception as e:
                print(f"An unexpected error occurred for {runtime_url}: {e}", flush=True)
                result_json_list.append({"error": f"An unexpected error occurred for {runtime_url}: {e}"})
    return result_json_list

async def record_gpu_metrics(global_scheduler):
    global util_list
    nvmlInit()
    while True:
        to_add = {}
        current_devices = list(global_scheduler.per_gpu_load.keys())
        for gpu_id in current_devices:
            handle = nvmlDeviceGetHandleByIndex(gpu_id)
            utilization = nvmlDeviceGetUtilizationRates(handle).gpu
            memory_info = nvmlDeviceGetMemoryInfo(handle)
            memory_used_percent = (memory_info.used / memory_info.total) * 100
            to_add[gpu_id] = utilization
        util_list.append(to_add)
        #print(f"Utilization record: active_gpu: {list(to_add.keys())}, utilizations: {list(to_add.values())}", flush=True)
        response_stats = await get_stats()
       # glog.info(f"Stats: {response_stats}")
       # await asyncio.sleep(1)
            

def get_available_gpu_memory(gpu_id, distributed=False):
    """
    Get available memory for cuda:gpu_id device.
    When distributed is True, the available memory is the minimum available memory of all GPUs.
    """
    num_gpus = torch.cuda.device_count()
    assert gpu_id < num_gpus

    if torch.cuda.current_device() != gpu_id:
        print(
            f"WARNING: current device is not {gpu_id}, but {torch.cuda.current_device()}, ",
            "which may cause useless memory allocation for torch CUDA context.",
        )

    free_gpu_memory, _ = torch.cuda.mem_get_info(gpu_id)

    return free_gpu_memory / (1 << 20)

def start_server_and_load_models(model_name="mistralai/Mistral-7B-v0.1", devices=[0], all_gpus=[0],host="127.0.0.1", port=8000, mode='regular'):
    print('Starting server and loading models', flush=True)
    """
    Loads the specified model onto the given devices and starts the server.

    Args:
        model_name (str): The name or path of the model to be loaded. Defaults to "mistralai/Mistral-7B-v0.1".
        devices (list): A list of GPU device IDs to load the model onto. Defaults to [0, 1].
        host (str): The host address for the server. Defaults to '127.0.0.1'.
        port (int): The port number for the server. Defaults to 8000.
    
    Example: preble deploy_and_run
    
    Raises:
        KeyboardInterrupt: If the server is interrupted, it unloads the model.
    """
    global stats_list
    global stats_keep_count
    global runtime_url_list
    stats_list = {
        'output_throughput': [],
        'gpu_power': [],
        'gpu_mem_used': [],
        'gpu_utils': [],
        'p99_ttft_ms': [],
        'p99_tpot_ms': [],
        'p99_itl_ms': [],
        'p99_e2e_latency_ms': [],
        
    }
    stats_keep_count = {
        'output_throughput': 0,
        'gpu_power': 0,
        'gpu_mem_used': 0,
        'gpu_utils': 0,
        'p99_ttft_ms': 0,
        'p99_tpot_ms': 0,
        'p99_itl_ms': 0,
        'p99_e2e_latency_ms': 0,
    }
    
    
    server_args = {
        'log_prefix_hit': True,
        'mem_fraction_static': 0.8,
        'context_length': 32768,
        "enable_flashinfer": True,
        'schedule_heuristic': 'fcfs-mpq',
        "chunk_prefill_budget": 512,
        'report_hit_ratio': True ,
        'enable_iterative_eviction': True,
    }
    # GPU Configuration
    global all_possible_gpus, loader
    all_possible_gpus = all_gpus
    gpu_configs = [
        GPUConfig(gpu_id=device, url=None, use_ssh=False, runtime_args=server_args)
        for device in devices
    ]
    global model_details
    loader = MultiNodeLoader(server_args=server_args)
    model_details = loader.load_model(
        model_path=model_name,
        gpu_configs=gpu_configs,
    )
    runtimes = []
    for runtime in model_details.runtimes:
        runtimes.append(runtime.generate_url)
        
    runtime_url_list = [runtime.generate_url[:runtime.generate_url.rfind('/')] for runtime in model_details.runtimes]
    print(f"Loading runtimes at {runtimes}", flush=True)
    try:
        start_server(runtime_selection_policy="custom", runtime_urls=",".join(runtimes), model=model_name, host=host, port=port, mode=mode)
    except KeyboardInterrupt:
        print("Unloading model", flush=True)
        loader.unload_model(model_details)

runtime_events = {}
runtime_request_queue = asyncio.Queue()
finished_requests_queue = asyncio.Queue()
request_router = None

def main():
    fire.Fire({
        "run": start_server,
        "deploy_and_run": start_server_and_load_models
    })

if __name__ == "__main__":
    main()
