import json
import torch
import os
import numpy as np
from uni_model_2 import *
# from data_loader import MusicArrayLoader
from torch import optim
from torch.distributions import kl_divergence, Normal
from torch.nn import functional as F
from torch.optim.lr_scheduler import ExponentialLR
from sklearn.model_selection import train_test_split
from adversarial_test import *
from polyphonic_event_based_v2 import parse_pretty_midi

from ptb_v2 import *

import psutil


# some initialization
with open('uni_model_config_2.json') as f:
    args = json.load(f)
if not os.path.isdir('log'):
    os.mkdir('log')
if not os.path.isdir('params'):
    os.mkdir('params')
save_path = 'params/{}.pt'.format(args['name'])

from datetime import datetime
timestamp = str(datetime.now())
save_path_timing = 'params/{}.pt'.format(args['name'] + "_" + timestamp)

# model dimensions
EVENT_DIMS = 342
RHYTHM_DIMS = 3
NOTE_DIMS = 16
TEMPO_DIMS = 264
VELOCITY_DIMS = 126
CHROMA_DIMS = 24

model = MusicAttrRegVAE(roll_dims=EVENT_DIMS, rhythm_dims=RHYTHM_DIMS, note_dims=NOTE_DIMS, 
                        tempo_dims=TEMPO_DIMS, velocity_dims=VELOCITY_DIMS, chroma_dims=CHROMA_DIMS,
                        hidden_dims=args['hidden_dim'], z_dims=args['z_dim'], 
                        n_step=args['time_step'])

if os.path.exists(save_path):
    print("Loading {}".format(save_path))
    model.load_state_dict(torch.load(save_path))
else:
    print("Save path: {}".format(save_path))


optimizer = optim.Adam(model.parameters(), lr=args['lr'])

if torch.cuda.is_available():
    print('Using: ', torch.cuda.get_device_name(torch.cuda.current_device()))
    model.cuda()
else:
    print('CPU mode')

step, pre_epoch = 0, 0
batch_size = args["batch_size"]
model.train()

