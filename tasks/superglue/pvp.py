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

"""
This file contains the pattern-verbalizer pairs (PVPs) for all tasks.
"""
import copy
import random
import string
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Tuple, List, Union, Dict

from tasks.data_utils import InputExample, num_special_tokens_to_add, build_input_from_ids, build_sample
from utils import print_rank_0

FilledPattern = Tuple[List[Union[str, Tuple[str, bool]]], List[Union[str, Tuple[str, bool]]]]


class PVP(ABC):
    """
    This class contains functions to apply patterns and verbalizers as required by PET. Each task requires its own
    custom implementation of a PVP.
    """

    def __init__(self, tokenizer, label_list, max_seq_length, pattern_id: int = 0, verbalizer_file: str = None,
                 seed: int = 42):
        """
        Create a new PVP.

        :param wrapper: the wrapper for the underlying language model
        :param pattern_id: the pattern id to use
        :param verbalizer_file: an optional file that contains the verbalizer to be used
        :param seed: a seed to be used for generating random numbers if necessary
        """
        self.tokenizer = tokenizer
        self.label_list = label_list
        self.max_seq_length = max_seq_length
        self.pattern_id = pattern_id
        self.rng = random.Random(seed)

        if verbalizer_file:
            self.verbalize = PVP._load_verbalizer_from_file(verbalizer_file, self.pattern_id)

    @property
    def is_multi_token(self):
        return False

    @property
    def mask(self) -> str:
        """Return the underlying LM's mask token"""
        return self.tokenizer.get_command('MASK').token

    @property
    def mask_id(self) -> int:
        """Return the underlying LM's mask id"""
        return self.tokenizer.get_command('MASK').Id

    @property
    def max_num_verbalizers(self) -> int:
        """Return the maximum number of verbalizers across all labels"""
        return max(len(self.verbalize(label)) for label in self.label_list)

    @staticmethod
    def shortenable(s):
        """Return an instance of this string that is marked as shortenable"""
        return s, True

    @staticmethod
    def remove_final_punc(s: Union[str, Tuple[str, bool]]):
        """Remove the final punctuation mark"""
        if isinstance(s, tuple):
            return PVP.remove_final_punc(s[0]), s[1]
        return s.rstrip(string.punctuation)

    @staticmethod
    def lowercase_first(s: Union[str, Tuple[str, bool]]):
        """Lowercase the first character"""
        if isinstance(s, tuple):
            return PVP.lowercase_first(s[0]), s[1]
        return s[0].lower() + s[1:]

    def encode(self, example: InputExample, priming: bool = False, labeled: bool = False):
        """
        Encode an input example using this pattern-verbalizer pair.

        :param example: the input example to encode
        :param priming: whether to use this example for priming
        :param labeled: if ``priming=True``, whether the label should be appended to this example
        :return: A tuple, consisting of a list of input ids and a list of token type ids
        """

        if not priming:
            assert not labeled, "'labeled' can only be set to true if 'priming' is also set to true"

        tokenizer = self.tokenizer
        parts_a, parts_b = self.get_parts(example)

        parts_a = [x if isinstance(x, tuple) else (x, False) for x in parts_a]
        parts_a = [(tokenizer.EncodeAsIds(x).tokenization, s) for x, s in parts_a if x]

        if parts_b:
            parts_b = [x if isinstance(x, tuple) else (x, False) for x in parts_b]
            parts_b = [(tokenizer.EncodeAsIds(x).tokenization, s) for x, s in parts_b if x]

        if self.is_multi_token:
            answers = self.get_answers(example)
            ids_list, positions_list, sep_list, mask_list, target_list = [], [], [], [], []
            for answer in answers:
                this_parts_a, this_parts_b = copy.deepcopy(parts_a), copy.deepcopy(parts_b)
                answer_ids = get_verbalization_ids(answer, tokenizer, force_single_token=False)
                self.truncate(this_parts_a, this_parts_b, answer_ids, max_length=self.max_seq_length)
                tokens_a = [token_id for part, _ in this_parts_a for token_id in part]
                tokens_b = [token_id for part, _ in this_parts_b for token_id in part] if parts_b else None
                data = build_input_from_ids(tokens_a, tokens_b, answer_ids, self.max_seq_length, self.tokenizer,
                                            add_cls=True, add_sep=False, add_piece=True)
                ids, types, paddings, position_ids, sep, target_ids, loss_masks = data
                ids_list.append(ids)
                positions_list.append(position_ids)
                sep_list.append(sep)
                target_list.append(target_ids)
                mask_list.append(loss_masks)
            label = example.label
            if len(label) == 0:
                label = self.label_list.index(label[0])
            else:
                label = [self.label_list.index(l) for l in label]
            sample = build_sample(ids_list, positions=positions_list, masks=sep_list, label=label,
                                  logit_mask=mask_list, target=target_list,
                                  unique_id=example.guid)
            return sample
        else:
            self.truncate(parts_a, parts_b, [], max_length=self.max_seq_length)

            tokens_a = [token_id for part, _ in parts_a for token_id in part]
            tokens_b = [token_id for part, _ in parts_b for token_id in part] if parts_b else None

            if priming:
                input_ids = tokens_a
                if tokens_b:
                    input_ids += tokens_b
                if labeled:
                    mask_idx = input_ids.index(self.mask_id)
                    assert mask_idx == 1, 'sequence of input_ids must contain a mask token'
                    assert len(self.verbalize(example.label)) == 1, 'priming only supports one verbalization per label'
                    verbalizer = self.verbalize(example.label)[0]
                    verbalizer_id = get_verbalization_ids(verbalizer, self.tokenizer, force_single_token=True)
                    input_ids[mask_idx] = verbalizer_id
                return input_ids
            data = build_input_from_ids(tokens_a, tokens_b, None, self.max_seq_length, self.tokenizer, add_cls=True,
                                        add_sep=False, add_piece=True)
            ids, types, paddings, position_ids, sep, target_ids, loss_masks = data
            label = self.label_list.index(example.label)
            sample = build_sample(ids=ids, positions=position_ids, target=target_ids, masks=sep, logit_mask=loss_masks,
                                  label=label)
            return sample

    @staticmethod
    def _seq_length(parts: List[Tuple[List[int], bool]], only_shortenable: bool = False):
        return sum([len(x) for x, shortenable in parts if not only_shortenable or shortenable]) if parts else 0

    @staticmethod
    def _remove_last(parts: List[Tuple[List[int], bool]]):
        last_idx = max(idx for idx, (seq, shortenable) in enumerate(parts) if shortenable and seq)
        parts[last_idx] = (parts[last_idx][0][:-1], parts[last_idx][1])

    def truncate(self, parts_a: List[Tuple[List[int], bool]], parts_b: List[Tuple[List[int], bool]], answer: List[int],
                 max_length: int):
        """Truncate two sequences of text to a predefined total maximum length"""
        total_len = self._seq_length(parts_a) + self._seq_length(parts_b)
        total_len += num_special_tokens_to_add(parts_a, parts_b, answer, add_cls=True, add_sep=False, add_piece=True)
        num_tokens_to_remove = total_len - max_length

        if num_tokens_to_remove <= 0:
            return parts_a, parts_b, answer

        for _ in range(num_tokens_to_remove):
            if self._seq_length(parts_a, only_shortenable=True) > self._seq_length(parts_b, only_shortenable=True):
                self._remove_last(parts_a)
            else:
                self._remove_last(parts_b)

    @abstractmethod
    def get_parts(self, example: InputExample) -> FilledPattern:
        """
        Given an input example, apply a pattern to obtain two text sequences (text_a and text_b) containing exactly one
        mask token (or one consecutive sequence of mask tokens for PET with multiple masks). If a task requires only a
        single sequence of text, the second sequence should be an empty list.

        :param example: the input example to process
        :return: Two sequences of text. All text segments can optionally be marked as being shortenable.
        """
        pass

    def get_answers(self, example: InputExample):
        return []

    @abstractmethod
    def verbalize(self, label) -> List[str]:
        """
        Return all verbalizations for a given label.

        :param label: the label
        :return: the list of verbalizations
        """
        pass

    def get_mask_positions(self, input_ids: List[int]) -> List[int]:
        label_idx = input_ids.index(self.mask_id)
        labels = [-1] * len(input_ids)
        labels[label_idx] = 1
        return labels

    @staticmethod
    def _load_verbalizer_from_file(path: str, pattern_id: int):

        verbalizers = defaultdict(dict)  # type: Dict[int, Dict[str, List[str]]]
        current_pattern_id = None

        with open(path, 'r') as fh:
            for line in fh.read().splitlines():
                if line.isdigit():
                    current_pattern_id = int(line)
                elif line:
                    label, *realizations = line.split()
                    verbalizers[current_pattern_id][label] = realizations

        print_rank_0("Automatically loaded the following verbalizer: \n {}".format(verbalizers[pattern_id]))

        def verbalize(label) -> List[str]:
            return verbalizers[pattern_id][label]

        return verbalize


