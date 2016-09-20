#!/usr/bin/env python
# ----------------------------------------------------------------------------
# Copyright 2016 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
"""
Test a trained Faster-RCNN model to do object detection using PASCAL VOC dataset.
This test currently runs 1 image at a time.

Reference:
    "Faster R-CNN"
    http://arxiv.org/abs/1506.01497
    https://github.com/rbgirshick/py-faster-rcnn

Usage:
    python examples/faster-rcnn/inference.py --model_file faster_rcnn.pkl

At the end of the a training process, the model is serialized with the bounding box
regression layer normalized. If you like to test on a model file before the training
is finished, use --normalize flag to do the normalization here.

The mAP evaluation script is adapted from:
https://github.com/rbgirshick/py-faster-rcnn/
"""

import sys
import os
from builtins import range

import util
from objectlocalization import PASCALVOC, ObjectLocalization

from neon.backends import gen_backend
from neon.util.persist import get_data_cache_dir
from neon.util.argparser import NeonArgparser, extract_valid_args
from neon import logger as neon_logger
from voc_eval import voc_eval
from neon.data.dataloader_transformers import BGRMeanSubtract, TypeCast
from aeon import DataLoader
import numpy as np
import faster_rcnn

# parse the command line arguments
parser = NeonArgparser(__doc__, default_overrides={'batch_size': 1})
parser.add_argument('--normalize', action='store_true',
                    help='Normalize the final bounding box regression layers.')
parser.add_argument('--output_dir', default=None,
                    help='Directory to save AP metric results. Default is [data_dir]/frcn_output/')
parser.add_argument('--width', type=int, default=1000, help='Width of input image')
parser.add_argument('--height', type=int, default=1000, help='Height of input image')

args = parser.parse_args()
if args.output_dir is None:
    args.output_dir = os.path.join(args.data_dir, 'frcn_output')

assert args.model_file is not None, "Model file required for Faster-RCNN testing"
assert 'val' in args.manifest, "Path to manifest file requred"

# hyperparameters
assert args.batch_size is 1, "Faster-RCNN only supports batch size 1"
rpn_rois_per_img = 256
frcn_rois_per_img = 128

# setup backend
be = gen_backend(**extract_valid_args(args, gen_backend))

# build data loader
cache_dir = get_data_cache_dir(args.data_dir, subdir='pascalvoc_cache')
config = PASCALVOC(args.manifest['val'], cache_dir,
                   width=args.width, height=args.height,
                   rois_per_img=rpn_rois_per_img, inference=True)

dl = DataLoader(config, be)
dl = TypeCast(dl, index=0, dtype=np.float32)
dl = BGRMeanSubtract(dl, index=0, pixel_mean=util.FRCN_PIXEL_MEANS)
valid_set = ObjectLocalization(dl, frcn_rois_per_img=frcn_rois_per_img)

num_classes = valid_set.num_classes

# build the Faster-RCNN network
(model, proposalLayer) = faster_rcnn.build_model(valid_set, frcn_rois_per_img, inference=True)

# load parameters and initialize model
model.load_params(args.model_file)
model.initialize(dataset=valid_set)

# normalize the model by the bbtarget mean and std if needed
# if a full training run was completed using train.py, then normalization
# was already performed prior to saving the model.
if args.normalize:
    model = util.scale_bbreg_weights(model, [0.0, 0.0, 0.0, 0.0],
                                     [0.1, 0.1, 0.2, 0.2], num_classes)

# run inference

# detection parameters
num_images = valid_set.ndata
max_per_image = 100   # maximum detections per image
thresh = 0.001  # minimum threshold on score
nms_thresh = 0.3  # threshold used for non-maximum supression

# all detections are collected into:
#    all_boxes[cls][image] = N x 5 array of detections in
#    (x1, y1, x2, y2, score)
all_boxes = [[[] for _ in range(num_classes)]
             for _ in range(num_images)]
all_gt_boxes = [[] for _ in xrange(num_images)]

last_strlen = 0
for mb_idx, (x, y) in enumerate(valid_set):

    prt_str = "Finished: {} / {}".format(mb_idx, num_images)
    sys.stdout.write('\r' + ' '*last_strlen + '\r')
    sys.stdout.write(prt_str.encode('utf-8'))
    last_strlen = len(prt_str)
    sys.stdout.flush()

    # perform forward pass
    outputs = model.fprop(x, inference=True)

    # retrieve image metadata
    (im_shape, im_scale, gt_boxes, gt_classes,
        num_gt_boxes, difficult) = valid_set.get_metadata_buffers()

    num_gt_boxes = int(num_gt_boxes.get())
    im_scale = float(im_scale.get())

    # retrieve region proposals generated by the model
    (proposals, num_proposals) = proposalLayer.get_proposals()

    # convert outputs to bounding boxes
    boxes = faster_rcnn.get_bboxes(outputs, proposals, num_proposals, valid_set.num_classes,
                                   im_shape.get(), im_scale, max_per_image, thresh, nms_thresh)

    all_boxes[mb_idx] = boxes

    # retrieve gt boxes
    # we add a extra column to track detections during the AP calculation
    detected = np.array([False] * num_gt_boxes)
    gt_boxes = np.hstack([gt_boxes.get()[:num_gt_boxes] / im_scale,
                          gt_classes.get()[:num_gt_boxes],
                          difficult.get()[:num_gt_boxes], detected[:, np.newaxis]])

    all_gt_boxes[mb_idx] = gt_boxes


neon_logger.display('Evaluating detections')
voc_eval(all_boxes, all_gt_boxes, valid_set.CLASSES, use_07_metric=True)