# dataloaders
is_shuffle = True
data_lst, rhythm_lst, note_density_lst, chroma_lst = get_classic_piano()
tlen, vlen = int(0.8 * len(data_lst)), int(0.9 * len(data_lst))
train_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="train")
train_dl_dist = DataLoader(train_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
val_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="val")
val_dl_dist = DataLoader(val_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
test_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="test")
test_dl_dist = DataLoader(test_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
dl = train_dl_dist
print(len(train_ds_dist), len(val_ds_dist), len(test_ds_dist))


is_class = args["is_class"]
is_res = args["is_res"]
# end of initialization


def std_normal(shape):
    N = Normal(torch.zeros(shape), torch.ones(shape))
    if torch.cuda.is_available():
        N.loc = N.loc.cuda()
        N.scale = N.scale.cuda()
    return N


def loss_function(out, d,
                r_out, r,
                n_out, n,
                dis,
                step,
                beta=.1):

    CE_X = F.nll_loss(out.view(-1, out.size(-1)),
                    d.view(-1), reduction='mean')
    CE_R = F.nll_loss(r_out.view(-1, r_out.size(-1)),
                    r.view(-1), reduction='mean')
    CE_N = F.nll_loss(n_out.view(-1, n_out.size(-1)),
                    n.view(-1), reduction='mean')

    CE = 5 * CE_X + CE_R + CE_N     # speed up reconstruction training

    # all distribution conform to standard gaussian
    inputs = dis
    KLD = 0
    for input in inputs:
        normal = std_normal(input.mean.size())
        KLD += kl_divergence(input, normal).mean()

    # anneal beta
    # beta0 = min(step / 3000 * beta, beta)  
    beta0 = beta
    return CE + beta0 * KLD, CE_X, CE_R, CE_N


def latent_regularized_loss_function(z_out, r, n, c=None):
    return latent_regularized_loss_function_glsr_v2(z_out, r, n, c)


def latent_regularized_loss_function_glsr(z_out, r, n, c):
    # GLSR -- Hadjeres 17
    z_r, z_n = z_out
    epsilon = 1e-3

    # rhythm regularized
    r_density = r

    # delta z_r
    d_z_r = torch.zeros_like(z_r)
    deltas = (1 + torch.rand(z_r.size(0))) * epsilon
    deltas = deltas.cuda()
    z_r_plus = z_r.clone()
    z_r_plus[:, 0] += deltas
    z_r_minus = z_r.clone()
    z_r_minus[:, 0] -= deltas

    z_plus_new = torch.cat([z_r_plus, z_n, c], dim=1)  
    out_plus = model.global_decoder(z_plus_new, steps=100)
    z_minus_new = torch.cat([z_r_minus, z_n, c], dim=1)  
    out_minus = model.global_decoder(z_minus_new, steps=100)

    # delta attr r
    r_density_plus = []
    for out in out_plus:
        try:
            pm = magenta_decode_midi(clean_output(out))
            pr = parse_pretty_midi(pm)
            _, _, _, _, rhythm = encode_midi(pr, beat=4, is_pr=True)
            r_density_plus.append(Counter(rhythm)[1] / len(rhythm))
            # print("Success!")
        except Exception as e:
            r_density_plus.append(0)
    
    r_density_minus= []
    for out in out_minus:
        try:
            pm = magenta_decode_midi(clean_output(out))
            pr = parse_pretty_midi(pm)
            _, _, _, _, rhythm = encode_midi(pr, beat=4, is_pr=True)
            r_density_minus.append(Counter(rhythm)[1] / len(rhythm))
            # print("Success!")
        except Exception as e:
            r_density_minus.append(0)
    
    grad_softmax = out_plus - out_minus

    grad_attr = (torch.Tensor(r_density_plus) - torch.Tensor(r_density_minus)).cuda()
    grad_attr = grad_attr / (2 * deltas)

    prior_mean = torch.zeros_like(grad_attr).cuda()
    prior_std = torch.ones_like(grad_attr).cuda()
    reg_loss = -Normal(prior_mean, prior_std).log_prob(grad_attr)
    l_r = reg_loss.mean()
    
    # note regularized
    n_density = n
   
    # delta z_n
    d_z_n = torch.zeros_like(z_n)
    deltas = (1 + torch.rand(z_n.size(0))) * epsilon
    deltas = deltas.cuda()
    z_n_plus = z_n.clone()
    z_n_plus[:, 0] += deltas
    z_n_minus = z_n.clone()
    z_n_minus[:, 0] -= deltas

    z_plus_new = torch.cat([z_r, z_n_plus, c], dim=1)  
    out_plus = model.global_decoder(z_plus_new, steps=100)
    z_minus_new = torch.cat([z_r, z_n_minus, c], dim=1)  
    out_minus = model.global_decoder(z_minus_new, steps=100)

    # delta attr r
    n_density_plus = []
    for out in out_plus:
        try:
            pm = magenta_decode_midi(clean_output(out))
            pr = parse_pretty_midi(pm)
            _, pitch_lst, _, _, _ = encode_midi(pr, beat=4, is_pr=True)
            note_density = np.array([len(k) for k in pitch_lst])
            n_density_plus.append(sum(note_density) / len(note_density))
            # print("Success!")
        except Exception as e:
            n_density_plus.append(0)
    
    n_density_minus= []
    for out in out_minus:
        try:
            pm = magenta_decode_midi(clean_output(out))
            pr = parse_pretty_midi(pm)
            _, pitch_lst, _, _, _ = encode_midi(pr, beat=4, is_pr=True)
            note_density = np.array([len(k) for k in pitch_lst])
            n_density_minus.append(sum(note_density) / len(note_density))
            # print("Success!")
        except Exception as e:
            n_density_minus.append(0)
    
    grad_softmax = out_plus - out_minus

    grad_attr = (torch.Tensor(n_density_plus) - torch.Tensor(n_density_minus)).cuda()
    grad_attr = grad_attr / (2 * deltas)

    prior_mean = torch.zeros_like(grad_attr).cuda()
    prior_std = torch.ones_like(grad_attr).cuda()
    reg_loss = -Normal(prior_mean, prior_std).log_prob(grad_attr)
    l_n = reg_loss.mean()

    return l_r, l_n


def latent_regularized_loss_function_glsr_v2(z_out, r, n, c):

    # Use approximation on musical attributes
    def approx_played_notes(out_logits):
        # played note mask
        played_note_mask = torch.zeros(342,).cuda()
        played_note_mask[2:90] = 1      # tokens 2 - 89 are MIDI on tokens
        played_note_mask = torch.stack([played_note_mask] * out_logits.shape[0], dim=0).unsqueeze(-1)
        res = torch.bmm(F.softmax(out_logits, dim=-1), played_note_mask)
        return res

    def approx_time_separators(out_logits):
        # time step separator mask
        time_step_mask = torch.zeros(342,).cuda()
        time_step_mask[180:278] = 1     # tokens 178 - 277 are time shift tokens, choose from 180 (30ms) to start as time separator
        time_step_mask = torch.stack([time_step_mask] * out_logits.shape[0], dim=0).unsqueeze(-1)
        res = torch.bmm(F.softmax(out_logits, dim=-1), time_step_mask)
        return res
    
    def approx_note_density(out_logits):
        played_notes = approx_played_notes(out_logits)
        return torch.sum(played_notes, dim=1)
    
    def approx_rhythm_density(out_logits):
        played_notes = approx_played_notes(out_logits)
        time_separators = approx_time_separators(out_logits)

        res_lst = []
        for idx in range(len(time_separators)):
            total = 0
            cur = 0
            for i, elem in enumerate(time_separators[idx].squeeze()):
                
                # if not reach time separator
                if elem.item() < 0.9: 
                    cur += played_notes[0][i]   # add number of played notes first

                else:
                    if cur == 0:
                        continue
                    elif cur.item() > 1e-2:     # if cur_played_notes is non-zero, add 1
                        total += cur / cur
                    else:
                        total += cur            # else add zero
                    cur = 0
            
            r_density = total / torch.sum(time_separators[idx].squeeze())
            if r_density.item() != 0.0:
                res_lst.append(r_density)   # add normalized rhythm density 
            else:
                res_lst.append(torch.Tensor([0]).cuda())
        
        return torch.stack(res_lst, dim=0)
    
    # GLSR -- Hadjeres 17
    z_r, z_n = z_out
    epsilon = 1e-2

    # delta z_r
    d_z_r = torch.zeros_like(z_r)
    deltas = (1 + torch.rand(z_r.size(0))) * epsilon
    deltas = deltas.cuda()
    z_r_plus = z_r.clone()
    z_r_plus[:, 0] += deltas
    z_r_minus = z_r.clone()
    z_r_minus[:, 0] -= deltas

    z_plus_new = torch.cat([z_r_plus, z_n, c], dim=1)  
    out_plus = model.global_decoder(z_plus_new, steps=100)
    z_minus_new = torch.cat([z_r_minus, z_n, c], dim=1)  
    out_minus = model.global_decoder(z_minus_new, steps=100)

    r_density_plus = approx_rhythm_density(out_plus)
    r_density_minus = approx_rhythm_density(out_minus)
    
    # delta z attr
    grad_attr = (r_density_plus - r_density_minus).cuda().squeeze()
    print(grad_attr.shape)
    grad_attr = grad_attr / (2 * deltas)

    prior_mean = torch.zeros_like(grad_attr).cuda()
    prior_std = torch.ones_like(grad_attr).cuda()
    reg_loss = -Normal(prior_mean, prior_std).log_prob(grad_attr)
    l_r = reg_loss.mean()
   
    # delta z_n
    d_z_n = torch.zeros_like(z_n)
    deltas = (1 + torch.rand(z_n.size(0))) * epsilon
    deltas = deltas.cuda()
    z_n_plus = z_n.clone()
    z_n_plus[:, 0] += deltas
    z_n_minus = z_n.clone()
    z_n_minus[:, 0] -= deltas

    z_plus_new = torch.cat([z_r, z_n_plus, c], dim=1)  
    out_plus = model.global_decoder(z_plus_new, steps=100)
    z_minus_new = torch.cat([z_r, z_n_minus, c], dim=1)  
    out_minus = model.global_decoder(z_minus_new, steps=100)

    n_density_plus = approx_note_density(out_plus)
    n_density_minus = approx_note_density(out_minus)

    # delta z attr
    grad_attr = (n_density_plus - n_density_minus).cuda().squeeze()
    grad_attr = grad_attr / (2 * deltas)

    prior_mean = torch.zeros_like(grad_attr).cuda()
    prior_std = torch.ones_like(grad_attr).cuda()
    reg_loss = -Normal(prior_mean, prior_std).log_prob(grad_attr)
    l_n = reg_loss.mean()

    return l_r, l_n


def train(step, d_oh, r_oh, n_oh,
          d, r, n, c, c_r, c_n, c_r_oh, c_n_oh, r_density, n_density):
    
    optimizer.zero_grad()
    res = model(d_oh, r_oh, n_oh, c, c_r_oh, c_n_oh, is_class=is_class, is_res=is_res)

    # package output
    output, dis, z_out = res
    out, r_out, n_out, r_r_density, r_n_density = output
    z_r, z_n = z_out
    
    # calculate loss
    loss, CE_X, CE_R, CE_N = loss_function(out, d,
                                        r_out, r,
                                        n_out, n,
                                        dis,
                                        step,
                                        beta=args['beta'])
    
    l_r, l_n = torch.Tensor([0]), torch.Tensor([0])
    if step > 20:
        l_r, l_n = latent_regularized_loss_function(z_out, r_density, n_density, c)
        loss += l_r + l_n

    # adversarial loss
    l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])
    
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
    optimizer.step()
    step += 1
    
    output = loss.item(), CE_X.item(), CE_R.item(), CE_N.item(), l_r.item(), l_n.item(), 0, 0
    return step, output


