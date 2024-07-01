r""" VAT """
from functools import reduce
from operator import add

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet
from torchvision.models import vgg

from .base.feature import extract_feat_vgg, extract_feat_res
from .base.correlation import Correlation
from .learner import VATLearner

import numpy as np


class Attention_block(nn.Module):
    def __init__(self,F_g,F_l,F_int):
        super(Attention_block,self).__init__()
        self.W_g = nn.Sequential(
            nn.Conv2d(F_g, F_int, kernel_size=1,stride=1,padding=0,bias=True),
            nn.BatchNorm2d(F_int)
            )
        
        self.W_x = nn.Sequential(
            nn.Conv2d(F_l, F_int, kernel_size=1,stride=1,padding=0,bias=True),
            nn.BatchNorm2d(F_int)
        )

        self.psi = nn.Sequential(
            nn.Conv2d(F_int, 1, kernel_size=1,stride=1,padding=0,bias=True),
            nn.BatchNorm2d(1),
            nn.Sigmoid()
        )
        
        self.relu = nn.ReLU(inplace=True)
#        self.m = nn.Softmax(dim=-1)        
        
    def forward(self,x,g):
        g1 = self.W_g(g)
        x1 = self.W_x(x)
        psi = self.relu(g1+x1)
        psi = self.psi(psi)
#        psi = self.m(psi)
        
        return x*psi



class VAT(nn.Module):
    def __init__(self, cfg, use_original_imgsize):
        super(VAT, self).__init__()
        self.cfg = cfg

        # 1. Backbone network initialization
        self.backbone_type = cfg.TRAIN.BACKBONE
        self.use_original_imgsize = use_original_imgsize
        if self.backbone_type == 'vgg16':
            self.backbone = vgg.vgg16(pretrained=True)
            self.feat_ids = [17, 19, 21, 24, 26, 28, 30]
            self.extract_feats = extract_feat_vgg
            nbottlenecks = [2, 2, 3, 3, 3, 1]
        elif self.backbone_type == 'resnet50':
            self.backbone = resnet.resnet50(pretrained=True)
            self.feat_ids = list(range(3, 17))
            self.extract_feats = extract_feat_res
            nbottlenecks = [3, 4, 6, 3]
            nbottlenecks2 = [6, 8, 12, 6]
            
        elif self.backbone_type == 'resnet101':
            self.backbone = resnet.resnet101(pretrained=True)
            self.feat_ids = list(range(3, 34))
            self.extract_feats = extract_feat_res
            nbottlenecks = [3, 4, 23, 3]
            nbottlenecks2 = [6, 8, 46, 6]            
        else:
            raise Exception('Unavailable backbone: %s' % self.backbone_type)

        self.bottleneck_ids = reduce(add, list(map(lambda x: list(range(x)), nbottlenecks)))
        self.lids = reduce(add, [[i + 1] * x for i, x in enumerate(nbottlenecks)])
        self.stack_ids = torch.tensor(self.lids).bincount().__reversed__().cumsum(dim=0)[:3]
        self.backbone.eval()
        self.hpn_learner = VATLearner(cfg, inch=list(reversed(nbottlenecks2[-3:])))
        self.cross_entropy_loss = nn.CrossEntropyLoss()
        self.Att1 = Attention_block(3,3,3)
        self.Att2 = Attention_block(6,6,6)
        self.Att3 = Attention_block(4,4,4)
        
    def stack_feats(self, feats):

        feats_l4 = torch.stack(feats[-self.stack_ids[0]:]).transpose(0, 1)
        feats_l3 = torch.stack(feats[-self.stack_ids[1]:-self.stack_ids[0]]).transpose(0, 1)
        feats_l2 = torch.stack(feats[-self.stack_ids[2]:-self.stack_ids[1]]).transpose(0, 1)
        feats_l1 = torch.stack(feats[:-self.stack_ids[2]]).transpose(0, 1)

        return [feats_l4, feats_l3, feats_l2, feats_l1]
    
    def resize_feats(self, feats, stack_ids):
        slices = (
            slice(None, -stack_ids[2]),
            slice(-stack_ids[2], -stack_ids[1]),
            slice(-stack_ids[1], -stack_ids[0]),
            slice(-stack_ids[0], None),
        )
        img_size = (128, 64, 32, 16)
        resized_feats = []
        for s, size in zip(slices, img_size):
            for feat in feats[s]:
                resized_feats.append(F.interpolate(feat, size=size, mode='bilinear', align_corners=True))
        
        return resized_feats

    def forward(self, query_img, support_img, support_mask):
        with torch.no_grad():
            query_feats = self.extract_feats(query_img, self.backbone, self.feat_ids, self.bottleneck_ids, self.lids)

            supprot_img2 = torch.zeros_like(support_img)            
            
            supprot_img2[:,0,:,:]= support_img[:,0,:,:]*support_mask 
            supprot_img2[:,1,:,:]= support_img[:,1,:,:]*support_mask  
            supprot_img2[:,2,:,:]= support_img[:,2,:,:]*support_mask  

            support_feats = self.extract_feats(support_img, self.backbone, self.feat_ids, self.bottleneck_ids, self.lids)         
            Input_masking = self.extract_feats(supprot_img2, self.backbone, self.feat_ids, self.bottleneck_ids, self.lids)

            query_feats = self.resize_feats(query_feats, self.stack_ids)
            support_feats = self.resize_feats(support_feats, self.stack_ids)
            Input_masking = self.resize_feats(Input_masking, self.stack_ids)

            corr_1 = Correlation.multilayer_correlation(query_feats[-self.stack_ids[-1]:], support_feats[-self.stack_ids[-1]:], self.stack_ids)
            corr_2 = Correlation.multilayer_correlation(query_feats[-self.stack_ids[-1]:], Input_masking[-self.stack_ids[-1]:], self.stack_ids)
            

        corr3=[]
