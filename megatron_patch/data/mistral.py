# Copyright (c) 2023 Alibaba PAI Team.
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

import numpy as np
import io
import copy
import json
import torch
from megatron import get_args

from megatron_patch.tokenizer import get_tokenizer

class MistralRawDataset(torch.utils.data.Dataset):
    def __init__(self, path, max_padding_length):
        args = get_args()
        self.tokenizer = get_tokenizer()
        self.IGNORE_INDEX = self.tokenizer.pad_token_id
        if "-Pretrain" in args.dataset:
            self.max_padding_length = max_padding_length + 1
        else:
            self.max_padding_length = max_padding_length
        PROMPT_DICT = {
            'prompt_input':
            ('Below is an instruction that describes a task,'
             ' paired with an input that provides further context. '
             'Write a response that appropriately completes the request.\n\n'
             '### Instruction:\n{instruction}'
             '\n\n### Input:\n{input}\n\n### Response:'),
            'prompt_no_input':
            ('Below is an instruction that describes a task. '
             'Write a response that appropriately completes the request.\n\n'
             '### Instruction:\n{instruction}\n\n### Response:'),
        }

        list_data_dict = self.jload(path[0])
        prompt_input, prompt_no_input = PROMPT_DICT[
            'prompt_input'], PROMPT_DICT['prompt_no_input']

        sources = [
            prompt_input.format_map(example) if example.get('input', '') != ''
            else prompt_no_input.format_map(example)
            for example in list_data_dict
        ]
        if 'output' in list_data_dict[0].keys():
            temp = 'output'
        elif 'content' in list_data_dict[0].keys():
            temp = 'content'

        targets = [
            f"{example[temp]}{self.tokenizer.eos_token}"
            for example in list_data_dict
        ]
        data_dict = self.preprocess(sources, targets, self.tokenizer)

        self.input_ids = data_dict['input_ids']
        self.labels = data_dict['labels']
        self.samples = []
        for inputs, labels in zip(self.input_ids, self.labels):
            self.samples.append([inputs, labels])

        print('  >> total number of samples: {}'.format(len(self.samples)))

    def _make_r_io_base(self, f, mode: str):
        if not isinstance(f, io.IOBase):
            f = open(f, mode=mode, encoding='utf-8')
        return f

    def jload(self, f, mode='r'):
        """
        Load a .json file into a dictionary.
        Args:
            f: The file object or string representing the file path.
            mode: The mode in which to open the file (e.g., 'r', 'w', 'a').
        Returns:
            A dictionary containing the contents of the JSON file.
        """
        f = self._make_r_io_base(f, mode)
        jdict = json.load(f)
        f.close()
        return jdict

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        raw_sample = self.samples[idx]
        return self.gpt_convert_example_to_feature(raw_sample)

    def preprocess(self, sources, targets, tokenizer):
        """
        Preprocess the data by tokenizing.
        Args:
            sources (List[str]): a list of source strings
            targets (List[str]): a list of target strings
            tokenizer (Tokenizer): a tokenizer object used for tokenization
        Returns:
            dict: a dictionary containing the input_ids and labels for the examples
        """

        examples = [s + t for s, t in zip(sources, targets)]
        examples_tokenized, sources_tokenized = [
            self.tokenize(strings, tokenizer)
            for strings in (examples, sources)
        ]
        input_ids = examples_tokenized['input_ids']
        labels = copy.deepcopy(input_ids)
        for label, source_len in zip(labels,
                                     sources_tokenized['input_ids_lens']):
            label[:source_len] = self.IGNORE_INDEX
        return dict(input_ids=input_ids, labels=labels)

    def tokenize(self, strings, tokenizer):
        """
        Tokenize a list of strings.
        Args:
            strings (List[str]): a list of strings to be tokenized
            tokenizer (Tokenizer): a tokenizer object used for tokenization
        Returns:
            dict: a dictionary containing the input_ids and labels for the tokenized strings
        """
        tokenized_list = [
            tokenizer.encode(text) for text in strings
        ]
        new_tokenzied_list = []
        for tokenized in tokenized_list:
            if self.max_padding_length <= len(tokenized):
                new_tokenzied_list.append(torch.tensor(tokenized[:self.max_padding_length]))
            else:
                padding_len = self.max_padding_length - len(tokenized)
                new_tokenized = torch.cat(
                    [torch.tensor(tokenized), torch.full((padding_len,), tokenizer.pad_token_id, dtype=torch.int)])
                new_tokenzied_list.append(new_tokenized)

        input_ids = labels = [
            tokenized for tokenized in new_tokenzied_list
        ]
        input_ids_lens = labels_lens = [
            (tokenized != tokenizer.pad_token_id).sum().item()
            for tokenized in new_tokenzied_list
        ]

        return dict(
            input_ids=input_ids,
            labels=labels,
            input_ids_lens=input_ids_lens,
            labels_lens=labels_lens,
        )

    def gpt_convert_example_to_feature(self, sample):
        """
        Convert a single sample containing input_id, label and loss_mask into a format suitable for GPT training.
        """
        input_ids, labels = sample
        train_sample = {
            'input_ids': input_ids,
            'labels': labels
        }

        return train_sample