def evaluate(step, d_oh, r_oh, n_oh,
             d, r, n, c, c_r, c_n, c_r_oh, c_n_oh, r_density, n_density):
    
    res = model(d_oh, r_oh, n_oh, c, c_r_oh, c_n_oh, is_class=is_class, is_res=is_res)

    # package output
    output, dis, z_out = res
    out, r_out, n_out, r_r_density, r_n_density = output
    z_r, z_n = z_out

    # calculate loss
    loss, CE_X, CE_R, CE_N = loss_function(out, d,
                                            r_out, r,
                                            n_out, n,
                                            dis,
                                            step,
                                            beta=args['beta'])
    
    l_r, l_n = torch.Tensor([0]), torch.Tensor([0])
    if step > 20:
        l_r, l_n = latent_regularized_loss_function(z_out, r_density, n_density, c)
        loss += l_r + l_n

    # adversarial loss
    l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])

    
    output = loss.item(), CE_X.item(), CE_R.item(), CE_N.item(), l_r.item(), l_n.item(), 0, 0
    return output


def convert_to_one_hot(input, dims):
    if len(input.shape) > 1:
        input_oh = torch.zeros((input.shape[0], input.shape[1], dims)).cuda()
        input_oh = input_oh.scatter_(-1, input.unsqueeze(-1), 1.)
    else:
        input_oh = torch.zeros((input.shape[0], dims)).cuda()
        input_oh = input_oh.scatter_(-1, input.unsqueeze(-1), 1.)
    return input_oh


