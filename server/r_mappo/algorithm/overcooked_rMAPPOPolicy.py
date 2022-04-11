import numpy as np
import torch
from r_mappo.algorithm.r_actor_critic import R_Actor, R_Critic
from r_mappo.utils.util import update_linear_schedule

class R_MAPPOPolicy:
    def __init__(self, args, obs_space, act_space, idx=0, device=torch.device("cpu")):
        self.device = device
        self.obs_space = obs_space
        self.act_space = act_space
        self.actor = R_Actor(args, self.obs_space, self.act_space, self.device)

        #for ovevrcooked demo
        self.agent_idx = idx


    def act(self, obs, rnn_states_actor, masks, available_actions=None, deterministic=False):
        actions, _, rnn_states_actor = self.actor(obs, rnn_states_actor, masks, available_actions, deterministic)
        return actions, rnn_states_actor
