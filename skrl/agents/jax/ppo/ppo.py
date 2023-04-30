from typing import Union, Tuple, Dict, Any, Optional

import gym, gymnasium
import copy
import itertools
import functools

import jax
import jaxlib
import jax.numpy as jnp
import numpy as np

from skrl.memories.jax import Memory
from skrl.models.jax import Model
from skrl.resources.schedulers.jax import KLAdaptiveRL
from skrl.resources.optimizers.jax import Adam

from skrl.agents.jax import Agent
from skrl import config


PPO_DEFAULT_CONFIG = {
    "rollouts": 16,                 # number of rollouts before updating
    "learning_epochs": 8,           # number of learning epochs during each update
    "mini_batches": 2,              # number of mini batches during each learning epoch

    "discount_factor": 0.99,        # discount factor (gamma)
    "lambda": 0.95,                 # TD(lambda) coefficient (lam) for computing returns and advantages

    "learning_rate": 1e-3,                  # learning rate
    "learning_rate_scheduler": None,        # learning rate scheduler class (see torch.optim.lr_scheduler)
    "learning_rate_scheduler_kwargs": {},   # learning rate scheduler's kwargs (e.g. {"step_size": 1e-3})

    "state_preprocessor": None,             # state preprocessor class (see skrl.resources.preprocessors)
    "state_preprocessor_kwargs": {},        # state preprocessor's kwargs (e.g. {"size": env.observation_space})
    "value_preprocessor": None,             # value preprocessor class (see skrl.resources.preprocessors)
    "value_preprocessor_kwargs": {},        # value preprocessor's kwargs (e.g. {"size": 1})

    "random_timesteps": 0,          # random exploration steps
    "learning_starts": 0,           # learning starts after this many steps

    "grad_norm_clip": 0.5,              # clipping coefficient for the norm of the gradients
    "ratio_clip": 0.2,                  # clipping coefficient for computing the clipped surrogate objective
    "value_clip": 0.2,                  # clipping coefficient for computing the value loss (if clip_predicted_values is True)
    "clip_predicted_values": False,     # clip predicted values during value loss computation

    "entropy_loss_scale": 0.0,      # entropy loss scaling factor
    "value_loss_scale": 1.0,        # value loss scaling factor

    "kl_threshold": 0,              # KL divergence threshold for early stopping

    "rewards_shaper": None,         # rewards shaping function: Callable(reward, timestep, timesteps) -> reward

    "experiment": {
        "directory": "",            # experiment's parent directory
        "experiment_name": "",      # experiment name
        "write_interval": 250,      # TensorBoard writing interval (timesteps)

        "checkpoint_interval": 1000,        # interval for checkpoints (timesteps)
        "store_separately": False,          # whether to store checkpoints separately

        "wandb": False,             # whether to use Weights & Biases
        "wandb_kwargs": {}          # wandb kwargs (see https://docs.wandb.ai/ref/python/init)
    }
}


def compute_gae(rewards: np.ndarray,
                dones: np.ndarray,
                values: np.ndarray,
                next_values: np.ndarray,
                discount_factor: float = 0.99,
                lambda_coefficient: float = 0.95) -> np.ndarray:
    """Compute the Generalized Advantage Estimator (GAE)

    :param rewards: Rewards obtained by the agent
    :type rewards: np.ndarray
    :param dones: Signals to indicate that episodes have ended
    :type dones: np.ndarray
    :param values: Values obtained by the agent
    :type values: np.ndarray
    :param next_values: Next values obtained by the agent
    :type next_values: np.ndarray
    :param discount_factor: Discount factor
    :type discount_factor: float
    :param lambda_coefficient: Lambda coefficient
    :type lambda_coefficient: float

    :return: Generalized Advantage Estimator
    :rtype: np.ndarray
    """
    advantage = 0
    advantages = np.zeros_like(rewards)
    not_dones = np.logical_not(dones)
    memory_size = rewards.shape[0]

    # advantages computation
    for i in reversed(range(memory_size)):
        next_values = values[i + 1] if i < memory_size - 1 else next_values
        advantage = rewards[i] - values[i] + discount_factor * not_dones[i] * (next_values + lambda_coefficient * advantage)
        advantages[i] = advantage
    # returns computation
    returns = advantages + values
    # normalize advantages
    advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

    return returns, advantages

