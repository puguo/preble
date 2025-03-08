import random
from enum import Enum, auto
from typing import List, Optional
from dataclasses import dataclass
random.seed(10)
import pandas as pd
from sglang.srt.managers.router.model_runner import GPUConfig
import threading
import numpy as np

#from preble.global_scheduler_with_time import GlobalSchedulerWithTime
import glog


@dataclass
class CustomRuntimeSelector:
    """
    Provide a input function to hash the input text.
    Can be used for testing purposes in order to deterministcally send values via an orcale
    """
    InputText = str
    NodeID = int

    num_nodes: int
    def __init__(self, num_nodes: int, enable_eviction=True, enable_rebalancing=True, enable_miss_rate=True):
        """
        Initialize the runtime selector with the given number of GPU nodes and scheduling configurations.
        """
        self.num_nodes = num_nodes
        self.scheduler = GlobalSchedulerWithTime(
            num_nodes=num_nodes, 
            enable_eviction=enable_eviction, 
            enable_rebalancing=enable_rebalancing, 
            enable_miss_rate=enable_miss_rate
        )
    def runtime_selector(self, text: InputText, request_id: str, input_ids: List, sampling_params, *args, **kwargs) -> NodeID:
        #pass
        """
        Selects the optimal GPU for processing the given request using the scheduling algorithm.
        """
        # Update GPU utilization before scheduling
        self.scheduler.update_gpu_utilization()
        
        decoding_length = sampling_params.get("max_new_tokens", sampling_params.get("max_tokens", 45))
        glog.info(f"Decoding length: {decoding_length}")

        runtime_idx = self.scheduler.runtime_selector(
            text=text,
            request_id=request_id,
            input_ids=input_ids,
            sampling_params=sampling_params,
            *args, **kwargs
        )

        # Update GPU utilization after scheduling
        self.scheduler.update_gpu_utilization()

        return runtime_idx

    def finish_request(self, text: InputText, request_id: str, input_ids: List, func_output, *args, **kwargs) -> NodeID:
        #pass
        """
        Updates the scheduler with request completion information.
        """
        self.scheduler.finish_request(
            text=text,
            request_id=request_id,
            input_ids=input_ids,
            func_output=func_output,
            *args, **kwargs
        )

        # Update GPU utilization after request completion
        self.scheduler.update_gpu_utilization()
    

class DataParallelRuntimeSelectionPolicy(Enum):
    RANDOM = auto()
    ROUND_ROBIN = auto()
    LEAST_OUTSTANDING_REQUESTS = auto()

    CUSTOM = auto()

class CustomPolicyType(Enum):
    ORACLE = auto()
    ORACLE_HOT_COLD = auto()

    TBORACLE = auto()
    TBORACLE_B = auto()
    TB_DOMAIN_ORACLE = auto()

    LPM = auto()
    GLPM = auto()

    LOOGLE_ORACLE = auto()
    VIDEO_ORACLE = auto()
    PROGRAMMING_ORACLE = auto()

    GREEDY_LP = auto()
    GREEDY_LP_OLD = auto()

    BASIC_MEM_SCHEDULER = auto()
    BASIC_MEM_SCHEDULERV2 = auto()
    BASIC_MEM_SCHEDULERV2_5 = auto()
    BasicMemSchedulerV3 = auto()

    HistogramBasedMemoryLoadScheduler = auto()
    HiostgramBasedRecompLoad = auto()
    HiostgramBasedRecompLoadWithEviction = auto()
    GlobalScheduler = auto()
    GlobalSchedulerWithoutRebalancing = auto()
    GlobalSchedulerWithoutMissRate = auto()
    GlobalSchedulerTime = auto()
    GlobalSchedulerTimeWithEviction = auto()
    GlobalSchedulerTimeWithEvictionNoRebalance = auto()

    MemSchedulerEvictBasedOnLoad = auto()
    MemSchedulerWithGlobalEviction = auto()

    VirtualenvOracle = auto()

class DataParallelRequestRouter:
    def __init__(
        self,
        runtime_selection_policy: DataParallelRuntimeSelectionPolicy,
        total_nodes=2,
        custom_runtime_selector=None,
    ):
        self.runtime_selection_policy = runtime_selection_policy
        self.custom_selector: Optional[CustomRuntimeSelector] = custom_runtime_selector
        self.total_nodes = total_nodes
        self.model_selection_stats = []
        self.outstanding_requests = [0 for _ in range(self.total_nodes)]
        self.lock = threading.Lock()
        self.counter = 0

    def least_outstanding_requests(self, resources):
        selected_resource = int(np.argmin(resources))
        return selected_resource

    def select_runtime(self, text, experiment_id, request_id, input_ids=None, sampling_params=None, current_time_stamp=None, runtime_id_with_highest_hit_rate=None, hit_rates=None, **kwargs) -> int:
        if self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.RANDOM:
            selected_runtime = random.randint(0, self.total_nodes - 1)
        elif self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.ROUND_ROBIN:
            with self.lock:
                selected_runtime = self.counter % self.total_nodes
                self.counter += 1
        elif self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.LEAST_OUTSTANDING_REQUESTS:
            selected_runtime = self.least_outstanding_requests(self.outstanding_requests)
            with self.lock:
                self.outstanding_requests[selected_runtime] += 1
        elif self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.CUSTOM and self.custom_selector:
            selected_runtime = self.custom_selector.runtime_selector(text, request_id, input_ids, sampling_params, current_time_stamp=current_time_stamp, runtime_id_with_highest_hit_rate=runtime_id_with_highest_hit_rate, hit_rates=hit_rates)
        else:
            raise NotImplementedError(f"Runtime selection policy {self.runtime_selection_policy} not implemented with {self.custom_selector}")
        self.model_selection_stats.append(
            {
                "selected_runtime": selected_runtime,
                "text": text,
                "policy": self.runtime_selection_policy.name,
                "experiment_id": experiment_id,
                "request_id": request_id,
            }
        )
        return selected_runtime

    def finish_request(self, text, experiment_id, request_id, input_ids=None, func_output=None) -> int:
        if self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.RANDOM or self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.ROUND_ROBIN:
            pass
        elif self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.LEAST_OUTSTANDING_REQUESTS:
            with self.lock:
                self.outstanding_requests[func_output.runtime_selected] -= 1
        elif self.runtime_selection_policy == DataParallelRuntimeSelectionPolicy.CUSTOM and self.custom_selector:
            self.custom_selector.finish_request(text, request_id, input_ids, func_output)
        else:
            raise NotImplementedError

    def update_runtime_selection_policy(self, runtime_selection_policy):
        self.runtime_selection_policy = runtime_selection_policy

    def get_model_selection_counts(self):
        df = pd.DataFrame(self.model_selection_stats)
        df.drop("text", axis=1, inplace=True)
        counts = df["selected_runtime"].value_counts().to_dict()
        return counts
