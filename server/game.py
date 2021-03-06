from abc import ABC, abstractmethod
from threading import Lock, Thread
from queue import Queue, LifoQueue, Empty, Full
from time import time
import numpy as np
from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv
from overcooked_ai_py.mdp.actions import Action, Direction
from overcooked_ai_py.planning.planners import MotionPlanner, NO_COUNTERS_PARAMS
#from human_aware_rl.rllib.rllib import load_agent
from r_mappo.algorithm.overcooked_rMAPPOPolicy import R_MAPPOPolicy as Policy
import random, os, pickle, json
import gym
import ray
import pdb
import torch

# Relative path to where all static pre-trained agents are stored on server
AGENT_DIR = None

# Maximum allowable game time (in seconds)
MAX_GAME_TIME = None

class Dict2obj(dict):
    __setattr__ = dict.__setitem__
    __getattr__ = dict.__getitem__

def dict2obj(dictObj):
    if not isinstance(dictObj,dict):
        return dictObj
    d = Dict2obj()
    for k,v in dictObj.items():
        d[k] = dict2obj(v)
    return d

def _configure(max_game_time, agent_dir):
    global AGENT_DIR, MAX_GAME_TIME
    MAX_GAME_TIME = max_game_time
    AGENT_DIR = agent_dir

def _loadPPOargs(loadargs):
    global PPO_args
    obj = dict2obj(loadargs)
    PPO_args = obj
    

def _t2n(x):
    return x.detach().cpu().numpy()

class Game(ABC):

    """
    Class representing a game object. Coordinates the simultaneous actions of arbitrary
    number of players. Override this base class in order to use. 

    Players can post actions to a `pending_actions` queue, and driver code can call `tick` to apply these actions.


    It should be noted that most operations in this class are not on their own thread safe. Thus, client code should
    acquire `self.lock` before making any modifications to the instance. 

    One important exception to the above rule is `enqueue_actions` which is thread safe out of the box
    """

    # Possible TODO: create a static list of IDs used by the class so far to verify id uniqueness
    # This would need to be serialized, however, which might cause too great a performance hit to 
    # be worth it

    EMPTY = 'EMPTY'
    
    class Status:
        DONE = 'done'
        ACTIVE = 'active'
        RESET = 'reset'
        INACTIVE = 'inactive'
        ERROR = 'error'



    def __init__(self, *args, **kwargs):
        """
        players (list): List of IDs of players currently in the game
        spectators (set): Collection of IDs of players that are not allowed to enqueue actions but are currently watching the game
        id (int):   Unique identifier for this game
        pending_actions List[(Queue)]: Buffer of (player_id, action) pairs have submitted that haven't been commited yet
        lock (Lock):    Used to serialize updates to the game state
        is_active(bool): Whether the game is currently being played or not
        """
        self.players = []
        self.spectators = set()
        self.pending_actions = []
        self.id = kwargs.get('id', id(self))
        self.lock = Lock()
        self._is_active = False

    @abstractmethod
    def is_full(self):
        """
        Returns whether there is room for additional players to join or not
        """
        pass

    @abstractmethod
    def apply_action(self, player_idx, action):
        """
        Updates the game state by applying a single (player_idx, action) tuple. Subclasses should try to override this method
        if possible
        """
        pass


    @abstractmethod
    def is_finished(self):
        """
        Returns whether the game has concluded or not
        """
        pass

    def is_ready(self):
        """
        Returns whether the game can be started. Defaults to having enough players
        """
        return self.is_full()

    @property
    def is_active(self):
        """
        Whether the game is currently being played
        """
        return self._is_active

    @property
    def reset_timeout(self):
        """
        Number of milliseconds to pause game on reset
        """
        return 3000

    def apply_actions(self):
        """
        Updates the game state by applying each of the pending actions in the buffer. Is called by the tick method. Subclasses
        should override this method if joint actions are necessary. If actions can be serialized, overriding `apply_action` is 
        preferred
        """
        for i in range(len(self.players)):
            try:
                while True:
                    action = self.pending_actions[i].get(block=False)
                    self.apply_action(i, action)
            except Empty:
                pass

    def activate(self):
        """
        Activates the game to let server know real-time updates should start. Provides little functionality but useful as
        a check for debugging
        """
        self._is_active = True

    def deactivate(self):
        """
        Deactives the game such that subsequent calls to `tick` will be no-ops. Used to handle case where game ends but 
        there is still a buffer of client pings to handle
        """
        self._is_active = False

    def reset(self):
        """
        Restarts the game while keeping all active players by resetting game stats and temporarily disabling `tick`
        """
        if not self.is_active:
            raise ValueError("Inactive Games cannot be reset")
        if self.is_finished():
            return self.Status.DONE
        self.deactivate()
        self.activate()
        return self.Status.RESET

    def needs_reset(self):
        """
        Returns whether the game should be reset on the next call to `tick`
        """
        return False


    def tick(self):
        """
        Updates the game state by applying each of the pending actions. This is done so that players cannot directly modify
        the game state, offering an additional level of safety and thread security. 

        One can think of "enqueue_action" like calling "git add" and "tick" like calling "git commit"

        Subclasses should try to override `apply_actions` if possible. Only override this method if necessary
        """ 
        
        if not self.is_active:
            return self.Status.INACTIVE
        if self.needs_reset():
            self.reset()
            return self.Status.RESET
        self.apply_actions()
        return self.Status.DONE if self.is_finished() else self.Status.ACTIVE
    
    def enqueue_action(self, player_id, action):
        """
        Add (player_id, action) pair to the pending action queue, without modifying underlying game state

        Note: This function IS thread safe
        """
        if not self.is_active:
            # Could run into issues with is_active not being thread safe
            return
        if player_id not in self.players:
            # Only players actively in game are allowed to enqueue actions
            return
        try:
            player_idx = self.players.index(player_id)
            self.pending_actions[player_idx].put(action)
        except Full:
            pass

    def get_state(self):
        """
        Return a JSON compatible serialized state of the game. Note that this should be as minimalistic as possible
        as the size of the game state will be the most important factor in game performance. This is sent to the client
        every frame update.
        """
        return { "players" : self.players }

    def to_json(self):
        """
        Return a JSON compatible serialized state of the game. Contains all information about the game, does not need to
        be minimalistic. This is sent to the client only once, upon game creation
        """
        return self.get_state()

    def is_empty(self):
        """
        Return whether it is safe to garbage collect this game instance
        """
        return not self.num_players

    def add_player(self, player_id, idx=None, buff_size=-1):
        """
        Add player_id to the game
        """
        if self.is_full():
            raise ValueError("Cannot add players to full game")
        if self.is_active:
            raise ValueError("Cannot add players to active games")
        if not idx and self.EMPTY in self.players:
            idx = self.players.index(self.EMPTY)
        elif not idx:
            idx = len(self.players)
        
        padding = max(0, idx - len(self.players) + 1)
        for _ in range(padding):
            self.players.append(self.EMPTY)
            self.pending_actions.append(self.EMPTY)
        
        self.players[idx] = player_id
        self.pending_actions[idx] = Queue(maxsize=buff_size)

    def add_spectator(self, spectator_id):
        """
        Add spectator_id to list of spectators for this game
        """
        if spectator_id in self.players:
            raise ValueError("Cannot spectate and play at same time")
        self.spectators.add(spectator_id)

    def remove_player(self, player_id):
        """
        Remove player_id from the game
        """
        try:
            idx = self.players.index(player_id)
            self.players[idx] = self.EMPTY
            self.pending_actions[idx] = self.EMPTY
        except ValueError:
            return False
        else:
            return True

    def remove_spectator(self, spectator_id):
        """
        Removes spectator_id if they are in list of spectators. Returns True if spectator successfully removed, False otherwise
        """
        try:
            self.spectators.remove(spectator_id)
        except ValueError:
            return False
        else:
            return True


    def clear_pending_actions(self):
        """
        Remove all queued actions for all players
        """
        for i, player in enumerate(self.players):
            if player != self.EMPTY:
                queue = self.pending_actions[i]
                queue.queue.clear()

    @property
    def num_players(self):
        return len([player for player in self.players if player != self.EMPTY])

    def get_data(self):
        """
        Return any game metadata to server driver. Really only relevant for Psiturk code
        """
        return {}
        


