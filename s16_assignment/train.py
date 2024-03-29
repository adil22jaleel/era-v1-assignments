from model import build_transformer
from dataset import BilingualDataset, casual_mask
from config import get_config, get_weights_file_path

import torchtext.datasets as datasets
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split
from torch.optim.lr_scheduler import LambdaLR

import warnings
from tqdm import tqdm
import os
from pathlib import Path

from datasets import load_dataset
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.trainers import WordLevelTrainer
from tokenizers.pre_tokenizers import Whitespace

import torchmetrics
from torch.utils.tensorboard import SummaryWriter



def greedy_decode(model, source, source_mask, tokenizer_src, tokenizer_tgt, max_len, device): 
    # Greedy decoding function for generating translations
    sos_idx = tokenizer_tgt.token_to_id('[SOS]')
    eos_idx = tokenizer_tgt.token_to_id('[EOS]')
    
    # Precompute the encoder output and reuse it for every step 
    encoder_output = model.encode(source, source_mask) 
    
    # Initialize the decoder input with the sos token 
    decoder_input = torch.empty(1, 1).fill_(sos_idx).type_as(source).to(device) 
    
    while True: 
        if decoder_input.size(1) == max_len:  
            break 
        
        # build mask for target 
        decoder_mask = casual_mask(decoder_input.size(1)).type_as(source_mask).to(device) 
        
        # calculate output 
        out = model.decode(encoder_output, source_mask, decoder_input, decoder_mask) 

        # get next token 
        prob = model.project(out[:, -1])
        _, next_word = torch.max(prob, dim=1)
        decoder_input = torch.cat(
            [decoder_input, torch.empty(1, 1).type_as(source).fill_(next_word.item()).to(device)
            ], dim = 1
        )
        
        if next_word == eos_idx: 
            break 
        
    return decoder_input.squeeze(0)


def run_validation(model, validation_ds, tokenizer_src, tokenizer_tgt, max_len, 
                   device, print_msg, global_step, writer, num_examples=2):
    model.eval() 
    count = 0 
    source_texts = [] 
    expected = [] 
    predicted = [] 
    
    try: 
        # get the console window width 
        with os.popen('stty size', 'r') as console: 
            _, console_width = console.read().split() 
            console_width = int(console_width) 
    except: 
        # If we can't get the console width, use 80 as default 
        console_width = 80 
        
    with torch.no_grad(): 
        for batch in validation_ds: 
            count += 1 
            encoder_input = batch["encoder_input"].to(device) # (b, seq_len)
            encoder_mask = batch["encoder_mask"].to(device) # (b, 1, 1, seq_len) 

            # check that the batch size is 1 
            assert encoder_input.size(0) == 1, "Batch  size must be 1 for val"
            
            model_out = greedy_decode(model, encoder_input, encoder_mask, tokenizer_src, tokenizer_tgt, max_len, device)
            
            source_text = batch["src_text"][0]
            target_text = batch["tgt_text"][0] 
            model_out_text = tokenizer_tgt.decode(model_out.detach().cpu().numpy()) 
            
            source_texts.append(source_text) 
            expected.append(target_text) 
            predicted.append(model_out_text) 
            
            # Print the source, target and model output 
            print_msg('-'*console_width) 
            print_msg(f"{f'SOURCE: ':>12}{source_text}") 
            print_msg(f"{f'TARGET: ':>12}{target_text}")
            print_msg(f"{f'PREDICTED: ':>12}{model_out_text}") 
                      
            if count == num_examples: 
                print_msg('-'*console_width)
                break 
            
    if writer:
        # Compute the character error rate
        metric = torchmetrics.CharErrorRate()
        cer = metric(predicted, expected)
        writer.add_scalar('validation/cer', cer, global_step)
        writer.flush()

        # Compute the word error rate
        metric = torchmetrics.WordErrorRate()
        wer = metric(predicted, expected)
        writer.add_scalar('validation/wer', wer, global_step)
        writer.flush()

        # Compute the BLEU metric
        metric = torchmetrics.BLEUScore()
        bleu = metric(predicted, expected)
        writer.add_scalar('validation/bleu', bleu, global_step)
        writer.flush()
        
def get_all_sentences(ds, lang):
    # Helper function to get all sentences from a dataset in a specified language 
    for item in ds: 
        yield item['translation'][lang]
        
