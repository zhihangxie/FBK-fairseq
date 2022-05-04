# Copyright 2021 FBK

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License
import logging
from functools import lru_cache
from itertools import groupby

import torch
from torch.nn import functional as F

from examples.speech_to_text.tasks.speech_to_text_ctc import SpeechToTextCtcTask
from fairseq.data import BaseWrapperDataset
from fairseq.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("speech_to_text_ctcgen")
class SpeechToTextCtcGenTask(SpeechToTextCtcTask):
    """
    Task for generating the transcripts with the CTC module of an encoder-decoder model.
    """
    use_src_dict_as_target = True

    def build_generator(self, models, args, **kwargs):
        return CTCGenerator(args, self)

    def load_dataset(self, split, epoch=1, combine=False, **kwargs):
        super().load_dataset(split, epoch=epoch, combine=combine, **kwargs)
        # We need to remove the target from the returned dataset,
        # as in this scenario the target is the translation into another language
        # but when we decode the CTC output we are generating the transcript
        # in the source language.
        # As such, we use the source dictionary in the generate for the target,
        # so if we do not remove the target here, it would be printed, generating
        # it with the wrong (source) dictionary, potentially causing also exceptions.
        self.datasets[split] = MaskTargetDataset(self.datasets[split])


class MaskTargetDataset(BaseWrapperDataset):
    def collater(self, samples):
        sample = super().collater(samples)
        sample["target"] = None
        return sample


class CTCBeamEntry:
    def __init__(self, prefix, non_blank_end_logprob, blank_end_logprob):
        self.prefix = prefix
        self.non_blank_end_logprob = non_blank_end_logprob
        self.blank_end_logprob = blank_end_logprob

    @property
    @lru_cache()
    def logprob(self):
        return torch.logsumexp(torch.stack([self.non_blank_end_logprob, self.blank_end_logprob]), -1)

    def normalized_logprob(self, len_penalty):
        return self.logprob / (1 + len(self.prefix)) ** len_penalty

    def __last_idx(self, blank_idx=0):
        if len(self.prefix) > 0:
            return self.prefix[-1]
        return blank_idx

    def add_next(
            self,
            lprobs: torch.Tensor,
            tokens_to_be_considered: set,
            blank_idx=0,
            beam_size=10,):
        """
        Returns the prefixes generated by the current one for the next
        step of the search. The prefixes include:
         - the current prefix with updated probabilities;
         - the `beam_size` most likely prefixes starting from this one;
         - the prefixes obtained adding each of the `tokens_to_be_considered`
           (i.e. prefixes already in the beam that can be generated from the
           current prefix by adding a token).
        """
        last_idx = self.__last_idx(blank_idx=blank_idx)
        # Update probabilities for this prefix:
        # - ending with blank is the prob of this prefix * prob of blank
        # - ending with noblank is the nonblank prob * prob of the same last idx
        # as consecutive equal predictions are merged by the CTC
        next_beam_entries = [CTCBeamEntry(
            self.prefix,
            self.non_blank_end_logprob + lprobs[last_idx],
            self.logprob + lprobs[blank_idx]
        )]
        # Take 2 more in case we have blank and last_idx
        _, top_indices = torch.topk(lprobs, beam_size + 2)
        i = 0
        # We take the current prefix updated and the `beam_size`
        # most likely prefixes that can be derived from it
        while len(next_beam_entries) <= beam_size:
            curr_idx = top_indices[i].item()
            i += 1
            # Ignore blank and last_idx (they are managed separately)
            if curr_idx == blank_idx or curr_idx == last_idx:
                continue
            next_beam_entries.append(CTCBeamEntry(
                self.prefix + [curr_idx],
                self.logprob + lprobs[curr_idx],
                torch.tensor(-float('inf')).to(lprobs.device)
            ))
            if curr_idx in tokens_to_be_considered:
                tokens_to_be_considered.remove(curr_idx)
        for idx in tokens_to_be_considered:
            next_beam_entries.append(CTCBeamEntry(
                self.prefix + [idx],
                self.logprob + lprobs[idx],
                torch.tensor(-float('inf')).to(lprobs.device)
            ))
        return next_beam_entries