def training_phase(step):

    for i in range(1, args['n_epochs'] + 1):
        print("Epoch {} / {}".format(i, args['n_epochs']))

        batch_loss, batch_test_loss = 0, 0
        b_CE_X, b_CE_R, b_CE_N, b_CE_C, b_CE_T, b_CE_V = 0, 0, 0, 0, 0, 0
        t_CE_X, t_CE_R, t_CE_N, t_CE_C, t_CE_T, t_CE_V = 0, 0, 0, 0, 0, 0
        b_l_r, b_l_n, t_l_r, t_l_n = 0, 0, 0, 0
        b_l_adv_r, b_l_adv_n, t_l_adv_r, t_l_adv_n = 0, 0, 0, 0

        for j, x in tqdm(enumerate(train_dl_dist), total=len(train_dl_dist)):

            d, r, n, c, c_r, c_n, r_density, n_density = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS)
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS)
            n_oh = convert_to_one_hot(n, NOTE_DIMS)

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            step, loss = train(step, d_oh, r_oh, n_oh,
                                            d, r, n, c, c_r, c_n, c_r_oh, c_n_oh, r_density, n_density)
            loss, CE_X, CE_R, CE_N, l_r, l_n, l_adv_r, l_adv_n = loss
            batch_loss += loss

            inputs = b_CE_X, b_CE_R, b_CE_N
            updates = CE_X, CE_R, CE_N
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            b_CE_X, b_CE_R, b_CE_N = outputs
            b_l_r += l_r
            b_l_n += l_n
            b_l_adv_r += l_adv_r
            b_l_adv_n += l_adv_n

        for j, x in tqdm(enumerate(val_dl_dist), total=len(val_dl_dist)):
            
            d, r, n, c, c_r, c_n, r_density, n_density = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS)
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS)
            n_oh = convert_to_one_hot(n, NOTE_DIMS)

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            loss = evaluate(step - 1, d_oh, r_oh, n_oh,
                            d, r, n, c, c_r, c_n, c_r_oh, c_n_oh, r_density, n_density)
            loss, CE_X, CE_R, CE_N, l_r, l_n, l_adv_r, l_adv_n = loss
            batch_test_loss += loss

            inputs = t_CE_X, t_CE_R, t_CE_N
            updates = CE_X, CE_R, CE_N
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_CE_X, t_CE_R, t_CE_N = outputs
            t_l_r += l_r
            t_l_n += l_n
            t_l_adv_r += l_adv_r
            t_l_adv_n += l_adv_n
        
        print('batch loss: {:.5f}  {:.5f}'.format(batch_loss / len(train_dl_dist),
                                                  batch_test_loss / len(val_dl_dist)))
        print("Data, Rhythm, Note, Chroma")
        print("train loss by term: {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}".format(
            b_CE_X / len(train_dl_dist), b_CE_R / len(train_dl_dist), 
            b_CE_N / len(train_dl_dist),
            b_l_r / len(train_dl_dist), b_l_n / len(train_dl_dist),
            b_l_adv_r / len(train_dl_dist), b_l_adv_n / len(train_dl_dist)
        ))
        print("test loss by term: {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}".format(
            t_CE_X / len(val_dl_dist), t_CE_R / len(val_dl_dist), 
            t_CE_N / len(val_dl_dist),
            t_l_r / len(val_dl_dist), t_l_n / len(val_dl_dist),
            t_l_adv_r / len(val_dl_dist), t_l_adv_n / len(val_dl_dist)
        ))

        print("Saving model...")
        torch.save(model.cpu().state_dict(), save_path)
        model.cuda()

    timestamp = str(datetime.now())
    save_path_timing = 'params/{}.pt'.format(args['name'] + "_" + timestamp)
    torch.save(model.cpu().state_dict(), save_path_timing)

    if torch.cuda.is_available():
        model.cuda()
    print('Model saved as {}!'.format(save_path))


