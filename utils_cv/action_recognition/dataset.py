# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import os
from pathlib import Path
import warnings
from typing import Callable, Tuple

import decord
from einops.layers.torch import Rearrange
import matplotlib.pyplot as plt
import numpy as np
from numpy.random import randint
import torch
from torch.utils.data import Dataset
from torchvision.transforms import Compose

from .references import transforms_video as transforms
from .references.functional_video import denormalize
from ..common.misc import Config

Trans = Callable[[object, dict], Tuple[object, dict]]

DEFAULT_MEAN = (0.43216, 0.394666, 0.37645)
DEFAULT_STD = (0.22803, 0.22145, 0.216989)


class VideoRecord(object):
    def __init__(self, row):
        self._data = row
        self._num_frames = -1

    @property
    def path(self):
        return self._data[0]

    @property
    def num_frames(self):
        if self._num_frames == -1:
            self._num_frames = int(
                len([x for x in Path(self._data[0]).glob("img_*")]) - 1
            )
        return self._num_frames

    @property
    def label(self):
        return int(self._data[1])


def _get_transforms(tfms_config: Config) -> Trans:
    """ Get default transformations to apply.

    Args:
        Config object with tranforms-related configs

    Returns:
        A list of transforms to apply
    """
    # 1. resize
    tfms = [
        transforms.ToTensorVideo(),
        transforms.ResizeVideo(
            tfms_config.im_scale, tfms_config.resize_keep_ratio
        ),
    ]
    # 2. crop
    if tfms_config.random_crop:
        if tfms_config.random_crop_scales is not None:
            crop = transforms.RandomResizedCropVideo(
                tfms_config.input_size, tfms_config.random_crop_scales
            )
        else:
            crop = transforms.RandomCropVideo(tfms_config.input_size)
    else:
        crop = transforms.CenterCropVideo(tfms_config.input_size)
    tfms.append(crop)
    # 3. flip
    tfms.append(transforms.RandomHorizontalFlipVideo(tfms_config.flip_ratio))
    # 4. normalize
    tfms.append(transforms.NormalizeVideo(tfms_config.mean, tfms_config.std))

    return Compose(tfms)


def get_default_tfms_config(train: bool) -> Config:
    """
    Args:
        train: whether or not this is for training
    Settings:
        input_size (int or tuple): Model input image size.
        im_scale (int or tuple): Resize target size.
        resize_keep_ratio (bool): If True, keep the original ratio when resizing.
        mean (tuple): Normalization mean.
        std (tuple): Normalization std.
        flip_ratio (float): Horizontal flip ratio.
        random_crop (bool): If False, do center-crop.
        random_crop_scales (tuple): Range of size of the origin size random cropped.
    """
    flip_ratio = 0.5 if train else 0.0
    random_crop = True if train else False
    random_crop_scales = (0.6, 1.0) if train else None

    return Config(
        dict(
            input_size=112,
            im_scale=128,
            resize_keep_ratio=True,
            mean=DEFAULT_MEAN,
            std=DEFAULT_STD,
            flip_ratio=flip_ratio,
            random_crop=random_crop,
            random_crop_scales=random_crop_scales,
        )
    )


