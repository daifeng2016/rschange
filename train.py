import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from pytorch_lightning import LightningModule, Trainer, seed_everything
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint, TQDMProgressBar
from pytorch_lightning.loggers import TensorBoardLogger
import torchmetrics
from tqdm import tqdm

import prettytable
import numpy as np
import argparse
from rscd.models.build_model import build_model
from rscd.datasets import build_dataloader
from rscd.optimizers import build_optimizer
from rscd.losses import build_loss
from utils.config import Config

import os
import sys
sys.path.append('rscd')

seed_everything(1234, workers=True)

def get_args():
    parser = argparse.ArgumentParser('description=Change detection of remote sensing images')
    parser.add_argument("-c", "--config", type=str, default="configs/STNet.py")
    return parser.parse_args()

class myTrain(LightningModule):
    def __init__(self, cfg, log_dir = None):
        super(myTrain, self).__init__()
        
        self.cfg = cfg
        self.log_dir = log_dir
        self.net = build_model(cfg.model_config)
        self.loss = build_loss(cfg.loss_config)

        self.loss.to("cuda")
        
        metric_cfg1 = cfg.metric_cfg1
        metric_cfg2 = cfg.metric_cfg2
        
        self.tr_oa=torchmetrics.Accuracy(**metric_cfg1)
        self.tr_prec = torchmetrics.Precision(**metric_cfg2)
        self.tr_recall = torchmetrics.Recall(**metric_cfg2)
        self.tr_f1 = torchmetrics.F1Score(**metric_cfg2)
        self.tr_iou=torchmetrics.JaccardIndex(**metric_cfg2)

        self.val_oa=torchmetrics.Accuracy(**metric_cfg1)
        self.val_prec = torchmetrics.Precision(**metric_cfg2)
        self.val_recall = torchmetrics.Recall(**metric_cfg2)
        self.val_f1 = torchmetrics.F1Score(**metric_cfg2)
        self.val_iou=torchmetrics.JaccardIndex(**metric_cfg2)

        self.test_oa=torchmetrics.Accuracy(**metric_cfg1)
        self.test_prec = torchmetrics.Precision(**metric_cfg2)
        self.test_recall = torchmetrics.Recall(**metric_cfg2)
        self.test_f1 = torchmetrics.F1Score(**metric_cfg2)
        self.test_iou=torchmetrics.JaccardIndex(**metric_cfg2)

        self.test_max_f1 = -1

        self.test_loader = build_dataloader(cfg.dataset_config, mode='test')

    def forward(self, x1, x2) :
        pred = self.net(x1, x2)
        return pred

    def configure_optimizers(self):
        optimizer, scheduler = build_optimizer(self.cfg.optimizer_config, self.net)
        return {'optimizer':optimizer,'lr_scheduler':scheduler}

    def train_dataloader(self):
        loader = build_dataloader(self.cfg.dataset_config, mode='train')
        return loader

    def val_dataloader(self):
        loader = build_dataloader(self.cfg.dataset_config, mode='val')
        return loader

    def output(self, metrics, total_metrics, mode):
        result_table = prettytable.PrettyTable()
        result_table.field_names = ['Class', 'OA', 'Precision', 'Recall', 'F1_Score', 'IOU']

        for i in range(len(metrics[0])):
            item = [i, '--']
            for j in range(len(metrics)):
                item.append(np.round(metrics[j][i].cpu().numpy(), 4))
            result_table.add_row(item)

        total = list(total_metrics.values())
        total = [np.round(v, 4) for v in total]
        total.insert(0, 'total')
        result_table.add_row(total)

        if mode == 'val' or mode == 'test':
            print(mode)
            print(result_table)

        if self.log_dir:
            base_dir = self.log_dir
        else:
            base_dir = os.path.join('work_dirs', cfg.exp_name)

        if mode == 'test':
            file_name = os.path.join(base_dir, "test_metrics_rest.txt") 
            if metrics[2][1] > self.test_max_f1:
                self.test_max_f1 = metrics[2][1]
                file_name = os.path.join(base_dir, "test_metrics_max.txt") 
        else:
            file_name = os.path.join(base_dir, "train_metrics.txt") 
            
        f = open(file_name,"a")
        f.write('epoch:{}/{} {}\n'.format(self.current_epoch, self.cfg.epoch, mode))
        f.write(str(result_table)+'\n')
        f.close()

    def training_step(self, batch, batch_idx):
        imgA, imgB, mask = batch[0], batch[1], batch[2]
        preds = self(imgA, imgB)

        loss = self.loss(preds, mask)
                    
        pred = preds.argmax(dim=1)

        self.tr_oa(pred, mask)
        self.tr_prec(pred, mask)
        self.tr_recall(pred, mask)
        self.tr_f1(pred, mask)
        self.tr_iou(pred, mask)

        self.log('tr_loss', loss, on_step=True,on_epoch=True,prog_bar=True)
        return loss

    def on_train_epoch_end(self):
        metrics = [self.tr_prec.compute(),
                   self.tr_recall.compute(),
                   self.tr_f1.compute(),
                   self.tr_iou.compute()]
        
        log = {'tr_oa': float(self.tr_oa.compute().cpu()),
               'tr_prec': np.mean([item.cpu() for item in metrics[0]]),
               'tr_recall': np.mean([item.cpu() for item in metrics[1]]),
               'tr_f1': np.mean([item.cpu() for item in metrics[2]]),
               'tr_miou': np.mean([item.cpu() for item in metrics[3]])}
        
        self.output(metrics, log, 'train')
        
        for key, value in zip(log.keys(), log.values()):
            self.log(key, value, on_step=False,on_epoch=True,prog_bar=True)
        self.log('tr_change_f1', metrics[2][1], on_step=False,on_epoch=True,prog_bar=True)

        self.tr_oa.reset()
        self.tr_prec.reset()
        self.tr_recall.reset()
        self.tr_f1.reset()
        self.tr_iou.reset()

    def validation_step(self, batch, batch_idx):
        imgA, imgB, mask = batch[0], batch[1], batch[2]
        preds = self(imgA, imgB)
        
        loss = self.loss(preds, mask)
        pred = preds.argmax(dim=1)

        self.val_oa(pred, mask)
        self.val_prec(pred, mask)
        self.val_recall(pred, mask)
        self.val_f1(pred, mask)
        self.val_iou(pred, mask)

        self.log('val_loss', loss, on_step=True,on_epoch=True,prog_bar=True)
        return loss

    def on_validation_epoch_end(self):
        metrics = [self.val_prec.compute(),
                   self.val_recall.compute(),
                   self.val_f1.compute(),
                   self.val_iou.compute()]

        log = {'val_oa': float(self.val_oa.compute().cpu()),
               'val_prec': np.mean([item.cpu() for item in metrics[0]]),
               'val_recall': np.mean([item.cpu() for item in metrics[1]]),
               'val_f1': np.mean([item.cpu() for item in metrics[2]]),
               'val_miou': np.mean([item.cpu() for item in metrics[3]])}
        
        self.output(metrics, log, 'val')

        for key, value in zip(log.keys(), log.values()):
            self.log(key, value, on_step=False,on_epoch=True,prog_bar=True)
        self.log('val_change_f1', metrics[2][1], on_step=False,on_epoch=True,prog_bar=True)

        self.val_oa.reset()
        self.val_prec.reset()
        self.val_recall.reset()
        self.val_f1.reset()
        self.val_iou.reset()

        res1 = self.test()

        self.log('test_change_f1', res1, on_step=False,on_epoch=True,prog_bar=True)

    def test(self):
        for input in tqdm(self.test_loader):
            raw_predictions, mask_test = self(input[0].cuda(cfg.gpus[0]), input[1].cuda(cfg.gpus[0])), input[2].cuda(cfg.gpus[0])

            pred_test = raw_predictions.argmax(dim=1)

            self.test_oa(pred_test, mask_test)
            self.test_iou(pred_test, mask_test)
            self.test_prec(pred_test, mask_test)
            self.test_f1(pred_test, mask_test)
            self.test_recall(pred_test, mask_test)

        metrics_test = [self.test_prec.compute(),
                   self.test_recall.compute(),
                   self.test_f1.compute(),
                   self.test_iou.compute()]
        
        log = {'test_oa': float(self.test_oa.compute().cpu()),
            'test_prec': np.mean([item.cpu() for item in metrics_test[0]]),
            'test_recall': np.mean([item.cpu() for item in metrics_test[1]]),
            'test_f1': np.mean([item.cpu() for item in metrics_test[2]]),
            'test_miou': np.mean([item.cpu() for item in metrics_test[3]])}
        
        self.output(metrics_test, log, 'test')
        
        self.test_oa.reset()
        self.test_prec.reset()
        self.test_recall.reset()
        self.test_f1.reset()
        self.test_iou.reset()

        return metrics_test[2][1]