class DummyGame(Game):

    """
    Standin class used to test basic server logic
    """

    def __init__(self, **kwargs):
        super(DummyGame, self).__init__(**kwargs)
        self.counter = 0

    def is_full(self):
        return self.num_players == 2

    def apply_action(self, idx, action):
        pass

    def apply_actions(self):
        self.counter += 1

    def is_finished(self):
        return self.counter >= 100

    def get_state(self):
        state = super(DummyGame, self).get_state()
        state['count'] = self.counter
        return state


class DummyInteractiveGame(Game):

    """
    Standing class used to test interactive components of the server logic
    """

    def __init__(self, **kwargs):
        super(DummyInteractiveGame, self).__init__(**kwargs)
        self.max_players = int(kwargs.get('playerZero', 'human') == 'human') + int(kwargs.get('playerOne', 'human') == 'human')
        self.max_count = kwargs.get('max_count', 30)
        self.counter = 0
        self.counts = [0] * self.max_players

    def is_full(self):
        return self.num_players == self.max_players

    def is_finished(self):
        return max(self.counts) >= self.max_count

    def apply_action(self, player_idx, action):
        if action.upper() == Direction.NORTH:
            self.counts[player_idx] += 1
        if action.upper() == Direction.SOUTH:
            self.counts[player_idx] -= 1

    def apply_actions(self):
        super(DummyInteractiveGame, self).apply_actions()
        self.counter += 1

    def get_state(self):
        state = super(DummyInteractiveGame, self).get_state()
        state['count'] = self.counter
        for i in range(self.num_players):
            state['player_{}_count'.format(i)] = self.counts[i]
        return state

    