class VideoDataset(Dataset):
    """
    Args:
        split_file (str): Annotation file containing video filenames and labels.
        video_dir (str): Videos directory.
        num_segments (int): Number of clips to sample from each video.
        sample_length (int): Number of consecutive frames to sample from a video (i.e. clip length).
        sample_step (int): Sampling step.
        temporal_jitter (bool): Randomly skip frames when sampling each frames.
        random_shift (bool): Random temporal shift when sample a clip.
        video_ext (str): Video file extension.
        warning (bool): On or off warning.
        tfms_config: configs for transforms (assume for training by default)
    """

    def __init__(
        self,
        split_file: str,
        video_dir: str,
        num_segments: int = 1,
        sample_length: int = 8,
        sample_step: int = 1,
        temporal_jitter: bool = False,
        random_shift: bool = False,
        video_ext: str = "mp4",
        warning: bool = False,
        tfms_config: Config = get_default_tfms_config(True),
    ):
        # TODO maybe check wrong arguments to early failure
        assert sample_step > 0
        assert num_segments > 0

        self.video_dir = video_dir
        self.video_records = [
            VideoRecord(x.strip().split(" ")) for x in open(split_file)
        ]

        self.num_segments = num_segments
        self.sample_length = sample_length
        self.sample_step = sample_step
        self.presample_length = sample_length * sample_step

        # transform params
        self.transforms = _get_transforms(tfms_config)

        # Temporal noise
        self.random_shift = random_shift
        self.temporal_jitter = temporal_jitter

        self.video_ext = video_ext
        self.warning = warning

    def __len__(self):
        return len(self.video_records)

    def _sample_indices(self, record):
        """
        Args:
            record (VideoRecord): A video record.
        Return:
            list: Segment offsets (start indices)
        """
        if record.num_frames > self.presample_length:
            if self.random_shift:
                # Random sample
                offsets = np.sort(
                    randint(
                        record.num_frames - self.presample_length + 1,
                        size=self.num_segments,
                    )
                )
            else:
                # Uniform sample
                distance = (
                    record.num_frames - self.presample_length + 1
                ) / self.num_segments
                offsets = np.array(
                    [
                        int(distance / 2.0 + distance * x)
                        for x in range(self.num_segments)
                    ]
                )
        else:
            if self.warning:
                warnings.warn(
                    "num_segments and/or sample_length > num_frames in {}".format(
                        record.path
                    )
                )
            offsets = np.zeros((self.num_segments,), dtype=int)

        return offsets

    def _get_frames(self, video_reader, offset):
        clip = list()

        # decord.seek() seems to have a bug. use seek_accurate().
        video_reader.seek_accurate(offset)
        # first frame
        clip.append(video_reader.next().asnumpy())
        # remaining frames
        try:
            if self.temporal_jitter:
                for i in range(self.sample_length - 1):
                    step = randint(self.sample_step + 1)
                    if step == 0:
                        clip.append(clip[-1].copy())
                    else:
                        if step > 1:
                            video_reader.skip_frames(step - 1)
                        cur_frame = video_reader.next().asnumpy()
                        if len(cur_frame.shape) != 3:
                            # maybe end of the video
                            break
                        clip.append(cur_frame)
            else:
                for i in range(self.sample_length - 1):
                    if self.sample_step > 1:
                        video_reader.skip_frames(self.sample_step - 1)
                    cur_frame = video_reader.next().asnumpy()
                    if len(cur_frame.shape) != 3:
                        # maybe end of the video
                        break
                    clip.append(cur_frame)
        except StopIteration:
            pass

        # if clip needs more frames, simply duplicate the last frame in the clip.
        while len(clip) < self.sample_length:
            clip.append(clip[-1].copy())

        return clip

    def __getitem__(self, idx):
        """
        Return:
            clips (torch.tensor), label (int)
        """
        record = self.video_records[idx]
        video_reader = decord.VideoReader(
            "{}.{}".format(
                os.path.join(self.video_dir, record.path), self.video_ext
            ),
            # TODO try to add `ctx=decord.ndarray.gpu(0) or .cuda(0)`
        )
        record._num_frames = len(video_reader)

        offsets = self._sample_indices(record)
        clips = np.array([self._get_frames(video_reader, o) for o in offsets])

        if self.num_segments == 1:
            # [T, H, W, C] -> [C, T, H, W]
            return self.transforms(torch.from_numpy(clips[0])), record.label
        else:
            # [S, T, H, W, C] -> [S, C, T, H, W]
            return (
                torch.stack(
                    [self.transforms(torch.from_numpy(c)) for c in clips]
                ),
                record.label,
            )


def show_batch(batch, sample_length, mean=DEFAULT_MEAN, std=DEFAULT_STD):
    """
    Args:
        batch (list[torch.tensor]): List of sample (clip) tensors
        sample_length (int): Number of frames to show for each sample
        mean (tuple): Normalization mean
        std (tuple): Normalization std-dev
    """
    batch_size = len(batch)
    plt.tight_layout()
    fig, axs = plt.subplots(
        batch_size, sample_length, figsize=(4 * sample_length, 3 * batch_size)
    )

    for i, ax in enumerate(axs):
        if batch_size == 1:
            clip = batch[0]
        else:
            clip = batch[i]
        clip = Rearrange("c t h w -> t c h w")(clip)
        if not isinstance(ax, np.ndarray):
            ax = [ax]
        for j, a in enumerate(ax):
            a.axis("off")
            a.imshow(
                np.moveaxis(denormalize(clip[j], mean, std).numpy(), 0, -1)
            )