class CopaPVP(PVP):
    @property
    def is_multi_token(self):
        return True

    def get_answers(self, example: InputExample):
        choice1 = self.remove_final_punc(self.lowercase_first(example.meta['choice1']))
        choice2 = self.remove_final_punc(self.lowercase_first(example.meta['choice2']))
        return [choice1, choice2]

    def get_parts(self, example: InputExample) -> FilledPattern:

        premise = self.remove_final_punc(self.shortenable(example.text_a))
        choice1 = self.remove_final_punc(self.lowercase_first(example.meta['choice1']))
        choice2 = self.remove_final_punc(self.lowercase_first(example.meta['choice2']))

        question = example.meta['question']
        assert question in ['cause', 'effect']

        if question == 'cause':
            if self.pattern_id == 0:
                return ['"', choice1, '" or "', choice2, '"?', premise, 'because', self.mask, '.'], []
            elif self.pattern_id == 1:
                return [choice1, 'or', choice2, '?', premise, 'because', self.mask, '.'], []
        else:
            if self.pattern_id == 0:
                return ['"', choice1, '" or "', choice2, '"?', premise, ', so', self.mask, '.'], []
            elif self.pattern_id == 1:
                return [choice1, 'or', choice2, '?', premise, ', so', self.mask, '.'], []

    def verbalize(self, label) -> List[str]:
        return []


