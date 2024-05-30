#!/usr/bin/env python
# coding=utf-8
'''
Author: JiangJi
Email: johnjim0816@gmail.com
Date: 2023-12-25 09:28:26
LastEditor: JiangJi
LastEditTime: 2024-05-26 20:17:08
Discription: 
'''
from enum import Enum
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Categorical,Normal
from joyrl.algos.base.base_layer import LayerConfig
from joyrl.algos.base.base_layer import create_layer

class ActionLayerType(Enum):
    ''' Action layer type
    '''
    DISCRETE = 1
    CONTINUOUS = 2
    DPG = 3
    DQNACTION = 4
    
class BaseActionLayer(nn.Module):
    def __init__(self,cfg, input_size, action_dim, id = 0, **kwargs):
        super(BaseActionLayer, self).__init__()
        self.cfg = cfg
        self.id = id

    def get_action(self, **kwargs):
        mode = kwargs.get("mode", "sample")
        actor_output = kwargs.get("actor_output", None)
        if mode == "sample":
            return self.sample_action(**actor_output)
        elif mode == "predict":
            return self.predict_action(**actor_output)
        else:
            raise NotImplementedError
        
class DQNActionLayer(BaseActionLayer):
    def __init__(self, cfg, input_size, action_dim, id = 0, **kwargs):
        super(DQNActionLayer, self).__init__(cfg=cfg, input_size=input_size, action_dim=action_dim, id=id)
        self.action_dim = action_dim
        output_size = input_size
        self.dueling = hasattr(cfg, 'dueling') and cfg.dueling
        if self.dueling:
            state_value_layer_cfg = LayerConfig(layer_type='linear', layer_size=[1], activation='none')
            self.state_value_layer, _ = create_layer(output_size, state_value_layer_cfg)
            action_value_layer_cfg = LayerConfig(layer_type='linear', layer_size=[action_dim], activation='none')
            self.action_value_layer, _ = create_layer(output_size, action_value_layer_cfg)
        else:
            action_layer_cfg = LayerConfig(layer_type='linear', layer_size=[action_dim], activation='none')
            self.action_value_layer, _ = create_layer(output_size, action_layer_cfg)

    def forward(self, x, **kwargs):
        if self.dueling:
            state_value = self.state_value_layer(x)
            action_value = self.action_value_layer(x)
            q_value = state_value + action_value - action_value.mean(dim=1, keepdim=True)
        else:
            q_value = self.action_value_layer(x)
        output = {"q_value": q_value}
        self.q_value = q_value
        return output
    
    def get_qvalue(self):
        return self.q_value
    
    def get_action(self, **kwargs):
        mode = kwargs.get("mode", "sample")
        if mode == "sample":
            return self.sample_action()
        elif mode == "predict":
            return self.predict_action()
        else:
            raise NotImplementedError
        
    def sample_action(self):
        return torch.argmax(self.q_value).detach().cpu().numpy().item()
    
    def predict_action(self):
        ''' get action
        '''
        return torch.argmax(self.q_value).detach().cpu().numpy().item()

class DiscreteActionLayer(BaseActionLayer):
    def __init__(self, cfg, input_size, action_dim, id = 0, **kwargs):
        super(DiscreteActionLayer, self).__init__(cfg=cfg, input_size=input_size, action_dim=action_dim, id=id)
        self.min_policy = cfg.min_policy
        self.action_dim = action_dim
        output_size = input_size
        action_layer_cfg = LayerConfig(layer_type='linear', layer_size=[self.action_dim], activation='leakyrelu')
        self.logits_p_layer, _ = create_layer(output_size, action_layer_cfg)
        self.probs = None

    def forward(self, x, **kwargs):
        pre_legal_action = kwargs.get("pre_legal_action", None)   
        logits_p = self.logits_p_layer(x)
        if pre_legal_action is not None:
            pre_legal_action = logits_p.type(logits_p.dtype)
            large_negative = torch.finfo(torch.float16).min if logits_p.dtype == torch.float16 else -1e9
            mask_logits_p = logits_p * pre_legal_action + (1 - pre_legal_action) * large_negative
            probs = F.softmax(mask_logits_p, dim=1)
        else:
            probs = F.softmax(logits_p - logits_p.max(dim=1, keepdim=True).values, dim=1) # avoid overflow
            probs = (probs + self.min_policy) / (1.0 + self.min_policy * self.action_dim) # add a small probability to explore
        output = {"probs": probs}
        self.probs = probs
        return output

    def sample_action(self):
        ''' get action
        '''
        dist = Categorical(self.probs)
        action = dist.sample()
        self.log_prob = dist.log_prob(action)
        action = action.detach().cpu().numpy().item()
        return action
    
    def predict_action(self):
        ''' get action
        '''
        return torch.argmax(self.probs).detach().cpu().numpy().item()
    
    def get_log_prob(self):
        ''' get log_probs
        '''
        return self.log_prob
    
    def get_log_prob_action(self, action):
        ''' get log_probs
        '''
        # action shape is [batch_size]
        if not isinstance(action, torch.Tensor):
            action = torch.tensor(action, dtype=torch.int64, device=self.probs.device)
        dist = Categorical(self.probs)
        log_prob = dist.log_prob(action)
        return log_prob
    
    def get_mean_entropy(self):
        ''' get entropy
        '''
        dist = Categorical(self.probs)
        entropy = dist.entropy()
        entropy_mean = torch.mean(entropy)
        return entropy_mean
 
