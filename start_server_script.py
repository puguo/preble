#%%
from preble.server import server
from transformers import AutoTokenizer, AutoModelForCausalLM
def main():

    server.start_server_and_load_models(
        model_name="meta-llama/Llama-3.2-1B",
        devices=[0],
        host="127.0.0.1",
        port=8010,
    )
    # Load model directly

 #   tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-1B")
 #   model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-3.2-1B")
if __name__ == '__main__':
    main()



#instructions to enable it:
#