import torch
import dataset_utils as du
import os
import torch_geometric.transforms as T

from torch import Tensor, LongTensor
from torch_geometric.utils import is_undirected, to_undirected
from torch_geometric.datasets import WikipediaNetwork, Actor, WebKB


class BaseGraph:
    '''
        A general format for datasets.
        Args:
            x (Tensor): node feature, of shape (number of node, F).
            edge_index (LongTensor): of shape (2, number of edge)
            edge_weight (Tensor): of shape (number of edge)
            mask: a node mask to show a training/valid/test dataset split, of shape (number of node). mask[i]=0, 1, 2 means the i-th node in train, valid, test dataset respectively.
    '''
    def __init__(self, x: Tensor, edge_index: LongTensor, edge_weight: Tensor,
                 y: Tensor, mask: LongTensor):
        self.x = x
        self.edge_index = edge_index
        self.edge_attr = edge_weight
        self.y = y
        self.num_classes = torch.unique(y).shape[0]
        self.num_nodes = x.shape[0]
        self.mask = mask
        self.to_undirected()

    def get_split(self, split: str):
        tar_mask = {"train": 0, "valid": 1, "test": 2}[split]
        tmask = self.mask == tar_mask
        return self.edge_index, self.edge_attr, tmask, self.y[tmask]

    def to_undirected(self):
        if not is_undirected(self.edge_index):
            self.edge_index, self.edge_attr = to_undirected(
                self.edge_index, self.edge_attr)

    def to(self, device):
        self.x = self.x.to(device)
        self.edge_index = self.edge_index.to(device)
        self.edge_attr = self.edge_attr.to(device)
        self.y = self.y.to(device)
        self.mask = self.mask.to(device)
        return self


def split(data: BaseGraph, split: str="dense", split_id: int = 0):
    '''
    split data in to train/valid/test set.
    Args:
        data (BaseGraph): the dataset to split.
        split (str): the split mode, choice: ["sparse", "dense"] 
    '''
    dense_split = [0.6, 0.2]
    sparse_split = [0.025, 0.025]
    if split == "default":
        for mask_type in ['train', 'val', 'test']:
            assert hasattr(data, f'{mask_type}_mask')
        train_mask, val_mask, test_mask = \
            data.train_mask[:, split_id], data.val_mask[:, split_id], data.test_mask[:, split_id]
    else:
        if split == "dense":
            u_split = dense_split
        elif split == "sparse":
            u_split = sparse_split
        else:
            raise NotImplementedError("split is dense or sparse")
        percls_trn = int(round(u_split[0] * len(data.y) / data.num_classes))
        val_lb = int(round(u_split[1] * len(data.y)))
        train_mask, val_mask, test_mask = du.random_planetoid_splits(
            data, data.num_classes, percls_trn, val_lb)
            
    dev = data.x.device
    mask = torch.empty((data.x.shape[0]), dtype=torch.int8, device=dev)
    mask[train_mask] = 0
    mask[val_mask] = 1
    mask[test_mask] = 2
    return mask


def load_dataset(name: str, split_t="dense", split_id: int = 0):
    '''
    load dataset into a base graph format.
    '''
    savepath = f"./pyg_data/{name}.pt"
    if name in du.DATASET_LIST:
        if os.path.exists(savepath):
            pass
        ds = du.DataLoader(name)
        data = ds[0]
        if isinstance(ds, list):
            data.num_classes = len(torch.unique(data.y))
        else:
            data.num_classes = ds.num_classes
        x = data.x  # torch.empty((data.x.shape[0], 0))
        ei = data.edge_index
        ea = torch.ones(ei.shape[1])
        y = data.y 
        mask = split(data, split=split_t, split_id=split_id)
        bg = BaseGraph(x, ei, ea, y, mask)
        bg.num_classes = data.num_classes
        bg.y = bg.y.to(torch.int64)
        torch.save(bg, savepath)
        return bg
    else:
        raise NotImplementedError()