class WscPVP(PVP):
    @property
    def is_multi_token(self):
        return True

    def get_answers(self, example: InputExample):
        target = example.meta['span1_text']
        return [target]

    def get_parts(self, example: InputExample) -> FilledPattern:
        pronoun = example.meta['span2_text']
        pronoun_idx = example.meta['span2_index']

        words_a = example.text_a.split()
        words_a[pronoun_idx] = '*' + words_a[pronoun_idx] + '*'
        text_a = ' '.join(words_a)
        text_a = self.shortenable(text_a)

        if self.pattern_id == 0:
            return [text_a, "The pronoun '*" + pronoun + "*' refers to", self.mask, '.'], []
        elif self.pattern_id == 1:
            return [text_a, "In the previous sentence, the pronoun '*" + pronoun + "*' refers to", self.mask,
                    '.'], []
        elif self.pattern_id == 2:
            return [text_a,
                    "Question: In the passage above, what does the pronoun '*" + pronoun + "*' refer to? Answer: ",
                    self.mask, '.'], []

    def verbalize(self, label) -> List[str]:
        return []


class RecordPVP(PVP):
    @property
    def is_multi_token(self):
        return True

    def get_answers(self, example: InputExample):
        choices = example.meta['candidates']
        return choices

    def get_parts(self, example: InputExample) -> FilledPattern:
        premise = self.shortenable(example.text_a)

        assert '@placeholder' in example.text_b, f'question "{example.text_b}" does not contain a @placeholder token'
        question = example.text_b.replace('@placeholder', self.mask + " ")
        return [premise, question], []

    def verbalize(self, label) -> List[str]:
        return []