class MistralIdxMapDataset(torch.utils.data.Dataset):
    """LLAMA dataset class for mmap format data"""
    def __init__(self,
                 name,
                 data_prefix,
                 documents,
                 indexed_dataset,
                 num_samples,
                 seed,
                 max_padding_length,
                 return_doc_ids=False):

        # self.IGNORE_INDEX = -100
        args = get_args()
        self.tokenizer = get_tokenizer()
        self.max_padding_length = max_padding_length

        self.name = name
        self.indexed_dataset = indexed_dataset
        self.return_doc_ids = return_doc_ids
        self.split = args.split
        # Checks
        assert np.min(documents) >= 0
        assert np.max(documents) < indexed_dataset.sizes.shape[0]

        from megatron.data.gpt_dataset import _build_index_mappings
        # Build index mappings.
        try:
            self.doc_idx, self.sample_idx, self.shuffle_idx, self.index_prefix = \
                _build_index_mappings(self.name, data_prefix,
                                  documents, self.indexed_dataset.sizes,
                                  num_samples, self.max_padding_length, seed)
        except:
            self.doc_idx, self.sample_idx, self.shuffle_idx, self.desc, self.desc_hash = \
                _build_index_mappings(self.name, data_prefix,
                                  documents, self.indexed_dataset.sizes,
                                  self.split, num_samples, self.max_padding_length, seed,
                                  data_cache_path=None)

    def __len__(self):
        # -1 is due to data structure used to retieve the index:
        #    sample i --> [sample_idx[i], sample_idx[i+1])
        return self.sample_idx.shape[0] - 1

    def __getitem__(self, idx):
        # Get the shuffled index.
        idx = self.shuffle_idx[idx]
        # Start and end documents and offsets.
        doc_index_f = self.sample_idx[idx][0]
        doc_index_l = self.sample_idx[idx + 1][0]
        offset_f = self.sample_idx[idx][1]
        offset_l = self.sample_idx[idx + 1][1]
        # If we are within the same document, just extract the chunk.
        doc_ids = []

        if doc_index_f == doc_index_l:
            doc_ids.append(self.doc_idx[doc_index_f])
            sample = self.indexed_dataset.get(self.doc_idx[doc_index_f],
                                              offset=offset_f,
                                              length=offset_l - offset_f + 1)
        else:
            # Otherwise, get the rest of the initial document.
            doc_ids.append(self.doc_idx[doc_index_f])
            sample_list = [
                self.indexed_dataset.get(self.doc_idx[doc_index_f],
                                         offset=offset_f)
            ]
            # Loop over all in between documents and add the entire document.
            for i in range(doc_index_f + 1, doc_index_l):
                doc_ids.append(self.doc_idx[i])
                sample_list.append(self.indexed_dataset.get(self.doc_idx[i]))
            # And finally add the relevant portion of last document.
            doc_ids.append(self.doc_idx[doc_index_l])
            sample_list.append(
                self.indexed_dataset.get(self.doc_idx[doc_index_l],
                                         length=offset_l + 1))
            sample = np.concatenate(sample_list)

        tokens = sample.tolist()
        sample = []
        sample.append(np.array(tokens))
        sample.append(np.array(tokens))

        return self.gpt_convert_example_to_feature(sample)

    def gpt_convert_example_to_feature(self, sample):
        input_ids, labels = sample
        loss_mask = np.ones(labels.shape, dtype=np.int64)
        loss_mask[labels == self.tokenizer.bos_token_id] = 0
        loss_mask[labels == self.tokenizer.pad_token_id] = 0
        train_sample = {
            'input_ids': input_ids,
            'labels': labels,
            'loss_mask': loss_mask
        }

        return train_sample