class OvercookedGame(Game):
    """
    Class for bridging the gap between Overcooked_Env and the Game interface

    Instance variable:
        - max_players (int): Maximum number of players that can be in the game at once
        - mdp (OvercookedGridworld): Controls the underlying Overcooked game logic
        - score (int): Current reward acheived by all players
        - max_time (int): Number of seconds the game should last
        - npc_policies (dict): Maps user_id to policy (Agent) for each AI player
        - npc_state_queues (dict): Mapping of NPC user_ids to LIFO queues for the policy to process
        - curr_tick (int): How many times the game server has called this instance's `tick` method
        - ticker_per_ai_action (int): How many frames should pass in between NPC policy forward passes. 
            Note that this is a lower bound; if the policy is computationally expensive the actual frames
            per forward pass can be higher
        - action_to_overcooked_action (dict): Maps action names returned by client to action names used by OvercookedGridworld
            Note that this is an instance variable and not a static variable for efficiency reasons
        - human_players (set(str)): Collection of all player IDs that correspond to humans
        - npc_players (set(str)): Collection of all player IDs that correspond to AI
        - randomized (boolean): Whether the order of the layouts should be randomized
    
    Methods:
        - npc_policy_consumer: Background process that asynchronously computes NPC policy forward passes. One thread
            spawned for each NPC
        - _curr_game_over: Determines whether the game on the current mdp has ended
    """

    def __init__(self, layouts=["cramped_room"], mdp_params={}, num_players=2, gameTime=30, playerZero='human', playerOne='human', showPotential=False, randomized=False, **kwargs):
        # print('init game')
        self.args = kwargs
        # print('game params: ', self.args)
        super(OvercookedGame, self).__init__(**kwargs)

        import time
        self.user_name = self.args['name']
        self.traj = []
        self.save_traj = showPotential != 'on'
        self.traj_save_dir = f'data/{self.args["layout"]}/{playerZero}-{playerOne}/{self.user_name}/'
        self.traj_save_path = self.traj_save_dir + time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()) + '.txt'


        # self.show_potential = showPotential
        self.show_potential = False
        self.mdp_params = mdp_params
        self.layouts = layouts
        self.max_players = int(num_players)
        self.mdp = None
        self.mp = None
        self.score = 0
        self.phi = 0

        self.cnt = 0

        self.max_time = min(int(gameTime), MAX_GAME_TIME)
        self.npc_policies = {}
        self.npc_state_queues = {}
        self.action_to_overcooked_action = {
            "STAY" : Action.STAY,
            "UP" : Direction.NORTH,
            "DOWN" : Direction.SOUTH,
            "LEFT" : Direction.WEST,
            "RIGHT" : Direction.EAST,
            "SPACE" : Action.INTERACT
        }
        # !!! ticks_per_ai_action act as idle
        self.ticks_per_ai_action = 7
        self.curr_tick = 0
        self.human_players = set()
        self.npc_players = set()
        #for PPO params
        self.num_agents = 2
        #self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        self.masks = np.ones((1, self.num_agents, 1), dtype=np.float32)
        self.curr_layout = self.layouts.pop()
        env_params = {'horizon': 800}
        self.mdp_fn = lambda: OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        self.mdp = self.mdp_fn()
        self.base_env = OvercookedEnv(self.mdp_fn, **env_params)
        self.featurize_fn = lambda state: self.mdp.lossless_state_encoding(state) # Encoding obs for PPO
        #self.featurize_fn_bc = lambda state: self.mdp.featurize_state(state) # Encoding obs for BC
        #self.featurize_fn_mapping = {
        #    "ppo": self.featurize_fn_ppo,
        #    "bc": self.featurize_fn_bc
        #}
        #self.mdp = OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        dummy_state = self.mdp.get_standard_start_state()

        #self.featurize_fn = lambda state: OvercookedEnv.from_mdp(self.mdp, horizon=400).lossless_state_encoding_mdp(state) # Encoding obs for PPO
        self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        self.obs = self._setup_observation_space()

        if randomized:
            random.shuffle(self.layouts)

        # print('before get policy')
        if playerZero != 'human':
            player_zero_id = playerZero + '_0'
            self.add_player(player_zero_id, idx=0, buff_size=1, is_human=False)
            self.npc_policies[player_zero_id] = self.get_policy(playerZero, self.args['layout'], idx=0)
            self.npc_state_queues[player_zero_id] = LifoQueue()

        if playerOne != 'human':
            player_one_id = playerOne + '_1'
            self.add_player(player_one_id, idx=1, buff_size=1, is_human=False)
            self.npc_policies[player_one_id] = self.get_policy(playerOne, self.args['layout'], idx=1)
            self.npc_state_queues[player_one_id] = LifoQueue()

    def _action_convertor(self, action):
        return [a[0] for a in list(action)]

    def _curr_game_over(self):
        return time() - self.start_time >= self.max_time


    def needs_reset(self):
        return self._curr_game_over() and not self.is_finished()

    def add_player(self, player_id, idx=None, buff_size=-1, is_human=True):
        super(OvercookedGame, self).add_player(player_id, idx=idx, buff_size=buff_size)
        if is_human:
            self.human_players.add(player_id)
        else:
            self.npc_players.add(player_id)

    def remove_player(self, player_id):
        removed = super(OvercookedGame, self).remove_player(player_id)
        if removed:
            if player_id in self.human_players:
                self.human_players.remove(player_id)
            elif player_id in self.npc_players:
                self.npc_players.remove(player_id)
            else:
                raise ValueError("Inconsistent state")


    def npc_policy_consumer(self, policy_id):
        queue = self.npc_state_queues[policy_id]
        policy = self.npc_policies[policy_id]
        while self._is_active:
            state = queue.get()
            #obs = np.stack(np.array([self.featurize_fn(state)]))*255
            obs = np.array([self.featurize_fn(state)[policy.agent_idx]])*255
            #npc_action, rnn_states = policy.act(np.expand_dims(obs[:,policy.agent_idx,:,:,:],axis=1),
            #                           np.expand_dims(self.rnn_states[:,policy.agent_idx,:,:],axis=1),
            #                           np.expand_dims(self.masks[:,policy.agent_idx,:],axis=1),
            #                           deterministic=True)
            npc_action, rnn_states = policy.act(obs,
                                       self.rnn_states[:,policy.agent_idx,:,:],
                                       self.masks[:,policy.agent_idx,:],
                                       deterministic=True)
            #actions = np.array(np.split(_t2n(npc_action), 1))
            action = self._action_convertor(npc_action)
            #print("agent",policy.agent_idx,action)
            npc_action = Action.INDEX_TO_ACTION[action[0]]
            #npc_action = Action.INDEX_TO_ACTION[action[0]]
            self.rnn_states[:,policy.agent_idx,:,:] = np.array(_t2n(rnn_states)).copy()
            super(OvercookedGame, self).enqueue_action(policy_id, npc_action)


    def is_full(self):
        return self.num_players >= self.max_players

    def is_finished(self):
        val = not self.layouts and self._curr_game_over()
        return val

    def is_empty(self):
        """
        Game is considered safe to scrap if there are no active players or if there are no humans (spectating or playing)
        """
        return super(OvercookedGame, self).is_empty() or not self.spectators and not self.human_players

    def is_ready(self):
        """
        Game is ready to be activated if there are a sufficient number of players and at least one human (spectator or player)
        """
        return super(OvercookedGame, self).is_ready() and not self.is_empty()

    def apply_action(self, player_id, action):
        pass

    def apply_actions(self):
        # Default joint action, as NPC policies and clients probably don't enqueue actions fast 
        # enough to produce one at every tick
        self.cnt += 1
        joint_action = [Action.STAY] * len(self.players)

        # Synchronize individual player actions into a joint-action as required by overcooked logic
        for i in range(len(self.players)):
            try:
                joint_action[i] = self.pending_actions[i].get(block=False)
                
            except Empty:
                pass
        #print("joint:",joint_action)
        # Apply overcooked game logic to get state transition
        prev_state = self.state
        self.state, sparse_reward, _ = self.mdp.get_state_transition(prev_state, joint_action)
        if self.show_potential:
            self.phi = self.mdp.potential_function(prev_state, self.mp, gamma=0.99)

        # Send next state to all background consumers if needed
        if self.curr_tick % self.ticks_per_ai_action == 0:
            for npc_id in self.npc_policies:
                self.npc_state_queues[npc_id].put(self.state, block=False)

        # Update score based on soup deliveries that might have occured
        curr_reward = sparse_reward
        self.score += curr_reward
        #print(prev_state, joint_action, sparse_reward,":",self.cnt)
        # Return about the current transition

        if self.save_traj:
            curr_reward = sparse_reward
            transition = {
                "state" : json.dumps(prev_state.to_dict()),
                "joint_action" : json.dumps(joint_action),
                "reward" : curr_reward,
                "time_left" : max(self.max_time - (time() - self.start_time), 0),
                "score" : self.score,
                "time_elapsed" : time() - self.start_time,
                "cur_gameloop" : self.curr_tick,
                "layout" : json.dumps(self.mdp.terrain_mtx),
                "layout_name" : self.curr_layout,
                "player_0_id" : self.players[0],
                "player_1_id" : self.players[1],
                "player_0_is_human" : self.players[0] in self.human_players,
                "player_1_is_human" : self.players[1] in self.human_players
            }

            self.traj.append(transition)

        return prev_state, joint_action, sparse_reward
        

    def enqueue_action(self, player_id, action):
        overcooked_action = self.action_to_overcooked_action[action]
        super(OvercookedGame, self).enqueue_action(player_id, overcooked_action)

    def reset(self):
        status = super(OvercookedGame, self).reset()
        if status == self.Status.RESET:
            # Hacky way of making sure game timer doesn't "start" until after reset timeout has passed
            self.start_time += self.reset_timeout / 1000


    def tick(self):
        self.curr_tick += 1
        return super(OvercookedGame, self).tick()

    def activate(self):
        super(OvercookedGame, self).activate()

        # Sanity check at start of each game
        if not self.npc_players.union(self.human_players) == set(self.players):
            raise ValueError("Inconsistent State")

        #self.curr_layout = self.layouts.pop()
        #self.mdp = OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        #dummy_state = self.mdp.get_standard_start_state()
        #self.featurize_fn = lambda state: OvercookedEnv.from_mdp(self.mdp, horizon=400).lossless_state_encoding_mdp(state) # Encoding obs for PPO
        #self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        #self.obs = self._setup_observation_space()
        #self.share_obs = self._setup_share_observation_space()
        self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        if self.show_potential:
            self.mp = MotionPlanner.from_pickle_or_compute(self.mdp, counter_goals=NO_COUNTERS_PARAMS)
        self.state = self.mdp.get_standard_start_state()
        if self.show_potential:
            self.phi = self.mdp.potential_function(self.state, self.mp, gamma=0.99)
        self.start_time = time()
        self.curr_tick = 0
        self.score = 0
        self.threads = []
        for npc_policy in self.npc_policies:
            #self.npc_policies[npc_policy].reset()
            self.npc_state_queues[npc_policy].put(self.state)
            t = Thread(target=self.npc_policy_consumer, args=(npc_policy,))
            self.threads.append(t)
            t.start()

    def deactivate(self):
        super(OvercookedGame, self).deactivate()
        # Ensure the background consumers do not hang
        for npc_policy in self.npc_policies:
            self.npc_state_queues[npc_policy].put(self.state)

        # Wait for all background threads to exit
        for t in self.threads:
            t.join()

        # Clear all action queues
        self.clear_pending_actions()

        if self.save_traj:
            if not os.path.exists(self.traj_save_dir):
                os.makedirs(self.traj_save_dir)
            with open(self.traj_save_path, 'w+') as f:
                json.dump(self.traj, f, indent=4)


    def get_state(self):
        state_dict = {}
        state_dict['potential'] = self.phi if self.show_potential else None
        state_dict['state'] = self.state.to_dict()
        state_dict['score'] = self.score
        state_dict['time_left'] = max(self.max_time - (time() - self.start_time), 0)
        return state_dict

    def to_json(self):
        obj_dict = {}
        obj_dict['terrain'] = self.mdp.terrain_mtx if self._is_active else None
        obj_dict['state'] = self.get_state() if self._is_active else None
        return obj_dict

    def _setup_observation_space(self):
        dummy_state = self.base_env.mdp.get_standard_start_state()
        featurize_fn_ppo = lambda state: self.mdp.lossless_state_encoding(state)
        obs_shape = featurize_fn_ppo(dummy_state)[0].shape
        high = np.ones(obs_shape) * float("inf")
        low = np.ones(obs_shape) * 0

        return gym.spaces.Box(np.float32(low), np.float32(high), dtype=np.float32)
        

    def get_policy(self, npc_id, layout, idx=0):
        # print('Start get policy')
        obs_space = []
        obs_space.append(self.obs)
        action_space = []
        action_space.append(gym.spaces.Discrete(len(Action.ALL_ACTIONS)))
        policy = Policy(PPO_args,
                             obs_space[0],
                             action_space[0],
                             idx)
        # for i, j in policy.actor.state_dict().items():
        #     print(i, j.shape)
        # TODO(th): rewrite policy path
        path = f'static/assets/agents/{npc_id}/{layout}_actor.pt'
        print(f'get policy from: {path}')
        policy_actor_state_dict = torch.load(path, map_location=torch.device('cpu'))
        policy.actor.load_state_dict(policy_actor_state_dict)
        policy.actor.eval()
        # print('Successfully get policy')
        return policy


        '''
        if npc_id.lower().startswith("rllib"):
            try:
                # Loading rllib agents requires additional helpers
                fpath = os.path.join(AGENT_DIR, npc_id, 'agent', 'agent')
                agent =  load_agent(fpath, agent_index=idx)
                return agent
            except Exception as e:
                raise IOError("Error loading Rllib Agent\n{}".format(e.__repr__()))
            finally:
                # Always kill ray after loading agent, otherwise, ray will crash once process exits
                if ray.is_initialized():
                    ray.shutdown()
        else:
            try:
                fpath = os.path.join(AGENT_DIR, npc_id, 'agent.pickle')
                with open(fpath, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                raise IOError("Error loading agent\n{}".format(e.__repr__()))
        '''