def evaluation_phase():
    if torch.cuda.is_available():
        model.cuda()

    if os.path.exists(save_path):
        print("Loading {}".format(save_path))
        model.load_state_dict(torch.load(save_path))
    
    def run(dl):
        
        t_CE_X, t_CE_R, t_CE_N, t_CE_C, t_CE_T, t_CE_V = 0, 0, 0, 0, 0, 0
        t_l_r, t_l_n = 0, 0
        t_l_adv_r, t_l_adv_n = 0, 0
        t_acc_x, t_acc_r, t_acc_n, t_acc_t, t_acc_v = 0, 0, 0, 0, 0
        c_acc_r, c_acc_n, c_acc_t, c_acc_v = 0, 0, 0, 0
        data_len = 0
        linear_r, linear_n = [], []

        for i, x in tqdm(enumerate(dl), total=len(dl)):
            d, r, n, c, c_r, c_n, r_density_lst, n_density_lst = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS)
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS)
            n_oh = convert_to_one_hot(n, NOTE_DIMS)

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            res = model(d_oh, r_oh, n_oh, c, c_r_oh, c_n_oh, is_class=is_class, is_res=is_res)

            # package output
            if is_class:
                output, dis, clas, z_out = res
                cls_r, cls_n = clas
            else:
                output, dis, z_out = res
            
            out, r_out, n_out, r_r_density, r_n_density = output
            z_r, z_n = z_out

            # calculate loss
            loss, CE_X, CE_R, CE_N = loss_function(out, d,
                                                r_out, r,
                                                n_out, n,
                                                dis,
                                                step,
                                                beta=args['beta'])
            
            l_r, l_n = latent_regularized_loss_function(z_out, r_density_lst, n_density_lst, c)
            loss += l_r + l_n

            l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])

            # update
            inputs = t_CE_X, t_CE_R, t_CE_N
            updates = CE_X.item(), CE_R.item(), CE_N.item()
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_CE_X, t_CE_R, t_CE_N = outputs
            t_l_r += l_r.item()
            t_l_n += l_n.item()
                    
            # calculate accuracy
            def acc(a, b, t, trim=False):
                a = torch.argmax(a, dim=-1).squeeze().cpu().detach().numpy()
                b = b.squeeze().cpu().detach().numpy()

                b_acc = 0
                for i in range(len(a)):
                    a_batch = a[i]
                    b_batch = b[i]

                    if trim:
                        b_batch = np.trim_zeros(b_batch)
                        a_batch = a_batch[:len(b_batch)]

                    correct = 0
                    for j in range(len(a_batch)):
                        if a_batch[j] == b_batch[j]:
                            correct += 1
                    acc = correct / len(a_batch)
                    b_acc += acc
                
                return b_acc

            acc_x, acc_r, acc_n = acc(out, d, "d", trim=True), \
                                  acc(r_out, r, "r"), acc(n_out, n, "n")
            data_len += out.shape[0]

            # accuracy update store
            inputs = t_acc_x, t_acc_r, t_acc_n
            updates = acc_x, acc_r, acc_n
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_acc_x, t_acc_r, t_acc_n = outputs        

            from sklearn.linear_model import LinearRegression

            reg = LinearRegression().fit(z_r[:, 0].unsqueeze(-1).cpu().detach().numpy(), 
                                        r_density_lst.unsqueeze(-1).cpu().detach().numpy())
            linear_r.append(reg.score(z_r[:, 0].unsqueeze(-1).cpu().detach().numpy(), 
                    r_density_lst.unsqueeze(-1).cpu().detach().numpy()))

            reg = LinearRegression().fit(z_n[:, 0].unsqueeze(-1).cpu().detach().numpy(), 
                                        n_density_lst.unsqueeze(-1).cpu().detach().numpy())
            linear_n.append(reg.score(z_n[:, 0].unsqueeze(-1).cpu().detach().numpy(), 
                    n_density_lst.unsqueeze(-1).cpu().detach().numpy()))
        
        
        print(sum(linear_r) / len(linear_r), sum(linear_n) / len(linear_n))

        # Print results
        print(data_len)
        print("CE: {:.4}  {:.4}  {:.4}".format(t_CE_X / len(dl),
                                                    t_CE_R / len(dl), 
                                                    t_CE_N / len(dl)))
        
        print("Regularized: {:.4}  {:.4}".format(t_l_r / len(dl),
                                                t_l_n / len(dl)))

        print("Adversarial: {:.4}  {:.4}".format(t_l_adv_r / len(dl),
                                                t_l_adv_n / len(dl)))
        
        print("Acc: {:.4}  {:.4}  {:.4}".format(t_acc_x / data_len,
                                                t_acc_r / data_len, 
                                                t_acc_n / data_len))
        
        if is_class:
            print("Class acc: {:.4}  {:.4}".format(c_acc_r / data_len,
                                                    c_acc_n / data_len))

    dl = DataLoader(train_ds_dist, batch_size=128, shuffle=False, num_workers=0)
    run(dl)
    dl = DataLoader(test_ds_dist, batch_size=128, shuffle=False, num_workers=0)
    run(dl)


training_phase(step)
evaluation_phase()

