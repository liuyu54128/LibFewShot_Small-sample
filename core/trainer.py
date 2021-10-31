# -*- coding: utf-8 -*-
import datetime
import logging
import os
from logging import getLogger
from time import time

import torch
import yaml
from torch import nn
import torch.distributed as dist

from queue import Queue
import core.model as arch
from core.data import get_dataloader
from core.utils import (
    AverageMeter,
    ModelType,
    SaveType,
    TensorboardWriter,
    count_parameters,
    create_dirs,
    force_symlink,
    get_local_time,
    init_logger,
    init_seed,
    prepare_device,
    save_model,
    get_instance,
    data_prefetcher,
)


class Trainer(object):
    """
    The trainer.

    Build a trainer from config dict, set up optimizer, model, etc. Train/test/val and log.
    """

    def __init__(self, rank, config):
        self.rank = rank
        self.config = config
        self.config["rank"] = rank
        self.distribute = self.config["n_gpu"] > 1
        self.device, self.list_ids = self._init_device(rank, config)
        (
            self.result_path,
            self.log_path,
            self.checkpoints_path,
            self.viz_path,
        ) = self._init_files(config)
        self.writer = self._init_writer(self.viz_path)
        self.train_meter, self.val_meter, self.test_meter = self._init_meter()
        self.logger = self._init_logger(self.log_path)
        self.model, self.model_type = self._init_model(config)
        (
            self.train_loader,
            self.val_loader,
            self.test_loader,
        ) = self._init_dataloader(config)
        self.optimizer, self.scheduler, self.from_epoch = self._init_optim(config)

    def train_loop(self, rank):
        """
        The normal train loop: train-val-test and save model when val-acc increases.
        """
        best_val_acc = float("-inf")
        best_test_acc = float("-inf")
        experiment_begin = time()
        for epoch_idx in range(self.from_epoch + 1, self.config["epoch"]):
            self.logger.info("============ Train on the train set ============")
            if self.distribute:
                # self.train_loader.sampler.set_epoch(epoch_idx)
                if self.model_type == ModelType.FINETUNING:
                    self.train_loader.sampler.set_epoch(epoch_idx)
                else:
                    self.train_loader.batch_sampler.set_epoch(epoch_idx)
            train_acc = self._train(epoch_idx)
            self.logger.info(" * Acc@1 {:.3f} ".format(train_acc)) 
            self.logger.info("============ Validation on the val set ============")
            val_acc = self._validate(epoch_idx, is_test=False)
            self.logger.info(" * Acc@1 {:.3f} Best acc {:.3f}".format(val_acc, best_val_acc))
            self.logger.info("============ Testing on the test set ============")
            test_acc = self._validate(epoch_idx, is_test=True)
            self.logger.info(" * Acc@1 {:.3f} Best acc {:.3f}".format(test_acc, best_test_acc))
            time_scheduler = self._cal_time_scheduler(experiment_begin, epoch_idx)
            self.logger.info(" * Time: {}".format(time_scheduler))
            self.scheduler.step()

            if self.rank == 0:
                if val_acc > best_val_acc:
                    best_val_acc = val_acc
                    best_test_acc = test_acc
                    self._save_model(epoch_idx, SaveType.BEST)

                if epoch_idx != 0 and epoch_idx % self.config["save_interval"] == 0:
                    self._save_model(epoch_idx, SaveType.NORMAL)

                self._save_model(epoch_idx, SaveType.LAST)
        
        if self.rank == 0:
            self.logger.info(
                "End of experiment, took {}".format(
                    str(datetime.timedelta(seconds=int(time() - experiment_begin)))
                )
            )
            self.logger.info("Result DIR: {}".format(self.result_path))

    def _train(self, epoch_idx):
        """
        The train stage.

        Args:
            epoch_idx (int): Epoch index.

        Returns:
            float: Acc.
        """
        self.model.train()

        meter = self.train_meter
        meter.reset()
        episode_size = 1 if self.model_type == ModelType.FINETUNING else self.config["episode_size"]

        end = time()
        log_scale = 1 if self.config["n_gpu"] == 0 else self.config["n_gpu"]            

        prefetcher = data_prefetcher(self.train_loader)
        batch = prefetcher.next()
        batch_idx = -1
        while batch is not None:
            batch_idx += 1
            if self.rank == 0:
                self.writer.set_step(epoch_idx * len(self.train_loader) + batch_idx * episode_size)

            # visualize the weight
            if self.rank==0 and self.config["log_paramerter"]:
                for i, (name, param) in enumerate(self.model.named_parameters()):
                    if "bn" not in name:
                        save_name = name.replace(".", "/")
                        self.writer.add_histogram(save_name, param)

            meter.update("data_time", time() - end)

            # calculate the output
            calc_begin = time()
            if self.distribute:
                output, acc, loss = self.model.module.set_forward_loss(batch)
            else:
                output, acc, loss = self.model.set_forward_loss(batch)

            # compute gradients
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            meter.update("calc_time", time() - calc_begin)

            # measure accuracy and record loss
            meter.update("loss", loss.item())
            meter.update("acc1", acc)

            # measure elapsed time
            meter.update("batch_time", time() - end)

            # print the intermediate results
            
            if self.rank == 0 and (((batch_idx + 1) % self.config["log_interval"] == 0) or (
                batch_idx + 1
            ) * episode_size >= len(self.train_loader)):
                info_str = (
                    "Epoch-({}): [{}/{}]\t"
                    "Time {:.3f} ({:.3f})\t"
                    "Calc {:.3f} ({:.3f})\t"
                    "Data {:.3f} ({:.3f})\t"
                    "Loss {:.3f} ({:.3f})\t"
                    "Acc@1 {:.3f} ({:.3f})".format(
                        epoch_idx,
                        (batch_idx + 1) * episode_size * log_scale,
                        len(self.train_loader) * log_scale,
                        meter.last("batch_time"),
                        meter.avg("batch_time"),
                        meter.last("calc_time"),
                        meter.avg("calc_time"),
                        meter.last("data_time"),
                        meter.avg("data_time"),
                        meter.last("loss"),
                        meter.avg("loss"),
                        meter.last("acc1"),
                        meter.avg("acc1"),
                    )
                )
                self.logger.info(info_str)
            end = time()

            batch = prefetcher.next()

        return meter.avg("acc1")

    def _validate(self, epoch_idx, is_test=False):
        """
        The val/test stage.

        Args:
            epoch_idx (int): Epoch index.

        Returns:
            float: Acc.
        """
        # switch to evaluate mode
        self.model.eval()
        if self.distribute:
            self.model.module.reverse_setting_info()
        else:
            self.model.reverse_setting_info()
        meter = self.test_meter if is_test else self.val_meter
        meter.reset()
        episode_size = self.config["episode_size"]

        end = time()
        enable_grad = self.model_type != ModelType.METRIC
        log_scale = 1 if self.config["n_gpu"] == 0 else self.config["n_gpu"]
        with torch.set_grad_enabled(enable_grad):
            loader = self.test_loader if is_test else self.val_loader
            prefetcher = data_prefetcher(loader)
            batch = prefetcher.next()
            batch_idx = -1
            while batch is not None:
                batch_idx += 1
                if self.rank == 0:
                    self.writer.set_step(
                        int(
                            (epoch_idx * len(loader) + batch_idx * episode_size)
                            * self.config["tb_scale"]
                        )
                    )

                meter.update("data_time", time() - end)

                # calculate the output
                calc_begin = time()
                if self.distribute:
                    output, acc = self.model.module.set_forward(batch)
                else:
                    output, acc = self.model.set_forward(batch)
                meter.update("calc_time", time() - calc_begin)

                # measure accuracy and record loss
                meter.update("acc1", acc)

                # measure elapsed time
                meter.update("batch_time", time() - end)

                if self.rank == 0 and (((batch_idx + 1) % self.config["log_interval"] == 0) or (
                    batch_idx + 1
                ) * episode_size >= len(loader)):
                    info_str = (
                        "Epoch-({}): [{}/{}]\t"
                        "Time {:.3f} ({:.3f})\t"
                        "Calc {:.3f} ({:.3f})\t"
                        "Data {:.3f} ({:.3f})\t"
                        "Acc@1 {:.3f} ({:.3f})".format(
                            epoch_idx,
                            (batch_idx + 1) * episode_size,
                            len(loader),
                            meter.last("batch_time"),
                            meter.avg("batch_time"),
                            meter.last("calc_time"),
                            meter.avg("calc_time"),
                            meter.last("data_time"),
                            meter.avg("data_time"),
                            meter.last("acc1"),
                            meter.avg("acc1"),
                        )
                    )
                    self.logger.info(info_str)
                end = time()

                batch = prefetcher.next()
        if self.distribute:
            self.model.module.reverse_setting_info()
        else:
            self.model.reverse_setting_info()
        return meter.avg("acc1")

    def _init_files(self, config):
        """
        Init result_path(checkpoints_path, log_path, viz_path) from the config dict.

        Args:
            config (dict): Parsed config file.

        Returns:
            tuple: A tuple of (result_path, log_path, checkpoints_path, viz_path).
        """
        if self.rank != 0:
            return None, None, None, None
        # you should ensure that data_root name contains its true name
        base_dir = "{}-{}-{}-{}-{}".format(
            config["classifier"]["name"],
            config["data_root"].split("/")[-1],
            config["backbone"]["name"],
            config["way_num"],
            config["shot_num"],
        )
        result_dir = (
            base_dir
            + "{}-{}".format(
                ("-" + config["tag"]) if config["tag"] is not None else "", get_local_time()
            )
            if config["log_name"] is None
            else config["log_name"]
        )
        result_path = os.path.join(config["result_root"], result_dir)
        # self.logger.log("Result DIR: " + result_path)
        checkpoints_path = os.path.join(result_path, "checkpoints")
        log_path = os.path.join(result_path, "log_files")
        viz_path = os.path.join(log_path, "tfboard_files")
        create_dirs([result_path, log_path, checkpoints_path, viz_path])

        with open(os.path.join(result_path, "config.yaml"), "w", encoding="utf-8") as fout:
            fout.write(yaml.dump(config))

        return result_path, log_path, checkpoints_path, viz_path

    def _init_dataloader(self, config):
        """
        Init dataloaders.(train_loader, val_loader and test_loader)

        Args:
            config (dict): Parsed config file.

        Returns:
            tuple: A tuple of (train_loader, val_loader and test_loader).
        """
        distribute = self.distribute
        train_loader = get_dataloader(config, "train", self.model_type, distribute)
        self.logger.info("load {} image with {} label for train.".format(train_loader.dataset.length, train_loader.dataset.label_num))
        val_loader = get_dataloader(config, "val", self.model_type, distribute)
        self.logger.info("load {} image with {} label for val.".format(val_loader.dataset.length, val_loader.dataset.label_num))
        test_loader = get_dataloader(config, "test", self.model_type, distribute)
        self.logger.info("load {} image with {} label for test.".format(test_loader.dataset.length, test_loader.dataset.label_num))

        return train_loader, val_loader, test_loader

    def _init_model(self, config):
        """
        Init model(backbone+classifier) from the config dict and load the pretrained params or resume from a
        checkpoint, then parallel if necessary .

        Args:
            config (dict): Parsed config file.

        Returns:
            tuple: A tuple of the model and model's type.
        """
        emb_func = get_instance(arch, "backbone", config)
        model_kwargs = {
            "way_num": config["way_num"],
            "shot_num": config["shot_num"] * config["augment_times"],
            "query_num": config["query_num"],
            "test_way": config["test_way"],
            "test_shot": config["test_shot"] * config["augment_times"],
            "test_query": config["test_query"],
            "emb_func": emb_func,
            "device": self.device,
        }
        model = get_instance(arch, "classifier", config, **model_kwargs)

        self.logger.info(model)
        self.logger.info("Trainable params in the model: {}".format(count_parameters(model)))
        # FIXME: May be inaccurate

        if self.config["pretrain_path"] is not None:
            self.logger.info(
                "load pretraining emb_func from {}".format(self.config["pretrain_path"])
            )
            state_dict = torch.load(self.config["pretrain_path"], map_location="cpu")
            msg = model.emb_func.load_state_dict(state_dict, strict=False)

            if len(msg.missing_keys) != 0:
                self.logger.warning("Missing keys:{}".format(msg.missing_keys))
            if len(msg.unexpected_keys) != 0:
                self.logger.warning("Unexpected keys:{}".format(msg.unexpected_keys))

        if self.config["resume"]:
            resume_path = os.path.join(self.config["resume_path"], "checkpoints", "model_last.pth")
            self.logger.info("load the resume model checkpoints dict from {}.".format(resume_path))
            state_dict = torch.load(resume_path, map_location="cpu")["model"]
            msg = model.load_state_dict(state_dict, strict=False)

            if len(msg.missing_keys) != 0:
                self.logger.warning("missing keys:{}".format(msg.missing_keys))
            if len(msg.unexpected_keys) != 0:
                self.logger.warning("unexpected keys:{}".format(msg.unexpected_keys))

        if self.distribute:
            model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
            torch.cuda.set_device(self.rank)
            model = model.to(self.rank)
            model = nn.parallel.DistributedDataParallel(model, device_ids=[self.rank], output_device=self.rank)

            return model, model.module.model_type
        else:
            model = model.to(self.rank)
            
            return model, model.model_type

    def _init_optim(self, config):
        """
        Init the optimizers and scheduler from config, if necessary, load the state dict from a checkpoint.

        Args:
            config (dict): Parsed config file.

        Returns:
            tuple: A tuple of optimizer, scheduler and epoch_index.
        """
        params_idx = []
        params_dict_list = []
        if config["optimizer"]["other"] is not None:
            for key, value in config["optimizer"]["other"].items():
                if self.distribute:
                    sub_model = getattr(self.model.module, key)
                else:
                    sub_model = getattr(self.model, key)
                params_idx.extend(list(map(id, sub_model.parameters())))
                if value is None:
                    for p in sub_model.parameters():
                        p.requires_grad = False
                else:
                    param_dict = {"params": sub_model.parameters()}
                    if isinstance(value, float):
                        param_dict.update({"lr": value})
                    elif isinstance(value, dict):
                        param_dict.update(value)
                    else:
                        raise Exception("Wrong config in optimizer.other")
                    params_dict_list.append(param_dict)

        params_dict_list.append(
            {"params": filter(lambda p: id(p) not in params_idx, self.model.parameters())}
        )
        optimizer = get_instance(torch.optim, "optimizer", config, params=params_dict_list)
        scheduler = get_instance(
            torch.optim.lr_scheduler, "lr_scheduler", config, optimizer=optimizer
        )
        self.logger.info(optimizer)
        from_epoch = -1
        if self.config["resume"]:
            resume_path = os.path.join(self.config["resume_path"], "checkpoints", "model_last.pth")
            self.logger.info(
                "load the optimizer, lr_scheduler and epoch checkpoints dict from {}.".format(
                    resume_path
                )
            )
            all_state_dict = torch.load(resume_path, map_location="cpu")
            state_dict = all_state_dict["optimizer"]
            optimizer.load_state_dict(state_dict)
            state_dict = all_state_dict["lr_scheduler"]
            scheduler.load_state_dict(state_dict)
            from_epoch = all_state_dict["epoch"]
            self.logger.info("model resume from the epoch {}".format(from_epoch))

        return optimizer, scheduler, from_epoch

    def _init_device(self, rank, config):
        """
        Init the devices from the config file.

        Args:
            config (dict): Parsed config file.

        Returns:
            tuple: A tuple of deviceand list_ids.
        """
        init_seed(config["seed"], config["deterministic"])
        device, list_ids = prepare_device(
            rank, 
            config["device_ids"], 
            config["n_gpu"],
            backend="nccl" if not "dist_backend" in self.config else self.config["dist_backend"],
            dist_url="tcp://127.0.0.1:25000" if not "dist_url" in self.config else self.config["dist_url"],
        )
        return device, list_ids

    def _save_model(self, epoch, save_type=SaveType.NORMAL):
        """
        Save the model, optimizer, scheduler and epoch.

        TODO

        Args:
            epoch (int): the current epoch index.
            save_type (SaveType, optional): type of (last, best). Defaults to SaveType.NORMAL.
        """
        save_model(
            self.model,
            self.optimizer,
            self.scheduler,
            self.checkpoints_path,
            "model",
            epoch,
            save_type,
            len(self.list_ids) > 1,
        )

        if save_type != SaveType.LAST:
            save_list = self.config["save_part"]
            if save_list is not None:
                for save_part in save_list:
                    save_module = self.model.module if self.distribute else self.model
                    if hasattr(save_module, save_part):
                        save_model(
                            getattr(save_module, save_part),
                            self.optimizer,
                            self.scheduler,
                            self.checkpoints_path,
                            save_part,
                            epoch,
                            save_type,
                            len(self.list_ids) > 1,
                        )
                    else:
                        self.logger.warning(
                            "{} is not included in {}".format(
                                save_part, self.config["classifier"]["name"]
                            )
                        )

    def _init_meter(self):
        """
        Init the AverageMeter of train/val/test stage to cal avg... of batch_time, data_time,calc_time ,loss and acc1.

        Returns:
            tuple: A tuple of train_meter, val_meter, test_meter.
        """
        train_meter = AverageMeter(
            "train",
            ["batch_time", "data_time", "calc_time", "loss", "acc1"],
            self.writer,
        )
        val_meter = AverageMeter(
            "val",
            ["batch_time", "data_time", "calc_time", "acc1"],
            self.writer,
        )
        test_meter = AverageMeter(
            "test",
            ["batch_time", "data_time", "calc_time", "acc1"],
            self.writer,
        )

        return train_meter, val_meter, test_meter

    def _init_logger(self, log_path):
        """
        Init the logger.

        Returns:
            logger: logger
        """
        if self.rank == 0:
            init_logger(
                self.config["log_level"],
                log_path,
                self.config["classifier"]["name"],
                self.config["backbone"]["name"],
            )

            logger = getLogger(__name__)
            logger.info(self.config)
            return logger
        else:
            logger = getLogger(__name__)
            # def log_pass(*args):
            #     pass
            # logger.info = log_pass
            # logger.warning = log_pass
            logger.setLevel(logging.DEBUG)
            return logger

    def _init_writer(self, viz_path):
        """
        Init the tensorboard writer.

        Return:
            writer: tensorboard writer
        """
        if self.rank == 0:
            writer = TensorboardWriter(viz_path)
            return writer
        else:
            return None

    def _cal_time_scheduler(self, start_time, epoch_idx):
        """
        Calculate the remaining time and consuming time of the training process.

        Returns:
            str: A string similar to "00:00:00/0 days, 00:00:00". First: comsuming time; Second: total time.
        """
        total_epoch = self.config["epoch"] - self.from_epoch - 1
        now_epoch = epoch_idx - self.from_epoch

        time_consum = datetime.datetime.now() - datetime.datetime.fromtimestamp(start_time)
        time_consum -= datetime.timedelta(microseconds=time_consum.microseconds)
        time_remain = (time_consum * (total_epoch - now_epoch)) / (now_epoch)

        res_str = str(time_consum) + "/" + str(time_remain + time_consum)

        return res_str