class OvercookedGame_new(Game):
    """
    Class for bridging the gap between Overcooked_Env and the Game interface

    Instance variable:
        - max_players (int): Maximum number of players that can be in the game at once
        - mdp (OvercookedGridworld): Controls the underlying Overcooked game logic
        - score (int): Current reward acheived by all players
        - max_time (int): Number of seconds the game should last
        - npc_policies (dict): Maps user_id to policy (Agent) for each AI player
        - npc_state_queues (dict): Mapping of NPC user_ids to LIFO queues for the policy to process
        - curr_tick (int): How many times the game server has called this instance's `tick` method
        - ticker_per_ai_action (int): How many frames should pass in between NPC policy forward passes. 
            Note that this is a lower bound; if the policy is computationally expensive the actual frames
            per forward pass can be higher
        - action_to_overcooked_action (dict): Maps action names returned by client to action names used by OvercookedGridworld
            Note that this is an instance variable and not a static variable for efficiency reasons
        - human_players (set(str)): Collection of all player IDs that correspond to humans
        - npc_players (set(str)): Collection of all player IDs that correspond to AI
        - randomized (boolean): Whether the order of the layouts should be randomized
    
    Methods:
        - npc_policy_consumer: Background process that asynchronously computes NPC policy forward passes. One thread
            spawned for each NPC
        - _curr_game_over: Determines whether the game on the current mdp has ended
    """

    def __init__(self, layouts=["cramped_room"], mdp_params={}, num_players=2, gameTime=30, playerZero='human', playerOne='human', showPotential=False, randomized=False, **kwargs):
        from overcooked_ai_py_new.mdp.overcooked_mdp import OvercookedGridworld
        from overcooked_ai_py_new.mdp.overcooked_env import OvercookedEnv
        from overcooked_ai_py_new.mdp.actions import Action, Direction
        from overcooked_ai_py_new.planning.planners import MotionPlanner, NO_COUNTERS_PARAMS
        # print('init game')
        self.args = kwargs
        # print('game params: ', self.args)
        super(OvercookedGame_new, self).__init__(**kwargs)

        import time
        self.user_name = self.args['name']
        self.traj = []
        self.save_traj = showPotential != 'on'
        self.traj_save_dir = f'data/{self.args["layout"]}/{playerZero}-{playerOne}/{self.user_name}/'
        self.traj_save_path = self.traj_save_dir + time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()) + '.txt'


        # self.show_potential = showPotential
        self.show_potential = False
        self.mdp_params = mdp_params
        self.layouts = layouts
        self.max_players = int(num_players)
        self.mdp = None
        self.mp = None
        self.score = 0
        self.phi = 0

        self.cnt = 0

        self.max_time = min(int(gameTime), MAX_GAME_TIME)
        self.npc_policies = {}
        self.npc_state_queues = {}
        self.action_to_overcooked_action = {
            "STAY" : Action.STAY,
            "UP" : Direction.NORTH,
            "DOWN" : Direction.SOUTH,
            "LEFT" : Direction.WEST,
            "RIGHT" : Direction.EAST,
            "SPACE" : Action.INTERACT
        }
        # !!! ticks_per_ai_action act as idle
        self.ticks_per_ai_action = 7
        self.curr_tick = 0
        self.human_players = set()
        self.npc_players = set()
        #for PPO params
        self.num_agents = 2
        #self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        self.masks = np.ones((1, self.num_agents, 1), dtype=np.float32)
        self.curr_layout = self.layouts.pop()
        env_params = {'horizon': 800}
        self.mdp_fn = lambda: OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        self.mdp = self.mdp_fn()
        self.base_env = OvercookedEnv(self.mdp_fn, **env_params)
        self.featurize_fn = lambda state: self.mdp.lossless_state_encoding(state) # Encoding obs for PPO
        #self.featurize_fn_bc = lambda state: self.mdp.featurize_state(state) # Encoding obs for BC
        #self.featurize_fn_mapping = {
        #    "ppo": self.featurize_fn_ppo,
        #    "bc": self.featurize_fn_bc
        #}
        #self.mdp = OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        dummy_state = self.mdp.get_standard_start_state()

        #self.featurize_fn = lambda state: OvercookedEnv.from_mdp(self.mdp, horizon=400).lossless_state_encoding_mdp(state) # Encoding obs for PPO
        self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        self.obs = self._setup_observation_space()

        if randomized:
            random.shuffle(self.layouts)

        # print('before get policy')
        if playerZero != 'human':
            player_zero_id = playerZero + '_0'
            self.add_player(player_zero_id, idx=0, buff_size=1, is_human=False)
            self.npc_policies[player_zero_id] = self.get_policy(playerZero, self.args['layout'], idx=0)
            self.npc_state_queues[player_zero_id] = LifoQueue()

        if playerOne != 'human':
            player_one_id = playerOne + '_1'
            self.add_player(player_one_id, idx=1, buff_size=1, is_human=False)
            self.npc_policies[player_one_id] = self.get_policy(playerOne, self.args['layout'], idx=1)
            self.npc_state_queues[player_one_id] = LifoQueue()

    def _action_convertor(self, action):
        return [a[0] for a in list(action)]

    def _curr_game_over(self):
        return time() - self.start_time >= self.max_time


    def needs_reset(self):
        return self._curr_game_over() and not self.is_finished()

    def add_player(self, player_id, idx=None, buff_size=-1, is_human=True):
        super(OvercookedGame_new, self).add_player(player_id, idx=idx, buff_size=buff_size)
        if is_human:
            self.human_players.add(player_id)
        else:
            self.npc_players.add(player_id)

    def remove_player(self, player_id):
        removed = super(OvercookedGame_new, self).remove_player(player_id)
        if removed:
            if player_id in self.human_players:
                self.human_players.remove(player_id)
            elif player_id in self.npc_players:
                self.npc_players.remove(player_id)
            else:
                raise ValueError("Inconsistent state")


    def npc_policy_consumer(self, policy_id):
        queue = self.npc_state_queues[policy_id]
        policy = self.npc_policies[policy_id]
        while self._is_active:
            state = queue.get()
            #obs = np.stack(np.array([self.featurize_fn(state)]))*255
            obs = np.array([self.featurize_fn(state)[policy.agent_idx]])*255
            #npc_action, rnn_states = policy.act(np.expand_dims(obs[:,policy.agent_idx,:,:,:],axis=1),
            #                           np.expand_dims(self.rnn_states[:,policy.agent_idx,:,:],axis=1),
            #                           np.expand_dims(self.masks[:,policy.agent_idx,:],axis=1),
            #                           deterministic=True)
            npc_action, rnn_states = policy.act(obs,
                                       self.rnn_states[:,policy.agent_idx,:,:],
                                       self.masks[:,policy.agent_idx,:],
                                       deterministic=True)
            #actions = np.array(np.split(_t2n(npc_action), 1))
            action = self._action_convertor(npc_action)
            #print("agent",policy.agent_idx,action)
            npc_action = Action.INDEX_TO_ACTION[action[0]]
            #npc_action = Action.INDEX_TO_ACTION[action[0]]
            self.rnn_states[:,policy.agent_idx,:,:] = np.array(_t2n(rnn_states)).copy()
            super(OvercookedGame_new, self).enqueue_action(policy_id, npc_action)


    def is_full(self):
        return self.num_players >= self.max_players

    def is_finished(self):
        val = not self.layouts and self._curr_game_over()
        return val

    def is_empty(self):
        """
        Game is considered safe to scrap if there are no active players or if there are no humans (spectating or playing)
        """
        return super(OvercookedGame_new, self).is_empty() or not self.spectators and not self.human_players

    def is_ready(self):
        """
        Game is ready to be activated if there are a sufficient number of players and at least one human (spectator or player)
        """
        return super(OvercookedGame_new, self).is_ready() and not self.is_empty()

    def apply_action(self, player_id, action):
        pass

    def apply_actions(self):
        # Default joint action, as NPC policies and clients probably don't enqueue actions fast 
        # enough to produce one at every tick
        self.cnt += 1
        joint_action = [Action.STAY] * len(self.players)

        # Synchronize individual player actions into a joint-action as required by overcooked logic
        for i in range(len(self.players)):
            try:
                joint_action[i] = self.pending_actions[i].get(block=False)
                
            except Empty:
                pass
        #print("joint:",joint_action)
        # Apply overcooked game logic to get state transition
        prev_state = self.state
        self.state, mdp_infos = self.mdp.get_state_transition(prev_state, joint_action)
        if self.show_potential:
            self.phi = self.mdp.potential_function(prev_state, self.mp, gamma=0.99)

        # Send next state to all background consumers if needed
        if self.curr_tick % self.ticks_per_ai_action == 0:
            for npc_id in self.npc_policies:
                self.npc_state_queues[npc_id].put(self.state, block=False)

        # Update score based on soup deliveries that might have occured
        curr_reward = sum(mdp_infos['sparse_reward_by_agent'])
        self.score += curr_reward
        #print(prev_state, joint_action, sparse_reward,":",self.cnt)
        # Return about the current transition

        if self.save_traj:
            curr_reward = sum(mdp_infos['sparse_reward_by_agent'])
            transition = {
                "state" : json.dumps(prev_state.to_dict()),
                "joint_action" : json.dumps(joint_action),
                "reward" : curr_reward,
                "time_left" : max(self.max_time - (time() - self.start_time), 0),
                "score" : self.score,
                "time_elapsed" : time() - self.start_time,
                "cur_gameloop" : self.curr_tick,
                "layout" : json.dumps(self.mdp.terrain_mtx),
                "layout_name" : self.curr_layout,
                "player_0_id" : self.players[0],
                "player_1_id" : self.players[1],
                "player_0_is_human" : self.players[0] in self.human_players,
                "player_1_is_human" : self.players[1] in self.human_players
            }

            self.traj.append(transition)

        return prev_state, joint_action, sum(mdp_infos['sparse_reward_by_agent'])
        

    def enqueue_action(self, player_id, action):
        overcooked_action = self.action_to_overcooked_action[action]
        super(OvercookedGame_new, self).enqueue_action(player_id, overcooked_action)

    def reset(self):
        status = super(OvercookedGame_new, self).reset()
        if status == self.Status.RESET:
            # Hacky way of making sure game timer doesn't "start" until after reset timeout has passed
            self.start_time += self.reset_timeout / 1000


    def tick(self):
        self.curr_tick += 1
        return super(OvercookedGame_new, self).tick()

    def activate(self):
        super(OvercookedGame_new, self).activate()

        # Sanity check at start of each game
        if not self.npc_players.union(self.human_players) == set(self.players):
            raise ValueError("Inconsistent State")

        #self.curr_layout = self.layouts.pop()
        #self.mdp = OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        #dummy_state = self.mdp.get_standard_start_state()
        #self.featurize_fn = lambda state: OvercookedEnv.from_mdp(self.mdp, horizon=400).lossless_state_encoding_mdp(state) # Encoding obs for PPO
        #self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        #self.obs = self._setup_observation_space()
        #self.share_obs = self._setup_share_observation_space()
        self.rnn_states = np.zeros((1, self.num_agents, PPO_args.recurrent_N, PPO_args.hidden_size), dtype=np.float32)
        if self.show_potential:
            self.mp = MotionPlanner.from_pickle_or_compute(self.mdp, counter_goals=NO_COUNTERS_PARAMS)
        self.state = self.mdp.get_standard_start_state()
        if self.show_potential:
            self.phi = self.mdp.potential_function(self.state, self.mp, gamma=0.99)
        self.start_time = time()
        self.curr_tick = 0
        self.score = 0
        self.threads = []
        for npc_policy in self.npc_policies:
            #self.npc_policies[npc_policy].reset()
            self.npc_state_queues[npc_policy].put(self.state)
            t = Thread(target=self.npc_policy_consumer, args=(npc_policy,))
            self.threads.append(t)
            t.start()

    def deactivate(self):
        super(OvercookedGame_new, self).deactivate()
        # Ensure the background consumers do not hang
        for npc_policy in self.npc_policies:
            self.npc_state_queues[npc_policy].put(self.state)

        # Wait for all background threads to exit
        for t in self.threads:
            t.join()

        # Clear all action queues
        self.clear_pending_actions()

        if self.save_traj:
            if not os.path.exists(self.traj_save_dir):
                os.makedirs(self.traj_save_dir)
            with open(self.traj_save_path, 'w+') as f:
                json.dump(self.traj, f, indent=4)


    def get_state(self):
        state_dict = {}
        state_dict['potential'] = self.phi if self.show_potential else None
        state_dict['state'] = self.state.to_dict()
        state_dict['score'] = self.score
        state_dict['time_left'] = max(self.max_time - (time() - self.start_time), 0)
        return state_dict

    def to_json(self):
        obj_dict = {}
        obj_dict['terrain'] = self.mdp.terrain_mtx if self._is_active else None
        obj_dict['state'] = self.get_state() if self._is_active else None
        return obj_dict

    def _setup_observation_space(self):
        dummy_state = self.base_env.mdp.get_standard_start_state()
        featurize_fn_ppo = lambda state: self.mdp.lossless_state_encoding(state)
        obs_shape = featurize_fn_ppo(dummy_state)[0].shape
        high = np.ones(obs_shape) * float("inf")
        low = np.ones(obs_shape) * 0

        return gym.spaces.Box(np.float32(low), np.float32(high), dtype=np.float32)
        

    def get_policy(self, npc_id, layout, idx=0):
        # print('Start get policy')
        obs_space = []
        obs_space.append(self.obs)
        action_space = []
        action_space.append(gym.spaces.Discrete(len(Action.ALL_ACTIONS)))
        policy = Policy(PPO_args,
                             obs_space[0],
                             action_space[0],
                             idx)
        # for i, j in policy.actor.state_dict().items():
        #     print(i, j.shape)
        # TODO(th): rewrite policy path
        path = f'static/assets/agents/{npc_id}/{layout}_actor.pt'
        print(f'get policy from: {path}')
        policy_actor_state_dict = torch.load(path, map_location=torch.device('cpu'))
        policy.actor.load_state_dict(policy_actor_state_dict)
        policy.actor.eval()
        # print('Successfully get policy')
        return policy


        '''
        if npc_id.lower().startswith("rllib"):
            try:
                # Loading rllib agents requires additional helpers
                fpath = os.path.join(AGENT_DIR, npc_id, 'agent', 'agent')
                agent =  load_agent(fpath, agent_index=idx)
                return agent
            except Exception as e:
                raise IOError("Error loading Rllib Agent\n{}".format(e.__repr__()))
            finally:
                # Always kill ray after loading agent, otherwise, ray will crash once process exits
                if ray.is_initialized():
                    ray.shutdown()
        else:
            try:
                fpath = os.path.join(AGENT_DIR, npc_id, 'agent.pickle')
                with open(fpath, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                raise IOError("Error loading agent\n{}".format(e.__repr__()))
        '''