class ContinuousActionLayer(BaseActionLayer):
    def __init__(self, cfg, input_size, action_dim, id = 0, **kwargs):
        super(ContinuousActionLayer, self).__init__(cfg=cfg, input_size=input_size, action_dim=action_dim, id=id)
        self.action_high = self.cfg.action_high_list[self.id]
        self.action_low = self.cfg.action_low_list[self.id]
        self.action_scale = (self.action_high - self.action_low)/2
        self.action_bias = (self.action_high + self.action_low)/2
        self.min_policy = cfg.min_policy
        self.action_dim = action_dim
        output_size = input_size
        mu_layer_cfg = LayerConfig(layer_type='linear', layer_size=[self.action_dim], activation='tanh')
        self.mu_layer, _ = create_layer(output_size, mu_layer_cfg)
        self.log_std = nn.Parameter(torch.zeros(1, self.action_dim))

    def forward(self,x, **kwargs):
        mu = self.mu_layer(x)
        sigma = torch.ones_like(mu) * torch.exp(self.log_std)
        # log_prob = -0.5 * (sigma.log() + ((mu - x) / sigma).pow(2) + math.log(2 * math.pi))
        # sigma = F.softplus(self.fc4(x)) + 0.001 # std of normal distribution, add a small value to avoid 0
        # sigma = torch.clamp(sigma, min=-0.25, max=0.25) # clamp the std between 0.001 and 1
        self.mu = mu # [batch_size, 1]
        self.sigma = sigma # [batch_size]
        self.mean = self.mu * self.action_scale + self.action_bias
        self.std = self.sigma
        output = {"mu": self.mu, "sigma": self.sigma, "mean": self.mean, "std": self.std}
        return output
    
    def sample_action(self, **kwargs):
        ''' get action
        '''
        mean = kwargs.get("mean", None)
        std = kwargs.get("std", None)
        dist = Normal(mean, std)
        action = dist.sample()
        log_prob = dist.log_prob(action)
        return {"action": action.detach().cpu().numpy().item(), "log_prob": log_prob.detach().cpu().numpy().item()}


    def predict_action(self, **kwargs):
        ''' get action
        '''
        return {"action": self.mean.detach().cpu().numpy().item(), "log_prob": None}
    
    def get_log_prob(self, **kwargs):
        ''' get log_probs
        '''
        mean = kwargs.get("mean", None)
        std = kwargs.get("std", None)
        dist = Normal(mean, std)
        action = dist.sample()
        return self.log_prob
    
    def get_log_prob_action(self, actor_output, action):
        ''' get log_probs
        '''
        # action shape is [batch_size, action_dim]
        mean = actor_output.get("mean", None)
        std = actor_output.get("std", None)
        dist = Normal(mean, std)
        log_prob = dist.log_prob(action)
        return log_prob
    
    def get_entropy(self, actor_output):
        ''' get entropy
        '''
        mean = actor_output.get("mean", None)
        std = actor_output.get("std", None)
        dist = Normal(mean, std)
        entropy = dist.entropy()
        entropy_mean = torch.mean(entropy)
        return entropy_mean
    
class DPGActionLayer(BaseActionLayer):
    def __init__(self, cfg, input_size, action_dim, id = 0, **kwargs):
        super(DPGActionLayer, self).__init__(cfg=cfg, input_size=input_size, action_dim=action_dim, id=id)
        # self.action_high = self.cfg.action_high_list[self.id]
        # self.action_low = self.cfg.action_low_list[self.id]
        self.action_high = self.cfg.action_space.high
        self.action_low = self.cfg.action_space.low
        self.action_scale = (self.action_high - self.action_low)/2
        self.action_bias = (self.action_high + self.action_low)/2
        self.action_dim = action_dim
        self.output_size = input_size
        action_layer_cfg = LayerConfig(layer_type='linear', layer_size=[self.action_dim], activation='tanh')
        self.action_layer, layer_out_size = create_layer(self.output_size, action_layer_cfg)
        self.output_size = layer_out_size
        
    def forward(self,x, **kwargs):
        mu = self.action_layer(x)
        self.mu = mu
        output = {"mu": mu}
        return output
    
    def get_mu(self):
        ''' get mu
        '''
        return self.mu
    
    def get_action(self, **kwargs):
        mode = kwargs.get("mode", "sample")
        actor_output = kwargs.get("actor_output", None)
        if mode == "sample":
            return self.sample_action(actor_output)
        elif mode == "predict":
            return self.predict_action(actor_output)
        else:
            raise NotImplementedError
        
    def sample_action(self, actor_outputs):
        ''' get action
        '''
        device_ = self.mu.device 
        action = torch.FloatTensor(self.action_scale).to(device_) * self.mu + torch.FloatTensor(self.action_bias).to(device_)
        action = action.detach().cpu().numpy()[0]
        return action
    
    def predict_action(self, actor_outputs):
        ''' get action
        '''
        device_ = self.mu.device 
        action = torch.FloatTensor(self.action_scale).to(device_) * self.mu + torch.FloatTensor(self.action_bias).to(device_)
        action = action.detach().cpu().numpy()[0]
        return action
        