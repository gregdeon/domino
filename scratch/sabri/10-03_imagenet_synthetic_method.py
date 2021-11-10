from typing import List

import nltk
import numpy as np
import psutil
import ray
import torch
from ray import tune
from torch._C import Value

from domino.data.imagenet import get_imagenet_dp
from domino.emb import embed_images
from domino.emb.clip import (
    CELEBA_GENDER_PHRASE_TEMPLATES,
    CELEBA_PHRASE_TEMPLATES,
    embed_phrases,
    embed_words,
    generate_phrases,
    get_wiki_words,
)
from domino.evaluate import run_sdms, score_sdm_explanations, score_sdms
from domino.pipelines.utils import parse_pipeline_args
from domino.sdm import (
    ConfusionSDM,
    GeorgeSDM,
    MixtureModelSDM,
    MultiaccuracySDM,
    SpotlightSDM,
)
from domino.slices import collect_settings
from domino.slices.abstract import concat_settings, random_filter_settings
from domino.train import (
    filter_settings,
    score_settings,
    synthetic_score_settings,
    train_settings,
)
from domino.utils import split_dp

# support for splitting up the job among multiple worker machines
args = parse_pipeline_args()
worker_idx, num_workers = args.worker_idx, args.num_workers
print(f"{worker_idx=}, {num_workers=}")

NUM_GPUS = torch.cuda.device_count()
NUM_CPUS = min(psutil.cpu_count(), 64)
print(f"Found {NUM_GPUS=}, {NUM_CPUS=}")
ray.init(num_gpus=NUM_GPUS, num_cpus=NUM_CPUS, ignore_reinit_error=True)

# simpler to install earlier on to avoid race condition with train
try:
    nltk.data.find("corpora/wordnet")
except LookupError:
    nltk.download("wordnet")

data_dp = get_imagenet_dp()

split = split_dp(dp=data_dp, split_on="image_id")


words_dp = get_wiki_words(top_k=10_000, eng_only=True)
words_dp = embed_words(words_dp=words_dp)

embs = {
    "bit": embed_images(
        emb_type="bit",
        dp=data_dp,
        split_dp=split,
        splits=["valid", "test"],
        img_column="image",
        num_workers=7,
        mmap=True,
    ),
    # "imagenet": embed_images(
    #     emb_type="imagenet",
    #     model="resnet18",
    #     dp=data_dp,
    #     split_dp=split,
    #     layers={"emb": "layer4"},
    #     splits=["valid", "test"],
    #     img_column="image",
    #     num_workers=7,
    #     mmap=True,
    # ),
    "random": embed_images(
        emb_type="imagenet",
        dp=data_dp,
        split_dp=split,
        layers={"emb": "layer4"},
        splits=["valid", "test"],
        img_column="image",
        num_workers=7,
        mmap=True,
        model="resnet50_random",
    ),
    "clip": embed_images(
        emb_type="clip",
        dp=data_dp,
        split_dp=split,
        splits=["valid", "test"],
        img_column="image",
        num_workers=7,
        mmap=True,
    ),
}

setting_dp = concat_settings(
    [
        # collect_settings(
        #     dataset="imagenet",
        #     slice_category="rare",
        #     data_dp=data_dp,
        #     num_slices=1,
        #     words_dp=words_dp,
        #     min_slice_frac=0.03,
        #     max_slice_frac=0.03,
        #     n=30_000,
        # ),
        # collect_settings(
        #     dataset="imagenet",
        #     slice_category="noisy_label",
        #     data_dp=data_dp,
        #     num_slices=1,
        #     words_dp=words_dp,
        #     min_error_rate=0.3,
        #     max_error_rate=0.3,
        #     n=30_000,
        # ),
        collect_settings(
            dataset="imagenet",
            slice_category="rare",
            data_dp=data_dp,
            num_slices=1,
            num_frac=4,
            words_dp=words_dp,
            min_slice_frac=0.01,
            max_slice_frac=0.05,
            n=30_000,
        ),
        collect_settings(
            dataset="imagenet",
            slice_category="noisy_label",
            data_dp=data_dp,
            num_samples=4,
            words_dp=words_dp,
            min_error_rate=0.1,
            max_error_rate=0.4,
            n=30_000,
        ),
    ]
)
print(len(setting_dp.load()))

if args.sanity:
    # filter
    setting_dp = random_filter_settings(setting_dp, subset_size=64)


if args.synthetic:
    setting_dp = synthetic_score_settings(
        setting_dp=setting_dp,
        data_dp=data_dp,
        split_dp=split,
        synthetic_kwargs={
            "sensitivity": 0.75,
            "slice_sensitivities": 0.4,
            "specificity": 0.75,
            "slice_specificities": 0.4,
        },
    )
else:
    raise ValueError

common_config = {
    "n_slices": 5,
    "emb": tune.grid_search(
        [
            # ("random", "emb"),
            # ("bit", "body"),
            ("clip", "emb"),
            # passing None for emb group tells run_sdms that the embedding is in
            # the score_dp – this for the model embeddings
            # (None, "layer4"),
        ]
    ),
    "xmodal_emb": "emb",
}
setting_dp = run_sdms(
    setting_dp=setting_dp,
    emb_dp=embs,
    xmodal_emb_dp=embs["clip"],
    word_dp=words_dp,
    sdm_config=[
        {
            "sdm_class": SpotlightSDM,
            "sdm_config": {
                "learning_rate": 1e-3,
                "device": "cpu",
                **common_config,
            },
        },
        {
            "sdm_class": MultiaccuracySDM,
            "sdm_config": {
                **common_config,
            },
        },
        {
            "sdm_class": GeorgeSDM,
            "sdm_config": {
                **common_config,
            },
        },
        {
            "sdm_class": MixtureModelSDM,
            "sdm_config": {
                "weight_y_log_likelihood": 10,
                **common_config,
            },
        },
        {
            "sdm_class": ConfusionSDM,
            "sdm_config": {
                **common_config,
            },
        },
    ],
    skip_terra_cache=True,
)


slices_df = score_sdms(
    setting_dp=setting_dp,
    spec_columns=["emb_group", "alpha", "sdm_class"],
    skip_terra_cache=True,
)
# slices_df = score_sdm_explanations(setting_dp=setting_dp)