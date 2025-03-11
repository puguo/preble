from dataclasses import asdict
import uuid
import json
import heapq
import numpy as np
import time
import logging
import random
from typing import List, Dict, Optional
import threading


from transformers import AutoTokenizer
from preble.global_scheduler_with_time import GlobalSchedulerWithTime  # Import your scheduler
from preble.benchmarks.benchmark_utils import RequestFuncOutput, BenchmarkMetrics
from preble.benchmarks.benchmark_workload_gen import WorkloadPrefixDataLoader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Generates a unique request ID
def random_uuid_string():
    return str(uuid.uuid4().hex)

# Simulates an LLM server running on a GPU
class ServerRuntimeSimulator:
    def __init__(self, gpu_id: int, model_path: str):
        self.gpu_id = gpu_id
        self.model_path = model_path
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.utilization = 0.0  # Simulated GPU utilization (0-1)
        self.memory_usage = 0.0  # Simulated memory utilization (0-1)
        self.queue = []  # Stores inference requests
        self.local_clock = 0.0
        self.kv_cache_size = 4096  
        self.current_cache_usage = 0 
        self.cache_hit_rate = 0.5  

    def reset_clock(self):
        self.local_clock = 0.0
        
    def estimate_cache_hits(self, num_tokens: int) -> int:
        cache_utilization = self.current_cache_usage / self.kv_cache_size  

        if cache_utilization < 0.5:
            hit_rate = 0.8 
        elif cache_utilization < 0.9:
            hit_rate = 0.5 
        else:
            hit_rate = 0.2 

        estimated_hits = int(num_tokens * hit_rate)  # 估算命中 KV 数量
        return max(1, estimated_hits)  # 至少命中 1 个 token，避免 0 除异常
        
    def simulate_forward_time(self, batch_size, num_tokens, unique_kvs):
        """ Simulate forward propagation time on GPU """
        BASE_TIME = 0.005  # Base processing time per request
        TOKEN_TIME = 0.0002  # Time per token
        MEMORY_OVERHEAD = 0.00005  # Extra time for KV cache management
        
        forward_time = (
            batch_size * BASE_TIME +
            num_tokens * TOKEN_TIME +
            unique_kvs * MEMORY_OVERHEAD
        )
        #self.current_cache_usage = min(self.kv_cache_size, self.current_cache_usage + unique_kvs)
        self.current_cache_usage = max(0, min(self.kv_cache_size, self.current_cache_usage + unique_kvs - random.randint(5, 20)))
        return forward_time

    def simulate_step(self):
        """ Simulates processing of a batch of requests on this GPU. """
        if self.queue:
            batch = self.queue[:4]  # Assume max batch size of 4
            self.queue = self.queue[4:]  # Remove processed requests

            batch_size = len(batch)
            num_tokens = sum(len(req["input_ids"]) for req in batch)
            unique_kvs = min(num_tokens, self.estimate_cache_hits(num_tokens))  # Simulate KV cache efficiency
            
            forward_time = self.simulate_forward_time(batch_size, num_tokens, unique_kvs)
            time.sleep(forward_time)  # Simulate processing delay

            return batch, forward_time
        return [],0

# Schedules and manages requests in the simulation
class Simulation:
    def __init__(self, scheduler: GlobalSchedulerWithTime, runtimes: List[ServerRuntimeSimulator]):
        self.global_clock = 0.0
        self.scheduler = scheduler
        self.runtimes = runtimes
        self.events = []
        self.request_output: Dict[str, RequestFuncOutput] = {}
        self.unfinished_requests = 0

    def add_event(self, event):
        heapq.heappush(self.events, event)

    def run(self, time_limit=30):
        """ Runs the simulation until all requests are processed or time runs out. """
        last_update_time = time.time()
        update_interval = 1.0  # 每秒更新一次 GPU 负载
        start_simulation_time = time.time()  
        while self.events and self.global_clock < time_limit:
            event = heapq.heappop(self.events)
            self.global_clock = max(self.global_clock, event.time)
            event.process_event(self)
            #print(self.global_clock)
            # **每隔 1 秒更新一次 GPU 负载**
            current_time = time.time()
            if current_time - last_update_time >= update_interval:
                self.scheduler.update_gpu_utilization(self.runtimes)
                last_update_time = current_time
        total_time = time.time() - start_simulation_time
        print(f"Total Simulation Time: {total_time:.2f} seconds")
        return list(self.request_output.values())

    def initialize_requests(self, requests, rps):
        """ Initializes all requests based on the given request rate (RPS). """
        send_time = self.global_clock
        for request in requests:
            self.add_event(SendRequestEvent(send_time, request))
            send_time += np.random.exponential(1 / rps)
        self.unfinished_requests = len(requests)

