from datetime import datetime
from pathlib import Path

import torch
import torch.nn as nn
import torch.optim as optim

import fire
import ignite
import ignite.distributed as idist
import utils
from ignite.contrib.engines import common
from ignite.contrib.handlers import PiecewiseLinear
from ignite.engine import Engine, Events, create_supervised_evaluator
from ignite.handlers import Checkpoint, DiskSaver
from ignite.metrics import Accuracy, Loss
from ignite.utils import manual_seed, setup_logger


def training(local_rank, config):

    rank = idist.get_rank()
    manual_seed(config["seed"] + rank)
    device = idist.device()

    logger = setup_logger(name="CIFAR10-QAT-Training", distributed_rank=local_rank)

    log_basic_info(logger, config)

    output_path = config["output_path"]
    if rank == 0:
        now = datetime.now().strftime("%Y%m%d-%H%M%S")

        folder_name = "{}_backend-{}-{}_{}".format(config["model"], idist.backend(), idist.get_world_size(), now)
        output_path = Path(output_path) / folder_name
        if not output_path.exists():
            output_path.mkdir(parents=True)
        config["output_path"] = output_path.as_posix()
        logger.info("Output path: {}".format(config["output_path"]))

        if "cuda" in device.type:
            config["cuda device name"] = torch.cuda.get_device_name(local_rank)

    # Setup dataflow, model, optimizer, criterion
    train_loader, test_loader = get_dataflow(config)

    config["num_iters_per_epoch"] = len(train_loader)
    model, optimizer, criterion, lr_scheduler = initialize(config)

    # Create trainer for current task
    trainer = create_trainer(model, optimizer, criterion, lr_scheduler, train_loader.sampler, config, logger)

    # Let's now setup evaluator engine to perform model's validation and compute metrics
    metrics = {
        "Accuracy": Accuracy(),
        "Loss": Loss(criterion),
    }

    # We define two evaluators as they wont have exactly similar roles:
    # - `evaluator` will save the best model based on validation score
    evaluator = create_supervised_evaluator(model, metrics=metrics, device=device, non_blocking=True)
    train_evaluator = create_supervised_evaluator(model, metrics=metrics, device=device, non_blocking=True)

    def run_validation(engine):
        epoch = trainer.state.epoch
        state = train_evaluator.run(train_loader)
        log_metrics(logger, epoch, state.times["COMPLETED"], "Train", state.metrics)
        state = evaluator.run(test_loader)
        log_metrics(logger, epoch, state.times["COMPLETED"], "Test", state.metrics)

    trainer.add_event_handler(Events.EPOCH_COMPLETED(every=config["validate_every"]) | Events.COMPLETED, run_validation)

    if rank == 0:
        # Setup TensorBoard logging on trainer and evaluators. Logged values are:
        #  - Training metrics, e.g. running average loss values
        #  - Learning rate
        #  - Evaluation train/test metrics
        evaluators = {"training": train_evaluator, "test": evaluator}
        tb_logger = common.setup_tb_logging(output_path, trainer, optimizer, evaluators=evaluators)

    # Store 3 best models by validation accuracy:
    common.save_best_model_by_val_score(
        output_path=config["output_path"],
        evaluator=evaluator,
        model=model,
        metric_name="Accuracy",
        n_saved=1,
        trainer=trainer,
        tag="test",
    )

    trainer.run(train_loader, max_epochs=config["num_epochs"])

    if rank == 0:
        tb_logger.close()


def run(
    seed=543,
    data_path="/tmp/cifar10",
    output_path="/tmp/output-cifar10/",
    model="resnet18_QAT_8b",
    batch_size=512,
    momentum=0.9,
    weight_decay=1e-4,
    num_workers=12,
    num_epochs=24,
    learning_rate=0.4,
    num_warmup_epochs=4,
    validate_every=3,
    checkpoint_every=200,
    backend=None,
    resume_from=None,
    log_every_iters=15,
    nproc_per_node=None,
    **spawn_kwargs,
):
    """Main entry to train an model on CIFAR10 dataset.

    Args:
        seed (int): random state seed to set. Default, 543.
        data_path (str): input dataset path. Default, "/tmp/cifar10".
        output_path (str): output path. Default, "/tmp/output-cifar10".
        model (str): model name (from torchvision) to setup model to train. Default, "resnet18".
        batch_size (int): total batch size. Default, 512.
        momentum (float): optimizer's momentum. Default, 0.9.
        weight_decay (float): weight decay. Default, 1e-4.
        num_workers (int): number of workers in the data loader. Default, 12.
        num_epochs (int): number of epochs to train the model. Default, 24.
        learning_rate (float): peak of piecewise linear learning rate scheduler. Default, 0.4.
        num_warmup_epochs (int): number of warm-up epochs before learning rate decay. Default, 4.
        validate_every (int): run model's validation every ``validate_every`` epochs. Default, 3.
        checkpoint_every (int): store training checkpoint every ``checkpoint_every`` iterations. Default, 200.
        backend (str, optional): backend to use for distributed configuration. Possible values: None, "nccl", "xla-tpu",
            "gloo" etc. Default, None.
        nproc_per_node (int, optional): optional argument to setup number of processes per node. It is useful,
            when main python process is spawning training as child processes.
        resume_from (str, optional): path to checkpoint to use to resume the training from. Default, None.
        log_every_iters (int): argument to log batch loss every ``log_every_iters`` iterations.
            It can be 0 to disable it. Default, 15.
        **spawn_kwargs: Other kwargs to spawn run in child processes: master_addr, master_port, node_rank, nnodes

    """
    # catch all local parameters
    config = locals()
    config.update(config["spawn_kwargs"])
    del config["spawn_kwargs"]

    spawn_kwargs["nproc_per_node"] = nproc_per_node

    with idist.Parallel(backend=backend, **spawn_kwargs) as parallel:
        try:
            parallel.run(training, config)
        except Exception as e:
            raise e


