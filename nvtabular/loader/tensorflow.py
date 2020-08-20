#
# Copyright (c) 2020, NVIDIA CORPORATION.
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
#
import os

import tensorflow as tf
import cupy as cp

from nvtabular.io import Dataset
from nvtabular.loader.backend import DataLoader
from nvtabular.loader.tf_utils import configure_tensorflow, get_dataset_schema_from_feature_columns

from_dlpack = configure_tensorflow()


def _validate_dataset(paths_or_dataset, batch_size, buffer_size, engine, reader_kwargs):
    # if a dataset was passed, just return it
    if isinstance(paths_or_dataset, Dataset):
        return paths_or_dataset

    # otherwise initialize a dataset
    # from paths or glob pattern
    if isinstance(paths_or_dataset, str):
        files = tf.io.gfile.glob(paths_or_dataset)
        _is_empty_msg = "Couldn't find file pattern {} in directory {}".format(
            *os.path.split(paths_or_dataset)
        )
    else:
        # TODO: some checking around attribute
        # error here?
        files = list(paths_or_dataset)
        _is_empty_msg = "paths_or_dataset list must contain at least one filename"

    assert isinstance(files, list)
    if len(files) == 0:
        raise ValueError(_is_empty_msg)

    # implement buffer size logic
    # TODO: IMPORTANT
    # should we divide everything by 3 to account
    # for extra copies laying around due to asynchronicity?
    if buffer_size >= 1:
        if buffer_size < batch_size:
            reader_kwargs["batch_size"] = int(batch_size * buffer_size)
        else:
            reader_kwargs["batch_size"] = buffer_size
    else:
        reader_kwargs["part_mem_fraction"] = buffer_size
    return Dataset(files, engine=engine, **reader_kwargs)


def _validate_schema(feature_columns, cont_names, cat_names):
    _uses_feature_columns = feature_columns is not None
    _uses_explicit_schema = (cat_names is not None) or (cont_names is not None)
    if _uses_feature_columns and _uses_explicit_schema:
        raise ValueError(
            "Passed `feature_column`s and explicit column names, must be one or the other"
        )
    elif _uses_feature_columns:
        return get_dataset_schema_from_feature_columns(feature_columns)
    elif _uses_explicit_schema:
        cat_names = cat_names or []
        cont_names = cont_names or []
        return cat_names, cont_names
    else:
        raise ValueError(
            "Must either pass a list of TensorFlow `feature_column`s "
            "or explicit `cat_name` and `cont_name` column name lists."
        )


class KerasSequenceLoader(tf.keras.utils.Sequence, DataLoader):
    def __init__(
        self,
        paths_or_dataset,
        batch_size,
        label_names,
        feature_columns=None,
        cat_names=None,
        cont_names=None,
        engine=None,
        shuffle=True,
        buffer_size=0.1,
        workflows=None,
        devices=None,
        parts_per_chunk=1,
        reader_kwargs={},
    ):
        dataset = _validate_dataset(
            paths_or_dataset, batch_size, buffer_size, engine, reader_kwargs
        )
        cat_names, cont_names = _validate_schema(feature_columns, cat_names, cont_names)

        DataLoader.__init__(
            self,
            dataset,
            cat_names,
            cont_names,
            label_names,
            batch_size,
            shuffle,
            parts_per_chunk=parts_per_chunk,
            workflows=workflows,
            devices=None # TODO: figure out multi-gpu support
        )

    def __len__(self):
        '''
        recreating since otherwise Keras yells at you
        '''
        return DataLoader.__len__(self)

    def __getitem__(self, idx):
        """
        implemented exclusively for consistency
        with Keras model.fit. Does not leverage
        passed idx in any way
        """
        return self.__next__()

    def _get_device_ctx(self, dev):
        class dummy:
            def __enter__(self):
                pass
            def __exit__(self, a, b, c):
                pass
        return dummy() # tf.device("/device:GPU:{}".format(dev))

    def _to_tensor(self, gdf, dtype=None):
        if gdf.empty:
            return
        dlpack = gdf.to_dlpack()
        x = from_dlpack(dlpack)
        # TODO: type checking?
        return tf.expand_dims(x, -1)

    def _create_tensors(self, gdf):
        # TODO: can we use these somehow to go faster?
        # what's the cost of doing axis 1 slicing in TF?
        # gdf_cats, gdf_conts, gdf_label = (
        #     gdf[cat_names], gdf[cont_names], gdf[label_names]
        # )
        X = {}
        for name in self.cat_names + self.cont_names:
            X[name] = self._to_tensor(gdf.pop(name))

        # TODO: do dictionaries instead for multi-output?
        y = []
        for name in self.label_names:
            y.append(self._to_tensor(gdf.pop(name)))
        del gdf
        return X, y