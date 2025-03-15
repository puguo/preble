from preble.server.server import start_server_and_load_models

start_server_and_load_models(
    model_name="meta-llama/Llama-3.2-1B",
    devices=[0],
    host="127.0.0.1",
    port=8089
)