class OvercookedPsiturk(OvercookedGame):
    """
    Wrapper on OvercookedGame that handles additional housekeeping for Psiturk experiments

    Instance Variables:
        - trajectory (list(dict)): list of state-action pairs in current trajectory
        - psiturk_uid (string): Unique id for each psiturk game instance (provided by Psiturk backend)
            Note, this is not the user id -- two users in the same game will have the same psiturk_uid
        - trial_id (string): Unique identifier for each psiturk trial, updated on each call to reset
            Note, one OvercookedPsiturk game handles multiple layouts. This is how we differentiate

    Methods:
        get_data: Returns the accumulated trajectory data and clears the self.trajectory instance variable
    
    """

    def __init__(self, *args, psiturk_uid='-1', **kwargs):
        super(OvercookedPsiturk, self).__init__(*args, showPotential=False, **kwargs)
        self.psiturk_uid = psiturk_uid
        self.trajectory = []

    def activate(self):
        """
        Resets trial ID at start of new "game"
        """
        super(OvercookedPsiturk, self).activate()
        self.trial_id = self.psiturk_uid + str(self.start_time)

    def apply_actions(self):
        """
        Applies pending actions then logs transition data
        """
        # Apply MDP logic
        prev_state, joint_action, info = super(OvercookedPsiturk, self).apply_actions()

        # Log data to send to psiturk client
        curr_reward = sum(info['sparse_reward_by_agent'])
        transition = {
            "state" : json.dumps(prev_state.to_dict()),
            "joint_action" : json.dumps(joint_action),
            "reward" : curr_reward,
            "time_left" : max(self.max_time - (time() - self.start_time), 0),
            "score" : self.score,
            "time_elapsed" : time() - self.start_time,
            "cur_gameloop" : self.curr_tick,
            "layout" : json.dumps(self.mdp.terrain_mtx),
            "layout_name" : self.curr_layout,
            "trial_id" : self.trial_id,
            "player_0_id" : self.players[0],
            "player_1_id" : self.players[1],
            "player_0_is_human" : self.players[0] in self.human_players,
            "player_1_is_human" : self.players[1] in self.human_players
        }

        self.trajectory.append(transition)

    def get_data(self):
        """
        Returns and then clears the accumulated trajectory
        """
        data = { "uid" : self.psiturk_uid  + "_" + str(time()), "trajectory" : self.trajectory }
        self.trajectory = []
        return data