# https://jax.readthedocs.io/en/latest/faq.html#strategy-1-jit-compiled-helper-function
@jax.jit
def _compute_gae(rewards: jnp.ndarray,
                dones: jnp.ndarray,
                values: jnp.ndarray,
                next_values: jnp.ndarray,
                discount_factor: float = 0.99,
                lambda_coefficient: float = 0.95) -> jnp.ndarray:
    """Compute the Generalized Advantage Estimator (GAE)

    :param rewards: Rewards obtained by the agent
    :type rewards: jnp.ndarray
    :param dones: Signals to indicate that episodes have ended
    :type dones: jnp.ndarray
    :param values: Values obtained by the agent
    :type values: jnp.ndarray
    :param next_values: Next values obtained by the agent
    :type next_values: jnp.ndarray
    :param discount_factor: Discount factor
    :type discount_factor: float
    :param lambda_coefficient: Lambda coefficient
    :type lambda_coefficient: float

    :return: Generalized Advantage Estimator
    :rtype: jnp.ndarray
    """
    raise NotImplementedError

@functools.partial(jax.jit, static_argnames=("policy_act"))
def _update_policy(policy_act,
                   policy_state_dict,
                   sampled_states,
                   sampled_actions,
                   sampled_log_prob,
                   sampled_advantages,
                   ratio_clip):
    # compute policy loss
    def _policy_loss(params):
        _, next_log_prob, _ = policy_act(params, {"states": sampled_states, "taken_actions": sampled_actions}, role="policy")

        # compute aproximate KL divergence
        ratio = next_log_prob - sampled_log_prob
        kl_divergence = ((jnp.exp(ratio) - 1) - ratio).mean()

        # compute policy loss
        ratio = jnp.exp(next_log_prob - sampled_log_prob)
        surrogate = sampled_advantages * ratio
        surrogate_clipped = sampled_advantages * jnp.clip(ratio, 1.0 - ratio_clip, 1.0 + ratio_clip)

        return -jnp.minimum(surrogate, surrogate_clipped).mean(), kl_divergence

    (policy_loss, kl_divergence), grad = jax.value_and_grad(_policy_loss, has_aux=True)(policy_state_dict.params)

    return grad, policy_loss, kl_divergence

def _update_value(value_act,
                  value_state_dict,
                  sampled_states,
                  sampled_values,
                  sampled_returns,
                  value_loss_scale,
                  clip_predicted_values,
                  value_clip):
    # compute value loss
    def _value_loss(params):
        predicted_values, _, _ = value_act(params, {"states": sampled_states}, role="value")
        if clip_predicted_values:
            predicted_values = sampled_values + jnp.clip(predicted_values - sampled_values, -value_clip, value_clip)
        return value_loss_scale * ((sampled_returns - predicted_values) ** 2).mean()

    value_loss, grad = jax.value_and_grad(_value_loss, has_aux=False)(value_state_dict.params)

    return grad, value_loss


