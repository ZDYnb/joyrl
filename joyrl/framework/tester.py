#!/usr/bin/env python
# coding=utf-8
'''
Author: JiangJi
Email: johnjim0816@gmail.com
Date: 2023-12-02 15:02:30
LastEditor: JiangJi
LastEditTime: 2023-12-30 17:48:42
Discription: 
'''
import ray
import torch
import time
import copy
import os
import threading
from joyrl.framework.config import MergedConfig
from joyrl.framework.base import Moduler
from joyrl.utils.utils import exec_method
    
class OnlineTester(Moduler):
    ''' Online tester
    '''
    def __init__(self, cfg : MergedConfig, *args, **kwargs) -> None:
        super().__init__(cfg, *args, **kwargs)
        self.env = kwargs['env']
        self.policy = copy.deepcopy(kwargs['policy'])
        self.best_eval_reward = -float('inf')
        self.curr_test_step = -1
        self._t_start()

    def _t_start(self):
        exec_method(self.logger, 'info', False, "Start online tester!")
        self._t_eval_policy = threading.Thread(target=self._eval_policy)
        self._t_eval_policy.setDaemon(True)
        self._t_eval_policy.start()
    
    def _check_updated_model(self):
        model_step_list = os.listdir(self.cfg.model_dir)
        model_step_list = [int(model_step) for model_step in model_step_list if model_step.isdigit()]
        model_step_list.sort()
        if len(model_step_list) == 0:
            return False, -1
        elif model_step_list[-1] == self.curr_test_step:
            return False, -1
        elif model_step_list[-1] > self.curr_test_step:
            return True, model_step_list[-1]
        
    def _eval_policy(self, *args, **kwargs):
        ''' Evaluate policy
        '''
        while True:
            updated, model_step = self._check_updated_model()
            if updated:
                self.curr_test_step = model_step
                self.policy.load_model(f"{self.cfg.model_dir}/{self.curr_test_step}")
                sum_eval_reward = 0
                for _ in range(self.cfg.online_eval_episode):
                    state, info = self.env.reset()
                    ep_reward, ep_step = 0, 0
                    while True:
                        action = self.policy.get_action(state, mode = 'predict')
                        next_state, reward, terminated, truncated, info = self.env.step(action)
                        state = next_state
                        ep_reward += reward
                        ep_step += 1
                        if terminated or truncated or (0<= self.cfg.max_step <= ep_step):
                            sum_eval_reward += ep_reward
                            break
                mean_eval_reward = sum_eval_reward / self.cfg.online_eval_episode
                logger_info = f"test_step: {self.curr_test_step}, online_eval_reward: {mean_eval_reward:.3f}"
                self.logger.info.remote(logger_info) if self.use_ray else self.logger.info(logger_info)
                if mean_eval_reward >= self.best_eval_reward:
                    logger_info = f"current test step obtain a better online_eval_reward: {mean_eval_reward:.3f}, save the best model!"
                    self.logger.info.remote(logger_info) if self.use_ray else self.logger.info(logger_info)
                    self.policy.save_model(f"{self.cfg.model_dir}/best")
                    self.best_eval_reward = mean_eval_reward
            time.sleep(1)
        
