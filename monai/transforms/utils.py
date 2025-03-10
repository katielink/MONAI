# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import itertools
import random
import warnings
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache, wraps
from inspect import getmembers, isclass
from typing import Any

import numpy as np
import torch

import monai
from monai.config import DtypeLike, IndexSelection
from monai.config.type_definitions import NdarrayOrTensor, NdarrayTensor
from monai.networks.layers import GaussianFilter
from monai.networks.utils import meshgrid_ij
from monai.transforms.compose import Compose, OneOf
from monai.transforms.transform import MapTransform, Transform, apply_transform
from monai.transforms.utils_pytorch_numpy_unification import (
    any_np_pt,
    ascontiguousarray,
    cumsum,
    isfinite,
    nonzero,
    ravel,
    searchsorted,
    unique,
    unravel_index,
    where,
)
from monai.utils import (
    GridSampleMode,
    GridSamplePadMode,
    InterpolateMode,
    NdimageMode,
    NumpyPadMode,
    PostFix,
    PytorchPadMode,
    SplineMode,
    TraceKeys,
    ensure_tuple,
    ensure_tuple_rep,
    ensure_tuple_size,
    fall_back_tuple,
    get_equivalent_dtype,
    issequenceiterable,
    look_up_option,
    min_version,
    optional_import,
    pytorch_after,
)
from monai.utils.enums import TransformBackends
from monai.utils.type_conversion import convert_data_type, convert_to_cupy, convert_to_dst_type, convert_to_tensor

measure, has_measure = optional_import("skimage.measure", "0.14.2", min_version)
morphology, has_morphology = optional_import("skimage.morphology")
ndimage, _ = optional_import("scipy.ndimage")
cp, has_cp = optional_import("cupy")
cp_ndarray, _ = optional_import("cupy", name="ndarray")
exposure, has_skimage = optional_import("skimage.exposure")

__all__ = [
    "allow_missing_keys_mode",
    "check_boundaries",
    "compute_divisible_spatial_size",
    "convert_applied_interp_mode",
    "copypaste_arrays",
    "check_non_lazy_pending_ops",
    "create_control_grid",
    "create_grid",
    "create_rotate",
    "create_scale",
    "create_shear",
    "create_translate",
    "extreme_points_to_image",
    "fill_holes",
    "Fourier",
    "generate_label_classes_crop_centers",
    "generate_pos_neg_label_crop_centers",
    "generate_spatial_bounding_box",
    "get_extreme_points",
    "get_largest_connected_component_mask",
    "remove_small_objects",
    "img_bounds",
    "in_bounds",
    "is_empty",
    "is_positive",
    "map_binary_to_indices",
    "map_classes_to_indices",
    "map_spatial_axes",
    "rand_choice",
    "rescale_array",
    "rescale_array_int_max",
    "rescale_instance_array",
    "resize_center",
    "weighted_patch_samples",
    "zero_margins",
    "equalize_hist",
    "get_number_image_type_conversions",
    "get_transform_backends",
    "print_transform_backends",
    "convert_pad_mode",
    "convert_to_contiguous",
    "get_unique_labels",
    "scale_affine",
    "attach_hook",
    "sync_meta_info",
    "reset_ops_id",
    "resolves_modes",
]


def rand_choice(prob: float = 0.5) -> bool:
    """
    Returns True if a randomly chosen number is less than or equal to `prob`, by default this is a 50/50 chance.
    """
    return bool(random.random() <= prob)


def img_bounds(img: np.ndarray):
    """
    Returns the minimum and maximum indices of non-zero lines in axis 0 of `img`, followed by that for axis 1.
    """
    ax0 = np.any(img, axis=0)
    ax1 = np.any(img, axis=1)
    return np.concatenate((np.where(ax0)[0][[0, -1]], np.where(ax1)[0][[0, -1]]))


def in_bounds(x: float, y: float, margin: float, maxx: float, maxy: float) -> bool:
    """
    Returns True if (x,y) is within the rectangle (margin, margin, maxx-margin, maxy-margin).
    """
    return bool(margin <= x < (maxx - margin) and margin <= y < (maxy - margin))


def is_empty(img: np.ndarray | torch.Tensor) -> bool:
    """
    Returns True if `img` is empty, that is its maximum value is not greater than its minimum.
    """
    return not (img.max() > img.min())  # use > instead of <= so that an image full of NaNs will result in True


def is_positive(img):
    """
    Returns a boolean version of `img` where the positive values are converted into True, the other values are False.
    """
    return img > 0


def zero_margins(img: np.ndarray, margin: int) -> bool:
    """
    Returns True if the values within `margin` indices of the edges of `img` in dimensions 1 and 2 are 0.
    """
    if np.any(img[:, :, :margin]) or np.any(img[:, :, -margin:]):
        return False

    return not np.any(img[:, :margin, :]) and not np.any(img[:, -margin:, :])


def rescale_array(
    arr: NdarrayOrTensor,
    minv: float | None = 0.0,
    maxv: float | None = 1.0,
    dtype: DtypeLike | torch.dtype = np.float32,
) -> NdarrayOrTensor:
    """
    Rescale the values of numpy array `arr` to be from `minv` to `maxv`.
    If either `minv` or `maxv` is None, it returns `(a - min_a) / (max_a - min_a)`.

    Args:
        arr: input array to rescale.
        minv: minimum value of target rescaled array.
        maxv: maxmum value of target rescaled array.
        dtype: if not None, convert input array to dtype before computation.

    """
    if dtype is not None:
        arr, *_ = convert_data_type(arr, dtype=dtype)
    mina = arr.min()
    maxa = arr.max()

    if mina == maxa:
        return arr * minv if minv is not None else arr

    norm = (arr - mina) / (maxa - mina)  # normalize the array first
    if (minv is None) or (maxv is None):
        return norm
    return (norm * (maxv - minv)) + minv  # rescale by minv and maxv, which is the normalized array by default


def rescale_instance_array(
    arr: np.ndarray, minv: float | None = 0.0, maxv: float | None = 1.0, dtype: DtypeLike = np.float32
) -> np.ndarray:
    """
    Rescale each array slice along the first dimension of `arr` independently.
    """
    out: np.ndarray = np.zeros(arr.shape, dtype or arr.dtype)
    for i in range(arr.shape[0]):
        out[i] = rescale_array(arr[i], minv, maxv, dtype)

    return out


def rescale_array_int_max(arr: np.ndarray, dtype: DtypeLike = np.uint16) -> np.ndarray:
    """
    Rescale the array `arr` to be between the minimum and maximum values of the type `dtype`.
    """
    info: np.iinfo = np.iinfo(dtype or arr.dtype)
    return np.asarray(rescale_array(arr, info.min, info.max), dtype=dtype or arr.dtype)