def get_dataflow(config):
    # - Get train/test datasets
    if idist.get_rank() > 0:
        # Ensure that only rank 0 download the dataset
        idist.barrier()

    train_dataset, test_dataset = utils.get_train_test_datasets(config["data_path"])

    if idist.get_rank() == 0:
        # Ensure that only rank 0 download the dataset
        idist.barrier()

    # Setup data loader also adapted to distributed config
    train_loader = idist.auto_dataloader(
        train_dataset, batch_size=config["batch_size"], num_workers=config["num_workers"], shuffle=True, drop_last=True,
    )

    test_loader = idist.auto_dataloader(
        test_dataset, batch_size=2 * config["batch_size"], num_workers=config["num_workers"], shuffle=False,
    )
    return train_loader, test_loader


def initialize(config):
    model = utils.get_model(config["model"])
    model = idist.auto_model(model, find_unused_parameters=True)

    optimizer = optim.SGD(
        model.parameters(),
        lr=config["learning_rate"],
        momentum=config["momentum"],
        weight_decay=config["weight_decay"],
        nesterov=True,
    )
    optimizer = idist.auto_optim(optimizer)
    criterion = nn.CrossEntropyLoss().to(idist.device())

    le = config["num_iters_per_epoch"]
    milestones_values = [
        (0, 0.0),
        (le * config["num_warmup_epochs"], config["learning_rate"]),
        (le * config["num_epochs"], 0.0),
    ]
    lr_scheduler = PiecewiseLinear(optimizer, param_name="lr", milestones_values=milestones_values)

    return model, optimizer, criterion, lr_scheduler


def log_metrics(logger, epoch, elapsed, tag, metrics):
    metrics_output = "\n".join([f"\t{k}: {v}" for k, v in metrics.items()])
    logger.info(f"\nEpoch {epoch} - Time taken (seconds) : {elapsed:.02f} - {tag} metrics:\n {metrics_output}")


def log_basic_info(logger, config):
    logger.info("Quantization Aware Training {} on CIFAR10".format(config["model"]))
    logger.info("- PyTorch version: {}".format(torch.__version__))
    logger.info("- Ignite version: {}".format(ignite.__version__))

    logger.info("\n")
    logger.info("Configuration:")
    for key, value in config.items():
        logger.info("\t{}: {}".format(key, value))
    logger.info("\n")

    if idist.get_world_size() > 1:
        logger.info("\nDistributed setting:")
        logger.info("\tbackend: {}".format(idist.backend()))
        logger.info("\tworld size: {}".format(idist.get_world_size()))
        logger.info("\n")


def create_trainer(model, optimizer, criterion, lr_scheduler, train_sampler, config, logger):

    device = idist.device()

    # Setup Ignite trainer:
    # - let's define training step
    # - add other common handlers:
    #    - TerminateOnNan,
    #    - handler to setup learning rate scheduling,
    #    - ModelCheckpoint
    #    - RunningAverage` on `train_step` output
    #    - Two progress bars on epochs and optionally on iterations

    def train_step(engine, batch):

        x, y = batch[0], batch[1]

        if x.device != device:
            x = x.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)

        model.train()
        y_pred = model(x)
        loss = criterion(y_pred, y)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        return {
            "batch loss": loss.item(),
        }

    trainer = Engine(train_step)
    trainer.logger = logger

    to_save = {"trainer": trainer, "model": model, "optimizer": optimizer, "lr_scheduler": lr_scheduler}
    metric_names = [
        "batch loss",
    ]

    common.setup_common_training_handlers(
        trainer=trainer,
        train_sampler=train_sampler,
        to_save=to_save,
        save_every_iters=config["checkpoint_every"],
        output_path=config["output_path"],
        lr_scheduler=lr_scheduler,
        output_names=metric_names if config["log_every_iters"] > 0 else None,
        with_pbars=False,
        clear_cuda_cache=False,
    )

    resume_from = config["resume_from"]
    if resume_from is not None:
        checkpoint_fp = Path(resume_from)
        assert checkpoint_fp.exists(), "Checkpoint '{}' is not found".format(checkpoint_fp.as_posix())
        logger.info("Resume from a checkpoint: {}".format(checkpoint_fp.as_posix()))
        checkpoint = torch.load(checkpoint_fp.as_posix(), map_location="cpu")
        Checkpoint.load_objects(to_load=to_save, checkpoint=checkpoint)

    return trainer


if __name__ == "__main__":
    fire.Fire({"run": run})