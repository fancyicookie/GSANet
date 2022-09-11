import logging
from collections import OrderedDict

import torch
import torch.nn as nn
from torch.nn.parallel import DataParallel, DistributedDataParallel
import models.networks as networks
import models.lr_scheduler as lr_scheduler
from .base_model import BaseModel
from models.customize_loss import tanh_L1Loss, tanh_L2Loss
import math
import numpy as np

logger = logging.getLogger('base')

class GenerationModel(BaseModel):
    def __init__(self, opt):
        super(GenerationModel, self).__init__(opt)

        if opt['dist']:
            self.rank = torch.distributed.get_rank()
        else:
            self.rank = -1  # non dist training
        train_opt = opt['train']

        # define network and load pretrained models
        self.netG = networks.define_G(opt).to(self.device)
        if opt['dist']:
            self.netG = DistributedDataParallel(self.netG, device_ids=[torch.cuda.current_device()])
        else:
            self.netG = DataParallel(self.netG)
        # print network
        self.print_network()
        self.load()

        # gamma
        self.gamma = 2.24
        self.percentile = 99

        if self.is_train:
            self.netG.train()

            # loss
            loss_type = train_opt['pixel_criterion']
            if loss_type == 'l1':
                self.cri_pix = nn.L1Loss().to(self.device)
            elif loss_type == 'l2':
                self.cri_pix = nn.MSELoss().to(self.device)
            elif loss_type == 'tanh_l1':
                self.cri_pix = tanh_L1Loss().to(self.device)
            elif loss_type == 'tanh_l2':
                self.cri_pix = tanh_L2Loss().to(self.device)
            else:
                raise NotImplementedError('Loss type [{:s}] is not recognized.'.format(loss_type))
            self.l_pix_w = train_opt['pixel_weight']

            # optimizers
            wd_G = train_opt['weight_decay_G'] if train_opt['weight_decay_G'] else 0
            optim_params = []
            for k, v in self.netG.named_parameters():  # can optimize for a part of the model
                if v.requires_grad:
                    optim_params.append(v)
                else:
                    if self.rank <= 0:
                        logger.warning('Params [{:s}] will not optimize.'.format(k))
            self.optimizer_G = torch.optim.Adam(optim_params, lr=train_opt['lr_G'],
                                                weight_decay=wd_G,
                                                betas=(train_opt['beta1'], train_opt['beta2']))
            self.optimizers.append(self.optimizer_G)

            # schedulers
            if train_opt['lr_scheme'] == 'MultiStepLR':
                for optimizer in self.optimizers:
                    self.schedulers.append(
                        lr_scheduler.MultiStepLR_Restart(optimizer, train_opt['lr_steps'],
                                                         restarts=train_opt['restarts'],
                                                         weights=train_opt['restart_weights'],
                                                         gamma=train_opt['lr_gamma'],
                                                         clear_state=train_opt['clear_state']))
            elif train_opt['lr_scheme'] == 'CosineAnnealingLR_Restart':
                for optimizer in self.optimizers:
                    self.schedulers.append(
                        lr_scheduler.CosineAnnealingLR_Restart(
                            optimizer, train_opt['T_period'], eta_min=train_opt['eta_min'],
                            restarts=train_opt['restarts'], weights=train_opt['restart_weights']))
            else:
                raise NotImplementedError('MultiStepLR learning rate scheme is enough.')

            self.log_dict = OrderedDict()

    # LQGT_condition_dataset
    def feed_data(self, data, need_GT=True):
        self.var_L = data['LQ'].to(self.device)  # LQ
        self.var_cond = data['cond'].to(self.device) # cond
        
        if need_GT:
            self.real_H = data['GT'].to(self.device)  # GT
            # print(self.real_H.shape) [1, 3, 1060, 1900]
    
    # LDRsToHDR_dataset
    def feed_ldrs_data(self, data, need_GT=True):
        self.ldrs = data['img_LDRs'].to(self.device)
        self.exp = data['float_exp']
        if need_GT:
            self.real_H = data['GT'].to(self.device)  # GT


    def tanh_norm_mu_tonemap(self, hdr_image, norm_value, mu=5000):
        bounded_hdr = torch.tanh(hdr_image / norm_value)
        return self.mu_tonemap(bounded_hdr, mu)


    def mu_tonemap(self, hdr_image, mu=5000):
        return torch.log(1 + mu * hdr_image) / math.log(1 + mu)
    
    def optimize_parameters(self, step):
        self.optimizer_G.zero_grad()
        self.fake_H = self.netG(self.ldrs, self.exp)
        
        hdr_linear_ref = self.fake_H ** self.gamma
        hdr_linear_res = self.real_H ** self.gamma
        norm_perc = np.percentile(hdr_linear_ref.data.cpu().numpy().astype(np.float32), self.percentile)
        self.fake_H = self.tanh_norm_mu_tonemap(hdr_linear_ref, norm_perc)
        self.real_H  = self.tanh_norm_mu_tonemap(hdr_linear_res, norm_perc)


        l_pix = self.l_pix_w * self.cri_pix(self.fake_H, self.real_H)
        l_pix.backward()
        self.optimizer_G.step()

        # set log
        self.log_dict['l_pix'] = l_pix.item()

    def test(self):
        self.netG.eval()
        with torch.no_grad():
            self.fake_H = self.netG(self.ldrs, self.exp)
            
        self.netG.train()

    def get_current_log(self):
        return self.log_dict

    def get_current_visuals(self, need_GT=True):
        out_dict = OrderedDict()
        out_dict['img_LDRs'] = self.ldrs.detach()[0].float().cpu()
        out_dict['Result'] = self.fake_H.detach()[0].float().cpu()
        
        if need_GT:
            out_dict['GT'] = self.real_H.detach()[0].float().cpu()
        return out_dict

    def print_network(self):
        s, n = self.get_network_description(self.netG)
        if isinstance(self.netG, nn.DataParallel) or isinstance(self.netG, DistributedDataParallel):
            net_struc_str = '{} - {}'.format(self.netG.__class__.__name__,
                                             self.netG.module.__class__.__name__)
        else:
            net_struc_str = '{}'.format(self.netG.__class__.__name__)
        if self.rank <= 0:
            logger.info('Network G structure: {}, with parameters: {:,d}'.format(net_struc_str, n))
            logger.info(s)

    ### load model
    def load(self):
        load_path_G = self.opt['path']['pretrain_model_G']
        if load_path_G is not None:
            logger.info('Loading model for G [{:s}] ...'.format(load_path_G))
            self.load_network(load_path_G, self.netG, self.opt['path']['strict_load'])

    def save(self, iter_label):
        self.save_network(self.netG, 'G', iter_label)
