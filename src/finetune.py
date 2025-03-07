# Copyright (c) Meta Platforms, Inc. and affiliates
# All rights reserved.
#
# This source code is licensed under the license found in the
# MIT_LICENSE file in the root directory of this source tree.

import argparse
import logging
import os
from pathlib import Path

import torch
from fairseq2.models.nllb.tokenizer import NllbTokenizer

from seamless_communication.cli.m4t.finetune import dataloader, dist_utils, trainer
from seamless_communication.models.unity import (
    UnitTokenizer,
    UnitYModel,
    load_unity_model,
    load_unity_text_tokenizer,
    load_unity_unit_tokenizer,
)
from seamless_communication.models.unity.t2u_builder import (
    create_unity_t2u_model, _base_nar, _medium_t2u

)

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s %(levelname)s -- %(name)s.{os.getpid()}: %(message)s",
)

logger = logging.getLogger("finetune")


def init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Example finetuning script for M4T models"
    )
    parser.add_argument(
        "--train_dataset",
        type=Path,
        required=True,
        help="Path to manifest with train samples",
    )
    parser.add_argument(
        "--t2u_model_path",
        type=Path,
        default="default",
        help="path for pretrained T2U model",
    )
    parser.add_argument(
        "--eval_dataset",
        type=Path,
        required=True,
        help="Path to manifest with eval samples",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default="seamlessM4T_medium",
        help="Base model name (`seamlessM4T_medium`, `seamlessM4T_large`)",
    )
    parser.add_argument(
        "--save_model_to",
        type=Path,
        required=True,
        help="Path to save best finetuned model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=2343,
        help="Randomizer seed value",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=5,
        help="Batch size for training and evaluation",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=3,
        help=(
            "Set early termination after `patience` number of evaluations "
            "without eval loss improvements"
        ),
    )
    parser.add_argument(
        "--max_epochs",
        type=int,
        default=10,
        help=("Max number of training epochs"),
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-7,
        help=("Finetuning learning rate"),
    )
    parser.add_argument(
        "--warmup_steps",
        type=int,
        default=100,
        help=("Number of steps with linearly increasing learning rate"),
    )
    parser.add_argument(
        "--eval_steps",
        type=int,
        default=50,
        help=("Get eval loss after each `eval_steps` training steps "),
    )
    parser.add_argument(
        "--log_steps",
        type=int,
        default=10,
        help=("Log inner loss after each `log_steps` training steps"),
    )
    parser.add_argument(
        "--mode",
        type=trainer.FinetuneMode,
        choices=list(trainer.FinetuneMode),
        default=trainer.FinetuneMode.SPEECH_TO_TEXT,
        help=(
            "* `SPEECH_TO_SPEECH` -- finetune S2T and T2U parts of the model; "
            "* `TEXT_TO_SPEECH` -- finetune only T2U; "
            "* `SPEECH_TO_TEXT` -- finetune only S2T"
        ),
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help=("Device to fine-tune on. See `torch.device`."),
    )
    return parser


def main() -> None:
    args = init_parser().parse_args()
    dist_utils.init_distributed([logger, trainer.logger])
    text_tokenizer: NllbTokenizer = load_unity_text_tokenizer(args.model_name)
    unit_tokenizer: UnitTokenizer = load_unity_unit_tokenizer(args.model_name)
    finetune_params = trainer.FinetuneParams(
        finetune_mode=args.mode,
        save_model_path=args.save_model_to,
        device=torch.device(args.device),
        float_dtype=torch.float16 if torch.device(args.device).type != "cpu" else torch.bfloat16,
        train_batch_size=args.batch_size,
        eval_batch_size=args.batch_size,
        patience=args.patience,
        max_epochs=args.max_epochs,
        learning_rate=args.learning_rate,
        warmup_steps=args.warmup_steps,
        eval_steps=args.eval_steps,
        log_steps=args.log_steps,
    )
    print(f"Finetune params: {finetune_params}")
    model: UnitYModel = load_unity_model(
        args.model_name, device=torch.device("cpu"), dtype=torch.float32
    )
    assert model.target_vocab_info == text_tokenizer.vocab_info
    # (optional) delete unused params to reduce GPU memory consumption
    # if (finetune_params.finetune_mode == trainer.FinetuneMode.SPEECH_TO_TEXT
    #         and model.t2u_model is not None):
    #     model.t2u_model = None

    #### for TEXT_TO_SPEECH
    # بررسی وجود pth مربوطه
    # if str(args.t2u_model_path) == 'default':
    #     model.t2u_model = create_unity_t2u_model(_medium_t2u(), args.device, finetune_params.float_dtype)
    #     print(f"set default T2U model*************************")
    # else:
    #     my_trained = torch.load(f'{args.t2u_model_path}', map_location=torch.device('cpu'))
    #     model.t2u_model.state_dict = my_trained['model_state_dict']
    #     print(f"set pretrained T2U model@@@@@@@@@@@@@@@@@@@@@@@@")
    #     del my_trained

    # if model.text_encoder is not None:
    #     model.text_encoder = None

    model = model.to(finetune_params.device)
    print(f"<{args.model_name}> {model}")

    train_dataloader = dataloader.UnitYDataLoader(
        text_tokenizer=text_tokenizer,
        unit_tokenizer=unit_tokenizer,
        batching_config=dataloader.BatchingConfig(
            batch_size=finetune_params.train_batch_size,
            rank=dist_utils.get_rank(),
            world_size=dist_utils.get_world_size(),
            max_audio_length_sec=15.0,
            float_dtype=finetune_params.float_dtype,
        ),
        dataset_manifest_path=args.train_dataset,
    )
    eval_dataloader = dataloader.UnitYDataLoader(
        text_tokenizer=text_tokenizer,
        unit_tokenizer=unit_tokenizer,
        batching_config=dataloader.BatchingConfig(
            batch_size=finetune_params.eval_batch_size,
            rank=dist_utils.get_rank(),
            world_size=dist_utils.get_world_size(),
            max_audio_length_sec=100.0,
            float_dtype=finetune_params.float_dtype,
        ),
        dataset_manifest_path=args.eval_dataset,
    )
    finetune = trainer.UnitYFinetune(
        model=model,
        params=finetune_params,
        train_data_loader=train_dataloader,
        eval_data_loader=eval_dataloader,
    )
    del args, finetune_params, train_dataloader, eval_dataloader, text_tokenizer, unit_tokenizer, model
    torch.cuda.empty_cache()

    finetune.run()


if __name__ == "__main__":
    main()