def get_or_build_tokenizer(config, ds, lang):
    # Get or build a tokenizer for a specified language 
    tokenizer_path = Path(config['tokenizer_file'].format(lang)) 
    if not Path.exists(tokenizer_path): 
        # Most code taken from: https://huggingface.co/docs/tokenizers/quicktour 
        tokenizer = Tokenizer(WordLevel(unk_token="[UNK]")) 
        tokenizer.pre_tokenizer = Whitespace() 
        trainer = WordLevelTrainer(special_tokens = ["[UNK]", "[PAD]", "[SOS]", "[EOS]"], min_frequency=2)
        tokenizer.train_from_iterator(get_all_sentences(ds, lang), trainer=trainer)
        tokenizer.save(str(tokenizer_path)) 
    else: 
        tokenizer = Tokenizer.from_file(str(tokenizer_path)) 
    return tokenizer 

def get_ds(config): 
    # It only has the train split, so we divide it overselves 
    ds_raw = load_dataset('opus_books', f"{config['lang_src']}-{config['lang_tgt']}", split='train')
    
    # Build tokenizers 
    tokenizer_src = get_or_build_tokenizer(config, ds_raw, config['lang_src'])
    tokenizer_tgt = get_or_build_tokenizer(config, ds_raw, config['lang_tgt']) 
    
    # Keep 90% for training, 10% for validation 
    train_ds_size = int(0.9* len(ds_raw))
    val_ds_size = len(ds_raw) - train_ds_size
    train_ds_raw, val_ds_raw = random_split(ds_raw, [train_ds_size, val_ds_size])
    
    ## Filtering the dataset with all English sentences with more than 150 "tokens"
    sorted_train_ds = sorted(train_ds_raw, key=lambda x: len(tokenizer_src.encode(x['translation'][config['lang_src']]).ids))
    filtered_sorted_train_ds = [item for item in sorted_train_ds if len(tokenizer_src.encode(item['translation'][config['lang_src']]).ids) < 150]
    filtered_sorted_train_ds = [item for item in filtered_sorted_train_ds if len(tokenizer_src.encode(item['translation'][config['lang_src']]).ids) > 2]
    filtered_sorted_train_ds = [item for item in filtered_sorted_train_ds if len(tokenizer_src.encode(item['translation'][config['lang_src']]).ids)+10 > len(tokenizer_tgt.encode(item['translation'][config['lang_tgt']]).ids)]
    train_ds = BilingualDataset(filtered_sorted_train_ds, tokenizer_src, tokenizer_tgt, config['lang_src'], 
                                config['lang_tgt'], config['seq_len'])
    val_ds = BilingualDataset(val_ds_raw, tokenizer_src, tokenizer_tgt, config['lang_src'], 
                                config['lang_tgt'], config['seq_len'])
    
    # Find the maximum length of each sentence in the source and target sentence 
    max_len_src = 0 
    max_len_tgt = 0 
    
    for item in ds_raw: 
        src_ids = tokenizer_src.encode(item['translation'][config['lang_src']]).ids
        tgt_ids = tokenizer_src.encode(item['translation'][config['lang_tgt']]).ids
        max_len_src = max(max_len_src, len(src_ids))
        max_len_tgt = max(max_len_tgt, len(tgt_ids))
        
    print(f'Max length of source sentence: {max_len_src}') 
    print(f'Max length of target sentence: {max_len_tgt}') 
    
    train_dataloader = DataLoader(train_ds, batch_size=config['batch_size'], shuffle=True, collate_fn=collate)
    val_dataloader = DataLoader(val_ds, batch_size=1, shuffle=True) 
    
    return train_dataloader, val_dataloader, tokenizer_src, tokenizer_tgt 

'''
Dynamic padding is important for optimizing training speed and memory usage 
when working with variable-length sequences, such as in machine translation tasks. 
It allows you to batch sequences of different lengths without wasting computation 
on unnecessary padding tokens.

'''
def collate(batch):
    #print('----------------------------------------------')
    encoder_input_max = max(x["encoder_str_length"] for x in batch)
    decoder_input_max = max(x["decoder_str_length"] for x in batch)

    encoder_inputs = []
    decoder_inputs = []
    encoder_mask = []
    decoder_mask = []
    label = []
    src_text = []
    tgt_text = []

    for b in batch:
        encoder_inputs.append(b["encoder_input"][:encoder_input_max])
        decoder_inputs.append(b["decoder_input"][:decoder_input_max])
        encoder_mask.append((b["encoder_mask"][0, 0, :encoder_input_max]).unsqueeze(0).unsqueeze(0).unsqueeze(0).int())
        decoder_mask.append((b["decoder_mask"][0, :decoder_input_max, :decoder_input_max]).unsqueeze(0).unsqueeze(0))
        label.append(b["label"][:decoder_input_max])
        src_text.append(b["src_text"])
        tgt_text.append(b["tgt_text"])
    return {
        "encoder_input":torch.vstack(encoder_inputs),
        "decoder_input":torch.vstack(decoder_inputs),
        "encoder_mask": torch.vstack(encoder_mask),
        "decoder_mask": torch.vstack(decoder_mask),
        "label":torch.vstack(label),
        "src_text":src_text,
        "tgt_text":tgt_text
    }