class OvercookedTutorial(OvercookedGame):

    """
    Wrapper on OvercookedGame that includes additional data for tutorial mechanics, most notably the introduction of tutorial "phases"

    Instance Variables:
        - curr_phase (int): Indicates what tutorial phase we are currently on
        - phase_two_score (float): The exact sparse reward the user must obtain to advance past phase 2
    """
    

    def __init__(self, layouts=["tutorial_0"], mdp_params={}, playerZero='human', playerOne='AI', phaseTwoScore=15, **kwargs):
        super(OvercookedTutorial, self).__init__(layouts=layouts, mdp_params=mdp_params, playerZero=playerZero, playerOne=playerOne, showPotential=False, **kwargs)
        self.phase_two_score = phaseTwoScore
        self.phase_two_finished = False
        self.max_time = 0
        self.max_players = 2
        self.ticks_per_ai_action = 8
        self.curr_phase = 0

    @property
    def reset_timeout(self):
        return 1

    def needs_reset(self):
        if self.curr_phase == 0:
            return self.score > 0
        elif self.curr_phase == 1:
            return self.score > 0
        elif self.curr_phase == 2:
            return self.phase_two_finished
        return False 

    def is_finished(self):
        return not self.layouts and self.score >= float('inf')

    def reset(self):
        super(OvercookedTutorial, self).reset()
        self.curr_phase += 1

    def get_policy(self, *args, **kwargs):
        return TutorialAI()

    def apply_actions(self):
        """
        Apply regular MDP logic with retroactive score adjustment tutorial purposes
        """
        _, _, info = super(OvercookedTutorial, self).apply_actions()

        human_reward, ai_reward = info['sparse_reward_by_agent']

        # We only want to keep track of the human's score in the tutorial
        self.score -= ai_reward

        # Phase two requires a specific reward to complete
        if self.curr_phase == 2:
            self.score = 0
            if human_reward == self.phase_two_score:
                self.phase_two_finished = True