class SendRequestEvent:
    def __init__(self, time: float, request):
        self.time = time
        self.request = request
    
    def __lt__(self, other):
        """ 让 heapq 知道如何比较两个事件，按照 `time` 排序 """
        return self.time < other.time

    def process_event(self, simulator: Simulation):
        """ Selects a GPU using the scheduler and submits the request. """
        scheduler = simulator.scheduler
        gpu_selected = scheduler.runtime_selector_simulator(
            text=self.request["text"],
            input_ids=self.request["input_ids"],
            sampling_params=self.request["sampling_params"],
            runtimes=simulator.runtimes
        )

        # Send request to selected GPU
        runtime = simulator.runtimes[gpu_selected]
        runtime.queue.append(self.request)

        # 计算前向传播时间
        processing_delay = runtime.simulate_forward_time(
            batch_size=1,
            num_tokens=len(self.request["input_ids"]),
            unique_kvs=min(len(self.request["input_ids"]), runtime.estimate_cache_hits(len(self.request["input_ids"])))
        )

        # **记录请求的总时延**
        request_func_output = RequestFuncOutput(
            rid=self.request["rid"],
            prompt_text=self.request["text"][:20],
            prompt_len=len(self.request["input_ids"]),
            send_out_time=self.time,
            route_dest=gpu_selected,
            runtime_selected=gpu_selected,
            max_new_tokens=self.request["sampling_params"]["max_new_tokens"],
            #request_latency=processing_delay,  # **存储处理时间**
            global_time=self.time + processing_delay,  # **存储全局时间**
            append_to_queue_time=simulator.global_clock
        )
        simulator.request_output[self.request["rid"]] = request_func_output
        
        ttft = random.uniform(0.0001, 0.0005)  # 设定一个随机的首 token 生成时间
        request_func_output.ttft = ttft  

        # **确保事件调度时间是基于 `heapq`**
        next_event_time = self.time + processing_delay  # 计算新事件的时间
        simulator.add_event(ModelStepEvent(next_event_time, gpu_selected))  

class ModelStepEvent:
    def __init__(self, time: float, runtime_id: int):
        self.time = time
        self.runtime_id = runtime_id
        
    def __lt__(self, other):
        """ 让 heapq 知道如何比较两个事件，按照 `time` 排序 """
        return self.time < other.time

    def process_event(self, simulator: Simulation):
        """ Simulates processing a request on the selected GPU. """
        runtime = simulator.runtimes[self.runtime_id]
        batch, forward_time = runtime.simulate_step()

        for request in batch:
            simulator.request_output[request["rid"]].success = True
            simulator.unfinished_requests -= 1
            request_output = simulator.request_output[request["rid"]]
            
            # **模拟生成文本**
            request_output.generated_text = "Simulated output for testing."
            request_output.output_len = len(runtime.tokenizer(request_output.generated_text).input_ids)
            
            # **更新时间**
            request_output.global_time = simulator.global_clock
            request_output.success = True
            #if request_output.ttft == 0:  # 只计算第一个 token
                #request_output.ttft = simulator.global_clock - request_output.append_to_queue_time
            request_output.request_latency = simulator.global_clock - request_output.send_out_time
            #request_output.ttft = simulator.global_clock - request_output.append_to_queue_time

            # **计算 tpot** 
            if request_output.output_len > 1:
                request_output.tpot = (request_output.request_latency - request_output.ttft) / max(1,request_output.output_len-1)

            # **更新度量**
            request_output.update_metrics(runtime.tokenizer)


        # Continue processing if more requests exist
        if runtime.queue:
            processing_delay = runtime.simulate_forward_time(
                batch_size=1, num_tokens=len(batch[0]["input_ids"]), unique_kvs=len(batch[0]["input_ids"]) // 2
            )
            simulator.global_clock += processing_delay  # 确保时间递增
            simulator.add_event(ModelStepEvent(simulator.global_clock, self.runtime_id))





if __name__ == "__main__":
    # Step 1: Initialize GPU simulators
    model_name = "meta-llama/Llama-3.2-1B"
    num_gpus = 2
    runtimes = [ServerRuntimeSimulator(gpu_id=i, model_path=model_name) for i in range(num_gpus)]

    # Step 2: Load the Scheduler
    scheduler = GlobalSchedulerWithTime(num_nodes=num_gpus)

    # Step 3: Initialize Simulation
    simulator = Simulation(scheduler, runtimes)

    # Step 4: Generate Workload
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    rps, exp_time = 8, 30
    num_requests = int(rps * exp_time)
    num_workloads = 10
    dataloader = WorkloadPrefixDataLoader(num_workloads, num_requests, tokenizer, num_in_context_examples=4, output_len=10)
    requests = dataloader.generate_workload(k=1)
    #print(requests[0])
        

    # Step 5: Run the Simulation
    simulator.initialize_requests(requests, rps)
    results = simulator.run(exp_time)
    print(results[0])
    # Step 6: Benchmark Performance
    bench_metrics = BenchmarkMetrics.gen_benchmark_metrics(
        tokenizer=tokenizer,
        req_func_outputs=results,
        overall_latency=exp_time,
        time_limit=exp_time,
        gpu_counts=num_gpus
    )

    # Step 7: Save Results
    with open("output.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=4)
    
    print("Simulation completed. Results saved in output.json")