def get_model(config, vocab_src_len, vocab_tgt_len): 
    model = build_transformer(vocab_src_len, vocab_tgt_len, config["seq_len"], 
                              config["seq_len"], d_model=config["d_model"])
    return model 

def train_model(config):
    # Train the Transformer model 
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'device : {device}')
    Path(config['model_folder']).mkdir(parents=True, exist_ok=True) 

    train_dataloader, val_dataloader, tokenizer_src, tokenizer_tgt = get_ds(config) 
    model = get_model(config, tokenizer_src.get_vocab_size(), tokenizer_tgt.get_vocab_size()).to(device)

    # Tensorboard 
    writer = SummaryWriter(config['experiment_name']) 

    optimizer = torch.optim.Adam(model.parameters(), lr=config['lr'], eps=1e-9) 

    MAX_LR = 10**-3
    STEPS_PER_EPOCH = len(train_dataloader)
    EPOCHS = config["num_epochs"]

    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, 
                                                    max_lr=MAX_LR, 
                                                    steps_per_epoch=STEPS_PER_EPOCH, 
                                                    epochs=EPOCHS,
                                                    pct_start=0.1,
                                                    div_factor=10,
                                                    three_phase=True,
                                                    final_div_factor=10,
                                                    anneal_strategy='linear')
    
    initial_epoch = 0 
    global_step = 0 
    if config['preload']: 
        model_filename = get_weights_file_path(config, config['preload']) 
        print(f'Preloading model {model_filename}') 
        state = torch.load(model_filename) 
        model.load_state_dict(state['model_state_dict'])
        global_step = state['global_step']
        print("Preloaded")
        
    loss_fn = nn.CrossEntropyLoss(ignore_index=tokenizer_src.token_to_id('[PAD]'), label_smoothing=0.1)
    scaler = torch.cuda.amp.GradScaler()
    lr = [0.0]
    for epoch in range(initial_epoch, EPOCHS): 
        loss_acc = []
        torch.cuda.empty_cache() 
        model.train() 
        batch_iterator = tqdm(train_dataloader, desc=f"Training on Epoch {epoch:02d}")
        for batch in batch_iterator: 
            optimizer.zero_grad(set_to_none=True) 
            encoder_input = batch['encoder_input'].to(device) # (B, seq_len)
            decoder_input = batch['decoder_input'].to(device) # (B, seq_len) 
            encoder_mask = batch['encoder_mask'].to(device) # (B, 1, 1, seq_len) 
            decoder_mask = batch['decoder_mask'].to(device) # (B, 1, seq_len,-seq_len) 
            
            # Run the tensors through the encoder, decoder and the projection layer 
            with torch.autocast(device_type='cuda', dtype=torch.float16):
                encoder_output = model.encode(encoder_input, encoder_mask) # (B, seq_len, d_model) 
                decoder_output = model.decode(encoder_output, encoder_mask, decoder_input, decoder_mask) 
                proj_output = model.project(decoder_output) # (B, seq_len, vocab_size) 
            
                # Compare the output with the label 
                label = batch['label'].to(device) # (B, seg_len)
                
                # Compute the loss using a simple cross entropy 
                loss = loss_fn(proj_output.view(-1, tokenizer_tgt.get_vocab_size()), label.view(-1)) 
                loss_acc.append(loss)
            batch_iterator.set_postfix({"Loss_Acc": f"{torch.mean(torch.stack(loss_acc)).item():6.3f}", 
                                        "Loss": f"{loss.item():6.3f}",
                                        "Sqn_L":f"{batch['encoder_input'].shape[1]}"}) 
            
            # Log the loss 
            writer.add_scalar('Train Loss', torch.mean(torch.stack(loss_acc)).item(), global_step) 
            writer.flush() 
            
            # Backpropagate the loss 
            scaler.scale(loss).backward()
            scale = scaler.get_scale()
            scaler.step(optimizer)
            scaler.update()
            skip_lr_sched = (scale > scaler.get_scale())
            if not skip_lr_sched:
                scheduler.step()
            lr.append(scheduler.get_last_lr())
            
            
            global_step += 1 
            
        # Run validation at the end of every epoch
        run_validation(model, val_dataloader, tokenizer_src, tokenizer_tgt, config['seq_len'], device, 
                        lambda msg: batch_iterator.write(msg), global_step, writer)
        
        # Save the model at the end of every epoch 
        model_filename = get_weights_file_path(config, f"{epoch:02d}") 
        torch.save({ 
                    'epoch': epoch, 
                    'model_state_dict': model.state_dict(), 
                    'optimizer_state_dict': optimizer.state_dict(), 
                    'global_step': global_step}
                    , model_filename) 