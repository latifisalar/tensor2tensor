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
"""Tests for the SAVP model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np

from tensor2tensor.data_generators import video_generated  # pylint: disable=unused-import
from tensor2tensor.models.research import next_frame_params
from tensor2tensor.models.research import next_frame_savp
from tensor2tensor.utils import registry

import tensorflow as tf


class NextFrameSAVPTest(tf.test.TestCase):

  def TestVideoModel(self,
                     in_frames,
                     out_frames,
                     hparams,
                     model,
                     expected_last_dim,
                     upsample_method="conv2d_transpose"):

    x = np.random.random_integers(0, high=255, size=(8, in_frames, 64, 64, 3))
    y = np.random.random_integers(0, high=255, size=(8, out_frames, 64, 64, 3))

    hparams.video_num_input_frames = in_frames
    hparams.video_num_target_frames = out_frames
    hparams.upsample_method = upsample_method
    problem = registry.problem("video_stochastic_shapes10k")
    p_hparams = problem.get_hparams(hparams)
    hparams.problem = problem
    hparams.problem_hparams = p_hparams

    with self.test_session() as session:
      features = {
          "inputs": tf.constant(x, dtype=tf.int32),
          "targets": tf.constant(y, dtype=tf.int32),
      }
      model = model(
          hparams, tf.estimator.ModeKeys.TRAIN)
      logits, _ = model(features)
      session.run(tf.global_variables_initializer())
      res = session.run(logits)
    expected_shape = y.shape + (expected_last_dim,)
    self.assertEqual(res.shape, expected_shape)

  def TestOnVariousInputOutputSizes(self, hparams, model, expected_last_dim):
    self.TestVideoModel(1, 1, hparams, model, expected_last_dim)
    self.TestVideoModel(1, 6, hparams, model, expected_last_dim)
    self.TestVideoModel(4, 1, hparams, model, expected_last_dim)
    self.TestVideoModel(7, 5, hparams, model, expected_last_dim)

  def TestOnVariousUpSampleLayers(self, hparams, model, expected_last_dim):
    self.TestVideoModel(4, 1, hparams, model, expected_last_dim,
                        upsample_method="bilinear_upsample_conv")
    self.TestVideoModel(4, 1, hparams, model, expected_last_dim,
                        upsample_method="nn_upsample_conv")

  def testStochasticSavp(self):
    self.TestOnVariousInputOutputSizes(
        next_frame_params.next_frame_savp(),
        next_frame_savp.NextFrameSAVP,
        1)
    self.TestOnVariousUpSampleLayers(
        next_frame_params.next_frame_savp(),
        next_frame_savp.NextFrameSAVP,
        1)


if __name__ == "__main__":
  tf.test.main()
