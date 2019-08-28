from msmarco_dataset import MsMarcoDataset
from pytorch_transformers import (BertForNextSentencePrediction,
                                  AdamW, WarmupLinearSchedule)
import random
import numpy as np
import torch
import logging
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
import multiprocessing
from sklearn.metrics import f1_score, recall_score, average_precision_score, accuracy_score
from args_parser import getArgs
import os
import sys


logger = logging.getLogger(__name__)


def init_optimizer(
        model: BertForNextSentencePrediction,
        n_steps, lr,
        warmup_proportion=0.1,
        weight_decay=0.0):

    no_decay = ['bias', 'LayerNorm.weight']

    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(
            nd in n for nd in no_decay)], 'weight_decay': weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=lr, eps=1e-8)
    warmup_steps = n_steps * warmup_proportion
    scheduler = WarmupLinearSchedule(
        optimizer, warmup_steps=warmup_steps, t_total=n_steps)
    return optimizer, scheduler


def fine_tune(
        train_dataset: MsMarcoDataset,
        dev_dataset: MsMarcoDataset,
        data_dir,
        seed: int = 42,
        limit_gpus: int = -1,
        bert_model="bert-base-uncased",
        batch_size=32,
        eval_batch_size=128,
        n_epochs=3,
        learning_rate=5e-5,
        n_workers=None,
        eval_steps=10):
    # Set random seeds
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Set CUDA
    n_gpu = 0
    if torch.cuda.is_available():
        logging.info("Using CUDA")
        torch.cuda.manual_seed_all(seed)
        if limit_gpus < 0:
            limit_gpus = torch.cuda.device_count()
        if limit_gpus > -1:
            n_gpu = min(torch.cuda.device_count(), limit_gpus)

    device = torch.device("cuda" if (torch.cuda.is_available()
                                     and n_gpu > 0 and limit_gpus != 1) else "cpu")
    logging.info("Using device {}".format(device))
    model = BertForNextSentencePrediction.from_pretrained(bert_model)
    logging.info("Model loaded")
    if n_workers is None:
        n_workers = multiprocessing.cpu_count() - 2

    if n_gpu > 0:
        gpu_ids = list(range(n_gpu))
        model = torch.nn.DataParallel(model, device_ids=gpu_ids)
        print("Using device IDs {}".format(str(gpu_ids)))

    model.to(device)
    data_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, num_workers=n_workers)
    num_train_optimization_steps = len(data_loader) // n_epochs
    optimizer, scheduler = init_optimizer(
        model, num_train_optimization_steps, learning_rate)

    logger.info("******Started trainning******")
    logger.info("   Num samples = %d", len(train_dataset))
    logger.info("   Num Epochs = %d", n_epochs)
    logger.info("   Batch size = %d", batch_size)
    logger.info("   Total optmization steps %d", num_train_optimization_steps)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    for _ in tqdm(range(n_epochs), desc="Epochs"):
        for batch in tqdm(data_loader, desc="Batches"):
            model.train()
            batch = tuple(t.to(device) for t in batch)
            inputs = {'input_ids': batch[0],
                      'attention_mask': batch[1],
                      'token_type_ids': batch[2],
                      'next_sentence_label': batch[3]}
            outputs = model(**inputs)

            loss = outputs[0]
            if n_gpu > 1:
                loss = loss.mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

            tr_loss += loss.item()
            optimizer.step()
            scheduler.step()
            model.zero_grad()
            global_step += 1
            if global_step % eval_steps == 0:
                print("Training loss: {}".format(loss))
                results = evaluate(dev_dataset,
                                   data_dir,
                                   model,
                                   device,
                                   data_dir,
                                   eval_batchsize=eval_batch_size,
                                   n_workers=n_workers)
                for key, value in results.items():
                    print('\teval_{}:\t{}'.format(key, value))
                print("\tlr: \t{}".format(scheduler.get_lr()[0]))
                print("\tLoss:\t{}".format(tr_loss - logging_loss / eval_steps))
                logging_loss = tr_loss
                # Save model
                output_dir = os.path.join(
                    data_dir, "checkpoint-{}".format(global_step))
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                model_to_save = model.module if hasattr(
                    model, 'module') else model
                model_to_save.save_pretrained(output_dir)
                torch.save(args, os.path.join(output_dir, 'training_args.bin'))
                logger.info("Saving model checkpoint to %s", output_dir)
    return global_step, tr_loss / global_step


def evaluate(eval_dataset: MsMarcoDataset,
             output_dir: str,
             model: BertForNextSentencePrediction,
             device: str,
             eval_output_dir: str,
             task_name="msmarco",
             prefix="",
             eval_batchsize=128,
             n_workers=2):
    results = {}
    eval_dataloader = DataLoader(
        eval_dataset, batch_size=eval_batchsize, shuffle=False, num_workers=n_workers)
    eval_loss = 0.0
    nb_eval_steps = 0
    preds = None
    out_label_ids = None
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        model.eval()
        batch = tuple(t.to(device) for t in batch)
        with torch.no_grad():
            inputs = {
                'input_ids': batch[0],
                'attention_mask': batch[1],
                'token_type_ids': batch[2],
                'next_sentence_label': batch[3]
            }
            outputs = model(**inputs)
            tmp_eval_loss, logits = outputs[:2]
            eval_loss += tmp_eval_loss.mean().item()
        nb_eval_steps += 1
        if preds is None:
            preds = logits.detach().cpu().numpy()
            out_label_ids = inputs['next_sentence_label'].detach().cpu().numpy().flatten()
        else:
            batch_predictions = logits.detach().cpu().numpy()
            preds = np.append(preds, batch_predictions, axis=0)
            out_label_ids = np.append(
                out_label_ids, inputs['next_sentence_label'].detach().cpu().numpy().flatten(), axis=0)
        eval_loss = eval_loss / nb_eval_steps
    preds = np.argmax(preds, axis=1)
    assert len(preds) == len(out_label_ids)
    result = {}
    result["acc"] = accuracy_score(out_label_ids, preds)
    result["f1"] = f1_score(out_label_ids, preds)
    result["AP"] = average_precision_score(out_label_ids, preds)
    result["acc_and_f1"] = (result["acc"] + result["f1"]) / 2
    results.update(result)
    output_eval_file = os.path.join(eval_output_dir, "eval_results.txt")
    with open(output_eval_file, "w") as writer:
        logger.info("***** Eval results {} *****".format(prefix))
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(result[key]))
            writer.write("%s = %s\n" % (key, str(result[key])))

    return results


if __name__ == "__main__":
    data_dir = "/ssd2/arthur/insy/msmarco/data"
    if len(sys.argv) > 3:
        argv = sys.argv[1:]
    else:
        argv = [
            "--data_dir", data_dir,
            "--train_file", data_dir + "/train-triples.top100",
            "--dev_file", data_dir + "/dev-triples.top100",
            "--bert_model", "bert-base-uncased"
        ]
    args = getArgs(argv)
    # limit_gpus = args.limit_gpus
    train_dataset = MsMarcoDataset(args.train_file, args.data_dir)
    dev_dataset = MsMarcoDataset(args.dev_file, args.data_dir)
    fine_tune(train_dataset, dev_dataset, args.data_dir,
              limit_gpus=-1,
              n_workers=14,
              batch_size=args.train_batch_size)
