﻿import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F

from dlshogi.common import *
from dlshogi import serializers
from dlshogi.swa import SWA

from dlshogi import cppshogi

import argparse
import random
import os

import logging

parser = argparse.ArgumentParser(description='Traning RL policy network using hcpe')
parser.add_argument('train_data', type=str, nargs='+', help='train data file')
parser.add_argument('test_data', type=str, help='test data file')
parser.add_argument('--batchsize', '-b', type=int, default=1024, help='Number of positions in each mini-batch')
parser.add_argument('--testbatchsize', type=int, default=640, help='Number of positions in each test mini-batch')
parser.add_argument('--epoch', '-e', type=int, default=1, help='Number of epoch times')
parser.add_argument('--network', type=str, default='wideresnet10', choices=['wideresnet10', 'wideresnet15', 'senet10', 'resnet10_swish', 'resnet20_swish', 'nfresnet10',], help='network type')
parser.add_argument('--model', type=str, default='model_rl_val_hcpe', help='model file name')
parser.add_argument('--state', type=str, default='state_rl_val_hcpe', help='state file name')
parser.add_argument('--initmodel', '-m', default='', help='Initialize the model from given file')
parser.add_argument('--resume', '-r', default='', help='Resume the optimization from snapshot')
parser.add_argument('--log', default=None, help='log file path')
parser.add_argument('--lr', type=float, default=0.01, help='learning rate')
parser.add_argument('--weightdecay_rate', type=float, default=0.0001, help='weightdecay rate')
parser.add_argument('--clip_grad_max_norm', type=float, default=10.0, help='max norm of the gradients')
parser.add_argument('--beta', type=float, default=0.001, help='entropy regularization coeff')
parser.add_argument('--val_lambda', type=float, default=0.333, help='regularization factor')
parser.add_argument('--gpu', '-g', type=int, default=0, help='GPU ID')
parser.add_argument('--eval_interval', type=int, default=1000, help='evaluation interval')
parser.add_argument('--swa_freq', type=int, default=250)
parser.add_argument('--swa_n_avr', type=int, default=10)
parser.add_argument('--swa_lr', type=float)
parser.add_argument('--use_amp', action='store_true', help='Use automatic mixed precision')
args = parser.parse_args()

if args.network == 'wideresnet15':
    from dlshogi.policy_value_network_wideresnet15 import *
    model = PolicyValueNetwork()
elif args.network == 'senet10':
    from dlshogi.policy_value_network_senet10 import *
    model = PolicyValueNetwork()
elif args.network == 'resnet10_swish':
    from dlshogi.policy_value_network_resnet10_swish import *
    model = PolicyValueNetwork()
elif args.network == 'resnet20_swish':
    from dlshogi.policy_value_network_resnet20_swish import *
    model = PolicyValueNetwork()
elif args.network == 'nfresnet10':
    from dlshogi.policy_value_network_nfresnet import *
    model = PolicyValueNetwork(num_blocks=10, num_filters=192, num_units=256)
else:
    from dlshogi.policy_value_network import *
    model = PolicyValueNetwork()

logging.basicConfig(format='%(asctime)s\t%(levelname)s\t%(message)s', datefmt='%Y/%m/%d %H:%M:%S', filename=args.log, level=logging.DEBUG)
logging.info('batchsize={}'.format(args.batchsize))
logging.info('MomentumSGD(lr={})'.format(args.lr))
logging.info('WeightDecay(rate={})'.format(args.weightdecay_rate))
logging.info('entropy regularization coeff={}'.format(args.beta))
logging.info('val_lambda={}'.format(args.val_lambda))

if args.gpu >= 0:
    torch.cuda.set_device(args.gpu)
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

model.to(device)

base_optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.weightdecay_rate, nesterov=True)
optimizer = SWA(base_optimizer, swa_start=args.swa_freq, swa_freq=args.swa_freq, swa_lr=args.swa_lr, swa_n_avr=args.swa_n_avr)
cross_entropy_loss = torch.nn.CrossEntropyLoss(reduction='none')
bce_with_logits_loss = torch.nn.BCEWithLogitsLoss()
if args.use_amp:
    logging.info('use amp')
    scaler = torch.cuda.amp.GradScaler()

# Init/Resume
if args.initmodel:
    print('Load model from', args.initmodel)
    serializers.load_npz(args.initmodel, model)
if args.resume:
    print('Load optimizer state from', args.resume)
    checkpoint = torch.load(args.resume, map_location=device)
    epoch = checkpoint['epoch']
    t = checkpoint['t']
    base_optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    if args.use_amp and 'scaler_state_dict' in checkpoint:
        scaler.load_state_dict(checkpoint['scaler_state_dict'])
else:
    epoch = 0
    t = 0

logging.debug('read teacher data')
def load_teacher(files):
    data = []
    for path in files:
        if os.path.exists(path):
            logging.debug(path)
            data.append(np.fromfile(path, dtype=HuffmanCodedPosAndEval))
        else:
            logging.debug('{} not found, skipping'.format(path))
    return np.concatenate(data)
