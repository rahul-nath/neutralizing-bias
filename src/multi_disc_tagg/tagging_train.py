# -*- coding: utf-8 -*-
"""
train bert 

python tagging_train.py --train ../../data/v6/corpus.wordbiased.tag.train --test ../../data/v6/corpus.wordbiased.tag.megatest --working_dir TEST/
"""
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam

from collections import defaultdict
from torch.utils.data import TensorDataset, DataLoader, RandomSampler
from tqdm import tqdm
import torch
import torch.nn as nn
import pickle
import sys
import os
import numpy as np
from pytorch_pretrained_bert.modeling import BertForTokenClassification
from torch.nn import CrossEntropyLoss
from tensorboardX import SummaryWriter
import argparse
import sklearn.metrics as metrics

import tagging_model

from joint_data import get_dataloader
from joint_args import ARGS

import tagging_utils




if not os.path.exists(ARGS.working_dir):
    os.makedirs(ARGS.working_dir)


CUDA = (torch.cuda.device_count() > 0)

if CUDA:
    print('USING CUDA')



print('LOADING DATA...')
tokenizer = BertTokenizer.from_pretrained(ARGS.bert_model, cache_dir=ARGS.working_dir + '/cache')
tok2id = tokenizer.vocab
tok2id['<del>'] = len(tok2id)

train_dataloader, num_train_examples = get_dataloader(
    ARGS.train, 
    tok2id, ARGS.train_batch_size, 
    ARGS.max_seq_len, ARGS.working_dir + '/train_data.pkl', 
    categories_path=ARGS.train_categories_file)
eval_dataloader, num_eval_examples = get_dataloader(
    ARGS.test,
    tok2id, ARGS.test_batch_size, ARGS.max_seq_len, ARGS.working_dir + '/test_data.pkl',
    test=True, categories_path=ARGS.test_categories_file)


print('BUILDING MODEL...')
if ARGS.extra_features_top:
    model = tagging_model.BertForMultitaskWithFeaturesOnTop.from_pretrained(
            ARGS.bert_model,
            cls_num_labels=ARGS.num_categories,
            tok_num_labels=ARGS.num_tok_labels,
            cache_dir=ARGS.working_dir + '/cache',
            tok2id=tok2id)
elif ARGS.extra_features_bottom:
    model = tagging_model.BertForMultitaskWithFeaturesOnBottom.from_pretrained(
            ARGS.bert_model,
            cls_num_labels=ARGS.num_categories,
            tok_num_labels=ARGS.num_tok_labels,
            cache_dir=ARGS.working_dir + '/cache',
            tok2id=tok2id)
else:
    model = tagging_model.BertForMultitask.from_pretrained(
        ARGS.bert_model,
        cls_num_labels=ARGS.num_categories,
        tok_num_labels=ARGS.num_tok_labels,
        cache_dir=ARGS.working_dir + '/cache',
        tok2id=tok2id)
if CUDA:
    model = model.cuda()

print('PREPPING RUN...')
# # # # # # # # ## # # # ## # # OPTIMIZER, LOSS # # # # # # # # ## # # # ## # #

def make_optimizer(model, num_train_steps):
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
    ]
    optimizer = BertAdam(optimizer_grouped_parameters,
                         lr=ARGS.learning_rate,
                         warmup=0.1,
                         t_total=num_train_steps)
    return optimizer

optimizer = make_optimizer(
    model, int((num_train_examples * ARGS.epochs) / ARGS.train_batch_size))

loss_fn = tagging_utils.build_loss_fn(ARGS)

if ARGS.predict_categories:
    category_criterion = CrossEntropyLoss()
    if CUDA:
        category_criterion = category_criterion.cuda()

    def cross_entropy(pred, soft_targets):
        logsoftmax = nn.LogSoftmax()
        return torch.mean(torch.sum(- soft_targets * logsoftmax(pred), 1))

# # # # # # # # ## # # # ## # # END LOSS # # # # # # # # ## # # # ## # #

writer = SummaryWriter(ARGS.working_dir)


print('INITIAL EVAL...')
model.eval()
results = tagging_utils.run_inference(model, eval_dataloader, loss_fn, tokenizer)
writer.add_scalar('eval/tok_loss', np.mean(results['tok_loss']), 0)
writer.add_scalar('eval/tok_acc', np.mean(results['labeling_hits']), 0)

print('TRAINING...')
model.train()
train_step = 0
for epoch in range(ARGS.epochs):
    print('STARTING EPOCH ', epoch)
    for step, batch in enumerate(tqdm(train_dataloader)):
        if CUDA:
            batch = tuple(x.cuda() for x in batch)
        ( 
            pre_id, pre_mask, pre_len, 
            post_in_id, post_out_id, 
            tok_label_id, _, tok_dist,
            replace_id, rel_ids, pos_ids, type_ids, categories
        ) = batch
        bias_logits, tok_logits = model(pre_id, attention_mask=1.0-pre_mask, 
            rel_ids=rel_ids, pos_ids=pos_ids, categories=categories)
        loss = loss_fn(tok_logits, tok_label_id, apply_mask=tok_label_id)
        if ARGS.predict_categories:
            category_loss = cross_entropy(bias_logits, categories)
            print(category_loss)
            loss = loss + category_loss
        loss.backward()
        optimizer.step()
        model.zero_grad()
        if train_step % 100 == 0:
            writer.add_scalar('train/loss', loss.data[0], train_step)
        train_step += 1

    # eval
    print('EVAL...')
    model.eval()
    results = tagging_utils.run_inference(model, eval_dataloader, loss_fn, tokenizer)
    writer.add_scalar('eval/tok_loss', np.mean(results['tok_loss']), epoch + 1)
    writer.add_scalar('eval/tok_acc', np.mean(results['labeling_hits']), epoch + 1)

    model.train()
    print('SAVING...')
    
    torch.save(model.state_dict(), ARGS.working_dir + '/model_%d.ckpt' % epoch)    
    

