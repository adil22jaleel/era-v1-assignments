from pathlib import Path

def get_config():
    return {
        "batch_size": 128,
        "num_epochs": 50,
        "lr": 10**-3,
        "seq_len": 160,
        "d_model": 512,
        "d_ff": 128,
        "lang_src": 'en',
        "lang_tgt": 'fr',
        "model_folder": "weights",
        "model_basename": "tmodel_",
        "preload": False,
        "tokenizer_file": "tokenizer_{0}.json",
        "experiment_name": "runs/tmodel",
        "ds_mode": "not_disk",
        "ds_path": "/home/e183534/OpusBooks",
        "save_ds_to_disk": False,
    }

def get_weights_file_path(config, epoch:str):
    model_folder = config['model_folder']
    model_basename = config['model_basename']
    model_filename = f"{model_basename}{epoch}.pt"
    return str(Path('.') / model_folder / model_filename)