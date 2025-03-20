#%%
from preble.server import server
from transformers import AutoTokenizer, AutoModelForCausalLM
def main():

    server.start_server_and_load_models(
        model_name="meta-llama/Llama-3.1-8B",
        devices=[0], all_gpus=[0],
        host="127.0.0.1",
        port=8010,
        mode='regular',
        cache_weight=0.7,
        queue_penalty_weight=0.3
    )
    # Load model directly

 #   tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.1-8B")
 #   model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-3.1-8B")
if __name__ == '__main__':
    main()



#instructions to enable it:
#