class PPO(Agent):
    def __init__(self,
                 models: Dict[str, Model],
                 memory: Optional[Union[Memory, Tuple[Memory]]] = None,
                 observation_space: Optional[Union[int, Tuple[int], gym.Space, gymnasium.Space]] = None,
                 action_space: Optional[Union[int, Tuple[int], gym.Space, gymnasium.Space]] = None,
                 device: Optional[Union[str, jaxlib.xla_extension.Device]] = None,
                 cfg: Optional[dict] = None) -> None:
        """Proximal Policy Optimization (PPO)

        https://arxiv.org/abs/1707.06347

        :param models: Models used by the agent
        :type models: dictionary of skrl.models.jax.Model
        :param memory: Memory to storage the transitions.
                       If it is a tuple, the first element will be used for training and
                       for the rest only the environment transitions will be added
        :type memory: skrl.memory.jax.Memory, list of skrl.memory.jax.Memory or None
        :param observation_space: Observation/state space or shape (default: None)
        :type observation_space: int, tuple or list of integers, gym.Space, gymnasium.Space or None, optional
        :param action_space: Action space or shape (default: None)
        :type action_space: int, tuple or list of integers, gym.Space, gymnasium.Space or None, optional
        :param device: Device on which a jax array is or will be allocated (default: ``None``).
                       If None, the device will be either ``"cuda:0"`` if available or ``"cpu"``
        :type device: str or jaxlib.xla_extension.Device, optional
        :param cfg: Configuration dictionary
        :type cfg: dict

        :raises KeyError: If the models dictionary is missing a required key
        """
        # _cfg = copy.deepcopy(PPO_DEFAULT_CONFIG)  # TODO: TypeError: cannot pickle 'jaxlib.xla_extension.Device' object
        _cfg = PPO_DEFAULT_CONFIG
        _cfg.update(cfg if cfg is not None else {})
        super().__init__(models=models,
                         memory=memory,
                         observation_space=observation_space,
                         action_space=action_space,
                         device=device,
                         cfg=_cfg)

        self._jax = config.jax.backend == "jax"

        # models
        self.policy = self.models.get("policy", None)
        self.value = self.models.get("value", None)

        # checkpoint models
        self.checkpoint_modules["policy"] = self.policy
        self.checkpoint_modules["value"] = self.value

        # configuration
        self._learning_epochs = self.cfg["learning_epochs"]
        self._mini_batches = self.cfg["mini_batches"]
        self._rollouts = self.cfg["rollouts"]
        self._rollout = 0

        self._grad_norm_clip = self.cfg["grad_norm_clip"]
        self._ratio_clip = self.cfg["ratio_clip"]
        self._value_clip = self.cfg["value_clip"]
        self._clip_predicted_values = self.cfg["clip_predicted_values"]

        self._value_loss_scale = self.cfg["value_loss_scale"]
        self._entropy_loss_scale = self.cfg["entropy_loss_scale"]

        self._kl_threshold = self.cfg["kl_threshold"]

        self._learning_rate = self.cfg["learning_rate"]
        self._learning_rate_scheduler = self.cfg["learning_rate_scheduler"]

        self._state_preprocessor = self.cfg["state_preprocessor"]
        self._value_preprocessor = self.cfg["value_preprocessor"]

        self._discount_factor = self.cfg["discount_factor"]
        self._lambda = self.cfg["lambda"]

        self._random_timesteps = self.cfg["random_timesteps"]
        self._learning_starts = self.cfg["learning_starts"]

        self._rewards_shaper = self.cfg["rewards_shaper"]

        # set up optimizer and learning rate scheduler
        if self.policy is not None and self.value is not None:
            # scheduler
            scale = True
            self.scheduler = None
            if self._learning_rate_scheduler is not None:
                if self._learning_rate_scheduler == KLAdaptiveRL:
                    scale = False
                    self.scheduler = self._learning_rate_scheduler(self._learning_rate, **self.cfg["learning_rate_scheduler_kwargs"])
                else:
                    self._learning_rate = self._learning_rate_scheduler(self._learning_rate, **self.cfg["learning_rate_scheduler_kwargs"])
            # optimizer
            self.policy_optimizer = Adam(model=self.policy, lr=self._learning_rate, scale=scale)
            self.value_optimizer = Adam(model=self.value, lr=self._learning_rate, scale=scale)

            self.checkpoint_modules["policy_optimizer"] = self.policy_optimizer
            self.checkpoint_modules["value_optimizer"] = self.value_optimizer

        # set up preprocessors
        if self._state_preprocessor:
            self._state_preprocessor = self._state_preprocessor(**self.cfg["state_preprocessor_kwargs"])
            self.checkpoint_modules["state_preprocessor"] = self._state_preprocessor
        else:
            self._state_preprocessor = self._empty_preprocessor

        if self._value_preprocessor:
            self._value_preprocessor = self._value_preprocessor(**self.cfg["value_preprocessor_kwargs"])
            self.checkpoint_modules["value_preprocessor"] = self._value_preprocessor
        else:
            self._value_preprocessor = self._empty_preprocessor

    def init(self, trainer_cfg: Optional[Dict[str, Any]] = None) -> None:
        """Initialize the agent
        """
        super().init(trainer_cfg=trainer_cfg)
        self.set_mode("eval")

        # create tensors in memory
        if self.memory is not None:
            self.memory.create_tensor(name="states", size=self.observation_space, dtype=jnp.float32)
            self.memory.create_tensor(name="actions", size=self.action_space, dtype=jnp.float32)
            self.memory.create_tensor(name="rewards", size=1, dtype=jnp.float32)
            self.memory.create_tensor(name="terminated", size=1, dtype=jnp.bool_)
            self.memory.create_tensor(name="log_prob", size=1, dtype=jnp.float32)
            self.memory.create_tensor(name="values", size=1, dtype=jnp.float32)
            self.memory.create_tensor(name="returns", size=1, dtype=jnp.float32)
            self.memory.create_tensor(name="advantages", size=1, dtype=jnp.float32)

            # tensors sampled during training
            self._tensors_names = ["states", "actions", "log_prob", "values", "returns", "advantages"]

        # create temporary variables needed for storage and computation
        self._current_log_prob = None
        self._current_next_states = None

        # set up models for just-in-time compilation with XLA
        self.policy.apply = jax.jit(self.policy.apply, static_argnums=2)
        if self.value is not None:
            self.value.apply = jax.jit(self.value.apply, static_argnums=2)

    def act(self, states: jnp.ndarray, timestep: int, timesteps: int) -> jnp.ndarray:
        """Process the environment's states to make a decision (actions) using the main policy

        :param states: Environment's states
        :type states: jnp.ndarray
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int

        :return: Actions
        :rtype: jnp.ndarray
        """
        # sample random actions
        # TODO, check for stochasticity
        if timestep < self._random_timesteps:
            return self.policy.random_act({"states": self._state_preprocessor(states)}, role="policy")

        # sample stochastic actions
        actions, log_prob, outputs = self.policy.act(None, {"states": self._state_preprocessor(states)}, role="policy")
        if not self._jax:  # numpy backend
            actions = jax.device_get(actions)
            log_prob = jax.device_get(log_prob)

        self._current_log_prob = log_prob

        return actions, log_prob, outputs

    def record_transition(self,
                          states: jnp.ndarray,
                          actions: jnp.ndarray,
                          rewards: jnp.ndarray,
                          next_states: jnp.ndarray,
                          terminated: jnp.ndarray,
                          truncated: jnp.ndarray,
                          infos: Any,
                          timestep: int,
                          timesteps: int) -> None:
        """Record an environment transition in memory

        :param states: Observations/states of the environment used to make the decision
        :type states: jnp.ndarray
        :param actions: Actions taken by the agent
        :type actions: jnp.ndarray
        :param rewards: Instant rewards achieved by the current actions
        :type rewards: jnp.ndarray
        :param next_states: Next observations/states of the environment
        :type next_states: jnp.ndarray
        :param terminated: Signals to indicate that episodes have terminated
        :type terminated: jnp.ndarray
        :param truncated: Signals to indicate that episodes have been truncated
        :type truncated: jnp.ndarray
        :param infos: Additional information about the environment
        :type infos: Any type supported by the environment
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        super().record_transition(states, actions, rewards, next_states, terminated, truncated, infos, timestep, timesteps)

        if self.memory is not None:
            self._current_next_states = next_states

            # reward shaping
            if self._rewards_shaper is not None:
                rewards = self._rewards_shaper(rewards, timestep, timesteps)

            # compute values
            values, _, _ = self.value.act(None, {"states": self._state_preprocessor(states)}, role="value")
            values = self._value_preprocessor(values, inverse=True)
            if not self._jax:  # numpy backend
                values = jax.device_get(values)

            # storage transition in memory
            self.memory.add_samples(states=states, actions=actions, rewards=rewards, next_states=next_states,
                                    terminated=terminated, truncated=truncated, log_prob=self._current_log_prob, values=values)
            for memory in self.secondary_memories:
                memory.add_samples(states=states, actions=actions, rewards=rewards, next_states=next_states,
                                   terminated=terminated, truncated=truncated, log_prob=self._current_log_prob, values=values)

    def pre_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called before the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        pass

    def post_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called after the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        self._rollout += 1
        if not self._rollout % self._rollouts and timestep >= self._learning_starts:
            self.set_mode("train")
            self._update(timestep, timesteps)
            self.set_mode("eval")

        # write tracking data and checkpoints
        super().post_interaction(timestep, timesteps)

    def _update(self, timestep: int, timesteps: int) -> None:
        """Algorithm's main update step

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        # compute returns and advantages
        self.value.training = False  # TODO: .train(False)
        last_values, _, _ = self.value.act(None, {"states": self._state_preprocessor(self._current_next_states)}, role="value")  # TODO: .float()
        self.value.training = True
        last_values = self._value_preprocessor(last_values, inverse=True)
        if not self._jax:  # numpy backend
            last_values = jax.device_get(last_values)

        values = self.memory.get_tensor_by_name("values")
        returns, advantages = compute_gae(rewards=self.memory.get_tensor_by_name("rewards"),
                                          dones=self.memory.get_tensor_by_name("terminated"),
                                          values=values,
                                          next_values=last_values,
                                          discount_factor=self._discount_factor,
                                          lambda_coefficient=self._lambda)

        self.memory.set_tensor_by_name("values", self._value_preprocessor(values, train=True))
        self.memory.set_tensor_by_name("returns", self._value_preprocessor(returns, train=True))
        self.memory.set_tensor_by_name("advantages", advantages)

        # sample mini-batches from memory
        sampled_batches = self.memory.sample_all(names=self._tensors_names, mini_batches=self._mini_batches)

        cumulative_policy_loss = 0
        cumulative_entropy_loss = 0
        cumulative_value_loss = 0

        # learning epochs
        for epoch in range(self._learning_epochs):
            kl_divergences = []

            # mini-batches loop
            for sampled_states, sampled_actions, sampled_log_prob, sampled_values, sampled_returns, sampled_advantages in sampled_batches:

                sampled_states = self._state_preprocessor(sampled_states, train=not epoch)

                # compute policy loss
                grad, policy_loss, kl_divergence = _update_policy(self.policy.act,
                                                                  self.policy.state_dict,
                                                                  sampled_states,
                                                                  sampled_actions,
                                                                  sampled_log_prob,
                                                                  sampled_advantages,
                                                                  self._ratio_clip)

                kl_divergences.append(kl_divergence.item())

                # early stopping with KL divergence
                if self._kl_threshold and kl_divergence > self._kl_threshold:
                    break

                # compute entropy loss
                if self._entropy_loss_scale:
                    # TODO
                    entropy_loss = -self._entropy_loss_scale * self.policy.get_entropy(role="policy").mean()
                else:
                    entropy_loss = 0

                # optimization step (policy)
                self.policy_optimizer = self.policy_optimizer.step(grad, self.policy, self.scheduler._lr if self.scheduler else None)

                # compute value loss
                grad, value_loss = _update_value(self.value.act,
                                                 self.value.state_dict,
                                                 sampled_states,
                                                 sampled_values,
                                                 sampled_returns,
                                                 self._value_loss_scale,
                                                 self._clip_predicted_values,
                                                 self._value_clip)

                # optimization step (value)
                self.value_optimizer = self.value_optimizer.step(grad, self.value, self.scheduler._lr if self.scheduler else None)

                # # optimization step
                # self.optimizer.zero_grad()
                # (policy_loss + entropy_loss + value_loss).backward()
                # if self._grad_norm_clip > 0:
                #     if self.policy is self.value:
                #         nn.utils.clip_grad_norm_(self.policy.parameters(), self._grad_norm_clip)
                #     else:
                #         nn.utils.clip_grad_norm_(itertools.chain(self.policy.parameters(), self.value.parameters()), self._grad_norm_clip)
                # self.optimizer.step()

                # update cumulative losses
                cumulative_policy_loss += policy_loss.item()
                cumulative_value_loss += value_loss.item()
                if self._entropy_loss_scale:
                    cumulative_entropy_loss += entropy_loss.item()

            # update learning rate
            if self._learning_rate_scheduler:
                if isinstance(self.scheduler, KLAdaptiveRL):
                    self.scheduler.step(np.mean(kl_divergences))

        # record data
        self.track_data("Loss / Policy loss", cumulative_policy_loss / (self._learning_epochs * self._mini_batches))
        self.track_data("Loss / Value loss", cumulative_value_loss / (self._learning_epochs * self._mini_batches))
        if self._entropy_loss_scale:
            self.track_data("Loss / Entropy loss", cumulative_entropy_loss / (self._learning_epochs * self._mini_batches))

        # self.track_data("Policy / Standard deviation", self.policy.distribution(role="policy").stddev.mean().item())  # TODO: this

        if self._learning_rate_scheduler:
            self.track_data("Learning / Learning rate", self.scheduler._lr)
