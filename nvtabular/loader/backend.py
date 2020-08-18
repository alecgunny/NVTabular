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
import queue
import threading

import cudf
import cupy as cp

from nvtabular.io import _shuffle_gdf


class TensorItr:
    """
        Iterator for returning batched chunks of the elemetns
        in a list of dictionary of tensors along their zeroth
        axis
        Parameters
        -----------
        tensors : list of tensors
        batch_size: the size of each batch to return.
        pin_memory: allows pinning of cpu memory, if used.
    """

    def __init__(self, tensors, batch_size=1, pin_memory=False):
        self.tensors = tensors
        self.batch_size = batch_size
        self.num_samples = self.tensors[2].size(0)

        if pin_memory:
            for tensor in self.tensors:
                tensor.pin_memory()

    def __len__(self):
        return (self.num_samples - 1) // self.batch_size + 1

    def __iter__(self):
        for idx in range(len(self)):
            # TODO: do some sort of type checking up front?
            # TODO: what will this slicing look like for multi-hots?
            if isinstance(self.tensors, dict):
                # TODO: this might be a bit inefficient on the TF side,
                # consider doing slicing up front on grouped matrices
                yield {name: x[idx : idx + self.batch_size] for name, x in self.tensors.items()}
            else:
                yield [
                    tensor[idx : idx + self.batch_size] if tensor is not None else None
                    for tensor in self.tensors
                ]


class ChunkQueue:
    """
        This class takes partitions (parts) from an NVTabular dataset
        and concatenates them into a cudf dataframe "chunk". This chunk
        is subsequently transformed into its tensor representation using
        the iterator's transform.
        Parameters:
        num_parts: int, number of partitions from the iterator, an NVTabular Dataset,
                   to concatenate into a "chunk"
        batch_size: int, the number of records in each batch
        iterator: TensorBatchDatasetItr, the iterator to pull the partitions (parts) from
        shuffle: bool, enable/disable shuffling
        cats: [str], the list of categorical columns in the dataset
        conts: [str], the list of continuous columns in the dataset
        labels: [str], the list of label columns in the dataset
    """

    def __init__(
        self,
        num_parts=3,
        batch_size=None,
        shuffle=False,
        cat_cols=None,
        cont_cols=None,
        label_cols=None,
    ):
        self.num_parts = num_parts
        self.batch_size = batch_size
        self.q_out = queue.Queue(1)
        self.cat_cols = cat_cols
        self.cont_cols = cont_cols
        self.label_cols = label_cols
        self.shuffle = shuffle
        self._stop_event = threading.Event()

    @property
    def stopped(self):
        return self._stop_event.is_set()

    def get(self):
        return self.q_out.get()

    def batch(self, itr):
        current = []
        for value in itr:
            current.append(value)
            if len(current) == self.num_parts:
                yield current
                current = []
        if len(current) > 0:
            yield current

    def load_chunks(self, dev, itr):
        with itr.device_ctx(dev):
            spill = None
            for chunks in self.batch(itr):
                if self.stopped:
                    return
                if spill and not spill.empty:
                    chunks.insert(0, spill)
                chunks = cudf.core.reshape.concat(chunks)
                chunks.reset_index(drop=True, inplace=True)
                chunks, spill = self.get_batch_div_chunk(chunks)
                if self.shuffle:
                    _shuffle_gdf(chunks)

                if len(chunks) > 0:
                    itr.preprocess(chunks)
                    chunks = itr.create_tensors(
                        chunks,
                        cat_names=self.cat_cols,
                        cont_names=self.cont_cols,
                        label_names=self.label_cols,
                    )
                    # chunks tensorized
                    self.q_out.put(chunks)
                    chunks = None
            # takes care final batch, which is less than batch size
            if spill:
                itr.preprocess(chunks)
                spill = itr.create_tensors(
                    spill,
                    cat_names=self.cat_cols,
                    cont_names=self.cont_cols,
                    label_names=self.label_cols,
                )
                self.q_out.put(spill)
                spill = None
            self.q_out.put("end")

    # For when an iterator is stopped before iteration is complete.
    def stop(self):
        self._stop_event.set()
        self.q_out.queue.clear()

    def get_batch_div_chunk(self, chunks):
        spill_idx = int(chunks.shape[0] / self.batch_size) * self.batch_size
        spill = cudf.DataFrame(chunks.iloc[spill_idx:])
        chunks = cudf.DataFrame(chunks.iloc[:spill_idx])
        if not chunks.empty:
            chunks.reset_index(drop=True, inplace=True)
        if not spill.empty:
            spill.reset_index(drop=True, inplace=True)
        return chunks, spill


