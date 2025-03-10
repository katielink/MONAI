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

import os
import tempfile
import unittest

import nibabel as nib
import numpy as np
import torch

from monai.apps.auto3dseg import (
    AlgoEnsembleBestByFold,
    AlgoEnsembleBestN,
    AlgoEnsembleBuilder,
    BundleGen,
    DataAnalyzer,
    EnsembleRunner,
)
from monai.bundle.config_parser import ConfigParser
from monai.data import create_test_image_3d
from monai.transforms import SaveImage
from monai.utils import optional_import, set_determinism
from monai.utils.enums import AlgoKeys
from tests.utils import (
    SkipIfBeforePyTorchVersion,
    get_testing_algo_template_path,
    skip_if_downloading_fails,
    skip_if_no_cuda,
    skip_if_quick,
)

_, has_tb = optional_import("torch.utils.tensorboard", name="SummaryWriter")

num_images_perfold = max(torch.cuda.device_count(), 4)
num_images_per_batch = 2

fake_datalist: dict[str, list[dict]] = {
    "testing": [{"image": "val_001.fake.nii.gz"}, {"image": "val_002.fake.nii.gz"}],
    "training": [
        {
            "fold": f,
            "image": f"tr_image_{(f * num_images_perfold + idx):03d}.nii.gz",
            "label": f"tr_label_{(f * num_images_perfold + idx):03d}.nii.gz",
        }
        for f in range(num_images_per_batch + 1)
        for idx in range(num_images_perfold)
    ],
}

train_param = (
    {
        "num_images_per_batch": num_images_per_batch,
        "num_epochs": 2,
        "num_epochs_per_validation": 1,
        "num_warmup_epochs": 1,
        "use_pretrain": False,
        "pretrained_path": "",
        "determ": True,
    }
    if torch.cuda.is_available()
    else {}
)

pred_param = {"files_slices": slice(0, 1), "mode": "mean", "sigmoid": True}


@skip_if_quick
@SkipIfBeforePyTorchVersion((1, 11, 1))
@unittest.skipIf(not has_tb, "no tensorboard summary writer")
class TestEnsembleBuilder(unittest.TestCase):
    def setUp(self) -> None:
        set_determinism(0)
        self.test_dir = tempfile.TemporaryDirectory()

    @skip_if_no_cuda
    def test_ensemble(self) -> None:
        test_path = self.test_dir.name

        dataroot = os.path.join(test_path, "dataroot")
        work_dir = os.path.join(test_path, "workdir")

        da_output_yaml = os.path.join(work_dir, "datastats.yaml")
        data_src_cfg = os.path.join(work_dir, "data_src_cfg.yaml")

        if not os.path.isdir(dataroot):
            os.makedirs(dataroot)

        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)

        # Generate a fake dataset
        for d in fake_datalist["testing"] + fake_datalist["training"]:
            im, seg = create_test_image_3d(24, 24, 24, rad_max=10, num_seg_classes=1)
            nib_image = nib.Nifti1Image(im, affine=np.eye(4))
            image_fpath = os.path.join(dataroot, d["image"])
            nib.save(nib_image, image_fpath)

            if "label" in d:
                nib_image = nib.Nifti1Image(seg, affine=np.eye(4))
                label_fpath = os.path.join(dataroot, d["label"])
                nib.save(nib_image, label_fpath)

        # write to a json file
        fake_json_datalist = os.path.join(dataroot, "fake_input.json")
        ConfigParser.export_config_file(fake_datalist, fake_json_datalist)

        da = DataAnalyzer(fake_json_datalist, dataroot, output_path=da_output_yaml)
        da.get_all_case_stats()

        data_src = {
            "name": "fake_data",
            "task": "segmentation",
            "modality": "MRI",
            "datalist": fake_json_datalist,
            "dataroot": dataroot,
            "multigpu": False,
            "class_names": ["label_class"],
        }

        ConfigParser.export_config_file(data_src, data_src_cfg)

        with skip_if_downloading_fails():
            bundle_generator = BundleGen(
                algo_path=work_dir,
                data_stats_filename=da_output_yaml,
                data_src_cfg_name=data_src_cfg,
                templates_path_or_url=get_testing_algo_template_path(),
            )
        bundle_generator.generate(work_dir, num_fold=1)
        history = bundle_generator.get_history()

        for algo_dict in history:
            name = algo_dict[AlgoKeys.ID]
            algo = algo_dict[AlgoKeys.ALGO]
            _train_param = train_param.copy()
            if name.startswith("segresnet"):
                _train_param["network#init_filters"] = 8
                _train_param["pretrained_ckpt_name"] = ""
            elif name.startswith("swinunetr"):
                _train_param["network#feature_size"] = 12
            algo.train(_train_param)

        builder = AlgoEnsembleBuilder(history, data_src_cfg)
        builder.set_ensemble_method(AlgoEnsembleBestN(n_best=1))
        ensemble = builder.get_ensemble()
        name = ensemble.get_algo_ensemble()[0][AlgoKeys.ID]
        if name.startswith("segresnet"):
            pred_param["network#init_filters"] = 8
        elif name.startswith("swinunetr"):
            pred_param["network#feature_size"] = 12
        preds = ensemble(pred_param)
        self.assertTupleEqual(preds[0].shape, (2, 24, 24, 24))

        builder.set_ensemble_method(AlgoEnsembleBestByFold(1))
        ensemble = builder.get_ensemble()
        for algo in ensemble.get_algo_ensemble():
            print(algo[AlgoKeys.ID])

    def test_ensemble_runner(self) -> None:
        runner = EnsembleRunner()
        runner.set_num_fold(3)
        self.assertTrue(runner.num_fold == 3)
        runner.set_ensemble_method(ensemble_method_name="AlgoEnsembleBestByFold")
        self.assertIsInstance(runner.ensemble_method, AlgoEnsembleBestByFold)
        self.assertTrue(runner.ensemble_method.n_fold == 3)  # type: ignore

        runner.set_ensemble_method(ensemble_method_name="AlgoEnsembleBestN", n_best=3)
        self.assertIsInstance(runner.ensemble_method, AlgoEnsembleBestN)
        self.assertTrue(runner.ensemble_method.n_best == 3)

        save_output = os.path.join(self.test_dir.name, "workdir")
        save_image = runner._pop_kwargs_to_get_image_save_transform(
            output_dir=save_output, output_postfix="test_ensemble", output_dtype=float, resample=True
        )
        self.assertIsInstance(ConfigParser(save_image).get_parsed_content(), SaveImage)

    def tearDown(self) -> None:
        set_determinism(None)
        self.test_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
