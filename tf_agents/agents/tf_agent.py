# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TensorFlow RL Agent API."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import tensorflow as tf

from tf_agents.trajectories import time_step as ts
from tf_agents.trajectories import trajectory
from tf_agents.utils import common
from tf_agents.utils import nest_utils


LossInfo = collections.namedtuple("LossInfo", ("loss", "extra"))


class TFAgent(tf.Module):
  """Abstract base class for TF RL agents."""

  # TODO(b/127327645) Remove this attribute.
  # This attribute allows subclasses to back out of automatic tf.function
  # attribute inside TF1 (for autodeps).
  _enable_functions = True

  def __init__(self,
               time_step_spec,
               action_spec,
               policy,
               collect_policy,
               train_sequence_length,
               num_outer_dims=2,
               debug_summaries=False,
               summarize_grads_and_vars=False,
               enable_summaries=True,
               train_step_counter=None):
    """Meant to be called by subclass constructors.

    Args:
      time_step_spec: A `TimeStep` spec of the expected time_steps. Provided by
        the user.
      action_spec: A nest of BoundedTensorSpec representing the actions.
        Provided by the user.
      policy: An instance of `tf_policy.Base` representing the Agent's current
        policy.
      collect_policy: An instance of `tf_policy.Base` representing the Agent's
        current data collection policy (used to set `self.step_spec`).
      train_sequence_length: A python integer or `None`, signifying the number
        of time steps required from tensors in `experience` as passed to
        `train()`.  All tensors in `experience` will be shaped `[B, T, ...]` but
        for certain agents, `T` should be fixed.  For example, DQN requires
        transitions in the form of 2 time steps, so for a non-RNN DQN Agent, set
        this value to 2.  For agents that don't care, or which can handle `T`
        unknown at graph build time (i.e. most RNN-based agents), set this
        argument to `None`.
      num_outer_dims: The number of outer dimensions for the agent. Must be
        either 1 or 2. If 2, training will require both a batch_size and time
        dimension on every Tensor; if 1, training will require only a batch_size
        outer dimension.
      debug_summaries: A bool; if true, subclasses should gather debug
        summaries.
      summarize_grads_and_vars: A bool; if true, subclasses should additionally
        collect gradient and variable summaries.
      enable_summaries: A bool; if false, subclasses should not gather any
        summaries (debug or otherwise); subclasses should gate *all* summaries
        using either `summaries_enabled`, `debug_summaries`, or
        `summarize_grads_and_vars` properties.
      train_step_counter: An optional counter to increment every time the train
        op is run.  Defaults to the global_step.

    Raises:
      ValueError: If `time_step_spec` is not an instance of `ts.TimeStep`.
      ValueError: If `num_outer_dims` is not in [1, 2].
    """
    common.tf_agents_gauge.get_cell("TFAgent").set(True)
    common.assert_members_are_not_overridden(base_cls=TFAgent, instance=self)
    if not isinstance(time_step_spec, ts.TimeStep):
      raise ValueError(
          "The `time_step_spec` must be an instance of `TimeStep`, but is `{}`."
          .format(type(time_step_spec)))

    if num_outer_dims not in [1, 2]:
      raise ValueError("num_outer_dims must be in [1, 2].")

    self._time_step_spec = time_step_spec
    self._action_spec = action_spec
    self._policy = policy
    self._collect_policy = collect_policy
    self._train_sequence_length = train_sequence_length
    self._num_outer_dims = num_outer_dims
    self._debug_summaries = debug_summaries
    self._summarize_grads_and_vars = summarize_grads_and_vars
    self._enable_summaries = enable_summaries
    if train_step_counter is None:
      train_step_counter = tf.compat.v1.train.get_or_create_global_step()
    self._train_step_counter = train_step_counter
    self._train_fn = common.function_in_tf1()(self._train)
    self._initialize_fn = common.function_in_tf1()(self._initialize)

  def initialize(self):
    """Initializes the agent.

    Returns:
      An operation that can be used to initialize the agent.

    Raises:
      RuntimeError: If the class was not initialized properly (`super.__init__`
        was not called).
    """
    if self._enable_functions and getattr(self, "_initialize_fn", None) is None:
      raise RuntimeError(
          "Cannot find _initialize_fn.  Did %s.__init__ call super?"
          % type(self).__name__)
    if self._enable_functions:
      return self._initialize_fn()
    else:
      return self._initialize()

  def _check_trajectory_dimensions(self, experience):
    """Checks the given Trajectory for batch and time outer dimensions."""
    if not nest_utils.is_batched_nested_tensors(
        experience, self.collect_data_spec,
        num_outer_dims=self._num_outer_dims):
      debug_str_1 = tf.nest.map_structure(lambda tp: tp.shape, experience)
      debug_str_2 = tf.nest.map_structure(lambda spec: spec.shape,
                                          self.collect_data_spec)

      if self._num_outer_dims == 2:
        raise ValueError(
            "All of the Tensors in `experience` must have two outer "
            "dimensions: batch size and time. Specifically, tensors should be "
            "shaped as [B x T x ...].\n"
            "Full shapes of experience tensors:\n{}.\n"
            "Full expected shapes (minus outer dimensions):\n{}.".format(
                debug_str_1, debug_str_2))
      else:
        # self._num_outer_dims must be 1.
        raise ValueError(
            "All of the Tensors in `experience` must have a single outer "
            "batch_size dimension. If you also want to include an outer time "
            "dimension, set num_outer_dims=2 when initializing your agent.\n"
            "Full shapes of experience tensors:\n{}.\n"
            "Full expected shapes (minus batch_size dimension):\n{}.".format(
                debug_str_1, debug_str_2))

    # If we have a time dimension and a train_sequence_length, make sure they
    # match.
    if self._num_outer_dims == 2 and self.train_sequence_length is not None:
      def check_shape(t):  # pylint: disable=invalid-name
        if t.shape[1] != self.train_sequence_length:
          debug_str = tf.nest.map_structure(lambda tp: tp.shape, experience)
          raise ValueError(
              "One of the Tensors in `experience` has a time axis dim value "
              "'%s', but we require dim value '%d'. Full shape structure of "
              "experience:\n%s" %
              (t.shape[1], self.train_sequence_length, debug_str))

      tf.nest.map_structure(check_shape, experience)

  def train(self, experience, weights=None):
    """Trains the agent.

    Args:
      experience: A batch of experience data in the form of a `Trajectory`. The
        structure of `experience` must match that of `self.collect_data_spec`.
        All tensors in `experience` must be shaped `[batch, time, ...]` where
        `time` must be equal to `self.train_step_length` if that
        property is not `None`.
      weights: (optional).  A `Tensor`, either `0-D` or shaped `[batch]`,
        containing weights to be used when calculating the total train loss.
        Weights are typically multiplied elementwise against the per-batch loss,
        but the implementation is up to the Agent.

    Returns:
        A `LossInfo` loss tuple containing loss and info tensors.
        - In eager mode, the loss values are first calculated, then a train step
          is performed before they are returned.
        - In graph mode, executing any or all of the loss tensors
          will first calculate the loss value(s), then perform a train step,
          and return the pre-train-step `LossInfo`.

    Raises:
      TypeError: If experience is not type `Trajectory`.  Or if experience
        does not match `self.collect_data_spec` structure types.
      ValueError: If experience tensors' time axes are not compatible with
        `self.train_sequence_length`.  Or if experience does not match
        `self.collect_data_spec` structure.
      RuntimeError: If the class was not initialized properly (`super.__init__`
        was not called).
    """
    if self._enable_functions and getattr(self, "_train_fn", None) is None:
      raise RuntimeError(
          "Cannot find _train_fn.  Did %s.__init__ call super?"
          % type(self).__name__)
    if not isinstance(experience, trajectory.Trajectory):
      raise ValueError(
          "experience must be type Trajectory, saw type: %s" % type(experience))

    self._check_trajectory_dimensions(experience)

    if self._enable_functions:
      loss_info = self._train_fn(experience=experience, weights=weights)
    else:
      loss_info = self._train(experience=experience, weights=weights)

    if not isinstance(loss_info, LossInfo):
      raise TypeError(
          "loss_info is not a subclass of LossInfo: {}".format(loss_info))
    return loss_info

  @property
  def time_step_spec(self):
    """Describes the `TimeStep` tensors expected by the agent.

    Returns:
      A `TimeStep` namedtuple with `TensorSpec` objects instead of Tensors,
      which describe the shape, dtype and name of each tensor.
    """
    return self._time_step_spec

  @property
  def action_spec(self):
    """TensorSpec describing the action produced by the agent.

    Returns:
      An single BoundedTensorSpec, or a nested dict, list or tuple of
      `BoundedTensorSpec` objects, which describe the shape and
      dtype of each action Tensor.
    """
    return self._action_spec

  @property
  def policy(self):
    """Return the current policy held by the agent.

    Returns:
      A `tf_policy.Base` object.
    """
    return self._policy

  @property
  def collect_policy(self):
    """Return a policy that can be used to collect data from the environment.

    Returns:
      A `tf_policy.Base` object.
    """
    return self._collect_policy

  @property
  def collect_data_spec(self):
    """Returns a `Trajectory` spec, as expected by the `collect_policy`.

    Returns:
      A `Trajectory` spec.
    """
    return self.collect_policy.trajectory_spec

  @property
  def train_sequence_length(self):
    """The number of time steps needed in experience tensors passed to `train`.

    Train requires experience to be a `Trajectory` containing tensors shaped
    `[B, T, ...]`.  This argument describes the value of `T` required.

    For example, for non-RNN DQN training, `T=2` because DQN requires single
    transitions.

    If this value is `None`, then `train` can handle an unknown `T` (it can be
    determined at runtime from the data).  Most RNN-based agents fall into
    this category.

    Returns:
      The number of time steps needed in experience tensors passed to `train`.
      May be `None` to mean no constraint.
    """
    return self._train_sequence_length

  @property
  def summaries_enabled(self):
    return self._enable_summaries

  @property
  def debug_summaries(self):
    return self._debug_summaries and self.summaries_enabled

  @property
  def summarize_grads_and_vars(self):
    return self._summarize_grads_and_vars and self.summaries_enabled

  @property
  def train_step_counter(self):
    return self._train_step_counter

  # Subclasses must implement these methods.
  @abc.abstractmethod
  def _initialize(self):
    """Returns an op to initialize the agent."""

  @abc.abstractmethod
  def _train(self, experience, weights):
    """Returns an op to train the agent.

    This method *must* increment self.train_step_counter exactly once.
    TODO(b/126271669): Consider automatically incrementing this

    Args:
      experience: A batch of experience data in the form of a `Trajectory`. The
        structure of `experience` must match that of `self.collect_data_spec`.
        All tensors in `experience` must be shaped `[batch, time, ...]` where
        `time` must be equal to `self.train_step_length` if that property is
        not `None`.
      weights: (optional).  A `Tensor`, either `0-D` or shaped `[batch]`,
        containing weights to be used when calculating the total train loss.
        Weights are typically multiplied elementwise against the per-batch loss,
        but the implementation is up to the Agent.

    Returns:
        A `LossInfo` containing the loss *before* the training step is taken.
        In most cases, if `weights` is provided, the entries of this tuple will
        have been calculated with the weights.  Note that each Agent chooses
        its own method of applying weights.
    """
