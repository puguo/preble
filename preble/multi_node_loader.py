from model_runtime_manager import ModelDetails
from typing import DefaultDict, List
from collections import defaultdict
import signal
import sys

from sglang.srt.managers.router.model_runner import GPUConfig

'''
class GPUConfig:
    def __init__(
        self, gpu_id, url=None, use_ssh=False, ssh_config={}, vllm_config=None
    ) -> None:
        self.gpu_id = gpu_id
        self.url = url
        self.use_ssh = use_ssh
        self.ssh_config = ssh_config
        self.vllm_config = vllm_config

    def __repr__(self) -> str:
        return f"GPUConfig(gpu_id={self.gpu_id}, url={self.url}, use_ssh={self.use_ssh}, ssh_config={self.ssh_config})"
'''

class MultiNodeLoader:
    def __init__(self, server_args=None, simulate=False) -> None:
        self.server_args = server_args
        self.simulate = simulate
        self.models_allocated = []
        self.gpus_to_model_allocated: DefaultDict[int, List[ModelDetails]] = (
            defaultdict(list)
        )
        signal.signal(signal.SIGINT, self.runtime_cleanup_handler)

    def runtime_cleanup_handler(self, sig, frame):
        print("You pressed Ctrl+C! Shutting down all remote servers...")
        for instance in self    .models_allocated:
            for runtime_instance in instance.runtimes:
                runtime_instance.shutdown()
        sys.exit(0)

    def load_model(self, model_path, gpu_configs=[]) -> ModelDetails:
        """
        Load a model onto the specified gpus

        Note: Could manage this directly in python but SGLang uses global variables
        There's also a question on how to unload memory
        """
        model_details = ModelDetails(model_path, gpu_configs, self.simulate)
        model_details.load_runtimes(
            model_path=model_path, gpu_configs=gpu_configs
        )
        # TODO verify if the memory is available
        self.models_allocated.append(model_details)
        print(f"Loaded model {model_path} on GPUs {[gpuc.gpu_id for gpuc in gpu_configs]}")
        for gpuconfig in gpu_configs:
            self.gpus_to_model_allocated[gpuconfig.gpu_id].append(model_details)
        # for gpu in , urls=url:
        #     self.gpus_to_model_allocated[gpu].append(model_details)
        return model_details

    def unload_model(self, model_details: ModelDetails):
        """
        Unload a model from the gpus
        """
        for runtime in model_details.runtimes:
            runtime.shutdown()
        if model_details in self.models_allocated:
            self.models_allocated.remove(model_details)

        # for gpu in model_details.gpus:
        #     self.gpus_to_model_allocated[gpu].remove(model_details)
        #     self.update_gpu_memory_usage(gpu)
        model_details.runtimes = []
        # model_details.gpus = []
        return model_details


    # Load a new instance on a specific GPU
    def load_instance(self, model_path, gpu_id) -> ModelDetails:
        gpu_config = GPUConfig(gpu_id=gpu_id,url=None, use_ssh=False, runtime_args=self.server_args)
        model_details = ModelDetails(model_path, [gpu_config], self.simulate)
        model_details.load_runtimes(model_path=model_path, gpu_configs=[gpu_config])

        self.models_allocated.append(model_details)
        self.gpus_to_model_allocated[gpu_id].append(model_details)

        print(f"Loaded new instance on GPU {gpu_id}")
        return model_details

    # Unload a specific instance from a GPU
    def unload_instance(self, gpu_id):
        
        popped = False
        for model_details in self.models_allocated:
            for gpu_config in model_details.gpu_configs:
                if gpu_config.gpu_id == gpu_id:
                    model_details.gpu_configs.remove(gpu_config)
                    popped = True 
            for runtime in model_details.runtimes:
                if runtime.gpu == gpu_id:
                    runtime.shutdown()
            model_details.runtimes = [runtime for runtime in model_details.runtimes if runtime.gpu != gpu_id]

       
        if popped:
            print(f"Unloaded instance from GPU {gpu_id}")
        else:
            print(f"No model instances found on GPU {gpu_id} to unload.")
        return model_details