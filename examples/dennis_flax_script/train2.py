"""
Code adapted from the code-search-net example

!! THE CODE IS NOT READY YET !!

Open TODO:
- Save the model
- Evaluate the model if it actually learns sensible embeddings. E.g. evaluate on STS benchmark dataset
- Compare results with PyTorch training script if comparable
"""
import sys
import gzip
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Union
from functools import partial

from sentence_transformers import InputExample

import jax
from jax.config import config
from flax import jax_utils, struct, traverse_util
from flax.training import train_state
from flax.training.common_utils import shard
import jax.numpy as jnp

import optax

from transformers import (
    AutoTokenizer,
    FlaxBertModel,
    FlaxAutoModel,
)

from tqdm.auto import tqdm

from trainer.loss.custom import multiple_negatives_ranking_loss
from trainer.utils.ops import normalize_L2, mean_pooling
from MultiDatasetDataLoader import MultiDatasetDataLoader



# from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast



@dataclass
class TrainingArgs:
    model_id: str = "microsoft/MiniLM-L12-H384-uncased"
    max_epochs: int = 2
    # batch_size: int = 256
    batch_size: int = 16
    seed: int = 42
    lr: float = 2e-5
    init_lr: float = 0
    warmup_steps: int = 500
    weight_decay: float = 1e-2

    input1_maxlen: int = 128
    input2_maxlen: int = 128

    tr_data_files: List[str] = field(
        default_factory=lambda: [
            "data/quora_duplicates.jsonl.gz",
        ]
    )

    steps_per_epoch = 2000
    # batch_size_pairs = 256
    batch_size_pairs = 16
    # batch_size_triplets = 256
    batch_size_triplets = 16
    random_batch_fraction=0.25


###########
# Helpers #
###########
def warmup_and_constant(
    lr,
    init_lr,
    warmup_steps,
):
    warmup_fn = optax.linear_schedule(
        init_value=init_lr,
        end_value=lr,
        transition_steps=warmup_steps,
    )
    constant_fn = optax.constant_schedule(value=lr)
    lr = optax.join_schedules(schedules=[warmup_fn, constant_fn], boundaries=[warmup_steps])
    return lr


def build_tx(
    lr,
    init_lr,
    warmup_steps,
    weight_decay,
):
    def weight_decay_mask(params):
        params = traverse_util.flatten_dict(params)
        mask = {
            k: (v[-1] != "bias" and v[-2:] != ("LayerNorm", "scale"))
            for k, v in params.items()
        }
        return traverse_util.unflatten_dict(mask)
    lr = warmup_and_constant(lr, init_lr, warmup_steps)
    tx = optax.adamw(learning_rate=lr, weight_decay=weight_decay, mask=weight_decay_mask)
    return tx, lr


class TrainState(train_state.TrainState):
    loss_fn: Callable = struct.field(pytree_node=False)
    scheduler_fn: Callable = struct.field(pytree_node=False)

def data_collator(batch, tokenizer):
    texts1 = [e.texts[0] for e in batch]
    texts2 = [e.texts[1] for e in batch]

    model_input1, model_input2 = (
        dict(
            tokenizer(
                text,
                return_tensors="jax",
                max_length=128,
                truncation=True,
                padding=True,
                pad_to_multiple_of=32,
            )
        ) for text in [texts1, texts2]
    )

    return shard(model_input1), shard(model_input2)

@partial(jax.pmap, axis_name="batch")
def train_step(state, model_input1, model_input2, drp_rng):
    train = True
    new_drp_rng, drp_rng = jax.random.split(drp_rng, 2)

    def loss_fn(params, model_input1, model_input2, drp_rng):

        def _forward(model_input):
            attention_mask = model_input["attention_mask"]
            model_output = state.apply_fn(
                **model_input,
                params=params,
                train=train,
                dropout_rng=drp_rng,
            )

            embedding = mean_pooling(model_output, attention_mask)
            embedding = normalize_L2(embedding)

            # gather all the embeddings on same device for calculation loss over global batch
            embedding = jax.lax.all_gather(embedding, axis_name="batch")
            embedding = jnp.reshape(embedding, (-1, embedding.shape[-1]))

            return embedding

        embedding1, embedding2 = _forward(model_input1), _forward(model_input2)
        return state.loss_fn(embedding1, embedding2)

    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(state.params, model_input1, model_input2, drp_rng)
    state = state.apply_gradients(grads=grads)

    step = jax.lax.pmean(state.step, axis_name="batch")
    metrics = {"tr_loss": loss, "lr": state.scheduler_fn(step)}

    return state, metrics, new_drp_rng


def get_batched_dataset(dataset, batch_size, seed=None):
    if seed is not None:
        dataset = dataset.shuffle(seed=seed)
    for i in range(len(dataset) // batch_size):
        batch = dataset[i*batch_size: (i+1)*batch_size]
        yield dict(batch)




def main(args, train_dataloader):
    config.update("jax_enable_x64", True)
    model = FlaxAutoModel.from_pretrained(args.model_id)
    tokenizer = AutoTokenizer.from_pretrained(args.model_id)


    tx_args = {
        "lr": args.lr,
        "init_lr": args.init_lr,
        "warmup_steps": args.warmup_steps,
        "weight_decay": args.weight_decay,
    }
    tx, lr = build_tx(**tx_args)

    state = TrainState.create(
        apply_fn=model.__call__,
        params=model.params,
        tx=tx,
        loss_fn=multiple_negatives_ranking_loss,
        scheduler_fn=lr,
    )
    state = jax_utils.replicate(state)

    rng = jax.random.PRNGKey(args.seed)
    drp_rng = jax.random.split(rng, jax.device_count())

    print("Train steps:", len(train_dataloader))
    loss = None
    for epoch in range(args.max_epochs):
        # training step
        pbar = tqdm(
            train_dataloader,
            total=len(train_dataloader),
        )
        def show_loss(pbar: tqdm, loss: float):
            str_loss = '<unknown>' if loss is None else f'{loss:.6f}'
            pbar.set_description(
                f"Running epoch-{epoch} --> current loss: {str_loss}"
            )
        show_loss(pbar, loss)
        for batch in pbar:
            model_input1, model_input2 = data_collator(batch, tokenizer)
            state, metrics, drp_rng = train_step(state, model_input1, model_input2, drp_rng)
            loss = metrics['tr_loss'].tolist()[0]
            show_loss(pbar, loss)
        # evaluation step
        # for batch in get_batched_dataset(val_dataset, args.batch_size, seed=None):
        #     model_input1, model_input2 = data_collator(batch)
        #     state, metric = val_step(state, model_input1, model_input2)

if __name__ == '__main__':
    args = TrainingArgs()

    datasets = []
    for filepath in sys.argv[1:]:
        filepath = filepath.strip()
        dataset = []

        with gzip.open(filepath, 'rt', encoding='utf8') as fIn:
            for line in fIn:
                data = json.loads(line.strip())

                if not isinstance(data, dict):
                    data = {'guid': None, 'texts': data}

                dataset.append(
                    InputExample(
                        guid=data.get('guid', None),
                        texts=data['texts'],
                    ),
                )
                if len(dataset) >= (args.steps_per_epoch * args.batch_size_pairs * 2):
                    break

        datasets.append(dataset)
        logging.info(f"{filepath}: {len(dataset)}")

    train_dataloader = MultiDatasetDataLoader(
        datasets,
        batch_size_pairs=args.batch_size_pairs,
        batch_size_triplets=args.batch_size_triplets,
        random_batch_fraction=args.random_batch_fraction,
    )

    main(args, train_dataloader)