class DummyOvercookedGame(OvercookedGame):
    """
    Class that hardcodes the AI to be random. Used for debugging
    """
    
    def __init__(self, layouts=["cramped_room"], **kwargs):
        super(DummyOvercookedGame, self).__init__(layouts, **kwargs)

    def get_policy(self, *args, **kwargs):
        return DummyAI()


class DummyAI():
    """
    Randomly samples actions. Used for debugging
    """
    def action(self, state):
        [action] = random.sample([Action.STAY, Direction.NORTH, Direction.SOUTH, Direction.WEST, Direction.EAST, Action.INTERACT], 1)
        return action, None

    def reset(self):
        pass

class DummyComputeAI(DummyAI):
    """
    Performs simulated compute before randomly sampling actions. Used for debugging
    """
    def __init__(self, compute_unit_iters=1e5):
        """
        compute_unit_iters (int): Number of for loop cycles in one "unit" of compute. Number of 
                                    units performed each time is randomly sampled
        """
        super(DummyComputeAI, self).__init__()
        self.compute_unit_iters = int(compute_unit_iters)
    
    def action(self, state):
        # Randomly sample amount of time to busy wait
        iters = random.randint(1, 10) * self.compute_unit_iters

        # Actually compute something (can't sleep) to avoid scheduling optimizations
        val = 0
        for i in range(iters):
            # Avoid branch prediction optimizations
            if i % 2 == 0:
                val += 1
            else:
                val += 2
        
        # Return randomly sampled action
        return super(DummyComputeAI, self).action(state)

    