train_data = load_teacher(args.train_data)
logging.debug('read test data')
logging.debug(args.test_data)
test_data = np.fromfile(args.test_data, dtype=HuffmanCodedPosAndEval)

logging.info('train position num = {}'.format(len(train_data)))
logging.info('test position num = {}'.format(len(test_data)))

# mini batch
def mini_batch(hcpevec):
    features1 = np.empty((len(hcpevec), FEATURES1_NUM, 9, 9), dtype=np.float32)
    features2 = np.empty((len(hcpevec), FEATURES2_NUM, 9, 9), dtype=np.float32)
    move = np.empty((len(hcpevec)), dtype=np.int32)
    result = np.empty((len(hcpevec)), dtype=np.float32)
    value = np.empty((len(hcpevec)), dtype=np.float32)

    cppshogi.hcpe_decode_with_value(hcpevec, features1, features2, move, result, value)

    z = result.astype(np.float32) - value + 0.5

    return (torch.tensor(features1).to(device),
            torch.tensor(features2).to(device),
            torch.tensor(move.astype(np.int64)).to(device),
            torch.tensor(result.reshape((len(hcpevec), 1))).to(device),
            torch.tensor(z).to(device),
            torch.tensor(value.reshape((len(value), 1))).to(device)
            )

# for SWA bn_update
def hcpe_loader(data, batchsize):
    for i in range(0, len(data) - batchsize + 1, batchsize):
        x1, x2, t1, t2, z, value = mini_batch(data[i:i+batchsize])
        yield x1, x2

def accuracy(y, t):
    return (torch.max(y, 1)[1] == t).sum().item() / len(t)

def binary_accuracy(y, t):
    pred = y >= 0
    truth = t >= 0.5
    return pred.eq(truth).sum().item() / len(t)

def unitwise_norm(x: torch.Tensor):
    if x.ndim <= 1:
        dim = 0
        keepdim = False
    elif x.ndim in [2, 3]:
        dim = 0
        keepdim = True
    elif x.ndim == 4:
        dim = [1, 2, 3]
        keepdim = True
    else:
        raise ValueError('Wrong input dimensions')

    return torch.norm(x.detach(), 2.0, dim=dim, keepdim=keepdim)

def adaptive_grad_clip_(parameters, clip=1e-2, eps=1e-3):
    if isinstance(parameters, torch.Tensor):
        parameters = [parameters]
    parameters = [p for p in parameters if p.grad is not None]
    if len(parameters) == 0:
        return torch.tensor(0.)
    for p in parameters:
        g_norm = unitwise_norm(p.grad)
        p_norm = unitwise_norm(p)
        # Maximum allowable norm
        max_norm = torch.max(p_norm, torch.tensor(eps).to(p_norm.device)) * clip
        # If grad norm > clipping * param_norm, rescale
        trigger = g_norm > max_norm
        # This little max(., 1e-6) is distinct from the normal eps and just prevents
        # division by zero. It technically should be impossible to engage.
        clipped_grad = p.grad * (max_norm / torch.max(g_norm, torch.tensor(1e-6).to(g_norm.device)))
        p.grad.detach().data.copy_(torch.where(trigger, clipped_grad, p.grad))