class RtePVP(PVP):
    VERBALIZER = {
        "not_entailment": ["No"],
        "entailment": ["Yes"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        # switch text_a and text_b to get the correct order
        text_a = self.shortenable(example.text_a)
        text_b = self.shortenable(example.text_b.rstrip(string.punctuation))

        if self.pattern_id == 0:
            return ['"', text_b, '" ?'], [self.mask, ', "', text_a, '"']
        elif self.pattern_id == 1:
            return [text_b, '?'], [self.mask, ',', text_a]
        if self.pattern_id == 2:
            return ['"', text_b, '" ?'], [self.mask, '. "', text_a, '"']
        elif self.pattern_id == 3:
            return [text_b, '?'], [self.mask, '.', text_a]
        elif self.pattern_id == 4:
            return [text_a, ' question: ', self.shortenable(example.text_b), ' True or False? answer:', self.mask], []

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 4:
            return ['true'] if label == 'entailment' else ['false']
        return RtePVP.VERBALIZER[label]


class CbPVP(RtePVP):
    VERBALIZER = {
        "contradiction": ["No"],
        "entailment": ["Yes"],
        "neutral": ["Maybe"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        if self.pattern_id == 4:
            text_a = self.shortenable(example.text_a)
            text_b = self.shortenable(example.text_b)
            return [text_a, ' question: ', text_b, ' true, false or neither? answer:', self.mask], []
        return super().get_parts(example)

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 4:
            return ['true'] if label == 'entailment' else ['false'] if label == 'contradiction' else ['neither']
        return CbPVP.VERBALIZER[label]


class BoolQPVP(PVP):
    VERBALIZER_A = {
        "False": ["No"],
        "True": ["Yes"]
    }

    VERBALIZER_B = {
        "False": ["false"],
        "True": ["true"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        passage = self.shortenable(example.text_a)
        question = self.shortenable(example.text_b)

        if self.pattern_id < 2:
            return [passage, '. Question: ', question, '? Answer: ', self.mask, '.'], []
        elif self.pattern_id < 4:
            return [passage, '. Based on the previous passage, ', question, '?', self.mask, '.'], []
        else:
            return ['Based on the following passage, ', question, '?', self.mask, '.', passage], []

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 0 or self.pattern_id == 2 or self.pattern_id == 4:
            return BoolQPVP.VERBALIZER_A[label]
        else:
            return BoolQPVP.VERBALIZER_B[label]


class MultiRcPVP(PVP):
    VERBALIZER = {
        "0": ["No"],
        "1": ["Yes"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        passage = self.shortenable(example.text_a)
        question = example.text_b
        answer = example.meta['answer']

        if self.pattern_id == 0:
            return [passage, '. Question: ', question, '? Is it ', answer, '?', self.mask, '.'], []
        if self.pattern_id == 1:
            return [passage, '. Question: ', question, '? Is the correct answer "', answer, '"?', self.mask, '.'], []
        if self.pattern_id == 2:
            return [passage, '. Based on the previous passage, ', question, '? Is "', answer, '" a correct answer?',
                    self.mask, '.'], []
        if self.pattern_id == 3:
            return [passage, question, '- [', self.mask, ']', answer], []

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 3:
            return ['False'] if label == "0" else ['True']
        return MultiRcPVP.VERBALIZER[label]


class WicPVP(PVP):
    VERBALIZER_A = {
        "F": ["No"],
        "T": ["Yes"]
    }
    VERBALIZER_B = {
        "F": ["2"],
        "T": ["b"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        text_a = self.shortenable(example.text_a)
        text_b = self.shortenable(example.text_b)
        word = example.meta['word']

        if self.pattern_id == 0:
            return ['"', text_a, '" / "', text_b, '" Similar sense of "' + word + '"?', self.mask, '.'], []
        if self.pattern_id == 1:
            return [text_a, text_b, 'Does ' + word + ' have the same meaning in both sentences?', self.mask], []
        if self.pattern_id == 2:
            return [word, ' . Sense (1) (a) "', text_a, '" (', self.mask, ') "', text_b, '"'], []

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 2:
            return WicPVP.VERBALIZER_B[label]
        return WicPVP.VERBALIZER_A[label]


class AgnewsPVP(PVP):
    VERBALIZER = {
        "1": ["World"],
        "2": ["Sports"],
        "3": ["Business"],
        "4": ["Tech"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:

        text_a = self.shortenable(example.text_a)
        text_b = self.shortenable(example.text_b)

        if self.pattern_id == 0:
            return [self.mask, ':', text_a, text_b], []
        elif self.pattern_id == 1:
            return [self.mask, 'News:', text_a, text_b], []
        elif self.pattern_id == 2:
            return [text_a, '(', self.mask, ')', text_b], []
        elif self.pattern_id == 3:
            return [text_a, text_b, '(', self.mask, ')'], []
        elif self.pattern_id == 4:
            return ['[ Category:', self.mask, ']', text_a, text_b], []
        elif self.pattern_id == 5:
            return [self.mask, '-', text_a, text_b], []
        else:
            raise ValueError("No pattern implemented for id {}".format(self.pattern_id))

    def verbalize(self, label) -> List[str]:
        return AgnewsPVP.VERBALIZER[label]


class YahooPVP(PVP):
    VERBALIZER = {
        "1": ["Society"],
        "2": ["Science"],
        "3": ["Health"],
        "4": ["Education"],
        "5": ["Computer"],
        "6": ["Sports"],
        "7": ["Business"],
        "8": ["Entertainment"],
        "9": ["Relationship"],
        "10": ["Politics"],
    }

    def get_parts(self, example: InputExample) -> FilledPattern:

        text_a = self.shortenable(example.text_a)
        text_b = self.shortenable(example.text_b)

        if self.pattern_id == 0:
            return [self.mask, ':', text_a, text_b], []
        elif self.pattern_id == 1:
            return [self.mask, 'Question:', text_a, text_b], []
        elif self.pattern_id == 2:
            return [text_a, '(', self.mask, ')', text_b], []
        elif self.pattern_id == 3:
            return [text_a, text_b, '(', self.mask, ')'], []
        elif self.pattern_id == 4:
            return ['[ Category:', self.mask, ']', text_a, text_b], []
        elif self.pattern_id == 5:
            return [self.mask, '-', text_a, text_b], []
        else:
            raise ValueError("No pattern implemented for id {}".format(self.pattern_id))

    def verbalize(self, label) -> List[str]:
        return YahooPVP.VERBALIZER[label]


class MnliPVP(PVP):
    VERBALIZER_A = {
        "contradiction": ["Wrong"],
        "entailment": ["Right"],
        "neutral": ["Maybe"]
    }
    VERBALIZER_B = {
        "contradiction": ["No"],
        "entailment": ["Yes"],
        "neutral": ["Maybe"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        text_a = self.shortenable(self.remove_final_punc(example.text_a))
        text_b = self.shortenable(example.text_b)

        if self.pattern_id == 0 or self.pattern_id == 2:
            return ['"', text_a, '" ?'], [self.mask, ', "', text_b, '"']
        elif self.pattern_id == 1 or self.pattern_id == 3:
            return [text_a, '?'], [self.mask, ',', text_b]

    def verbalize(self, label) -> List[str]:
        if self.pattern_id == 0 or self.pattern_id == 1:
            return MnliPVP.VERBALIZER_A[label]
        return MnliPVP.VERBALIZER_B[label]


class YelpPolarityPVP(PVP):
    VERBALIZER = {
        "1": ["bad"],
        "2": ["good"]
    }

    def get_parts(self, example: InputExample) -> FilledPattern:
        text = self.shortenable(example.text_a)

        if self.pattern_id == 0:
            return ['It was', self.mask, '.', text], []
        elif self.pattern_id == 1:
            return [text, '. All in all, it was', self.mask, '.'], []
        elif self.pattern_id == 2:
            return ['Just', self.mask, "!"], [text]
        elif self.pattern_id == 3:
            return [text], ['In summary, the restaurant is', self.mask, '.']
        else:
            raise ValueError("No pattern implemented for id {}".format(self.pattern_id))

    def verbalize(self, label) -> List[str]:
        return YelpPolarityPVP.VERBALIZER[label]


class YelpFullPVP(YelpPolarityPVP):
    VERBALIZER = {
        "1": ["terrible"],
        "2": ["bad"],
        "3": ["okay"],
        "4": ["good"],
        "5": ["great"]
    }

    def verbalize(self, label) -> List[str]:
        return YelpFullPVP.VERBALIZER[label]


class XStancePVP(PVP):
    VERBALIZERS = {
        'en': {"FAVOR": ["Yes"], "AGAINST": ["No"]},
        'de': {"FAVOR": ["Ja"], "AGAINST": ["Nein"]},
        'fr': {"FAVOR": ["Oui"], "AGAINST": ["Non"]}
    }

    def get_parts(self, example: InputExample) -> FilledPattern:

        text_a = self.shortenable(example.text_a)
        text_b = self.shortenable(example.text_b)

        if self.pattern_id == 0 or self.pattern_id == 2 or self.pattern_id == 4:
            return ['"', text_a, '"'], [self.mask, '. "', text_b, '"']
        elif self.pattern_id == 1 or self.pattern_id == 3 or self.pattern_id == 5:
            return [text_a], [self.mask, '.', text_b]

    def verbalize(self, label) -> List[str]:
        lang = 'de' if self.pattern_id < 2 else 'en' if self.pattern_id < 4 else 'fr'
        return XStancePVP.VERBALIZERS[lang][label]


def get_verbalization_ids(word: str, tokenizer, force_single_token: bool) -> Union[int, List[int]]:
    """
    Get the token ids corresponding to a verbalization

    :param word: the verbalization
    :param tokenizer: the tokenizer to use
    :param force_single_token: whether it should be enforced that the verbalization corresponds to a single token.
           If set to true, this method returns a single int instead of a list and throws an error if the word
           corresponds to multiple tokens.
    :return: either the list of token ids or the single token id corresponding to this word
    """
    ids = tokenizer.EncodeAsIds(word)
    if not force_single_token:
        return ids
    assert len(ids) == 1, \
        f'Verbalization "{word}" does not correspond to a single token, got {tokenizer.DecodeIds(ids)}'
    verbalization_id = ids[0]
    assert verbalization_id not in tokenizer.all_special_ids, \
        f'Verbalization {word} is mapped to a special token {tokenizer.IdToToken(verbalization_id)}'
    return verbalization_id


PVPS = {
    'agnews': AgnewsPVP,
    'mnli': MnliPVP,
    'yelp-polarity': YelpPolarityPVP,
    'yelp-full': YelpFullPVP,
    'yahoo': YahooPVP,
    'xstance': XStancePVP,
    'xstance-de': XStancePVP,
    'xstance-fr': XStancePVP,
    'rte': RtePVP,
    'wic': WicPVP,
    'cb': CbPVP,
    'wsc': WscPVP,
    'boolq': BoolQPVP,
    'copa': CopaPVP,
    'multirc': MultiRcPVP,
    'record': RecordPVP,
    'ax-b': RtePVP,
    'ax-g': RtePVP,
}