import json
import logging
import os.path
from collections import defaultdict

import datasets
from datasets.fingerprint import Hasher
from datasets import Dataset
from tqdm import tqdm

from utilities import util, consts
from utilities.collate import LeftOversCollator, PadCollator

logger = logging.getLogger(__name__)


def add_speaker_information(tokens, speakers):
    token_to_new_token_map = []
    new_token_to_token_map = []
    new_tokens = []
    last_speaker = None

    for idx, (token, speaker) in enumerate(zip(tokens, speakers)):
        if last_speaker != speaker:
            new_tokens += [consts.SPEAKER_START, speaker, consts.SPEAKER_END]
            new_token_to_token_map += [None, None, None]
            last_speaker = speaker
        token_to_new_token_map.append(len(new_tokens))
        new_token_to_token_map.append(idx)
        new_tokens.append(token)

    return new_tokens, token_to_new_token_map, new_token_to_token_map


def _tokenize(tokenizer, tokens, clusters, speakers):
    new_tokens, token_to_new_token_map, new_token_to_token_map = tokens, [], []
    if speakers:
        new_tokens, token_to_new_token_map, new_token_to_token_map = add_speaker_information(tokens, speakers)
        for cluster in clusters:
            for start, end in cluster:
                assert tokens[start:end + 1] == new_tokens[token_to_new_token_map[start]:token_to_new_token_map[end] + 1]

    encoded_text = tokenizer(
        new_tokens, add_special_tokens=True, is_split_into_words=True,
        return_length=True, return_attention_mask=False
    )

    # shifting clusters indices to align with bpe tokens
    new_clusters = [[(encoded_text.word_to_tokens(token_to_new_token_map[start]).start,
                      encoded_text.word_to_tokens(token_to_new_token_map[end]).end - 1)
                     for start, end in cluster] for cluster in clusters]

    return {'tokens': tokens,
            'input_ids': encoded_text['input_ids'],
            'length': encoded_text['length'][0],

            'gold_clusters': new_clusters,
            # tokens to tokens + speakers
            'new_token_map': new_token_to_token_map,
            # tokens + speakers to bpe
            'subtoken_map': encoded_text.word_ids(),
            }


def encode(example, tokenizer):
    encoded_example = _tokenize(tokenizer, example['tokens'], example['clusters'], example['speakers'])

    gold_clusters = encoded_example['gold_clusters']
    encoded_example['num_clusters'] = len(gold_clusters) if gold_clusters else 0
    encoded_example['max_cluster_size'] = max(len(c) for c in gold_clusters) if gold_clusters else 0

    return encoded_example


def prepare_for_encode(example, nlp):
    if 'tokens' in example and example['tokens']:
        pass
    elif 'sentences' in example and example['sentences']:
        # Assume sentences already tokenized.
        # This is just for OntoNotes. please avoid using 'sentences' and use 'text' or 'tokens'
        example['tokens'] = util.flatten(example['sentences'])
        example['speakers'] = util.flatten(example['speakers'])
    elif 'text' in example and example['text']:
        example['tokens'] = [tok.text for tok in nlp(example['text'])]
    else:
        raise ValueError(f"Example is empty: {example}")

    return example


def create(file, tokenizer, nlp):
    def read_jsonlines(file):
        with open(file, 'r') as f:
            for i, line in enumerate(f):
                doc = json.loads(line)
                if "text" not in doc and "tokens" not in doc and "sentences" not in doc:
                    raise ValueError(f'The jsonlines should contains at lt least "text", "sentences" or "tokens" field')
                if "doc_key" not in doc:
                    doc["doc_key"] = str(i)
                if "text" not in doc:
                    doc["text"] = ""
                if "sentences" not in doc:
                    doc["sentences"] = []
                if "tokens" not in doc:
                    doc["tokens"] = []
                if "speakers" not in doc:
                    doc["speakers"] = []
                if "clusters" not in doc:
                    doc["clusters"] = []
                yield doc

    features = datasets.Features(
        {
            "doc_key": datasets.Value("string"),
            "text": datasets.Value("string"),
            "sentences": datasets.Sequence(datasets.Sequence((datasets.Value("int64")))),
            "tokens": datasets.Sequence(datasets.Value("string")),
            "speakers": datasets.Sequence(datasets.Value("string")),
            "clusters": datasets.Sequence(datasets.Sequence((datasets.Value("int64")))),
        }
    )

    dataset = Dataset.from_generator(read_jsonlines, features=features, gen_kwargs={'file': file})
    dataset = dataset.map(prepare_for_encode, batched=False, fn_kwargs={'nlp': nlp})
    dataset = dataset.map(encode, batched=False, fn_kwargs={'tokenizer': tokenizer})

    return dataset


def create_batches(sampler, dataset_files, cache_dir='cache'):
    key = Hasher.hash(dataset_files)
    if isinstance(sampler.collator, LeftOversCollator):
        key += '_segment_collator'
    elif isinstance(sampler.collator, PadCollator):
        key += '_longformer_collator'
    else:
        raise NotImplementedError('this collator not implemented!')

    cache_key = Hasher.hash(key)
    dataset_path = os.path.join(cache_dir, cache_key)

    try:
        batches = datasets.load_from_disk(dataset_path)
        logger.info(f'Batches restored from: {dataset_path}')
    except FileNotFoundError:
        logger.info(f'Creating batches for {len(sampler.dataset)} examples...')

        # huggingface dataset cannot save tensors. so we will save lists and on train loop transform to tensors.
        batches_dict = defaultdict(lambda: [])

        for i, batch in enumerate(tqdm(sampler)):
            for k, v in batch.items():
                batches_dict[k].append(v)

        batches = Dataset.from_dict(batches_dict)
        logger.info(f'{len(batches)} batches created.')

        logger.info(f'Saving batches to {dataset_path}')
        batches.save_to_disk(dataset_path)

    return batches