# train
itr = 0
sum_loss1 = 0
sum_loss2 = 0
sum_loss3 = 0
sum_loss = 0
eval_interval = args.eval_interval
for e in range(args.epoch):
    np.random.shuffle(train_data)

    itr_epoch = 0
    sum_loss1_epoch = 0
    sum_loss2_epoch = 0
    sum_loss3_epoch = 0
    sum_loss_epoch = 0
    for i in range(0, len(train_data) - args.batchsize + 1, args.batchsize):
        if args.use_amp:
            amp_context = torch.cuda.amp.autocast()
            amp_context.__enter__()

        model.train()

        x1, x2, t1, t2, z, value = mini_batch(train_data[i:i+args.batchsize])
        y1, y2 = model(x1, x2)

        model.zero_grad()
        loss1 = (cross_entropy_loss(y1, t1) * z).mean()
        if args.beta > 0:
            loss1 += args.beta * (F.softmax(y1, dim=1) * F.log_softmax(y1, dim=1)).sum(dim=1).mean()
        loss2 = bce_with_logits_loss(y2, t2)
        loss3 = bce_with_logits_loss(y2, value)
        loss = loss1 + (1 - args.val_lambda) * loss2 + args.val_lambda * loss3

        if args.use_amp:
            amp_context.__exit__()
            scaler.scale(loss).backward()
            if 'nf' in args.network:
                adaptive_grad_clip_(model.parameters())
            elif args.clip_grad_max_norm:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_max_norm)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            if 'nf' in args.network:
                adaptive_grad_clip_(model.parameters())
            elif args.clip_grad_max_norm:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_max_norm)
            optimizer.step()

        t += 1
        itr += 1
        sum_loss1 += loss1.item()
        sum_loss2 += loss2.item()
        sum_loss3 += loss3.item()
        sum_loss += loss.item()
        itr_epoch += 1
        sum_loss1_epoch += loss1.item()
        sum_loss2_epoch += loss2.item()
        sum_loss3_epoch += loss3.item()
        sum_loss_epoch += loss.item()

        # print train loss
        if t % eval_interval == 0:
            model.eval()

            x1, x2, t1, t2, z, value = mini_batch(np.random.choice(test_data, args.testbatchsize))
            with torch.no_grad():
                y1, y2 = model(x1, x2)

                loss1 = (cross_entropy_loss(y1, t1) * z).mean()
                loss2 = bce_with_logits_loss(y2, t2)
                loss3 = bce_with_logits_loss(y2, value)
                loss = loss1 + (1 - args.val_lambda) * loss2 + args.val_lambda * loss3

                logging.info('epoch = {}, iteration = {}, loss = {:.08f}, {:.08f}, {:.08f}, {:.08f}, test loss = {:.08f}, {:.08f}, {:.08f}, {:.08f}, test accuracy = {:.08f}, {:.08f}'.format(
                    epoch + 1, t,
                    sum_loss1 / itr, sum_loss2 / itr, sum_loss3 / itr, sum_loss / itr,
                    loss1.item(), loss2.item(), loss3.item(), loss.item(),
                    accuracy(y1, t1), binary_accuracy(y2, t2)))
            itr = 0
            sum_loss1 = 0
            sum_loss2 = 0
            sum_loss3 = 0
            sum_loss = 0

    optimizer.swap_swa_sgd()

    if args.use_amp:
        amp_context = torch.cuda.amp.autocast()
        amp_context.__enter__()

    if 'nf' not in args.network:
        optimizer.bn_update(hcpe_loader(train_data, args.batchsize), model)

    if args.use_amp:
        amp_context.__exit__()

    # print train loss for each epoch
    itr_test = 0
    sum_test_loss1 = 0
    sum_test_loss2 = 0
    sum_test_loss3 = 0
    sum_test_loss = 0
    sum_test_accuracy1 = 0
    sum_test_accuracy2 = 0
    sum_test_entropy1 = 0
    sum_test_entropy2 = 0
    model.eval()
    with torch.no_grad():
        for i in range(0, len(test_data) - args.testbatchsize, args.testbatchsize):
            x1, x2, t1, t2, z, value = mini_batch(test_data[i:i+args.testbatchsize])
            y1, y2 = model(x1, x2)

            itr_test += 1
            loss1 = (cross_entropy_loss(y1, t1) * z).mean()
            loss2 = bce_with_logits_loss(y2, t2)
            loss3 = bce_with_logits_loss(y2, value)
            loss = loss1 + (1 - args.val_lambda) * loss2 + args.val_lambda * loss3
            sum_test_loss1 += loss1.item()
            sum_test_loss2 += loss2.item()
            sum_test_loss3 += loss3.item()
            sum_test_loss += loss.item()
            sum_test_accuracy1 += accuracy(y1, t1)
            sum_test_accuracy2 += binary_accuracy(y2, t2)

            entropy1 = (- F.softmax(y1, dim=1) * F.log_softmax(y1, dim=1)).sum(dim=1)
            sum_test_entropy1 += entropy1.mean().item()

            p2 = y2.sigmoid()
            #entropy2 = -(p2 * F.log(p2) + (1 - p2) * F.log(1 - p2))
            log1p_ey2 = F.softplus(y2)
            entropy2 = -(p2 * (y2 - log1p_ey2) + (1 - p2) * -log1p_ey2)
            sum_test_entropy2 +=entropy2.mean().item()

        logging.info('epoch = {}, iteration = {}, train loss avr = {:.08f}, {:.08f}, {:.08f}, {:.08f}, test_loss = {:.08f}, {:.08f}, {:.08f}, {:.08f}, test accuracy = {:.08f}, {:.08f}, test entropy = {:.08f}, {:.08f}'.format(
            epoch + 1, t,
            sum_loss1_epoch / itr_epoch, sum_loss2_epoch / itr_epoch, sum_loss3_epoch / itr_epoch, sum_loss_epoch / itr_epoch,
            sum_test_loss1 / itr_test, sum_test_loss2 / itr_test, sum_test_loss3 / itr_test, sum_test_loss / itr_test,
            sum_test_accuracy1 / itr_test, sum_test_accuracy2 / itr_test,
            sum_test_entropy1 / itr_test, sum_test_entropy2 / itr_test))

    epoch += 1

    if e != args.epoch - 1:
        optimizer.swap_swa_sgd()

print('save the model')
serializers.save_npz(args.model, model)
print('save the optimizer')
state = {
    'epoch': epoch,
    't': t,
    'optimizer_state_dict': base_optimizer.state_dict(),
    }
if args.use_amp:
    state['scaler_state_dict'] = scaler.state_dict()
torch.save(state, args.state)
