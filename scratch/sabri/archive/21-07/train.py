import os
from functools import partial
from typing import Collection, Mapping, Sequence, Tuple, Union

import meerkat as mk
import numpy as np
import pandas as pd
import ray
import terra
import torch
import torch.nn as nn
from ray import tune

from domino.evaluate.linear import CorrelationImpossibleError, induce_correlation
from domino.utils import nested_getattr
from domino.vision import Classifier, score, train


@terra.Task
def train_model(
    dp: mk.DataPanel,
    target_correlate: Tuple[str],
    corr: float,
    num_examples: int,
    input_column: str = "input",
    id_column: str = "file",
    run_dir: str = None,
    **kwargs,
):
    # set seed
    target, correlate = target_correlate

    metadata = {
        "target": target,
        "correlate": correlate,
        "corr": corr,
        "num_examples": num_examples,
        "run_id": int(os.path.basename(run_dir)),
    }
    try:
        indices = induce_correlation(
            dp,
            corr=corr,
            attr_a=target,
            attr_b=correlate,
            n=num_examples,
            match_mu=True,
        )
    except CorrelationImpossibleError as e:
        print(e)
        return

    train(
        dp=dp.lz[indices],
        input_column=input_column,
        id_column=id_column,
        target_column=target,
        run_dir=run_dir,
        wandb_config=metadata,
        **kwargs,
    )

    return metadata


@terra.Task
def train_linear_slices(
    dp_run_id: int,
    target_correlate_pairs: Sequence[Tuple[str]],
    input_column: str,
    id_column: str,
    max_corr: float = 0.8,
    num_corrs: int = 9,
    num_examples: int = 3e4,
    num_samples: float = 1,
    run_dir: str = None,
    **kwargs,
):
    def _train_model(config):
        import meerkat.contrib.mimic

        return train_model(
            terra.out(dp_run_id),
            input_column=input_column,
            id_column=id_column,
            pbar=False,
            **config,
            num_examples=num_examples,
            **kwargs,
        )

    ray.init(num_gpus=4, num_cpus=32)
    analysis = tune.run(
        _train_model,
        config={
            "corr": tune.grid_search(list(np.linspace(0, max_corr, num_corrs))),
            "target_correlate": tune.grid_search(list(target_correlate_pairs)),
        },
        num_samples=num_samples,
        resources_per_trial={"gpu": 1},
    )
    return analysis.dataframe()


@terra.Task
def score_model(
    dp: mk.DataPanel,
    model: Classifier,
    target: str,
    correlate: str,
    corr: float,
    num_examples: int,
    split: str,
    input_column: str = "input",
    id_column: str = "file",
    layers: Union[nn.Module, Mapping[str, nn.Module]] = None,
    reduction_fns: Sequence[str] = None,
    run_dir: str = None,
    **kwargs,
):

    if layers is not None:
        layers = {name: nested_getattr(model, layer) for name, layer in layers.items()}

    if reduction_fns is not None:
        # get the actual function corresponding to the str passed in
        def _get_reduction_fn(reduction_name):
            if reduction_name == "max":
                reduction_fn = partial(torch.mean, dim=[-1, -2])
            elif reduction_name == "mean":
                reduction_fn = partial(torch.mean, dim=[-1, -2])
            else:
                raise ValueError(f"reduction_fn {reduction_name} not supported.")
            reduction_fn.__name__ = reduction_name
            return reduction_fn

        reduction_fns = list(map(_get_reduction_fn, reduction_fns))

    # set seed
    metadata = {
        "target": target,
        "correlate": correlate,
        "corr": corr,
        "num_examples": num_examples,
        "run_id": int(os.path.basename(run_dir)),
    }

    split_mask = (
        (dp["split"].data == split)
        if isinstance(split, str)
        else np.isin(dp["split"].data, split)
    )
    score_dp = score(
        model,
        dp=dp.lz[split_mask],
        input_column=input_column,
        id_column=id_column,
        target_column=target,
        run_dir=run_dir,
        layers=layers,
        wandb_config=metadata,
        reduction_fns=reduction_fns,
        **kwargs,
    )
    # get new columns and some identifying columns
    cols = [id_column, target, correlate, "split"] + list(
        set(score_dp.columns) - set(dp.columns)
    )
    return score_dp[cols], metadata


@terra.Task
def score_linear_slices(
    dp_run_id: int,
    model_df: pd.DataFrame,
    num_samples: float = 1,
    split: Union[str, Collection[str]] = "test",
    layers: Union[nn.Module, Mapping[str, str]] = None,
    reduction_fns: Sequence[str] = None,
    num_gpus: int = 1,
    num_cpus: int = 8,
    run_dir: str = None,
    **kwargs,
):
    def _score_model(config):
        import meerkat.contrib.mimic  # required otherwise we get a yaml import error

        args = config["args"]
        args["model"] = terra.get_artifacts(args.pop("run_id"), "best_chkpt")["model"]
        _, metadata = score_model(
            terra.out(dp_run_id),
            split=split,
            layers=layers,
            pbar=False,
            reduction_fns=reduction_fns,
            **args,
            **kwargs,
        )
        return metadata

    ray.init(num_gpus=num_gpus, num_cpus=num_cpus)
    analysis = tune.run(
        _score_model,
        config={
            "args": tune.grid_search(
                model_df[
                    ["run_id", "target", "correlate", "corr", "num_examples"]
                ].to_dict("records")
            )
        },
        num_samples=num_samples,
        resources_per_trial={"gpu": 1},
    )
    return analysis.dataframe()