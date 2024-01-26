import os
import time
import argparse
import numpy as np

import torch
from torch.utils.data import DataLoader
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger
import MinkowskiEngine as ME

from utils.models.minkunet_bev import MinkUNet34BEV
from utils.datasets.initialization import get_dataset
from utils.datasets.synth4d_bev import MultiBEVSourceDataset
from configs import get_config
from utils.collation import CollateFN, CollateFNSingleSourceBEVMultiLevel, CollateFNMultiSourceBEVMultiLevel
from utils.pipelines import PLTTrainer2D, PLTTrainer2DMulti

parser = argparse.ArgumentParser()
parser.add_argument("--config_file",
                    default="configs/source/semantickitti.yaml",
                    type=str,
                    help="Path to config file")
parser.add_argument("--auto_resume",
                    "-auto",
                    action='store_true',
                    default=False,
                    help="Automatically resume training from last checkpoint")


def train(config):

    def get_dataloader(dataset, batch_size, collate_fn, shuffle=False, pin_memory=True):
        return DataLoader(dataset,
                          batch_size=batch_size,
                          collate_fn=collate_fn,
                          shuffle=shuffle,
                          num_workers=config.pipeline.dataloader.num_workers,
                          pin_memory=pin_memory)

    def get_model(config):
        try:
            bound_2d = config.pipeline.bound_2d
        except AttributeError:
            bound_2d = 50.

        if config.model.name == 'MinkUNet34BEV':
            bottle_img_dim = dict(zip(config.model.decoder_2d_levels, config.model.bev_feats_sizes))
            bottle_out_img_dim = dict(zip(config.model.decoder_2d_levels, config.model.bev_img_sizes))

            try:
                scaling_factors = dict(zip(config.model.decoder_2d_levels, config.model.scaling_factors))
            except AttributeError:
                scaling_factors = {'block8': 1.0, 'block7': 1.0, 'block6': 1.0, 'bottle': 1.0}

            try:
                binary_segmentation_layer = config.model.binary_segmentation_layer
            except:
                binary_segmentation_layer = False

            m = MinkUNet34BEV(in_channels=config.model.in_channels,
                              out_channels=config.model.out_channels,
                              D=config.model.D,
                              initial_kernel_size=config.model.conv1_kernel_size,
                              decoder_2d_level=config.model.decoder_2d_levels,
                              bottle_img_dim=bottle_img_dim,
                              bottle_out_img_dim=bottle_out_img_dim,
                              mapping_bound_2d=bound_2d,
                              scaling_factors=scaling_factors,
                              binary_seg_layer=binary_segmentation_layer)
        else:
            raise NotImplementedError
        print(f'--> Using {config.model.name}!')
        return m

    def get_run_name(config):

        run_time = time.strftime("%Y_%m_%d_%H:%M", time.gmtime())

        run_time += config.model.name
        source_name = ''
        for s in range(len(config.source_dataset.name)):
            source_name += config.source_dataset.name[s]

        target_name = ''
        for s in range(len(config.target_dataset.name)):
            target_name += config.target_dataset.name[s]

        if config.pipeline.wandb.run_name is not None:
            run_name = run_time + source_name + '-TO-' + target_name + '_' + config.pipeline.wandb.run_name + '_'
        else:
            run_name = run_time + '_'
        run_name += 'BS' + str(config.pipeline.dataloader.batch_size) + '_'
        run_name += str(config.pipeline.optimizer.name) + '_'
        run_name += str(config.pipeline.optimizer.lr) + '_'
        run_name += str(config.pipeline.scheduler.name) + '_'
        run_name += str(config.pipeline.losses.sem_criterion) + '_'
        run_name += str(config.pipeline.losses.sem_bev_criterion) + '_'
        run_name += 'AUG' if config.source_dataset.augmentation_list is not None else 'NO_AUG'

        return run_name

    def get_source_domains():
        training_dataset = []
        validation_dataset = []

        num_source_domains = len(config.source_dataset.name)

        for sd in range(len(config.source_dataset.name)):
            dataset_name = config.source_dataset.name[sd]

            try:
                bound_2d = config.pipeline.bound_2d
            except AttributeError:
                bound_2d = 50.

            training_dataset_tmp, validation_dataset_tmp = get_dataset(dataset_name=dataset_name,
                                                                       voxel_size=config.source_dataset.voxel_size,
                                                                       sub_p=config.source_dataset.sub_p,
                                                                       num_classes=config.model.out_channels,
                                                                       ignore_label=config.source_dataset.ignore_label,
                                                                       use_cache=config.source_dataset.use_cache,
                                                                       augmentation_list=config.source_dataset.augmentation_list,
                                                                       scale_bev=config.pipeline.scale_bev,
                                                                       decoder_2d_levels=config.model.decoder_2d_levels,
                                                                       bev_img_sizes=config.model.bev_img_sizes,
                                                                       bound_2d=bound_2d)

            training_dataset.append(training_dataset_tmp)
            validation_dataset.append(validation_dataset_tmp)

        if num_source_domains == 1:
            training_dataset = training_dataset[0]
            validation_dataset = validation_dataset[0]

        else:
            training_dataset = MultiBEVSourceDataset(training_dataset)

        return training_dataset, validation_dataset

    def get_last_checkpoint(save_path):
        # list all paths and get the last one
        if not os.path.exists(save_path):
            return None, None

        all_names = os.listdir(os.path.join(save_path))

        if len(all_names) == 0:
            return None, None
        else:
            all_dates = [n[:16] for n in all_names]
            years = [int(n[:4]) for n in all_dates]
            months = [int(n[5:7]) for n in all_dates]
            days = [int(n[8:10]) for n in all_dates]
            h = [int(n[11:13]) for n in all_dates]
            m = [int(n[14:16]) for n in all_dates]
            last_idx = np.argmax(np.array(years) * 365 * 24 * 60 + np.array(months) * 30 * 24 * 60 + np.array(days) * 24 * 60 + np.array(h) * 60 + np.array(m))
            last_path = all_names[last_idx]

            # among all checkpoints we need to find the last
            all_ckpt = os.listdir(os.path.join(save_path, last_path, "checkpoints"))
            ep = [e[6:8] for e in all_ckpt]
            ckpts = []
            for e in ep:
                if not e.endswith("-"):
                    ckpts.append(int(e))
                else:
                    ckpts.append(int(e[0]))
            last_idx = np.argmax(np.array(ckpts))

            return os.path.join(save_path, last_path, "checkpoints", all_ckpt[last_idx]), last_path

    model = get_model(config)

    training_dataset, validation_dataset = get_source_domains()

    collation_single = CollateFN()
    collation_source = CollateFNMultiSourceBEVMultiLevel() if isinstance(training_dataset, MultiBEVSourceDataset) else CollateFNSingleSourceBEVMultiLevel()

    training_dataloader = get_dataloader(training_dataset,
                                         collate_fn=collation_source,
                                         batch_size=config.pipeline.dataloader.batch_size,
                                         shuffle=True)

    if len(config.source_dataset.name) > 1:
        validation_dataloader = [get_dataloader(v_dataset, collate_fn=collation_single, batch_size=config.pipeline.dataloader.batch_size, shuffle=False) for v_dataset in validation_dataset]
    else:
        validation_dataloader = get_dataloader(validation_dataset,
                                               collate_fn=collation_single,
                                               batch_size=config.pipeline.dataloader.batch_size,
                                               shuffle=False)

    # auto resume routine
    if args.auto_resume:
        # we get the last checkpoint and resume from there
        resume_from_checkpoint, run_name = get_last_checkpoint(config.pipeline.save_dir)
        if run_name is not None:
            if run_name[-1].isdigit():
                run_name = run_name[:-1] + str(int(run_name[-1]) + 1)
            else:
                run_name = run_name + "-PT2"
            # we name the run as the last one and append PT-X
            save_dir = os.path.join(config.pipeline.save_dir, run_name)
        else:
            resume_from_checkpoint = config.pipeline.lightning.resume_checkpoint
            run_name = get_run_name(config)
            save_dir = os.path.join(config.pipeline.save_dir, run_name)

    else:
        resume_from_checkpoint = config.pipeline.lightning.resume_checkpoint
        run_name = get_run_name(config)
        save_dir = os.path.join(config.pipeline.save_dir, run_name)

    wandb_logger = WandbLogger(project=config.pipeline.wandb.project_name,
                               entity=config.pipeline.wandb.entity_name,
                               name=run_name,
                               offline=config.pipeline.wandb.offline)

    loggers = [wandb_logger]

    checkpoint_callback = [ModelCheckpoint(dirpath=os.path.join(save_dir, 'checkpoints'),
                                           save_on_train_epoch_end=True,
                                           every_n_epochs=1,
                                           save_top_k=-1)]

    if len(config.pipeline.gpus) > 1:
        model = ME.MinkowskiSyncBatchNorm.convert_sync_batchnorm(model)
        strategy = 'ddp'
    else:
        strategy = None
    if len(config.target_dataset.name) > 1:
        target_name = config.target_dataset.name
    else:
        target_name = None

    if len(config.source_dataset.name) == 1:
        pl_module = PLTTrainer2D(training_dataset=training_dataset,
                                 validation_dataset=validation_dataset,
                                 model=model,
                                 warmup_epochs=config.pipeline.warmup_epochs,
                                 sem_criterion=config.pipeline.losses.sem_criterion,
                                 sem_bev_criterion=config.pipeline.losses.sem_bev_criterion,
                                 aux_criterion=config.pipeline.losses.aux_criterion,
                                 source_weights=config.pipeline.losses.source_weights,
                                 aux_weights=config.pipeline.losses.aux_weights,
                                 optimizer_name=config.pipeline.optimizer.name,
                                 batch_size=config.pipeline.dataloader.batch_size,
                                 val_batch_size=config.pipeline.dataloader.batch_size,
                                 lr=config.pipeline.optimizer.lr,
                                 num_classes=config.model.out_channels,
                                 train_num_workers=config.pipeline.dataloader.num_workers,
                                 val_num_workers=config.pipeline.dataloader.num_workers,
                                 clear_cache_int=config.pipeline.lightning.clear_cache_int,
                                 scheduler_name=config.pipeline.scheduler.name,
                                 source_domains_name=config.source_dataset.name,
                                 target_domains_name=target_name,
                                 save_dir=save_dir)

    elif len(config.source_dataset.name) == 2:
        pl_module = PLTTrainer2DMulti(training_dataset=training_dataset,
                                      validation_dataset=validation_dataset,
                                      model=model,
                                      warmup_epochs=config.pipeline.warmup_epochs,
                                      sem_criterion=config.pipeline.losses.sem_criterion,
                                      sem_bev_criterion=config.pipeline.losses.sem_bev_criterion,
                                      aux_criterion=config.pipeline.losses.aux_criterion,
                                      source_weights=config.pipeline.losses.source_weights,
                                      aux_weights=config.pipeline.losses.aux_weights,
                                      optimizer_name=config.pipeline.optimizer.name,
                                      batch_size=config.pipeline.dataloader.batch_size,
                                      val_batch_size=config.pipeline.dataloader.batch_size,
                                      lr=config.pipeline.optimizer.lr,
                                      num_classes=config.model.out_channels,
                                      train_num_workers=config.pipeline.dataloader.num_workers,
                                      val_num_workers=config.pipeline.dataloader.num_workers,
                                      clear_cache_int=config.pipeline.lightning.clear_cache_int,
                                      scheduler_name=config.pipeline.scheduler.name,
                                      source_domains_name=config.source_dataset.name,
                                      target_domains_name=target_name,
                                      save_dir=save_dir)

    else:
        raise ValueError('Source dataset number is not valid')

    trainer = Trainer(max_epochs=config.pipeline.epochs,
                      gpus=config.pipeline.gpus,
                      strategy=strategy,
                      default_root_dir=config.pipeline.save_dir,
                      precision=config.pipeline.precision,
                      logger=loggers,
                      check_val_every_n_epoch=config.pipeline.lightning.check_val_every_n_epoch,
                      val_check_interval=config.pipeline.lightning.val_check_interval,
                      num_sanity_val_steps=2,
                      callbacks=checkpoint_callback,
                      log_every_n_steps=50)

    trainer.fit(pl_module,
                train_dataloaders=training_dataloader,
                val_dataloaders=validation_dataloader,
                ckpt_path=resume_from_checkpoint)


if __name__ == '__main__':
    args = parser.parse_args()

    config = get_config(args.config_file)

    # fix random seed
    os.environ['PYTHONHASHSEED'] = str(config.pipeline.seed)
    np.random.seed(config.pipeline.seed)
    torch.manual_seed(config.pipeline.seed)
    torch.cuda.manual_seed(config.pipeline.seed)
    torch.backends.cudnn.benchmark = True

    train(config)