class AsyncIterator:
    """
        This class serves as the iterator class for the AsyncTensorBatchDatasetItr.
        This will control iteration and allow for clean up after iteration is complete.
        Without requiring the destruction of the Parent class.
        Parameters:
        dataset: NVTabular dataset
        cats: [str], the list of categorical columns in the dataset
        conts: [str], the list of continuous columns in the dataset
        labels: [str], the list of label columns in the dataset
        batch_size: int, the size of each batch to supply to the model
        shuffle: bool, enable/disable shuffling of dataset
        target: the target library that will use the tensor transformed data
                currently supported: torch
        devices: [int], list represents all avialable GPU IDs
    """

    def __init__(
        self,
        itr_cls=None,
        cats=None,
        conts=None,
        labels=None,
        batch_size=1,
        shuffle=False,
        devices=None,
        workflows=None,
    ):
        assert issubclass(itr_cls, TensorBatchDatasetItr)
        self.itr_ls = itr_cls
        self.shuffle = shuffle
        self.devices = devices if devices else [0]

        if workflows is not None:
            # TODO: need to replace cats, conts, and labels
            # with output from last workflow
            pass
        self.workflows = workflows

        self.buff = ChunkQueue(
            batch_size=batch_size,
            cat_cols=cats,
            cont_cols=conts,
            label_cols=labels,
            shuffle=shuffle,
        )

    def __iter__(self):
        indices = cp.arange(self.dataset.to_ddf().npartitions)
        if self.shuffle:
            cp.random.shuffle(indices)
        for dev in self.devices:
            itr = self.itr_cls(
                self.dataset,
                self.library,
                indices=indices.tolist(),
                device=dev,
                total_devs=self.devices,
                workflows=self.workflows,
            )
            t1 = threading.Thread(target=self.buff.load_chunks, args=(dev, itr))
            t1.daemon = True
            t1.start()
        ends = []
        while True:
            chunk = self.buff.get()
            # TODO: may need to do dlpack passing here if
            # TensorFlow starts complaining
            if isinstance(chunk, str):
                ends.append(chunk)
                if len(ends) == len(self.devices):
                    return
            else:
                yield from TensorItr(chunk, batch_size=self.buff.batch_size)
            chunk = None

    def __del__(self):
        self.buff.stop()


class TensorBatchDatasetItr:
    """
        Base class for all dataset to tensor iterators.
        Takes input of an NVTabular dataset
        and supplies user defined size chunks.

        Parameters
        dataset: NVTabular dataset
        shuffle: bool, specifying whether to shuffle the partitions
                 and shuffle chunks before creating tensor batches
        device: int, represents targeted GPU id
        total_devs: [int], list represents all avialable GPU IDs

    """

    def __init__(
        self, dataset, shuffle=None, workflows=None, device=0, total_devs=1, indices=None, **kwargs
    ):
        self.data = dataset
        self.indices = indices if indices else cp.arange(dataset.to_ddf().npartitions)
        self.workflows = workflows or []

        self.device = device
        self.total_devs = total_devs

    def __iter__(self):
        indices = self.gather_indices()
        yield from self.data.to_iter(indices=indices)

    def __len__(self):
        return int(self.data.num_rows / len(self.total_devs))

    def preprocess(self, x):
        for workflow in self.workflows:
            x = workflow.apply_ops(x)
        return x

    def gather_indices(self):
        per_worker = int(len(self.indices) // len(self.total_devs)) + 1
        worker_id = self.total_devs.index(self.device)
        start = worker_id * per_worker
        return self.indices[start : start + per_worker]

    def to_dlpack(self, gdf):
        return gdf.to_dlpack()

    def device_ctx(self, dev):
        """
        This function is designed to return a context for a target device. This
        should be an integer signifying the target GPU's identifier. Currently
        this is dependent on the targeted framework. Need method exposed via
        rapids or cudf to factor this api call out.

        Parameters
        device: int, the target GPU's id
        """
        raise NotImplementedError()

    def create_tensors(self, gdf, cat_names=None, cont_names=None, label_names=None):
        raise NotImplementedError()