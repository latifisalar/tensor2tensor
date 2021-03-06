# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
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
"""Basic models for testing simple tasks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial
import six

from tensor2tensor.layers import common_attention
from tensor2tensor.layers import common_layers
from tensor2tensor.layers import common_video
from tensor2tensor.models.research import next_frame_params  # pylint: disable=unused-import
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf

tfl = tf.layers
tfcl = tf.contrib.layers


@registry.register_model
class NextFrameBasic(t2t_model.T2TModel):
  """Basic next-frame model, may take actions and predict rewards too."""

  def body(self, features):
    hparams = self.hparams
    filters = hparams.hidden_size
    kernel1, kernel2 = (3, 3), (4, 4)

    # Embed the inputs.
    inputs_shape = common_layers.shape_list(features["inputs"])
    # Using non-zero bias initializer below for edge cases of uniform inputs.
    x = tf.layers.dense(
        features["inputs"], filters, name="inputs_embed",
        bias_initializer=tf.random_normal_initializer(stddev=0.01))
    x = common_attention.add_timing_signal_nd(x)

    # Down-stride.
    layer_inputs = [x]
    for i in range(hparams.num_compress_steps):
      with tf.variable_scope("downstride%d" % i):
        layer_inputs.append(x)
        x = common_layers.make_even_size(x)
        if i < hparams.filter_double_steps:
          filters *= 2
        x = tf.layers.conv2d(x, filters, kernel2, activation=common_layers.belu,
                             strides=(2, 2), padding="SAME")
        x = common_layers.layer_norm(x)

    # Add embedded action if present.
    if "input_action" in features:
      action = tf.reshape(features["input_action"][:, -1, :],
                          [-1, 1, 1, hparams.hidden_size])
      action_mask = tf.layers.dense(action, filters, name="action_mask")
      zeros_mask = tf.zeros(common_layers.shape_list(x)[:-1] + [filters],
                            dtype=tf.float32)
      if hparams.concatenate_actions:
        x = tf.concat([x, action_mask + zeros_mask], axis=-1)
      else:
        x *= action_mask + zeros_mask

    # Run a stack of convolutions.
    for i in range(hparams.num_hidden_layers):
      with tf.variable_scope("layer%d" % i):
        y = tf.layers.conv2d(x, filters, kernel1, activation=common_layers.belu,
                             strides=(1, 1), padding="SAME")
        y = tf.nn.dropout(y, 1.0 - hparams.dropout)
        if i == 0:
          x = y
        else:
          x = common_layers.layer_norm(x + y)

    # Up-convolve.
    layer_inputs = list(reversed(layer_inputs))
    for i in range(hparams.num_compress_steps):
      with tf.variable_scope("upstride%d" % i):
        if i >= hparams.num_compress_steps - hparams.filter_double_steps:
          filters //= 2
        x = tf.layers.conv2d_transpose(
            x, filters, kernel2, activation=common_layers.belu,
            strides=(2, 2), padding="SAME")
        y = layer_inputs[i]
        shape = common_layers.shape_list(y)
        x = x[:, :shape[1], :shape[2], :]
        x = common_layers.layer_norm(x + y)
        x = common_attention.add_timing_signal_nd(x)

    # Cut down to original size.
    x = x[:, :inputs_shape[1], :inputs_shape[2], :]

    # Reward prediction if needed.
    if "target_reward" not in features:
      return x
    reward_pred = tf.reduce_mean(x, axis=[1, 2], keepdims=True)
    return {"targets": x, "target_reward": reward_pred}

  def infer(self, features, *args, **kwargs):  # pylint: disable=arguments-differ
    """Produce predictions from the model by running it."""
    del args, kwargs
    # Inputs and features preparation needed to handle edge cases.
    if not features:
      features = {}
    inputs_old = None
    if "inputs" in features and len(features["inputs"].shape) < 4:
      inputs_old = features["inputs"]
      features["inputs"] = tf.expand_dims(features["inputs"], 2)

    def logits_to_samples(logits):
      """Get samples from logits."""
      # If the last dimension is 1 then we're using L1/L2 loss.
      if common_layers.shape_list(logits)[-1] == 1:
        return tf.to_int32(tf.squeeze(logits, axis=-1))
      # Argmax in TF doesn't handle more than 5 dimensions yet.
      logits_shape = common_layers.shape_list(logits)
      argmax = tf.argmax(tf.reshape(logits, [-1, logits_shape[-1]]), axis=-1)
      return tf.reshape(argmax, logits_shape[:-1])

    # Get predictions.
    try:
      num_channels = self.hparams.problem.num_channels
    except AttributeError:
      num_channels = 1
    if "inputs" in features:
      inputs_shape = common_layers.shape_list(features["inputs"])
      targets_shape = [inputs_shape[0], self.hparams.video_num_target_frames,
                       inputs_shape[2], inputs_shape[3], num_channels]
    else:
      tf.logging.warn("Guessing targets shape as no inputs are given.")
      targets_shape = [self.hparams.batch_size,
                       self.hparams.video_num_target_frames, 1, 1, num_channels]
    features["targets"] = tf.zeros(targets_shape, dtype=tf.int32)
    if "target_reward" in self.hparams.problem_hparams.target_modality:
      features["target_reward"] = tf.zeros(
          [targets_shape[0], 1, 1], dtype=tf.int32)
    logits, _ = self(features)  # pylint: disable=not-callable
    if isinstance(logits, dict):
      results = {}
      for k, v in six.iteritems(logits):
        results[k] = logits_to_samples(v)
        results["%s_logits" % k] = v
    else:
      results = logits_to_samples(logits)

    # Restore inputs to not confuse Estimator in edge cases.
    if inputs_old is not None:
      features["inputs"] = inputs_old

    # Return results.
    return results


_LARGE_STEP_NUMBER = 100000


@registry.register_model
class NextFrameStochastic(NextFrameBasic):
  """ SV2P: Stochastic Variational Video Prediction.

  based on the following papaer:
  https://arxiv.org/abs/1710.11252
  """

  @property
  def is_training(self):
    return self.hparams.mode == tf.estimator.ModeKeys.TRAIN

  def tinyify(self, array):
    if self.hparams.tiny_mode:
      return [1 for _ in array]
    return array

  def construct_latent_tower(self, images):
    """Builds convolutional latent tower for stochastic model.

    At training time this tower generates a latent distribution (mean and std)
    conditioned on the entire video. This latent variable will be fed to the
    main tower as an extra variable to be used for future frames prediction.
    At inference time, the tower is disabled and only returns latents sampled
    from N(0,1).
    If the multi_latent flag is on, a different latent for every timestep would
    be generated.

    Args:
      images: tensor of ground truth image sequences
    Returns:
      latent_mean: predicted latent mean
      latent_std: predicted latent standard deviation
      latent_loss: loss of the latent twoer
      samples: random samples sampled from standard guassian
    """
    conv_size = self.tinyify([32, 64, 64])
    with tf.variable_scope("latent", reuse=tf.AUTO_REUSE):
      # this allows more predicted frames at inference time
      latent_num_frames = self.hparams.latent_num_frames
      if latent_num_frames == 0:  # use all frames by default.
        latent_num_frames = (self.hparams.video_num_input_frames +
                             self.hparams.video_num_target_frames)
      tf.logging.info("Creating latent tower with %d frames."%latent_num_frames)
      latent_images = tf.unstack(images[:latent_num_frames], axis=0)
      images = tf.concat(latent_images, 3)

      x = images
      x = common_layers.make_even_size(x)
      x = tfl.conv2d(x, conv_size[0], [3, 3], strides=(2, 2),
                     padding="SAME", activation=tf.nn.relu, name="latent_conv1")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="latent_bn1")
      x = common_layers.make_even_size(x)
      x = tfl.conv2d(x, conv_size[1], [3, 3], strides=(2, 2),
                     padding="SAME", activation=tf.nn.relu, name="latent_conv2")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="latent_bn2")
      x = tfl.conv2d(x, conv_size[2], [3, 3], strides=(1, 1),
                     padding="SAME", activation=tf.nn.relu, name="latent_conv3")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="latent_bn3")

      nc = self.hparams.latent_channels
      mean = tfl.conv2d(x, nc, [3, 3], strides=(2, 2),
                        padding="SAME", activation=None, name="latent_mean")
      std = tfl.conv2d(x, nc, [3, 3], strides=(2, 2),
                       padding="SAME", activation=tf.nn.relu, name="latent_std")
      std += self.hparams.latent_std_min

      # No latent tower at inference time, just standard gaussian.
      if self.hparams.mode != tf.estimator.ModeKeys.TRAIN:
        return tf.zeros_like(mean), tf.zeros_like(std)

      return mean, std

  def bottom_part_tower(self, input_image, input_reward, action, latent,
                        lstm_state, lstm_size, conv_size, concat_latent=False):
    """The bottom part of predictive towers.

    With the current (early) design, the main prediction tower and
    the reward prediction tower share the same arcitecture. TF Scope can be
    adjusted as required to either share or not share the weights between
    the two towers.

    Args:
      input_image: the current image.
      input_reward: the current reward.
      action: the action taken by the agent.
      latent: the latent vector.
      lstm_state: the current internal states of conv lstms.
      lstm_size: the size of lstms.
      conv_size: the size of convolutions.
      concat_latent: whether or not to concatenate the latent at every step.

    Returns:
      - the output of the partial network.
      - intermidate outputs for skip connections.
    """
    lstm_func = common_video.conv_lstm_2d
    tile_and_concat = common_video.tile_and_concat

    input_image = common_layers.make_even_size(input_image)
    concat_input_image = tile_and_concat(
        input_image, latent, concat_latent=concat_latent)

    enc0 = tfl.conv2d(
        concat_input_image,
        conv_size[0], [5, 5],
        strides=(2, 2),
        activation=tf.nn.relu,
        padding="SAME",
        name="scale1_conv1")
    enc0 = tfcl.layer_norm(enc0, scope="layer_norm1")

    hidden1, lstm_state[0] = lstm_func(
        enc0, lstm_state[0], lstm_size[0], name="state1")
    hidden1 = tile_and_concat(hidden1, latent, concat_latent=concat_latent)
    hidden1 = tfcl.layer_norm(hidden1, scope="layer_norm2")
    hidden2, lstm_state[1] = lstm_func(
        hidden1, lstm_state[1], lstm_size[1], name="state2")
    hidden2 = tfcl.layer_norm(hidden2, scope="layer_norm3")
    hidden2 = common_layers.make_even_size(hidden2)
    enc1 = tfl.conv2d(hidden2, hidden2.get_shape()[3], [3, 3], strides=(2, 2),
                      padding="SAME", activation=tf.nn.relu, name="conv2")
    enc1 = tile_and_concat(enc1, latent, concat_latent=concat_latent)

    hidden3, lstm_state[2] = lstm_func(
        enc1, lstm_state[2], lstm_size[2], name="state3")
    hidden3 = tile_and_concat(hidden3, latent, concat_latent=concat_latent)
    hidden3 = tfcl.layer_norm(hidden3, scope="layer_norm4")
    hidden4, lstm_state[3] = lstm_func(
        hidden3, lstm_state[3], lstm_size[3], name="state4")
    hidden4 = tile_and_concat(hidden4, latent, concat_latent=concat_latent)
    hidden4 = tfcl.layer_norm(hidden4, scope="layer_norm5")
    hidden4 = common_layers.make_even_size(hidden4)
    enc2 = tfl.conv2d(hidden4, hidden4.get_shape()[3], [3, 3], strides=(2, 2),
                      padding="SAME", activation=tf.nn.relu, name="conv3")

    # Pass in reward and action.
    emb_action = common_video.encode_to_shape(
        action, enc2.get_shape(), "action_enc")
    emb_reward = common_video.encode_to_shape(
        input_reward, enc2.get_shape(), "reward_enc")
    enc2 = tf.concat(axis=3, values=[enc2, emb_action, emb_reward])

    if latent is not None and not concat_latent:
      with tf.control_dependencies([latent]):
        enc2 = tf.concat([enc2, latent], 3)

    enc3 = tfl.conv2d(enc2, hidden4.get_shape()[3], [1, 1], strides=(1, 1),
                      padding="SAME", activation=tf.nn.relu, name="conv4")

    hidden5, lstm_state[4] = lstm_func(
        enc3, lstm_state[4], lstm_size[4], name="state5")  # last 8x8
    hidden5 = tfcl.layer_norm(hidden5, scope="layer_norm6")
    hidden5 = tile_and_concat(hidden5, latent, concat_latent=concat_latent)
    return hidden5, (enc0, enc1)

  def reward_prediction(
      self, input_image, input_reward, action, lstm_state, latent):
    """Builds a reward prediction network."""
    conv_size = self.tinyify([32, 32, 16, 4])
    lstm_size = self.tinyify([32, 64, 128, 64, 32])

    with tf.variable_scope("reward_pred", reuse=tf.AUTO_REUSE):
      hidden5, _ = self.bottom_part_tower(
          input_image, input_reward, action, latent,
          lstm_state, lstm_size, conv_size)

      x = hidden5
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn0")
      x = tfl.conv2d(x, conv_size[1], [3, 3], strides=(2, 2),
                     padding="SAME", activation=tf.nn.relu, name="reward_conv1")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn1")
      x = tfl.conv2d(x, conv_size[2], [3, 3], strides=(2, 2),
                     padding="SAME", activation=tf.nn.relu, name="reward_conv2")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn2")
      x = tfl.conv2d(x, conv_size[3], [3, 3], strides=(2, 2),
                     padding="SAME", activation=tf.nn.relu, name="reward_conv3")

      pred_reward = common_video.decode_to_shape(
          x, input_reward.shape, "reward_dec")

      return pred_reward, lstm_state

  def construct_predictive_tower(
      self, input_image, input_reward, action, lstm_state, latent,
      concat_latent=False):
    # Main tower
    lstm_func = common_video.conv_lstm_2d
    batch_size = common_layers.shape_list(input_image)[0]
    # the number of different pixel motion predictions
    # and the number of masks for each of those predictions
    num_masks = self.hparams.num_masks
    upsample_method = self.hparams.upsample_method
    tile_and_concat = common_video.tile_and_concat

    lstm_size = self.tinyify([32, 32, 64, 64, 128, 64, 32])
    conv_size = self.tinyify([32])

    img_height, img_width, color_channels = self.hparams.problem.frame_shape

    with tf.variable_scope("main", reuse=tf.AUTO_REUSE):
      hidden5, skips = self.bottom_part_tower(
          input_image, input_reward, action, latent,
          lstm_state, lstm_size, conv_size, concat_latent=concat_latent)
      enc0, enc1 = skips

      with tf.variable_scope("upsample1", reuse=tf.AUTO_REUSE):
        enc4 = common_layers.cyclegan_upsample(
            hidden5, num_outputs=hidden5.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)

      enc1_shape = common_layers.shape_list(enc1)
      enc4 = enc4[:, :enc1_shape[1], :enc1_shape[2], :]  # Cut to shape.
      enc4 = tile_and_concat(enc4, latent, concat_latent=concat_latent)

      hidden6, lstm_state[5] = lstm_func(
          enc4, lstm_state[5], lstm_size[5], name="state6",
          spatial_dims=enc1_shape[1:-1])  # 16x16
      hidden6 = tile_and_concat(hidden6, latent, concat_latent=concat_latent)
      hidden6 = tfcl.layer_norm(hidden6, scope="layer_norm7")
      # Skip connection.
      hidden6 = tf.concat(axis=3, values=[hidden6, enc1])  # both 16x16

      with tf.variable_scope("upsample2", reuse=tf.AUTO_REUSE):
        enc5 = common_layers.cyclegan_upsample(
            hidden6, num_outputs=hidden6.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)

      enc0_shape = common_layers.shape_list(enc0)
      enc5 = enc5[:, :enc0_shape[1], :enc0_shape[2], :]  # Cut to shape.
      enc5 = tile_and_concat(enc5, latent, concat_latent=concat_latent)

      hidden7, lstm_state[6] = lstm_func(
          enc5, lstm_state[6], lstm_size[6], name="state7",
          spatial_dims=enc0_shape[1:-1])  # 32x32
      hidden7 = tfcl.layer_norm(hidden7, scope="layer_norm8")

      # Skip connection.
      hidden7 = tf.concat(axis=3, values=[hidden7, enc0])  # both 32x32

      with tf.variable_scope("upsample3", reuse=tf.AUTO_REUSE):
        enc6 = common_layers.cyclegan_upsample(
            hidden7, num_outputs=hidden7.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)
      enc6 = tfcl.layer_norm(enc6, scope="layer_norm9")
      enc6 = tile_and_concat(enc6, latent, concat_latent=concat_latent)

      if self.hparams.model_options == "DNA":
        # Using largest hidden state for predicting untied conv kernels.
        enc7 = tfl.conv2d_transpose(
            enc6,
            self.hparams.dna_kernel_size**2,
            [1, 1],
            strides=(1, 1),
            padding="SAME",
            name="convt4",
            activation=None)
      else:
        # Using largest hidden state for predicting a new image layer.
        enc7 = tfl.conv2d_transpose(
            enc6,
            color_channels,
            [1, 1],
            strides=(1, 1),
            padding="SAME",
            name="convt4",
            activation=None)
        # This allows the network to also generate one image from scratch,
        # which is useful when regions of the image become unoccluded.
        transformed = [tf.nn.sigmoid(enc7)]

      if self.hparams.model_options == "CDNA":
        # cdna_input = tf.reshape(hidden5, [int(batch_size), -1])
        cdna_input = tfcl.flatten(hidden5)
        transformed += common_video.cdna_transformation(
            input_image, cdna_input, num_masks, int(color_channels),
            self.hparams.dna_kernel_size, self.hparams.relu_shift)
      elif self.hparams.model_options == "DNA":
        # Only one mask is supported (more should be unnecessary).
        if num_masks != 1:
          raise ValueError("Only one mask is supported for DNA model.")
        transformed = [
            common_video.dna_transformation(
                input_image, enc7,
                self.hparams.dna_kernel_size, self.hparams.relu_shift)]

      masks = tfl.conv2d(
          enc6, filters=num_masks + 1, kernel_size=[1, 1],
          strides=(1, 1), name="convt7", padding="SAME")
      masks = tf.reshape(
          tf.nn.softmax(tf.reshape(masks, [-1, num_masks + 1])),
          [batch_size,
           int(img_height),
           int(img_width), num_masks + 1])
      mask_list = tf.split(
          axis=3, num_or_size_splits=num_masks + 1, value=masks)
      output = mask_list[0] * input_image
      for layer, mask in zip(transformed, mask_list[1:]):
        output += layer * mask

      return output, lstm_state

  def get_gaussian_latent(self, latent_mean, latent_std):
    latent = tf.random_normal(tf.shape(latent_mean), 0, 1, dtype=tf.float32)
    latent = latent_mean + tf.exp(latent_std / 2.0) * latent
    return latent

  def construct_model(self,
                      images,
                      actions,
                      rewards):
    """Build convolutional lstm video predictor using CDNA, or DNA.

    Args:
      images: list of tensors of ground truth image sequences
              there should be a 4D image ?xWxHxC for each timestep
      actions: list of action tensors
               each action should be in the shape ?x1xZ
      rewards: list of reward tensors
               each reward should be in the shape ?x1xZ
    Returns:
      gen_images: predicted future image frames
      gen_rewards: predicted future rewards
      latent_mean: mean of approximated posterior
      latent_std: std of approximated posterior

    Raises:
      ValueError: if more than 1 mask specified for DNA model.
    """
    context_frames = self.hparams.video_num_input_frames

    batch_size = common_layers.shape_list(images)[1]
    ss_func = self.get_scheduled_sample_func(batch_size)

    def process_single_frame(prev_outputs, inputs):
      """Process a single frame of the video."""
      cur_image, cur_reward, action = inputs
      time_step, prev_image, prev_reward, lstm_states = prev_outputs[:4]

      generated_items = [prev_image, prev_reward]
      groundtruth_items = [cur_image, cur_reward]
      done_warm_start = tf.greater(time_step, context_frames - 1)
      input_image, input_reward = self.get_scheduled_sample_inputs(
          done_warm_start, groundtruth_items, generated_items, ss_func)

      # Prediction
      pred_image, lstm_states = self.construct_predictive_tower(
          input_image, input_reward, action, lstm_states, latent)

      if self.hparams.reward_prediction:
        reward_lstm_states = prev_outputs[4]
        pred_reward, reward_lstm_states = self.reward_prediction(
            input_image, input_reward, action, reward_lstm_states, latent)
      else:
        pred_reward = input_reward

      time_step += 1
      outputs = (time_step, pred_image, pred_reward, lstm_states)
      if self.hparams.reward_prediction:
        outputs += (reward_lstm_states,)

      return outputs

    # Latent tower
    latent = None
    if self.hparams.stochastic_model:
      latent_mean, latent_std = self.construct_latent_tower(images)
      latent = self.get_gaussian_latent(latent_mean, latent_std)

    # HACK: Do first step outside to initialize all the variables
    lstm_states, reward_lstm_states = [None] * 7, [None] * 5
    inputs = images[0], rewards[0], actions[0]
    prev_outputs = (tf.constant(0), images[0], rewards[0], lstm_states)
    if self.hparams.reward_prediction:
      prev_outputs += (reward_lstm_states,)

    initializers = process_single_frame(prev_outputs, inputs)
    first_gen_images = tf.expand_dims(initializers[1], axis=0)
    first_gen_rewards = tf.expand_dims(initializers[2], axis=0)

    inputs = (images[1:-1], actions[1:-1], rewards[1:-1])

    outputs = tf.scan(process_single_frame, inputs, initializers)
    gen_images, gen_rewards = outputs[1:3]

    gen_images = tf.concat((first_gen_images, gen_images), axis=0)
    gen_rewards = tf.concat((first_gen_rewards, gen_rewards), axis=0)

    return gen_images, gen_rewards, [latent_mean], [latent_std]

  def get_scheduled_sample_func(self, batch_size):
    """Creates a function for scheduled sampling based on given hparams."""
    with tf.variable_scope("scheduled_sampling_func", reuse=False):
      iter_num = tf.train.get_global_step()
      # TODO(lukaszkaiser): figure out why iter_num can be None.
      if iter_num is None:
        iter_num = _LARGE_STEP_NUMBER

      if self.hparams.scheduled_sampling_mode == "prob":
        decay_steps = self.hparams.scheduled_sampling_decay_steps
        probability = tf.train.polynomial_decay(
            1.0, iter_num, decay_steps, 0.0)
        scheduled_sampling_func = common_video.scheduled_sample_prob
        scheduled_sampling_func_var = probability
      else:
        # Calculate number of ground-truth frames to pass in.
        k = self.hparams.scheduled_sampling_k
        num_ground_truth = tf.to_int32(
            tf.round(
                tf.to_float(batch_size) *
                (k / (k + tf.exp(tf.to_float(iter_num) / tf.to_float(k))))))
        scheduled_sampling_func = common_video.scheduled_sample_count
        scheduled_sampling_func_var = num_ground_truth

      tf.summary.scalar("scheduled_sampling_var", scheduled_sampling_func_var)
      partial_func = partial(scheduled_sampling_func,
                             batch_size=batch_size,
                             scheduled_sample_var=scheduled_sampling_func_var)
      return partial_func

  def get_scheduled_sample_inputs(self,
                                  done_warm_start,
                                  groundtruth_items,
                                  generated_items,
                                  scheduled_sampling_func):
    """Scheduled sampling.

    Args:
      done_warm_start: whether we are done with warm start or not.
      groundtruth_items: list of ground truth items.
      generated_items: list of generated items.
      scheduled_sampling_func: scheduled sampling function to choose between
        groundtruth items and generated items.

    Returns:
      A mix list of ground truth and generated items.
    """
    def sample():
      """Calculate the scheduled sampling params based on iteration number."""
      with tf.variable_scope("scheduled_sampling", reuse=tf.AUTO_REUSE):
        output_items = []
        for item_gt, item_gen in zip(groundtruth_items, generated_items):
          output_items.append(scheduled_sampling_func(item_gt, item_gen))
        return output_items

    cases = {
        tf.logical_not(done_warm_start): lambda: groundtruth_items,
        tf.logical_not(self.is_training): lambda: generated_items,
    }
    output_items = tf.case(cases, default=sample, strict=True)

    return output_items

  def get_input_if_exists(self, features, key, batch_size, num_frames):
    if key in features:
      x = features[key]
    else:
      x = tf.zeros((batch_size, num_frames, 1, self.hparams.hidden_size))
    return self.swap_time_and_batch_axes(x)

  def swap_time_and_batch_axes(self, x):
    transposed_axes = tf.concat([[1, 0], tf.range(2, tf.rank(x))], axis=0)
    return tf.transpose(x, transposed_axes)

  def body(self, features):
    hparams = self.hparams
    batch_size = common_layers.shape_list(features["inputs"])[0]

    # Swap time and batch axes.
    input_frames = self.swap_time_and_batch_axes(features["inputs"])
    target_frames = self.swap_time_and_batch_axes(features["targets"])

    # Get actions if exist otherwise use zeros
    input_actions = self.get_input_if_exists(
        features, "input_action", batch_size, hparams.video_num_input_frames)
    target_actions = self.get_input_if_exists(
        features, "target_action", batch_size, hparams.video_num_target_frames)

    # Get rewards if exist otherwise use zeros
    input_rewards = self.get_input_if_exists(
        features, "input_reward", batch_size, hparams.video_num_input_frames)
    target_rewards = self.get_input_if_exists(
        features, "target_reward", batch_size, hparams.video_num_target_frames)

    all_actions = tf.concat([input_actions, target_actions], axis=0)
    all_rewards = tf.concat([input_rewards, target_rewards], axis=0)
    all_frames = tf.concat([input_frames, target_frames], axis=0)

    # Each image is being used twice, in latent tower and main tower.
    # This is to make sure we are using the *same* image for both, ...
    # ... given how TF queues work.
    # NOT sure if this is required at all. Doesn"t hurt though! :)
    all_frames = tf.identity(all_frames)

    gen_images, gen_rewards, latent_means, latent_stds = self.construct_model(
        images=all_frames,
        actions=all_actions,
        rewards=all_rewards,
    )

    step_num = tf.train.get_global_step()
    # TODO(mbz): what should it be if it"s undefined?
    if step_num is None:
      step_num = _LARGE_STEP_NUMBER

    schedule = self.hparams.latent_loss_multiplier_schedule
    second_stage = self.hparams.num_iterations_2nd_stage
    # TODO(mechcoder): Add log_annealing schedule.
    if schedule == "constant":
      beta = tf.cond(tf.greater(step_num, second_stage),
                     lambda: self.hparams.latent_loss_multiplier,
                     lambda: 0.0)
    elif schedule == "linear_anneal":
      # Linearly anneal beta from 0.0 to self.hparams.latent_loss_multiplier.
      # between self.hparams.num_iterations_2nd_stage to anneal_end.
      # beta = latent_loss * (1 - (global_step - 2nd_stage) / (anneal_end - 2nd_stage))  # pylint:disable=line-too-long
      anneal_end = self.hparams.anneal_end
      latent_multiplier = self.hparams.latent_loss_multiplier
      if anneal_end < second_stage:
        raise ValueError("Expected hparams.num_iterations_2nd_stage < "
                         "hparams.anneal_end %d, got %d." %
                         (second_stage, anneal_end))

      def anneal_loss(step_num):
        step_num = tf.cast(step_num, dtype=tf.float32)
        fraction = (float(anneal_end) - step_num) / (anneal_end - second_stage)
        return self.hparams.latent_loss_multiplier * (1 - fraction)

      beta = tf.case(
          pred_fn_pairs={
              tf.less(step_num, second_stage): lambda: 0.0,
              tf.greater(step_num, anneal_end): lambda: latent_multiplier},
          default=lambda: anneal_loss(step_num))

    kl_loss = 0.0
    if self.is_training:
      for i, (mean, std) in enumerate(zip(latent_means, latent_stds)):
        kl_loss += common_layers.kl_divergence(mean, std)
        tf.summary.histogram("posterior_mean_%d" % i, mean)
        tf.summary.histogram("posterior_std_%d" % i, std)

      tf.summary.scalar("beta", beta)
      tf.summary.scalar("kl_raw", tf.reduce_mean(kl_loss))

    extra_loss = beta * kl_loss

    # Ignore the predictions from the input frames.
    # This is NOT the same as original paper/implementation.
    predictions = gen_images[hparams.video_num_input_frames-1:]
    reward_pred = gen_rewards[hparams.video_num_input_frames-1:]
    reward_pred = tf.squeeze(reward_pred, axis=2)  # Remove undeeded dimension.

    # TODO(mbz): clean this up!
    def fix_video_dims_and_concat_on_x_axis(x):
      x = tf.transpose(x, [1, 3, 4, 0, 2])
      x = tf.reshape(x, [batch_size, 64, 3, -1])
      x = tf.transpose(x, [0, 3, 1, 2])
      return x

    frames_gd = fix_video_dims_and_concat_on_x_axis(target_frames)
    frames_pd = fix_video_dims_and_concat_on_x_axis(predictions)
    side_by_side_video = tf.concat([frames_gd, frames_pd], axis=2)
    tf.summary.image("full_video", side_by_side_video)

    # Swap back time and batch axes.
    predictions = self.swap_time_and_batch_axes(predictions)
    reward_pred = self.swap_time_and_batch_axes(reward_pred)

    return_targets = predictions
    if "target_reward" in features:
      return_targets = {"targets": predictions, "target_reward": reward_pred}

    return return_targets, extra_loss


@registry.register_model
class NextFrameStochasticTwoFrames(NextFrameStochastic):
  """Stochastic next-frame model with 2 frames posterior."""

  def construct_model(self, images, actions, rewards):
    images = tf.unstack(images, axis=0)
    actions = tf.unstack(actions, axis=0)
    rewards = tf.unstack(rewards, axis=0)

    batch_size = common_layers.shape_list(images[0])[0]
    context_frames = self.hparams.video_num_input_frames

    # Predicted images and rewards.
    gen_rewards, gen_images, latent_means, latent_stds = [], [], [], []

    # LSTM states.
    lstm_state = [None] * 7
    reward_lstm_state = [None] * 5

    # Create scheduled sampling function
    ss_func = self.get_scheduled_sample_func(batch_size)

    pred_image, pred_reward, latent = images[0], rewards[0], None
    for timestep, image, action, reward in zip(
        range(len(images)-1), images[:-1], actions[:-1], rewards[:-1]):
      # Scheduled Sampling
      done_warm_start = timestep > context_frames - 1
      groundtruth_items = [image, reward]
      generated_items = [pred_image, pred_reward]
      input_image, input_reward = self.get_scheduled_sample_inputs(
          done_warm_start, groundtruth_items, generated_items, ss_func)

      # Latent
      # TODO(mbz): should we use input_image iunstead of image?
      latent_images = [image, images[timestep+1]]
      latent_mean, latent_std = self.construct_latent_tower(latent_images)
      latent = self.get_gaussian_latent(latent_mean, latent_std)
      latent_means.append(latent_mean)
      latent_stds.append(latent_std)

      # Prediction
      pred_image, lstm_state = self.construct_predictive_tower(
          input_image, input_reward, action, lstm_state, latent)

      if self.hparams.reward_prediction:
        pred_reward, reward_lstm_state = self.reward_prediction(
            input_image, input_reward, action, reward_lstm_state, latent)
      else:
        pred_reward = input_reward

      gen_images.append(pred_image)
      gen_rewards.append(pred_reward)

    gen_images = tf.stack(gen_images, axis=0)
    gen_rewards = tf.stack(gen_rewards, axis=0)

    return gen_images, gen_rewards, latent_means, latent_stds


@registry.register_model
class NextFrameStochasticEmily(NextFrameStochastic):
  """Model architecture for video prediction model.

     based on following paper:
     "Stochastic Video Generation with a Learned Prior"
     https://arxiv.org/pdf/1802.07687.pdf
     by Emily Denton and Rob Fergus.

     This code is a translation of the original code from PyTorch:
     https://github.com/edenton/svg
  """

  def encoder(self, inputs, nout):
    """VGG based image encoder.

    Args:
      inputs: image tensor with size BSx64x64xC
      nout: number of output channels
    Returns:
      net: encoded image with size BSxNout
      skips: skip connection after each layer
    """
    vgg_layer = common_video.vgg_layer
    net01 = inputs
    # h1
    net11 = tfcl.repeat(net01, 2, vgg_layer, 64,
                        scope="h1", is_training=self.is_training)
    net12 = tfl.max_pooling2d(net11, [2, 2], strides=(2, 2), name="h1_pool")
    # h2
    net21 = tfcl.repeat(net12, 2, vgg_layer, 128,
                        scope="h2", is_training=self.is_training)
    net22 = tfl.max_pooling2d(net21, [2, 2], strides=(2, 2), name="h2_pool")
    # h3
    net31 = tfcl.repeat(net22, 3, vgg_layer, 256,
                        scope="h3", is_training=self.is_training)
    net32 = tfl.max_pooling2d(net31, [2, 2], strides=(2, 2), name="h3_pool")
    # h4
    net41 = tfcl.repeat(net32, 3, vgg_layer, 512,
                        scope="h4", is_training=self.is_training)
    net42 = tfl.max_pooling2d(net41, [2, 2], strides=(2, 2), name="h4_pool")
    # h5
    net51 = tfcl.repeat(net42, 1, vgg_layer, nout,
                        kernel_size=4, padding="VALID", activation=tf.tanh,
                        scope="h5", is_training=self.is_training)
    skips = [net11, net21, net31, net41]
    return net51, skips

  def decoder(self, inputs, skips, nout):
    """VGG based image decoder.

    Args:
      inputs: image tensor with size BSxX
      skips: skip connections from encoder
      nout: number of output channels
    Returns:
      net: decoded image with size BSx64x64xNout
      skips: skip connection after each layer
    """
    vgg_layer = common_video.vgg_layer
    net = inputs
    # d1
    net = tfl.conv2d_transpose(net, 512, kernel_size=4, padding="VALID",
                               name="d1_deconv", activation=None)
    net = tfl.batch_normalization(net, training=self.is_training, name="d1_bn")
    net = tf.nn.leaky_relu(net)
    net = common_layers.upscale(net, 2)
    # d2
    net = tf.concat([net, skips[3]], axis=3)
    net = tfcl.repeat(net, 2, vgg_layer, 512, scope="d2a")
    net = tfcl.repeat(net, 1, vgg_layer, 256, scope="d2b")
    net = common_layers.upscale(net, 2)
    # d3
    net = tf.concat([net, skips[2]], axis=3)
    net = tfcl.repeat(net, 2, vgg_layer, 256, scope="d3a")
    net = tfcl.repeat(net, 1, vgg_layer, 128, scope="d3b")
    net = common_layers.upscale(net, 2)
    # d4
    net = tf.concat([net, skips[1]], axis=3)
    net = tfcl.repeat(net, 1, vgg_layer, 128, scope="d4a")
    net = tfcl.repeat(net, 1, vgg_layer, 64, scope="d4b")
    net = common_layers.upscale(net, 2)
    # d5
    net = tf.concat([net, skips[0]], axis=3)
    net = tfcl.repeat(net, 1, vgg_layer, 64, scope="d5")
    net = tfl.conv2d_transpose(net, nout, kernel_size=3, padding="SAME",
                               name="d6_deconv", activation=tf.sigmoid)
    return net

  def stacked_lstm(self, inputs, states, hidden_size, output_size, nlayers):
    """Stacked LSTM layers with FC layers as input and output embeddings.

    Args:
      inputs: input tensor
      states: a list of internal lstm states for each layer
      hidden_size: number of lstm units
      output_size: size of the output
      nlayers: number of lstm layers
    Returns:
      net: output of the network
      skips: a list of updated lstm states for each layer
    """
    net = inputs
    net = tfl.dense(
        net, hidden_size, activation=None, name="af1")
    for i in range(nlayers):
      net, states[i] = common_video.basic_lstm(
          net, states[i], hidden_size, name="alstm%d"%i)
    net = tfl.dense(
        net, output_size, activation=tf.nn.tanh, name="af2")
    return net, states

  def lstm_gaussian(self, inputs, states, hidden_size, output_size, nlayers):
    """Stacked LSTM layers with FC layer as input and gaussian as output.

    Args:
      inputs: input tensor
      states: a list of internal lstm states for each layer
      hidden_size: number of lstm units
      output_size: size of the output
      nlayers: number of lstm layers
    Returns:
      mu: mean of the predicted gaussian
      logvar: log(var) of the predicted gaussian
      skips: a list of updated lstm states for each layer
    """
    net = inputs
    net = tfl.dense(net, hidden_size, activation=None, name="bf1")
    for i in range(nlayers):
      net, states[i] = common_video.basic_lstm(
          net, states[i], hidden_size, name="blstm%d"%i)
    mu = tfl.dense(net, output_size, activation=None, name="bf2mu")
    logvar = tfl.dense(net, output_size, activation=None, name="bf2log")
    return mu, logvar, states

  def construct_model(self, images, actions, rewards):
    """Builds the stochastic model.

    The model first encodes all the images (x_t) in the sequence
    using the encoder. Let"s call the output e_t. Then it predicts the
    latent state of the next frame using a recurrent posterior network
    z ~ q(z|e_{0:t}) = N(mu(e_{0:t}), sigma(e_{0:t})).
    Another recurrent network predicts the embedding of the next frame
    using the approximated posterior e_{t+1} = p(e_{t+1}|e_{0:t}, z)
    Finally, the decoder decodes e_{t+1} into x_{t+1}.
    Skip connections from encoder to decoder help with reconstruction.

    Args:
      images: tensor of ground truth image sequences
      actions: NOT used list of action tensors
      rewards: NOT used list of reward tensors

    Returns:
      gen_images: generated images
      fakr_rewards: input rewards as reward prediction!
      pred_mu: predited means of posterior
      pred_logvar: predicted log(var) of posterior
    """
    # model does not support action conditioned and reward prediction
    fake_reward_prediction = rewards
    del actions, rewards

    z_dim = self.hparams.z_dim
    g_dim = self.hparams.g_dim
    rnn_size = self.hparams.rnn_size
    posterior_rnn_layers = self.hparams.posterior_rnn_layers
    predictor_rnn_layers = self.hparams.predictor_rnn_layers
    context_frames = self.hparams.video_num_input_frames

    seq_len, batch_size, _, _, color_channels = common_layers.shape_list(images)

    # LSTM initial sizesstates.
    predictor_states = [None] * predictor_rnn_layers
    posterior_states = [None] * posterior_rnn_layers

    tf.logging.info(">>>> Encoding")
    # Encoding:
    enc_images, enc_skips = [], []
    images = tf.unstack(images, axis=0)
    for i, image in enumerate(images):
      with tf.variable_scope("encoder", reuse=tf.AUTO_REUSE):
        enc, skips = self.encoder(image, rnn_size)
        enc = tfcl.flatten(enc)
        enc_images.append(enc)
        enc_skips.append(skips)

    tf.logging.info(">>>> Prediction")
    # Prediction
    pred_enc, pred_mu, pred_logvar = [], [], []
    for i in range(1, seq_len):
      with tf.variable_scope("prediction", reuse=tf.AUTO_REUSE):
        # current encoding
        h_current = enc_images[i-1]
        # target encoding
        h_target = enc_images[i]

        z = tf.random_normal([batch_size, z_dim], 0, 1, dtype=tf.float32)
        mu, logvar = tf.zeros_like(z), tf.zeros_like(z)

        # Only use Posterior if it's training time
        if self.hparams.mode == tf.estimator.ModeKeys.TRAIN:
          mu, logvar, posterior_states = self.lstm_gaussian(
              h_target, posterior_states, rnn_size, z_dim, posterior_rnn_layers)

          # The original implementation has a multiplier of 0.5
          # Removed here for simplicity i.e. replacing var with std
          z = z * tf.exp(logvar) + mu

        # Predict output encoding
        h_pred, predictor_states = self.stacked_lstm(
            tf.concat([h_current, z], axis=1),
            predictor_states, rnn_size, g_dim, predictor_rnn_layers)

        pred_enc.append(h_pred)
        pred_mu.append(mu)
        pred_logvar.append(logvar)

    tf.logging.info(">>>> Decoding")
    # Decoding
    gen_images = []
    for i in range(seq_len-1):
      with tf.variable_scope("decoding", reuse=tf.AUTO_REUSE):
        # use skip values of last available frame
        skip_index = min(context_frames-1, i)

        h_pred = tf.reshape(pred_enc[i], [batch_size, 1, 1, g_dim])
        x_pred = self.decoder(h_pred, enc_skips[skip_index], color_channels)
        gen_images.append(x_pred)

    tf.logging.info(">>>> Done")
    gen_images = tf.stack(gen_images, axis=0)
    return gen_images, fake_reward_prediction, pred_mu, pred_logvar