class StayAI():
    """
    Always returns "stay" action. Used for debugging
    """
    def action(self, state):
        return Action.STAY, None

    def reset(self):
        pass


class TutorialAI():

    COOK_SOUP_LOOP = [
        # Grab first onion
        Direction.WEST,
        Direction.WEST,
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Grab second onion
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Grab third onion
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Cook soup
        Action.INTERACT,
        
        # Grab plate
        Direction.EAST,
        Direction.SOUTH,
        Action.INTERACT,
        Direction.WEST,
        Direction.NORTH,

        # Deliver soup
        Action.INTERACT,
        Direction.EAST,
        Direction.EAST,
        Direction.EAST,
        Action.INTERACT,
        Direction.WEST
    ]

    COOK_SOUP_COOP_LOOP = [
        # Grab first onion
        Direction.WEST,
        Direction.WEST,
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.SOUTH,
        Action.INTERACT,

        # Move to start so this loops
        Direction.EAST,
        Direction.EAST,

        # Pause to make cooperation more real time
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY
    ]

    def __init__(self):
        self.curr_phase = -1
        self.curr_tick = -1

    def action(self, state):
        self.curr_tick += 1
        if self.curr_phase == 0:
            return self.COOK_SOUP_LOOP[self.curr_tick % len(self.COOK_SOUP_LOOP)], None
        elif self.curr_phase == 2:
            return self.COOK_SOUP_COOP_LOOP[self.curr_tick % len(self.COOK_SOUP_COOP_LOOP)], None
        return Action.STAY, None

    def reset(self):
        self.curr_tick = -1
        self.curr_phase += 1