if __name__ == "__main__":  
    args = get_args()
    cfg = Config.fromfile(args.config)
    logger = TensorBoardLogger(save_dir = "work_dirs",
                               sub_dir = 'log',
                               name = cfg.exp_name,
                               default_hp_metric = False)
    
    log_dir = os.path.dirname(logger.log_dir)

    model = myTrain(cfg, log_dir)
    
    lr_monitor=LearningRateMonitor(logging_interval = cfg.logging_interval)

    ckpt_cb1 = ModelCheckpoint(dirpath = f'{log_dir}/ckpts/val',
                              filename = '{val_change_f1:.4f}-{epoch:d}',
                              monitor = cfg.monitor1,
                              mode = 'max',
                              save_top_k = cfg.save_top_k,
                              save_last=True)

    ckpt_cb2 = ModelCheckpoint(dirpath = f'{log_dir}/ckpts/test',
                              filename = '{test_change_f1:.4f}-{epoch:d}',
                              monitor = cfg.monitor2,
                              mode = 'max',
                              save_top_k = cfg.save_top_k,
                              save_last=True)
    
    pbar = TQDMProgressBar(refresh_rate=1)

    callbacks = [ckpt_cb1, ckpt_cb2, pbar, lr_monitor]
    
    trainer = Trainer(max_epochs = cfg.epoch,
                    #   precision='16-mixed',
                      callbacks = callbacks,
                      logger = logger,
                      enable_model_summary = True,
                      accelerator = 'auto',
                      devices = cfg.gpus,
                      num_sanity_val_steps = 2,
                      benchmark = True)
    
    trainer.fit(model, ckpt_path=cfg.resume_ckpt_path)