def copypaste_arrays(
    src_shape, dest_shape, srccenter: Sequence[int], destcenter: Sequence[int], dims: Sequence[int | None]
) -> tuple[tuple[slice, ...], tuple[slice, ...]]:
    """
    Calculate the slices to copy a sliced area of array in `src_shape` into array in `dest_shape`.

    The area has dimensions `dims` (use 0 or None to copy everything in that dimension),
    the source area is centered at `srccenter` index in `src` and copied into area centered at `destcenter` in `dest`.
    The dimensions of the copied area will be clipped to fit within the
    source and destination arrays so a smaller area may be copied than expected. Return value is the tuples of slice
    objects indexing the copied area in `src`, and those indexing the copy area in `dest`.

    Example

    .. code-block:: python

        src_shape = (6,6)
        src = np.random.randint(0,10,src_shape)
        dest = np.zeros_like(src)
        srcslices, destslices = copypaste_arrays(src_shape, dest.shape, (3, 2),(2, 1),(3, 4))
        dest[destslices] = src[srcslices]
        print(src)
        print(dest)

        >>> [[9 5 6 6 9 6]
             [4 3 5 6 1 2]
             [0 7 3 2 4 1]
             [3 0 0 1 5 1]
             [9 4 7 1 8 2]
             [6 6 5 8 6 7]]
            [[0 0 0 0 0 0]
             [7 3 2 4 0 0]
             [0 0 1 5 0 0]
             [4 7 1 8 0 0]
             [0 0 0 0 0 0]
             [0 0 0 0 0 0]]

    """
    s_ndim = len(src_shape)
    d_ndim = len(dest_shape)
    srcslices = [slice(None)] * s_ndim
    destslices = [slice(None)] * d_ndim

    for i, ss, ds, sc, dc, dim in zip(range(s_ndim), src_shape, dest_shape, srccenter, destcenter, dims):
        if dim:
            # dimension before midpoint, clip to size fitting in both arrays
            d1 = np.clip(dim // 2, 0, min(sc, dc))
            # dimension after midpoint, clip to size fitting in both arrays
            d2 = np.clip(dim // 2 + 1, 0, min(ss - sc, ds - dc))

            srcslices[i] = slice(sc - d1, sc + d2)
            destslices[i] = slice(dc - d1, dc + d2)

    return tuple(srcslices), tuple(destslices)


def resize_center(img: np.ndarray, *resize_dims: int | None, fill_value: float = 0.0, inplace: bool = True):
    """
    Resize `img` by cropping or expanding the image from the center. The `resize_dims` values are the output dimensions
    (or None to use original dimension of `img`). If a dimension is smaller than that of `img` then the result will be
    cropped and if larger padded with zeros, in both cases this is done relative to the center of `img`. The result is
    a new image with the specified dimensions and values from `img` copied into its center.
    """

    resize_dims = fall_back_tuple(resize_dims, img.shape)

    half_img_shape = (np.asarray(img.shape) // 2).tolist()
    half_dest_shape = (np.asarray(resize_dims) // 2).tolist()
    srcslices, destslices = copypaste_arrays(img.shape, resize_dims, half_img_shape, half_dest_shape, resize_dims)

    if not inplace:
        dest = np.full(resize_dims, fill_value, img.dtype)  # type: ignore
        dest[destslices] = img[srcslices]
        return dest
    return img[srcslices]


def check_non_lazy_pending_ops(
    input_array: NdarrayOrTensor, name: None | str = None, raise_error: bool = False
) -> None:
    """
    Check whether the input array has pending operations, raise an error or warn when it has.

    Args:
        input_array: input array to be checked.
        name: an optional name to be included in the error message.
        raise_error: whether to raise an error, default to False, a warning message will be issued instead.
    """
    if isinstance(input_array, monai.data.MetaTensor) and input_array.pending_operations:
        msg = (
            "The input image is a MetaTensor and has pending operations,\n"
            f"but the function {name or ''} assumes non-lazy input, result may be incorrect."
        )
        if raise_error:
            raise ValueError(msg)
        warnings.warn(msg)


def map_binary_to_indices(
    label: NdarrayOrTensor, image: NdarrayOrTensor | None = None, image_threshold: float = 0.0
) -> tuple[NdarrayOrTensor, NdarrayOrTensor]:
    """
    Compute the foreground and background of input label data, return the indices after fattening.
    For example:
    ``label = np.array([[[0, 1, 1], [1, 0, 1], [1, 1, 0]]])``
    ``foreground indices = np.array([1, 2, 3, 5, 6, 7])`` and ``background indices = np.array([0, 4, 8])``

    Args:
        label: use the label data to get the foreground/background information.
        image: if image is not None, use ``label = 0 & image > image_threshold``
            to define background. so the output items will not map to all the voxels in the label.
        image_threshold: if enabled `image`, use ``image > image_threshold`` to
            determine the valid image content area and select background only in this area.
    """
    check_non_lazy_pending_ops(label, name="map_binary_to_indices")
    # Prepare fg/bg indices
    if label.shape[0] > 1:
        label = label[1:]  # for One-Hot format data, remove the background channel
    label_flat = ravel(any_np_pt(label, 0))  # in case label has multiple dimensions
    fg_indices = nonzero(label_flat)
    if image is not None:
        check_non_lazy_pending_ops(image, name="map_binary_to_indices")
        img_flat = ravel(any_np_pt(image > image_threshold, 0))
        img_flat, *_ = convert_to_dst_type(img_flat, label, dtype=bool)
        bg_indices = nonzero(img_flat & ~label_flat)
    else:
        bg_indices = nonzero(~label_flat)

    # no need to save the indices in GPU, otherwise, still need to move to CPU at runtime when crop by indices
    fg_indices, *_ = convert_data_type(fg_indices, device=torch.device("cpu"))
    bg_indices, *_ = convert_data_type(bg_indices, device=torch.device("cpu"))
    return fg_indices, bg_indices


def map_classes_to_indices(
    label: NdarrayOrTensor,
    num_classes: int | None = None,
    image: NdarrayOrTensor | None = None,
    image_threshold: float = 0.0,
    max_samples_per_class: int | None = None,
) -> list[NdarrayOrTensor]:
    """
    Filter out indices of every class of the input label data, return the indices after fattening.
    It can handle both One-Hot format label and Argmax format label, must provide `num_classes` for
    Argmax label.

    For example:
    ``label = np.array([[[0, 1, 2], [2, 0, 1], [1, 2, 0]]])`` and `num_classes=3`, will return a list
    which contains the indices of the 3 classes:
    ``[np.array([0, 4, 8]), np.array([1, 5, 6]), np.array([2, 3, 7])]``

    Args:
        label: use the label data to get the indices of every class.
        num_classes: number of classes for argmax label, not necessary for One-Hot label.
        image: if image is not None, only return the indices of every class that are within the valid
            region of the image (``image > image_threshold``).
        image_threshold: if enabled `image`, use ``image > image_threshold`` to
            determine the valid image content area and select class indices only in this area.
        max_samples_per_class: maximum length of indices in each class to reduce memory consumption.
            Default is None, no subsampling.

    """
    check_non_lazy_pending_ops(label, name="map_classes_to_indices")
    img_flat: NdarrayOrTensor | None = None
    if image is not None:
        check_non_lazy_pending_ops(image, name="map_classes_to_indices")
        img_flat = ravel((image > image_threshold).any(0))

    # assuming the first dimension is channel
    channels = len(label)

    num_classes_: int = channels
    if channels == 1:
        if num_classes is None:
            raise ValueError("channels==1 indicates not using One-Hot format label, must provide ``num_classes``.")
        num_classes_ = num_classes

    indices: list[NdarrayOrTensor] = []
    for c in range(num_classes_):
        if channels > 1:
            label_flat = ravel(convert_data_type(label[c], dtype=bool)[0])
        else:
            label_flat = ravel(label == c)
        if img_flat is not None:
            label_flat = img_flat & label_flat
        # no need to save the indices in GPU, otherwise, still need to move to CPU at runtime when crop by indices
        cls_indices: NdarrayOrTensor = convert_data_type(nonzero(label_flat), device=torch.device("cpu"))[0]
        if max_samples_per_class and len(cls_indices) > max_samples_per_class and len(cls_indices) > 1:
            sample_id = np.round(np.linspace(0, len(cls_indices) - 1, max_samples_per_class)).astype(int)
            indices.append(cls_indices[sample_id])
        else:
            indices.append(cls_indices)

    return indices


def weighted_patch_samples(
    spatial_size: int | Sequence[int],
    w: NdarrayOrTensor,
    n_samples: int = 1,
    r_state: np.random.RandomState | None = None,
) -> list:
    """
    Computes `n_samples` of random patch sampling locations, given the sampling weight map `w` and patch `spatial_size`.

    Args:
        spatial_size: length of each spatial dimension of the patch.
        w: weight map, the weights must be non-negative. each element denotes a sampling weight of the spatial location.
            0 indicates no sampling.
            The weight map shape is assumed ``(spatial_dim_0, spatial_dim_1, ..., spatial_dim_n)``.
        n_samples: number of patch samples
        r_state: a random state container

    Returns:
        a list of `n_samples` N-D integers representing the spatial sampling location of patches.

    """
    check_non_lazy_pending_ops(w, name="weighted_patch_samples")
    if w is None:
        raise ValueError("w must be an ND array, got None.")
    if r_state is None:
        r_state = np.random.RandomState()
    img_size = np.asarray(w.shape, dtype=int)
    win_size = np.asarray(fall_back_tuple(spatial_size, img_size), dtype=int)

    s = tuple(slice(w // 2, m - w + w // 2) if m > w else slice(m // 2, m // 2 + 1) for w, m in zip(win_size, img_size))
    v = w[s]  # weight map in the 'valid' mode
    v_size = v.shape
    v = ravel(v)  # always copy
    if (v < 0).any():
        v -= v.min()  # shifting to non-negative
    v = cumsum(v)
    if not v[-1] or not isfinite(v[-1]) or v[-1] < 0:  # uniform sampling
        idx = r_state.randint(0, len(v), size=n_samples)
    else:
        r, *_ = convert_to_dst_type(r_state.random(n_samples), v)
        idx = searchsorted(v, r * v[-1], right=True)  # type: ignore
    idx, *_ = convert_to_dst_type(idx, v, dtype=torch.int)  # type: ignore
    # compensate 'valid' mode
    diff = np.minimum(win_size, img_size) // 2
    diff, *_ = convert_to_dst_type(diff, v)  # type: ignore
    return [unravel_index(i, v_size) + diff for i in idx]


def correct_crop_centers(
    centers: list[int],
    spatial_size: Sequence[int] | int,
    label_spatial_shape: Sequence[int],
    allow_smaller: bool = False,
) -> tuple[Any]:
    """
    Utility to correct the crop center if the crop size and centers are not compatible with the image size.

    Args:
        centers: pre-computed crop centers of every dim, will correct based on the valid region.
        spatial_size: spatial size of the ROIs to be sampled.
        label_spatial_shape: spatial shape of the original label data to compare with ROI.
        allow_smaller: if `False`, an exception will be raised if the image is smaller than
            the requested ROI in any dimension. If `True`, any smaller dimensions will be set to
            match the cropped size (i.e., no cropping in that dimension).

    """
    spatial_size = fall_back_tuple(spatial_size, default=label_spatial_shape)
    if any(np.subtract(label_spatial_shape, spatial_size) < 0):
        if not allow_smaller:
            raise ValueError(
                "The size of the proposed random crop ROI is larger than the image size, "
                f"got ROI size {spatial_size} and label image size {label_spatial_shape} respectively."
            )
        spatial_size = tuple(min(l, s) for l, s in zip(label_spatial_shape, spatial_size))

    # Select subregion to assure valid roi
    valid_start = np.floor_divide(spatial_size, 2)
    # add 1 for random
    valid_end = np.subtract(label_spatial_shape + np.array(1), spatial_size / np.array(2)).astype(np.uint16)
    # int generation to have full range on upper side, but subtract unfloored size/2 to prevent rounded range
    # from being too high
    for i, valid_s in enumerate(valid_start):
        # need this because np.random.randint does not work with same start and end
        if valid_s == valid_end[i]:
            valid_end[i] += 1
    valid_centers = []
    for c, v_s, v_e in zip(centers, valid_start, valid_end):
        center_i = min(max(c, v_s), v_e - 1)
        valid_centers.append(int(center_i))
    return ensure_tuple(valid_centers)  # type: ignore


def generate_pos_neg_label_crop_centers(
    spatial_size: Sequence[int] | int,
    num_samples: int,
    pos_ratio: float,
    label_spatial_shape: Sequence[int],
    fg_indices: NdarrayOrTensor,
    bg_indices: NdarrayOrTensor,
    rand_state: np.random.RandomState | None = None,
    allow_smaller: bool = False,
) -> tuple[tuple]:
    """
    Generate valid sample locations based on the label with option for specifying foreground ratio
    Valid: samples sitting entirely within image, expected input shape: [C, H, W, D] or [C, H, W]

    Args:
        spatial_size: spatial size of the ROIs to be sampled.
        num_samples: total sample centers to be generated.
        pos_ratio: ratio of total locations generated that have center being foreground.
        label_spatial_shape: spatial shape of the original label data to unravel selected centers.
        fg_indices: pre-computed foreground indices in 1 dimension.
        bg_indices: pre-computed background indices in 1 dimension.
        rand_state: numpy randomState object to align with other modules.
        allow_smaller: if `False`, an exception will be raised if the image is smaller than
            the requested ROI in any dimension. If `True`, any smaller dimensions will be set to
            match the cropped size (i.e., no cropping in that dimension).

    Raises:
        ValueError: When the proposed roi is larger than the image.
        ValueError: When the foreground and background indices lengths are 0.

    """
    if rand_state is None:
        rand_state = np.random.random.__self__  # type: ignore

    centers = []
    fg_indices = np.asarray(fg_indices) if isinstance(fg_indices, Sequence) else fg_indices
    bg_indices = np.asarray(bg_indices) if isinstance(bg_indices, Sequence) else bg_indices
    if len(fg_indices) == 0 and len(bg_indices) == 0:
        raise ValueError("No sampling location available.")

    if len(fg_indices) == 0 or len(bg_indices) == 0:
        pos_ratio = 0 if len(fg_indices) == 0 else 1
        warnings.warn(
            f"Num foregrounds {len(fg_indices)}, Num backgrounds {len(bg_indices)}, "
            f"unable to generate class balanced samples, setting `pos_ratio` to {pos_ratio}."
        )

    for _ in range(num_samples):
        indices_to_use = fg_indices if rand_state.rand() < pos_ratio else bg_indices
        random_int = rand_state.randint(len(indices_to_use))
        idx = indices_to_use[random_int]
        center = unravel_index(idx, label_spatial_shape).tolist()
        # shift center to range of valid centers
        centers.append(correct_crop_centers(center, spatial_size, label_spatial_shape, allow_smaller))

    return ensure_tuple(centers)  # type: ignore


def generate_label_classes_crop_centers(
    spatial_size: Sequence[int] | int,
    num_samples: int,
    label_spatial_shape: Sequence[int],
    indices: Sequence[NdarrayOrTensor],
    ratios: list[float | int] | None = None,
    rand_state: np.random.RandomState | None = None,
    allow_smaller: bool = False,
    warn: bool = True,
) -> tuple[tuple]:
    """
    Generate valid sample locations based on the specified ratios of label classes.
    Valid: samples sitting entirely within image, expected input shape: [C, H, W, D] or [C, H, W]

    Args:
        spatial_size: spatial size of the ROIs to be sampled.
        num_samples: total sample centers to be generated.
        label_spatial_shape: spatial shape of the original label data to unravel selected centers.
        indices: sequence of pre-computed foreground indices of every class in 1 dimension.
        ratios: ratios of every class in the label to generate crop centers, including background class.
            if None, every class will have the same ratio to generate crop centers.
        rand_state: numpy randomState object to align with other modules.
        allow_smaller: if `False`, an exception will be raised if the image is smaller than
            the requested ROI in any dimension. If `True`, any smaller dimensions will be set to
            match the cropped size (i.e., no cropping in that dimension).
        warn: if `True` prints a warning if a class is not present in the label.

    """
    if rand_state is None:
        rand_state = np.random.random.__self__  # type: ignore

    if num_samples < 1:
        raise ValueError(f"num_samples must be an int number and greater than 0, got {num_samples}.")
    ratios_: list[float | int] = list(ensure_tuple([1] * len(indices) if ratios is None else ratios))
    if len(ratios_) != len(indices):
        raise ValueError(
            f"random crop ratios must match the number of indices of classes, got {len(ratios_)} and {len(indices)}."
        )
    if any(i < 0 for i in ratios_):
        raise ValueError(f"ratios should not contain negative number, got {ratios_}.")

    for i, array in enumerate(indices):
        if len(array) == 0:
            ratios_[i] = 0
            if warn:
                warnings.warn(f"no available indices of class {i} to crop, set the crop ratio of this class to zero.")

    centers = []
    classes = rand_state.choice(len(ratios_), size=num_samples, p=np.asarray(ratios_) / np.sum(ratios_))
    for i in classes:
        # randomly select the indices of a class based on the ratios
        indices_to_use = indices[i]
        random_int = rand_state.randint(len(indices_to_use))
        center = unravel_index(indices_to_use[random_int], label_spatial_shape).tolist()
        # shift center to range of valid centers
        centers.append(correct_crop_centers(center, spatial_size, label_spatial_shape, allow_smaller))

    return ensure_tuple(centers)  # type: ignore


def create_grid(
    spatial_size: Sequence[int],
    spacing: Sequence[float] | None = None,
    homogeneous: bool = True,
    dtype: DtypeLike | torch.dtype = float,
    device: torch.device | None = None,
    backend=TransformBackends.NUMPY,
) -> NdarrayOrTensor:
    """
    compute a `spatial_size` mesh.

        - when ``homogeneous=True``, the output shape is (N+1, dim_size_1, dim_size_2, ..., dim_size_N)
        - when ``homogeneous=False``, the output shape is (N, dim_size_1, dim_size_2, ..., dim_size_N)

    Args:
        spatial_size: spatial size of the grid.
        spacing: same len as ``spatial_size``, defaults to 1.0 (dense grid).
        homogeneous: whether to make homogeneous coordinates.
        dtype: output grid data type, defaults to `float`.
        device: device to compute and store the output (when the backend is "torch").
        backend: APIs to use, ``numpy`` or ``torch``.

    """
    _backend = look_up_option(backend, TransformBackends)
    _dtype = dtype or float
    if _backend == TransformBackends.NUMPY:
        return _create_grid_numpy(spatial_size, spacing, homogeneous, _dtype)  # type: ignore
    if _backend == TransformBackends.TORCH:
        return _create_grid_torch(spatial_size, spacing, homogeneous, _dtype, device)  # type: ignore
    raise ValueError(f"backend {backend} is not supported")


def _create_grid_numpy(
    spatial_size: Sequence[int],
    spacing: Sequence[float] | None = None,
    homogeneous: bool = True,
    dtype: DtypeLike | torch.dtype = float,
):
    """
    compute a `spatial_size` mesh with the numpy API.
    """
    spacing = spacing or tuple(1.0 for _ in spatial_size)
    ranges = [np.linspace(-(d - 1.0) / 2.0 * s, (d - 1.0) / 2.0 * s, int(d)) for d, s in zip(spatial_size, spacing)]
    coords = np.asarray(np.meshgrid(*ranges, indexing="ij"), dtype=get_equivalent_dtype(dtype, np.ndarray))
    if not homogeneous:
        return coords
    return np.concatenate([coords, np.ones_like(coords[:1])])


def _create_grid_torch(
    spatial_size: Sequence[int],
    spacing: Sequence[float] | None = None,
    homogeneous: bool = True,
    dtype=torch.float32,
    device: torch.device | None = None,
):
    """
    compute a `spatial_size` mesh with the torch API.
    """
    spacing = spacing or tuple(1.0 for _ in spatial_size)
    ranges = [
        torch.linspace(
            -(d - 1.0) / 2.0 * s,
            (d - 1.0) / 2.0 * s,
            int(d),
            device=device,
            dtype=get_equivalent_dtype(dtype, torch.Tensor),
        )
        for d, s in zip(spatial_size, spacing)
    ]
    coords = meshgrid_ij(*ranges)
    if not homogeneous:
        return torch.stack(coords)
    return torch.stack([*coords, torch.ones_like(coords[0])])


def create_control_grid(
    spatial_shape: Sequence[int],
    spacing: Sequence[float],
    homogeneous: bool = True,
    dtype: DtypeLike = float,
    device: torch.device | None = None,
    backend=TransformBackends.NUMPY,
):
    """
    control grid with two additional point in each direction
    """
    torch_backend = look_up_option(backend, TransformBackends) == TransformBackends.TORCH
    ceil_func: Callable = torch.ceil if torch_backend else np.ceil  # type: ignore
    grid_shape = []
    for d, s in zip(spatial_shape, spacing):
        d = torch.as_tensor(d, device=device) if torch_backend else int(d)  # type: ignore
        if d % 2 == 0:
            grid_shape.append(ceil_func((d - 1.0) / (2.0 * s) + 0.5) * 2.0 + 2.0)
        else:
            grid_shape.append(ceil_func((d - 1.0) / (2.0 * s)) * 2.0 + 3.0)
    return create_grid(
        spatial_size=grid_shape, spacing=spacing, homogeneous=homogeneous, dtype=dtype, device=device, backend=backend
    )


def create_rotate(
    spatial_dims: int,
    radians: Sequence[float] | float,
    device: torch.device | None = None,
    backend: str = TransformBackends.NUMPY,
) -> NdarrayOrTensor:
    """
    create a 2D or 3D rotation matrix

    Args:
        spatial_dims: {``2``, ``3``} spatial rank
        radians: rotation radians
            when spatial_dims == 3, the `radians` sequence corresponds to
            rotation in the 1st, 2nd, and 3rd dim respectively.
        device: device to compute and store the output (when the backend is "torch").
        backend: APIs to use, ``numpy`` or ``torch``.

    Raises:
        ValueError: When ``radians`` is empty.
        ValueError: When ``spatial_dims`` is not one of [2, 3].

    """
    _backend = look_up_option(backend, TransformBackends)
    if _backend == TransformBackends.NUMPY:
        return _create_rotate(
            spatial_dims=spatial_dims, radians=radians, sin_func=np.sin, cos_func=np.cos, eye_func=np.eye
        )
    if _backend == TransformBackends.TORCH:
        return _create_rotate(
            spatial_dims=spatial_dims,
            radians=radians,
            sin_func=lambda th: torch.sin(torch.as_tensor(th, dtype=torch.float32, device=device)),
            cos_func=lambda th: torch.cos(torch.as_tensor(th, dtype=torch.float32, device=device)),
            eye_func=lambda rank: torch.eye(rank, device=device),
        )
    raise ValueError(f"backend {backend} is not supported")


def _create_rotate(
    spatial_dims: int,
    radians: Sequence[float] | float,
    sin_func: Callable = np.sin,
    cos_func: Callable = np.cos,
    eye_func: Callable = np.eye,
) -> NdarrayOrTensor:
    radians = ensure_tuple(radians)
    if spatial_dims == 2:
        if len(radians) >= 1:
            sin_, cos_ = sin_func(radians[0]), cos_func(radians[0])
            out = eye_func(3)
            out[0, 0], out[0, 1] = cos_, -sin_
            out[1, 0], out[1, 1] = sin_, cos_
            return out  # type: ignore
        raise ValueError("radians must be non empty.")

    if spatial_dims == 3:
        affine = None
        if len(radians) >= 1:
            sin_, cos_ = sin_func(radians[0]), cos_func(radians[0])
            affine = eye_func(4)
            affine[1, 1], affine[1, 2] = cos_, -sin_
            affine[2, 1], affine[2, 2] = sin_, cos_
        if len(radians) >= 2:
            sin_, cos_ = sin_func(radians[1]), cos_func(radians[1])
            if affine is None:
                raise ValueError("Affine should be a matrix.")
            _affine = eye_func(4)
            _affine[0, 0], _affine[0, 2] = cos_, sin_
            _affine[2, 0], _affine[2, 2] = -sin_, cos_
            affine = affine @ _affine
        if len(radians) >= 3:
            sin_, cos_ = sin_func(radians[2]), cos_func(radians[2])
            if affine is None:
                raise ValueError("Affine should be a matrix.")
            _affine = eye_func(4)
            _affine[0, 0], _affine[0, 1] = cos_, -sin_
            _affine[1, 0], _affine[1, 1] = sin_, cos_
            affine = affine @ _affine
        if affine is None:
            raise ValueError("radians must be non empty.")
        return affine  # type: ignore

    raise ValueError(f"Unsupported spatial_dims: {spatial_dims}, available options are [2, 3].")


def create_shear(
    spatial_dims: int,
    coefs: Sequence[float] | float,
    device: torch.device | None = None,
    backend=TransformBackends.NUMPY,
) -> NdarrayOrTensor:
    """
    create a shearing matrix

    Args:
        spatial_dims: spatial rank
        coefs: shearing factors, a tuple of 2 floats for 2D, a tuple of 6 floats for 3D),
            take a 3D affine as example::

                [
                    [1.0, coefs[0], coefs[1], 0.0],
                    [coefs[2], 1.0, coefs[3], 0.0],
                    [coefs[4], coefs[5], 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]

        device: device to compute and store the output (when the backend is "torch").
        backend: APIs to use, ``numpy`` or ``torch``.

    Raises:
        NotImplementedError: When ``spatial_dims`` is not one of [2, 3].

    """
    _backend = look_up_option(backend, TransformBackends)
    if _backend == TransformBackends.NUMPY:
        return _create_shear(spatial_dims=spatial_dims, coefs=coefs, eye_func=np.eye)
    if _backend == TransformBackends.TORCH:
        return _create_shear(
            spatial_dims=spatial_dims, coefs=coefs, eye_func=lambda rank: torch.eye(rank, device=device)
        )
    raise ValueError(f"backend {backend} is not supported")


def _create_shear(spatial_dims: int, coefs: Sequence[float] | float, eye_func=np.eye) -> NdarrayOrTensor:
    if spatial_dims == 2:
        coefs = ensure_tuple_size(coefs, dim=2, pad_val=0.0)
        out = eye_func(3)
        out[0, 1], out[1, 0] = coefs[0], coefs[1]
        return out  # type: ignore
    if spatial_dims == 3:
        coefs = ensure_tuple_size(coefs, dim=6, pad_val=0.0)
        out = eye_func(4)
        out[0, 1], out[0, 2] = coefs[0], coefs[1]
        out[1, 0], out[1, 2] = coefs[2], coefs[3]
        out[2, 0], out[2, 1] = coefs[4], coefs[5]
        return out  # type: ignore
    raise NotImplementedError("Currently only spatial_dims in [2, 3] are supported.")


def create_scale(
    spatial_dims: int,
    scaling_factor: Sequence[float] | float,
    device: torch.device | str | None = None,
    backend=TransformBackends.NUMPY,
) -> NdarrayOrTensor:
    """
    create a scaling matrix

    Args:
        spatial_dims: spatial rank
        scaling_factor: scaling factors for every spatial dim, defaults to 1.
        device: device to compute and store the output (when the backend is "torch").
        backend: APIs to use, ``numpy`` or ``torch``.
    """
    _backend = look_up_option(backend, TransformBackends)
    if _backend == TransformBackends.NUMPY:
        return _create_scale(spatial_dims=spatial_dims, scaling_factor=scaling_factor, array_func=np.diag)
    if _backend == TransformBackends.TORCH:
        return _create_scale(
            spatial_dims=spatial_dims,
            scaling_factor=scaling_factor,
            array_func=lambda x: torch.diag(torch.as_tensor(x, device=device)),
        )
    raise ValueError(f"backend {backend} is not supported")


def _create_scale(spatial_dims: int, scaling_factor: Sequence[float] | float, array_func=np.diag) -> NdarrayOrTensor:
    scaling_factor = ensure_tuple_size(scaling_factor, dim=spatial_dims, pad_val=1.0)
    return array_func(scaling_factor[:spatial_dims] + (1.0,))  # type: ignore


def create_translate(
    spatial_dims: int,
    shift: Sequence[float] | float,
    device: torch.device | None = None,
    backend=TransformBackends.NUMPY,
) -> NdarrayOrTensor:
    """
    create a translation matrix

    Args:
        spatial_dims: spatial rank
        shift: translate pixel/voxel for every spatial dim, defaults to 0.
        device: device to compute and store the output (when the backend is "torch").
        backend: APIs to use, ``numpy`` or ``torch``.
    """
    _backend = look_up_option(backend, TransformBackends)
    spatial_dims = int(spatial_dims)
    if _backend == TransformBackends.NUMPY:
        return _create_translate(spatial_dims=spatial_dims, shift=shift, eye_func=np.eye, array_func=np.asarray)
    if _backend == TransformBackends.TORCH:
        return _create_translate(
            spatial_dims=spatial_dims,
            shift=shift,
            eye_func=lambda x: torch.eye(torch.as_tensor(x), device=device),  # type: ignore
            array_func=lambda x: torch.as_tensor(x, device=device),
        )
    raise ValueError(f"backend {backend} is not supported")


def _create_translate(
    spatial_dims: int, shift: Sequence[float] | float, eye_func=np.eye, array_func=np.asarray
) -> NdarrayOrTensor:
    shift = ensure_tuple(shift)
    affine = eye_func(spatial_dims + 1)
    for i, a in enumerate(shift[:spatial_dims]):
        affine[i, spatial_dims] = a
    return array_func(affine)  # type: ignore


def generate_spatial_bounding_box(
    img: NdarrayOrTensor,
    select_fn: Callable = is_positive,
    channel_indices: IndexSelection | None = None,
    margin: Sequence[int] | int = 0,
    allow_smaller: bool = True,
) -> tuple[list[int], list[int]]:
    """
    Generate the spatial bounding box of foreground in the image with start-end positions (inclusive).
    Users can define arbitrary function to select expected foreground from the whole image or specified channels.
    And it can also add margin to every dim of the bounding box.
    The output format of the coordinates is:

        [1st_spatial_dim_start, 2nd_spatial_dim_start, ..., Nth_spatial_dim_start],
        [1st_spatial_dim_end, 2nd_spatial_dim_end, ..., Nth_spatial_dim_end]

    If `allow_smaller`, the bounding boxes edges are aligned with the input image edges.
    This function returns [0, 0, ...], [0, 0, ...] if there's no positive intensity.

    Args:
        img: a "channel-first" image of shape (C, spatial_dim1[, spatial_dim2, ...]) to generate bounding box from.
        select_fn: function to select expected foreground, default is to select values > 0.
        channel_indices: if defined, select foreground only on the specified channels
            of image. if None, select foreground on the whole image.
        margin: add margin value to spatial dims of the bounding box, if only 1 value provided, use it for all dims.
        allow_smaller: when computing box size with `margin`, whether allow the image size to be smaller
            than box size, default to `True`.
    """
    check_non_lazy_pending_ops(img, name="generate_spatial_bounding_box")
    spatial_size = img.shape[1:]
    data = img[list(ensure_tuple(channel_indices))] if channel_indices is not None else img
    data = select_fn(data).any(0)
    ndim = len(data.shape)
    margin = ensure_tuple_rep(margin, ndim)
    for m in margin:
        if m < 0:
            raise ValueError(f"margin value should not be negative number, got {margin}.")

    box_start = [0] * ndim
    box_end = [0] * ndim

    for di, ax in enumerate(itertools.combinations(reversed(range(ndim)), ndim - 1)):
        dt = data
        if len(ax) != 0:
            dt = any_np_pt(dt, ax)

        if not dt.any():
            # if no foreground, return all zero bounding box coords
            return [0] * ndim, [0] * ndim

        arg_max = where(dt == dt.max())[0]
        min_d = arg_max[0] - margin[di]
        max_d = arg_max[-1] + margin[di] + 1
        if allow_smaller:
            min_d = max(min_d, 0)
            max_d = min(max_d, spatial_size[di])

        box_start[di] = min_d.detach().cpu().item() if isinstance(min_d, torch.Tensor) else min_d
        box_end[di] = max_d.detach().cpu().item() if isinstance(max_d, torch.Tensor) else max_d

    return box_start, box_end


def get_largest_connected_component_mask(
    img: NdarrayTensor, connectivity: int | None = None, num_components: int = 1
) -> NdarrayTensor:
    """
    Gets the largest connected component mask of an image.

    Args:
        img: Image to get largest connected component from. Shape is (spatial_dim1 [, spatial_dim2, ...])
        connectivity: Maximum number of orthogonal hops to consider a pixel/voxel as a neighbor.
            Accepted values are ranging from  1 to input.ndim. If ``None``, a full
            connectivity of ``input.ndim`` is used. for more details:
            https://scikit-image.org/docs/dev/api/skimage.measure.html#skimage.measure.label.
        num_components: The number of largest components to preserve.
    """
    # use skimage/cucim.skimage and np/cp depending on whether packages are
    # available and input is non-cpu torch.tensor
    cucim, has_cucim = optional_import("cucim")
    use_cp = has_cp and has_cucim and isinstance(img, torch.Tensor) and img.device != torch.device("cpu")
    if use_cp:
        img_ = convert_to_cupy(img.short())  # type: ignore
        label = cucim.skimage.measure.label
        lib = cp
    else:
        if not has_measure:
            raise RuntimeError("Skimage.measure required.")
        img_, *_ = convert_data_type(img, np.ndarray)
        label = measure.label
        lib = np

    # features will be an image -- 0 for background and then each different
    # feature will have its own index.
    features, num_features = label(img_, connectivity=connectivity, return_num=True)
    # if num features less than max desired, nothing to do.
    if num_features <= num_components:
        out = img_.astype(bool)
    else:
        # ignore background
        nonzeros = features[lib.nonzero(features)]
        # get number voxels per feature (bincount). argsort[::-1] to get indices
        # of largest components.
        features_to_keep = lib.argsort(lib.bincount(nonzeros))[::-1]
        # only keep the first n non-background indices
        features_to_keep = features_to_keep[:num_components]
        # generate labelfield. True if in list of features to keep
        out = lib.isin(features, features_to_keep)

    return convert_to_dst_type(out, dst=img, dtype=out.dtype)[0]


def remove_small_objects(
    img: NdarrayTensor, min_size: int = 64, connectivity: int = 1, independent_channels: bool = True
) -> NdarrayTensor:
    """
    Use `skimage.morphology.remove_small_objects` to remove small objects from images.
    See: https://scikit-image.org/docs/dev/api/skimage.morphology.html#remove-small-objects.

    Data should be one-hotted.

    Args:
        img: image to process. Expected shape: C, H,W,[D]. Expected to only have singleton channel dimension,
            i.e., not be one-hotted. Converted to type int.
        min_size: objects smaller than this size are removed.
        connectivity: Maximum number of orthogonal hops to consider a pixel/voxel as a neighbor.
            Accepted values are ranging from  1 to input.ndim. If ``None``, a full
            connectivity of ``input.ndim`` is used. For more details refer to linked scikit-image
            documentation.
        independent_channels: Whether to consider each channel independently.
    """
    # if all equal to one value, no need to call skimage
    if len(unique(img)) == 1:
        return img

    if not has_morphology:
        raise RuntimeError("Skimage required.")

    img_np: np.ndarray
    img_np, *_ = convert_data_type(img, np.ndarray)

    # morphology.remove_small_objects assumes them to be independent by default
    # else, convert to foreground vs background, remove small objects, then convert
    # back by multiplying the output by the input
    if not independent_channels:
        img_np = img_np > 0
    else:
        # if binary, convert to boolean, else int
        img_np = img_np.astype(bool if img_np.max() <= 1 else np.int32)

    out_np = morphology.remove_small_objects(img_np, min_size, connectivity)
    out, *_ = convert_to_dst_type(out_np, img)

    # convert back by multiplying
    if not independent_channels:
        out = img * out  # type: ignore
    return out


def get_unique_labels(img: NdarrayOrTensor, is_onehot: bool, discard: int | Iterable[int] | None = None) -> set[int]:
    """Get list of non-background labels in an image.

    Args:
        img: Image to be processed. Shape should be [C, W, H, [D]] with C=1 if not onehot else `num_classes`.
        is_onehot: Boolean as to whether input image is one-hotted. If one-hotted, only return channels with
        discard: Can be used to remove labels (e.g., background). Can be any value, sequence of values, or
            `None` (nothing is discarded).

    Returns:
        Set of labels
    """
    applied_labels: set[int]
    n_channels = img.shape[0]
    if is_onehot:
        applied_labels = {i for i, s in enumerate(img) if s.sum() > 0}
    else:
        if n_channels != 1:
            raise ValueError(f"If input not one-hotted, should only be 1 channel, got {n_channels}.")
        applied_labels = set(unique(img).tolist())
    if discard is not None:
        for i in ensure_tuple(discard):
            applied_labels.discard(i)
    return applied_labels


def fill_holes(
    img_arr: np.ndarray, applied_labels: Iterable[int] | None = None, connectivity: int | None = None
) -> np.ndarray:
    """
    Fill the holes in the provided image.

    The label 0 will be treated as background and the enclosed holes will be set to the neighboring class label.
    What is considered to be an enclosed hole is defined by the connectivity.
    Holes on the edge are always considered to be open (not enclosed).

    Note:

        The performance of this method heavily depends on the number of labels.
        It is a bit faster if the list of `applied_labels` is provided.
        Limiting the number of `applied_labels` results in a big decrease in processing time.

        If the image is one-hot-encoded, then the `applied_labels` need to match the channel index.

    Args:
        img_arr: numpy array of shape [C, spatial_dim1[, spatial_dim2, ...]].
        applied_labels: Labels for which to fill holes. Defaults to None,
            that is filling holes for all labels.
        connectivity: Maximum number of orthogonal hops to
            consider a pixel/voxel as a neighbor. Accepted values are ranging from  1 to input.ndim.
            Defaults to a full connectivity of ``input.ndim``.

    Returns:
        numpy array of shape [C, spatial_dim1[, spatial_dim2, ...]].
    """
    channel_axis = 0
    num_channels = img_arr.shape[channel_axis]
    is_one_hot = num_channels > 1
    spatial_dims = img_arr.ndim - 1
    structure = ndimage.generate_binary_structure(spatial_dims, connectivity or spatial_dims)

    # Get labels if not provided. Exclude background label.
    applied_labels = set(applied_labels) if applied_labels is not None else get_unique_labels(img_arr, is_one_hot)
    background_label = 0
    applied_labels.discard(background_label)

    for label in applied_labels:
        tmp = np.zeros(img_arr.shape[1:], dtype=bool)
        ndimage.binary_dilation(
            tmp,
            structure=structure,
            iterations=-1,
            mask=np.logical_not(img_arr[label]) if is_one_hot else img_arr[0] != label,
            origin=0,
            border_value=1,
            output=tmp,
        )
        if is_one_hot:
            img_arr[label] = np.logical_not(tmp)
        else:
            img_arr[0, np.logical_not(tmp)] = label

    return img_arr


def get_extreme_points(
    img: NdarrayOrTensor, rand_state: np.random.RandomState | None = None, background: int = 0, pert: float = 0.0
) -> list[tuple[int, ...]]:
    """
    Generate extreme points from an image. These are used to generate initial segmentation
    for annotation models. An optional perturbation can be passed to simulate user clicks.

    Args:
        img:
            Image to generate extreme points from. Expected Shape is ``(spatial_dim1, [, spatial_dim2, ...])``.
        rand_state: `np.random.RandomState` object used to select random indices.
        background: Value to be consider as background, defaults to 0.
        pert: Random perturbation amount to add to the points, defaults to 0.0.

    Returns:
        A list of extreme points, its length is equal to 2 * spatial dimension of input image.
        The output format of the coordinates is:

        [1st_spatial_dim_min, 1st_spatial_dim_max, 2nd_spatial_dim_min, ..., Nth_spatial_dim_max]

    Raises:
        ValueError: When the input image does not have any foreground pixel.
    """
    check_non_lazy_pending_ops(img, name="get_extreme_points")
    if rand_state is None:
        rand_state = np.random.random.__self__  # type: ignore
    indices = where(img != background)
    if np.size(indices[0]) == 0:
        raise ValueError("get_extreme_points: no foreground object in mask!")

    def _get_point(val, dim):
        """
        Select one of the indices within slice containing val.

        Args:
            val : value for comparison
            dim : dimension in which to look for value
        """
        idx = where(indices[dim] == val)[0]
        idx = idx.cpu() if isinstance(idx, torch.Tensor) else idx
        idx = rand_state.choice(idx) if rand_state is not None else idx
        pt = []
        for j in range(img.ndim):
            # add +- pert to each dimension
            val = int(indices[j][idx] + 2.0 * pert * (rand_state.rand() if rand_state is not None else 0.5 - 0.5))
            val = max(val, 0)
            val = min(val, img.shape[j] - 1)
            pt.append(val)
        return pt

    points = []
    for i in range(img.ndim):
        points.append(tuple(_get_point(indices[i].min(), i)))
        points.append(tuple(_get_point(indices[i].max(), i)))

    return points


def extreme_points_to_image(
    points: list[tuple[int, ...]],
    label: NdarrayOrTensor,
    sigma: Sequence[float] | float | Sequence[torch.Tensor] | torch.Tensor = 0.0,
    rescale_min: float = -1.0,
    rescale_max: float = 1.0,
) -> torch.Tensor:
    """
    Please refer to :py:class:`monai.transforms.AddExtremePointsChannel` for the usage.

    Applies a gaussian filter to the extreme points image. Then the pixel values in points image are rescaled
    to range [rescale_min, rescale_max].

    Args:
        points: Extreme points of the object/organ.
        label: label image to get extreme points from. Shape must be
            (1, spatial_dim1, [, spatial_dim2, ...]). Doesn't support one-hot labels.
        sigma: if a list of values, must match the count of spatial dimensions of input data,
            and apply every value in the list to 1 spatial dimension. if only 1 value provided,
            use it for all spatial dimensions.
        rescale_min: minimum value of output data.
        rescale_max: maximum value of output data.
    """
    # points to image
    # points_image = torch.zeros(label.shape[1:], dtype=torch.float)
    points_image = torch.zeros_like(torch.as_tensor(label[0]), dtype=torch.float)
    for p in points:
        points_image[p] = 1.0

    if isinstance(sigma, Sequence):
        sigma = [torch.as_tensor(s, device=points_image.device) for s in sigma]
    else:
        sigma = torch.as_tensor(sigma, device=points_image.device)

    # add channel and add batch
    points_image = points_image.unsqueeze(0).unsqueeze(0)
    gaussian_filter = GaussianFilter(label.ndim - 1, sigma=sigma)
    points_image = gaussian_filter(points_image).squeeze(0).detach()

    # rescale the points image to [rescale_min, rescale_max]
    min_intensity = points_image.min()
    max_intensity = points_image.max()
    points_image = (points_image - min_intensity) / (max_intensity - min_intensity)
    return points_image * (rescale_max - rescale_min) + rescale_min


def map_spatial_axes(
    img_ndim: int, spatial_axes: Sequence[int] | int | None = None, channel_first: bool = True
) -> list[int]:
    """
    Utility to map the spatial axes to real axes in channel first/last shape.
    For example:
    If `channel_first` is True, and `img` has 3 spatial dims, map spatial axes to real axes as below:
    None -> [1, 2, 3]
    [0, 1] -> [1, 2]
    [0, -1] -> [1, -1]
    If `channel_first` is False, and `img` has 3 spatial dims, map spatial axes to real axes as below:
    None -> [0, 1, 2]
    [0, 1] -> [0, 1]
    [0, -1] -> [0, -2]

    Args:
        img_ndim: dimension number of the target image.
        spatial_axes: spatial axes to be converted, default is None.
            The default `None` will convert to all the spatial axes of the image.
            If axis is negative it counts from the last to the first axis.
            If axis is a tuple of ints.
        channel_first: the image data is channel first or channel last, default to channel first.

    """
    if spatial_axes is None:
        return list(range(1, img_ndim) if channel_first else range(img_ndim - 1))
    spatial_axes_ = []
    for a in ensure_tuple(spatial_axes):
        if channel_first:
            spatial_axes_.append(a % img_ndim if a < 0 else a + 1)
        else:
            spatial_axes_.append((a - 1) % (img_ndim - 1) if a < 0 else a)
    return spatial_axes_


@contextmanager
def allow_missing_keys_mode(transform: MapTransform | Compose | tuple[MapTransform] | tuple[Compose]):
    """Temporarily set all MapTransforms to not throw an error if keys are missing. After, revert to original states.

    Args:
        transform: either MapTransform or a Compose

    Example:

    .. code-block:: python

        data = {"image": np.arange(16, dtype=float).reshape(1, 4, 4)}
        t = SpatialPadd(["image", "label"], 10, allow_missing_keys=False)
        _ = t(data)  # would raise exception
        with allow_missing_keys_mode(t):
            _ = t(data)  # OK!
    """
    # If given a sequence of transforms, Compose them to get a single list
    if issequenceiterable(transform):
        transform = Compose(transform)

    # Get list of MapTransforms
    transforms = []
    if isinstance(transform, MapTransform):
        transforms = [transform]
    elif isinstance(transform, Compose):
        # Only keep contained MapTransforms
        transforms = [t for t in transform.flatten().transforms if isinstance(t, MapTransform)]
    if len(transforms) == 0:
        raise TypeError(
            "allow_missing_keys_mode expects either MapTransform(s) or Compose(s) containing MapTransform(s)"
        )

    # Get the state of each `allow_missing_keys`
    orig_states = [t.allow_missing_keys for t in transforms]

    try:
        # Set all to True
        for t in transforms:
            t.allow_missing_keys = True
        yield
    finally:
        # Revert
        for t, o_s in zip(transforms, orig_states):
            t.allow_missing_keys = o_s


_interp_modes = list(InterpolateMode) + list(GridSampleMode)


def convert_applied_interp_mode(trans_info, mode: str = "nearest", align_corners: bool | None = None):
    """
    Recursively change the interpolation mode in the applied operation stacks, default to "nearest".

    See also: :py:class:`monai.transform.inverse.InvertibleTransform`

    Args:
        trans_info: applied operation stack, tracking the previously applied invertible transform.
        mode: target interpolation mode to convert, default to "nearest" as it's usually used to save the mode output.
        align_corners: target align corner value in PyTorch interpolation API, need to align with the `mode`.

    """
    if isinstance(trans_info, (list, tuple)):
        return [convert_applied_interp_mode(x, mode=mode, align_corners=align_corners) for x in trans_info]
    if not isinstance(trans_info, Mapping):
        return trans_info
    trans_info = dict(trans_info)
    if "mode" in trans_info:
        current_mode = trans_info["mode"]
        if current_mode[0] in _interp_modes:
            trans_info["mode"] = [mode for _ in range(len(mode))]
        elif current_mode in _interp_modes:
            trans_info["mode"] = mode
    if "align_corners" in trans_info:
        _align_corners = TraceKeys.NONE if align_corners is None else align_corners
        current_value = trans_info["align_corners"]
        trans_info["align_corners"] = (
            [_align_corners for _ in mode] if issequenceiterable(current_value) else _align_corners
        )
    if ("mode" not in trans_info) and ("align_corners" not in trans_info):
        return {
            k: convert_applied_interp_mode(trans_info[k], mode=mode, align_corners=align_corners) for k in trans_info
        }
    return trans_info


def reset_ops_id(data):
    """find MetaTensors in list or dict `data` and (in-place) set ``TraceKeys.ID`` to ``Tracekys.NONE``."""
    if isinstance(data, (list, tuple)):
        return [reset_ops_id(d) for d in data]
    if isinstance(data, monai.data.MetaTensor):
        data.applied_operations = reset_ops_id(data.applied_operations)
        return data
    if not isinstance(data, Mapping):
        return data
    data = dict(data)
    if TraceKeys.ID in data:
        data[TraceKeys.ID] = TraceKeys.NONE
    return {k: reset_ops_id(v) for k, v in data.items()}


def compute_divisible_spatial_size(spatial_shape: Sequence[int], k: Sequence[int] | int):
    """
    Compute the target spatial size which should be divisible by `k`.

    Args:
        spatial_shape: original spatial shape.
        k: the target k for each spatial dimension.
            if `k` is negative or 0, the original size is preserved.
            if `k` is an int, the same `k` be applied to all the input spatial dimensions.

    """
    k = fall_back_tuple(k, (1,) * len(spatial_shape))
    new_size = []
    for k_d, dim in zip(k, spatial_shape):
        new_dim = int(np.ceil(dim / k_d) * k_d) if k_d > 0 else dim
        new_size.append(new_dim)

    return tuple(new_size)


def equalize_hist(
    img: np.ndarray, mask: np.ndarray | None = None, num_bins: int = 256, min: int = 0, max: int = 255
) -> np.ndarray:
    """
    Utility to equalize input image based on the histogram.
    If `skimage` installed, will leverage `skimage.exposure.histogram`, otherwise, use
    `np.histogram` instead.

    Args:
        img: input image to equalize.
        mask: if provided, must be ndarray of bools or 0s and 1s, and same shape as `image`.
            only points at which `mask==True` are used for the equalization.
        num_bins: number of the bins to use in histogram, default to `256`. for more details:
            https://numpy.org/doc/stable/reference/generated/numpy.histogram.html.
        min: the min value to normalize input image, default to `0`.
        max: the max value to normalize input image, default to `255`.

    """

    orig_shape = img.shape
    hist_img = img[np.array(mask, dtype=bool)] if mask is not None else img
    if has_skimage:
        hist, bins = exposure.histogram(hist_img.flatten(), num_bins)
    else:
        hist, bins = np.histogram(hist_img.flatten(), num_bins)
        bins = (bins[:-1] + bins[1:]) / 2

    cum = hist.cumsum()
    # normalize the cumulative result
    cum = rescale_array(arr=cum, minv=min, maxv=max)

    # apply linear interpolation
    img = np.interp(img.flatten(), bins, cum)
    return img.reshape(orig_shape)


class Fourier:
    """
    Helper class storing Fourier mappings
    """

    @staticmethod
    def shift_fourier(x: NdarrayOrTensor, spatial_dims: int) -> NdarrayOrTensor:
        """
        Applies fourier transform and shifts the zero-frequency component to the
        center of the spectrum. Only the spatial dimensions get transformed.

        Args:
            x: Image to transform.
            spatial_dims: Number of spatial dimensions.

        Returns
            k: K-space data.
        """
        dims = tuple(range(-spatial_dims, 0))
        k: NdarrayOrTensor
        if isinstance(x, torch.Tensor):
            if hasattr(torch.fft, "fftshift"):  # `fftshift` is new in torch 1.8.0
                k = torch.fft.fftshift(torch.fft.fftn(x, dim=dims), dim=dims)
            else:
                # if using old PyTorch, will convert to numpy array and return
                k = np.fft.fftshift(np.fft.fftn(x.cpu().numpy(), axes=dims), axes=dims)
        else:
            k = np.fft.fftshift(np.fft.fftn(x, axes=dims), axes=dims)
        return k

    @staticmethod
    def inv_shift_fourier(k: NdarrayOrTensor, spatial_dims: int, n_dims: int | None = None) -> NdarrayOrTensor:
        """
        Applies inverse shift and fourier transform. Only the spatial
        dimensions are transformed.

        Args:
            k: K-space data.
            spatial_dims: Number of spatial dimensions.

        Returns:
            x: Tensor in image space.
        """
        dims = tuple(range(-spatial_dims, 0))
        out: NdarrayOrTensor
        if isinstance(k, torch.Tensor):
            if hasattr(torch.fft, "ifftshift"):  # `ifftshift` is new in torch 1.8.0
                out = torch.fft.ifftn(torch.fft.ifftshift(k, dim=dims), dim=dims, norm="backward").real
            else:
                # if using old PyTorch, will convert to numpy array and return
                out = np.fft.ifftn(np.fft.ifftshift(k.cpu().numpy(), axes=dims), axes=dims).real
        else:
            out = np.fft.ifftn(np.fft.ifftshift(k, axes=dims), axes=dims).real
        return out


def get_number_image_type_conversions(transform: Compose, test_data: Any, key: Hashable | None = None) -> int:
    """
    Get the number of times that the data need to be converted (e.g., numpy to torch).
    Conversions between different devices are also counted (e.g., CPU to GPU).

    Args:
        transform: composed transforms to be tested
        test_data: data to be used to count the number of conversions
        key: if using dictionary transforms, this key will be used to check the number of conversions.
    """

    def _get_data(obj, key):
        return obj if key is None else obj[key]

    # if the starting point is a string (e.g., input to LoadImage), start
    # at -1 since we don't want to count the string -> image conversion.
    num_conversions = 0 if not isinstance(_get_data(test_data, key), str) else -1

    tr = transform.flatten().transforms

    if isinstance(transform, OneOf) or any(isinstance(i, OneOf) for i in tr):
        raise RuntimeError("Not compatible with `OneOf`, as the applied transform is deterministically chosen.")

    for _transform in tr:
        prev_data = _get_data(test_data, key)
        prev_type = type(prev_data)
        prev_device = prev_data.device if isinstance(prev_data, torch.Tensor) else None
        test_data = apply_transform(_transform, test_data, transform.map_items, transform.unpack_items)
        # every time the type or device changes, increment the counter
        curr_data = _get_data(test_data, key)
        curr_device = curr_data.device if isinstance(curr_data, torch.Tensor) else None
        if not isinstance(curr_data, prev_type) or curr_device != prev_device:
            num_conversions += 1
    return num_conversions


def get_transform_backends():
    """Get the backends of all MONAI transforms.

    Returns:
        Dictionary, where each key is a transform, and its
        corresponding values are a boolean list, stating
        whether that transform supports (1) `torch.Tensor`,
        and (2) `np.ndarray` as input without needing to
        convert.
    """
    backends = {}
    unique_transforms = []
    for n, obj in getmembers(monai.transforms):
        # skip aliases
        if obj in unique_transforms:
            continue
        unique_transforms.append(obj)

        if (
            isclass(obj)
            and issubclass(obj, Transform)
            and n
            not in [
                "BatchInverseTransform",
                "Compose",
                "CuCIM",
                "CuCIMD",
                "Decollated",
                "InvertD",
                "InvertibleTransform",
                "Lambda",
                "LambdaD",
                "MapTransform",
                "OneOf",
                "RandCuCIM",
                "RandCuCIMD",
                "RandomOrder",
                "PadListDataCollate",
                "RandLambda",
                "RandLambdaD",
                "RandTorchVisionD",
                "RandomizableTransform",
                "TorchVisionD",
                "Transform",
            ]
        ):
            backends[n] = [TransformBackends.TORCH in obj.backend, TransformBackends.NUMPY in obj.backend]
    return backends


def print_transform_backends():
    """Prints a list of backends of all MONAI transforms."""

    class Colors:
        none = ""
        red = "91"
        green = "92"
        yellow = "93"

    def print_color(t, color):
        print(f"\033[{color}m{t}\033[00m")

    def print_table_column(name, torch, numpy, color=Colors.none):
        print_color(f"{name:<50} {torch:<8} {numpy:<8}", color)

    backends = get_transform_backends()
    n_total = len(backends)
    n_t_or_np, n_t, n_np, n_uncategorized = 0, 0, 0, 0
    print_table_column("Transform", "Torch?", "Numpy?")
    for k, v in backends.items():
        if all(v):
            color = Colors.green
            n_t_or_np += 1
        elif v[0]:
            color = Colors.green
            n_t += 1
        elif v[1]:
            color = Colors.yellow
            n_np += 1
        else:
            color = Colors.red
            n_uncategorized += 1
        print_table_column(k, v[0], v[1], color=color)

    print("Total number of transforms:", n_total)
    print_color(f"Number transforms allowing both torch and numpy: {n_t_or_np}", Colors.green)
    print_color(f"Number of TorchTransform: {n_t}", Colors.green)
    print_color(f"Number of NumpyTransform: {n_np}", Colors.yellow)
    print_color(f"Number of uncategorised: {n_uncategorized}", Colors.red)


def convert_pad_mode(dst: NdarrayOrTensor, mode: str | None):
    """
    Utility to convert padding mode between numpy array and PyTorch Tensor.

    Args:
        dst: target data to convert padding mode for, should be numpy array or PyTorch Tensor.
        mode: current padding mode.

    """
    if isinstance(dst, torch.Tensor):
        if mode == "wrap":
            mode = "circular"
        elif mode == "edge":
            mode = "replicate"
        return look_up_option(mode, PytorchPadMode)
    if isinstance(dst, np.ndarray):
        if mode == "circular":
            mode = "wrap"
        elif mode == "replicate":
            mode = "edge"
        return look_up_option(mode, NumpyPadMode)
    raise ValueError(f"unsupported data type: {type(dst)}.")


def convert_to_contiguous(
    data: NdarrayOrTensor | str | bytes | Mapping | Sequence[Any], **kwargs
) -> NdarrayOrTensor | Mapping | Sequence[Any]:
    """
    Check and ensure the numpy array or PyTorch Tensor in data to be contiguous in memory.

    Args:
        data: input data to convert, will recursively convert the numpy array or PyTorch Tensor in dict and sequence.
        kwargs: if `x` is PyTorch Tensor, additional args for `torch.contiguous`, more details:
            https://pytorch.org/docs/stable/generated/torch.Tensor.contiguous.html#torch.Tensor.contiguous.

    """
    if isinstance(data, (np.ndarray, torch.Tensor, str, bytes)):
        return ascontiguousarray(data, **kwargs)
    elif isinstance(data, Mapping):
        return {k: convert_to_contiguous(v, **kwargs) for k, v in data.items()}
    elif isinstance(data, Sequence) and not isinstance(data, bytes):
        return [convert_to_contiguous(i, **kwargs) for i in data]  # type: ignore
    else:
        return data


def scale_affine(spatial_size, new_spatial_size, centered: bool = True):
    """
    Compute the scaling matrix according to the new spatial size

    Args:
        spatial_size: original spatial size.
        new_spatial_size: new spatial size.
        centered: whether the scaling is with respect to the image center (True, default) or corner (False).

    Returns:
        the scaling matrix.

    """
    r = max(len(new_spatial_size), len(spatial_size))
    if spatial_size == new_spatial_size:
        return np.eye(r + 1)
    s = np.array([float(o) / float(max(n, 1)) for o, n in zip(spatial_size, new_spatial_size)], dtype=float)
    scale = create_scale(r, s.tolist())
    if centered:
        scale[:r, -1] = (np.diag(scale)[:r] - 1) / 2.0  # type: ignore
    return scale


def attach_hook(func, hook, mode="pre"):
    """
    Adds `hook` before or after a `func` call. If mode is "pre", the wrapper will call hook then func.
    If the mode is "post", the wrapper will call func then hook.
    """
    supported = {"pre", "post"}
    if look_up_option(mode, supported) == "pre":
        _hook, _func = hook, func
    else:
        _hook, _func = func, hook

    @wraps(func)
    def wrapper(inst, data):
        data = _hook(inst, data)
        return _func(inst, data)

    return wrapper


def sync_meta_info(key, data_dict, t: bool = True):
    """
    Given the key, sync up between metatensor `data_dict[key]` and meta_dict `data_dict[key_transforms/meta_dict]`.
    t=True: the one with more applied_operations in metatensor vs meta_dict is the output, False: less is the output.
    """
    if not isinstance(data_dict, Mapping):
        return data_dict
    d = dict(data_dict)

    # update meta dicts
    meta_dict_key = PostFix.meta(key)
    if meta_dict_key not in d:
        d[meta_dict_key] = monai.data.MetaTensor.get_default_meta()
    if not isinstance(d[key], monai.data.MetaTensor):
        d[key] = monai.data.MetaTensor(data_dict[key])
        d[key].meta = d[meta_dict_key]
    d[meta_dict_key].update(d[key].meta)  # prefer metatensor's data

    # update xform info
    xform_key = monai.transforms.TraceableTransform.trace_key(key)
    if xform_key not in d:
        d[xform_key] = monai.data.MetaTensor.get_default_applied_operations()
    from_meta, from_dict = d[key].applied_operations, d[xform_key]
    if not from_meta:  # avoid []
        d[key].applied_operations = d[xform_key] = from_dict
        return d
    if not from_dict:
        d[key].applied_operations = d[xform_key] = from_meta
        return d
    if t:  # larger transform info stack is used as the result
        ref = from_meta if len(from_meta) > len(from_dict) else from_dict
    else:  # smaller transform info stack is used as the result
        ref = from_dict if len(from_meta) > len(from_dict) else from_meta
    d[key].applied_operations = d[xform_key] = ref
    return d


def check_boundaries(boundaries) -> None:
    """
    Check boundaries for Signal transforms
    """
    if not (
        isinstance(boundaries, Sequence) and len(boundaries) == 2 and all(isinstance(i, float) for i in boundaries)
    ):
        raise ValueError("Incompatible values: boundaries needs to be a list of float.")


def paste_slices(tup):
    """
    given a tuple (pos,w,max_w), return a tuple of slices
    """
    pos, w, max_w = tup
    max_w = max_w.shape[-1]
    orig_min = max(pos, 0)
    orig_max = min(pos + w, max_w)
    block_min = -min(pos, 0)
    block_max = max_w - max(pos + w, max_w)
    block_max = block_max if block_max != 0 else None
    return slice(orig_min, orig_max), slice(block_min, block_max)


def paste(orig, block, loc):
    """
    given a location (loc) and an original array (orig), paste a block array into it
    """
    loc_zip = zip(loc, block.shape, orig)
    orig_slices, block_slices = zip(*map(paste_slices, loc_zip))

    orig[:, orig_slices[0]] = block[block_slices[0]]

    if orig.shape[0] == 1:
        orig = orig.squeeze()
    return orig


def squarepulse(sig, duty: float = 0.5):
    """
    compute squarepulse using pytorch
    equivalent to numpy implementation from
    https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.square.html
    """
    t, w = convert_to_tensor(sig), convert_to_tensor(duty)
    w = convert_to_tensor(w)
    t = convert_to_tensor(t)

    y = torch.zeros(t.shape)

    mask1 = (w > 1) | (w < 0)

    tmod = torch.remainder(t, 2 * torch.pi)
    mask2 = (~mask1) & (tmod < w * 2 * torch.pi)
    y[mask2] = 1
    mask3 = (~mask1) & (~mask2)
    y[mask3] = -1
    return y


def _to_numpy_resample_interp_mode(interp_mode):
    ret = look_up_option(str(interp_mode), SplineMode, default=None)
    if ret is not None:
        return int(ret)
    _mapping = {
        InterpolateMode.NEAREST: SplineMode.ZERO,
        InterpolateMode.NEAREST_EXACT: SplineMode.ZERO,
        InterpolateMode.LINEAR: SplineMode.ONE,
        InterpolateMode.BILINEAR: SplineMode.ONE,
        InterpolateMode.TRILINEAR: SplineMode.ONE,
        InterpolateMode.BICUBIC: SplineMode.THREE,
        InterpolateMode.AREA: SplineMode.ZERO,
    }
    ret = look_up_option(str(interp_mode), _mapping, default=None)
    if ret is not None:
        return ret
    return look_up_option(str(interp_mode), list(_mapping) + list(SplineMode))  # for better error msg


def _to_torch_resample_interp_mode(interp_mode):
    ret = look_up_option(str(interp_mode), InterpolateMode, default=None)
    if ret is not None:
        return ret
    _mapping = {
        SplineMode.ZERO: InterpolateMode.NEAREST_EXACT if pytorch_after(1, 11) else InterpolateMode.NEAREST,
        SplineMode.ONE: InterpolateMode.LINEAR,
        SplineMode.THREE: InterpolateMode.BICUBIC,
    }
    ret = look_up_option(str(interp_mode), _mapping, default=None)
    if ret is not None:
        return ret
    return look_up_option(str(interp_mode), list(_mapping) + list(InterpolateMode))


def _to_numpy_resample_padding_mode(m):
    ret = look_up_option(str(m), NdimageMode, default=None)
    if ret is not None:
        return ret
    _mapping = {
        GridSamplePadMode.ZEROS: NdimageMode.CONSTANT,
        GridSamplePadMode.BORDER: NdimageMode.NEAREST,
        GridSamplePadMode.REFLECTION: NdimageMode.REFLECT,
    }
    ret = look_up_option(str(m), _mapping, default=None)
    if ret is not None:
        return ret
    return look_up_option(str(m), list(_mapping) + list(NdimageMode))


def _to_torch_resample_padding_mode(m):
    ret = look_up_option(str(m), GridSamplePadMode, default=None)
    if ret is not None:
        return ret
    _mapping = {
        NdimageMode.CONSTANT: GridSamplePadMode.ZEROS,
        NdimageMode.GRID_CONSTANT: GridSamplePadMode.ZEROS,
        NdimageMode.NEAREST: GridSamplePadMode.BORDER,
        NdimageMode.REFLECT: GridSamplePadMode.REFLECTION,
        NdimageMode.WRAP: GridSamplePadMode.REFLECTION,
        NdimageMode.GRID_WRAP: GridSamplePadMode.REFLECTION,
        NdimageMode.GRID_MIRROR: GridSamplePadMode.REFLECTION,
    }
    ret = look_up_option(str(m), _mapping, default=None)
    if ret is not None:
        return ret
    return look_up_option(str(m), list(_mapping) + list(GridSamplePadMode))


@lru_cache(None)
def resolves_modes(
    interp_mode: str | None = "constant", padding_mode="zeros", backend=TransformBackends.TORCH, **kwargs
):
    """
    Automatically adjust the resampling interpolation mode and padding mode,
    so that they are compatible with the corresponding API of the `backend`.
    Depending on the availability of the backends, when there's no exact
    equivalent, a similar mode is returned.

    Args:
        interp_mode: interpolation mode.
        padding_mdoe: padding mode.
        backend: optional backend of `TransformBackends`. If None, the backend will be decided from `interp_mode`.
        kwargs: additional keyword arguments. currently support ``torch_interpolate_spatial_nd``, to provide
            additional information to determine ``linear``, ``bilinear`` and ``trilinear``;
            ``use_compiled`` to use MONAI's precompiled backend (pytorch c++ extensions), default to ``False``.
    """
    _interp_mode, _padding_mode, _kwargs = None, None, (kwargs or {}).copy()
    if backend is None:  # infer backend
        backend = (
            TransformBackends.NUMPY
            if look_up_option(str(interp_mode), SplineMode, default=None) is not None
            else TransformBackends.TORCH
        )
    if backend == TransformBackends.NUMPY:
        _interp_mode = _to_numpy_resample_interp_mode(interp_mode)
        _padding_mode = _to_numpy_resample_padding_mode(padding_mode)
        return backend, _interp_mode, _padding_mode, _kwargs
    _interp_mode = _to_torch_resample_interp_mode(interp_mode)
    _padding_mode = _to_torch_resample_padding_mode(padding_mode)
    if str(_interp_mode).endswith("linear"):
        nd = _kwargs.pop("torch_interpolate_spatial_nd", 2)
        if nd == 1:
            _interp_mode = InterpolateMode.LINEAR
        elif nd == 3:
            _interp_mode = InterpolateMode.TRILINEAR
        else:
            _interp_mode = InterpolateMode.BILINEAR  # torch grid_sample bilinear is trilinear in 3D
    if not _kwargs.pop("use_compiled", False):
        return backend, _interp_mode, _padding_mode, _kwargs
    _padding_mode = 1 if _padding_mode == "reflection" else _padding_mode
    if _interp_mode == "bicubic":
        _interp_mode = 3
    elif str(_interp_mode).endswith("linear"):
        _interp_mode = 1
    else:
        _interp_mode = GridSampleMode(_interp_mode)
    return backend, _interp_mode, _padding_mode, _kwargs


if __name__ == "__main__":
    print_transform_backends()