class CTCGenerator(object):
    def __init__(self, args, task):
        self.tgt_dict = task.source_dictionary
        self.vocab_size = len(self.tgt_dict)
        self.beam_size = args.beam
        self.blank = self.tgt_dict.index("<ctc_blank>")
        self.eos = self.tgt_dict.eos
        # The default value for lenpen is 1.0. In CTC decoding length penalty
        # usually is not needed so we encourage to set it to 0.0
        self.len_penalty = getattr(args, "lenpen", 1.0)
        if self.len_penalty == 0.0:
            self.penalty_aware_score = lambda x: x.logprob
        else:
            self.penalty_aware_score = lambda x: x.normalized_logprob(self.len_penalty)
        if getattr(args, "sampling", False):
            self.search = self.best_path_decoding
        else:
            self.search = self.beam_search

    def best_path_decoding(self, prob_ctc, ctc_lengths):
        """
        An implementation of the best path decoding algorithm, which consists in
        taking the most likely prediction for each time step, aka greedy search.
        See:
            A. Graves, et al. "A novel connectionist system for unconstrained handwriting recognition."
            IEEE Transactions on Pattern Analysis and Ma- chine Intelligence, 2009.
        """
        batch_predicted = []
        prob_ctc = F.softmax(prob_ctc, dim=-1).transpose(0, 1)  # from T x B x D to B x T x D
        for b in range(prob_ctc.shape[0]):
            highest_probs, most_likely_elements = prob_ctc[b][: ctc_lengths[b]].max(-1)
            predicted_tokens = [p[0] for p in groupby(most_likely_elements.tolist()) if p[0] != self.blank]
            batch_predicted.append({
                'tokens': torch.LongTensor(predicted_tokens),
                'score': highest_probs.prod(),
                'attention': None,
                'alignment': None,
                'positional_scores': highest_probs,
            })

    def beam_search(self, prob_ctc, ctc_lengths):
        """
        An implementation of the beam search algorithm on the CTC output.
        The implementation follows the specification by:
            K. Hwang and W. Sung. "Character-level incremental speech recognition with recurrent neural networks."
            IEEE International Conference on Acoustics, Speech and Signal Processing, 2016.
        """
        batch_predicted = []
        for batch_idx in range(prob_ctc.shape[0]):
            beam = [CTCBeamEntry(
                [],
                torch.tensor(-float('inf')).to(prob_ctc.device),
                torch.tensor(0.0).to(prob_ctc.device))]
            for n in range(ctc_lengths[batch_idx]):
                new_beam = []
                for entry in beam:
                    # if the current prefix can become another prefix in the beam,
                    # i.e. it is the same prefix except for the last token
                    # which is missing, we need to ensure we include this sequence
                    # that is deduplicated later to obtain the overall probability
                    # estimate for the prefix
                    tokens_to_be_considered = set()
                    for x in beam:
                        if entry.prefix == x.prefix[:-1] and len(x.prefix) > 0:
                            tokens_to_be_considered.add(x.prefix[-1])
                    new_beam.extend(entry.add_next(
                        prob_ctc[batch_idx, n, :],
                        tokens_to_be_considered,
                        blank_idx=self.blank,
                        beam_size=self.beam_size))
                beam = sorted(
                    self.deduplicate(new_beam),
                    key=self.penalty_aware_score,
                    reverse=True)[:self.beam_size]

            # Return the ordered batch by the most likely
            batch_predicted.append([{
                'tokens': torch.LongTensor(beam_entry.prefix),
                'score': self.penalty_aware_score(beam_entry),
                'attention': None,
                'alignment': None,
                'positional_scores': torch.FloatTensor([]),
            } for beam_entry in beam])
        return batch_predicted

    def generate(self, models, sample, **kwargs):
        encoder_input = {
            k: v for k, v in sample["net_input"].items() if k != "prev_output_tokens"
        }
        assert len(models) == 1, "Model ensemble for CTC is not yet supported"
        encoder_out = models[0].encoder(**encoder_input, return_all_hiddens=True)
        ctc_features = encoder_out["ctc_out"]
        ctc_lengths = encoder_out["ctc_lengths"]
        prob_ctc = F.log_softmax(ctc_features, dim=-1).to("cpu")
        if not getattr(ctc_features, "batch_first", False):
            prob_ctc = prob_ctc.transpose(0, 1)
        return self.search(prob_ctc, ctc_lengths)

    @staticmethod
    def deduplicate(beam_with_dup):
        """
        Collapses duplicates prefixes into the same, merging (i.e. summing) the probabilities.
        """
        dedup_dict = {}
        for e in beam_with_dup:
            if tuple(e.prefix) in dedup_dict:
                dedup_dict[tuple(e.prefix)].append(e)
            else:
                dedup_dict[tuple(e.prefix)] = [e]
        new_beam = []
        for same_entries in dedup_dict.values():
            if len(same_entries) == 1:
                new_beam.append(same_entries[0])
            else:
                new_beam.append(CTCBeamEntry(
                    same_entries[0].prefix,
                    torch.logsumexp(torch.stack([x.non_blank_end_logprob for x in same_entries]), -1),
                    torch.logsumexp(torch.stack([x.blank_end_logprob for x in same_entries]), -1),
                ))
        return new_beam