#        load = load3
        for i in range(0, len(corr_1)):
            corr3.append(torch.cat([corr_1[i],corr_2[i]], dim=1))


        logit_mask = self.hpn_learner(corr3, self.stack_feats(query_feats), support_mask)
        if not self.use_original_imgsize:
            logit_mask = F.interpolate(logit_mask, support_img.size()[2:], mode='bilinear', align_corners=True)

        return logit_mask

    def mask_feature(self, features, support_mask):
        for idx, feature in enumerate(features):
            mask = F.interpolate(support_mask.unsqueeze(1).float(), feature.size()[2:], mode='bilinear', align_corners=True)
            features[idx] = features[idx] * mask
        return features

    def predict_mask_nshot(self, batch, nshot):

        # Perform multiple prediction given (nshot) number of different support sets
        logit_mask_agg = 0
        for s_idx in range(nshot):
            logit_mask = self(batch['query_img'], batch['support_imgs'][:, s_idx], batch['support_masks'][:, s_idx])

            if self.use_original_imgsize:
                org_qry_imsize = tuple([batch['org_query_imsize'][1].item(), batch['org_query_imsize'][0].item()])
                logit_mask = F.interpolate(logit_mask, org_qry_imsize, mode='bilinear', align_corners=True)

            logit_mask_agg += logit_mask.argmax(dim=1).clone()
            if nshot == 1: return logit_mask_agg

        # Average & quantize predictions given threshold (=0.5)
        bsz = logit_mask_agg.size(0)
        max_vote = logit_mask_agg.view(bsz, -1).max(dim=1)[0]
        max_vote = torch.stack([max_vote, torch.ones_like(max_vote).long()])
        max_vote = max_vote.max(dim=0)[0].view(bsz, 1, 1)
        pred_mask = logit_mask_agg.float() / max_vote
        pred_mask[pred_mask < 0.5] = 0
        pred_mask[pred_mask >= 0.5] = 1

        return pred_mask

    def compute_objective(self, logit_mask, gt_mask):
        bsz = logit_mask.size(0)
        logit_mask = logit_mask.view(bsz, 2, -1)
        gt_mask = gt_mask.view(bsz, -1).long()

        return self.cross_entropy_loss(logit_mask, gt_mask)

    def train_mode(self):
        self.train()
        self.backbone.eval()  # to prevent BN from learning data statistics with exponential averaging