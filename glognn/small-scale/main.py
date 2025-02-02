import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch.optim as optim
import math
import numpy as np
import scipy.sparse as sp
import time
import argparse
import networkx as nx
import os
import json
import warnings

from copy import deepcopy
from collections import defaultdict
from torch.nn.parameter import Parameter
from sklearn.metrics import roc_auc_score

warnings.filterwarnings('ignore')


torch.set_default_dtype(torch.float64)


DATASET_LIST = [
    'squirrel_directed', 'chameleon_directed',
    'squirrel_filtered_directed', 'chameleon_filtered_directed',
    'roman_empire', 'minesweeper', 'questions', 'amazon_ratings', 'tolokers'
]


class GraphConvolution(nn.Module):
    """
    Simple GCN layer, similar to https://arxiv.org/abs/1609.02907
    """

    def __init__(self, in_features, out_features, bias=True):
        super(GraphConvolution, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.weight = Parameter(torch.DoubleTensor(in_features, out_features))
        if bias:
            self.bias = Parameter(torch.DoubleTensor(out_features))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.weight.size(1))
        self.weight.data.uniform_(-stdv, stdv)
        if self.bias is not None:
            self.bias.data.uniform_(-stdv, stdv)

    def forward(self, input, adj):
        support = torch.mm(input, self.weight)
        output = torch.spmm(adj, support)
        if self.bias is not None:
            return output + self.bias
        else:
            return output

    def __repr__(self):
        return self.__class__.__name__ + ' (' \
            + str(self.in_features) + ' -> ' \
            + str(self.out_features) + ')'


class GCN(nn.Module):
    def __init__(self, nfeat, nhid, nclass, dropout):
        super(GCN, self).__init__()

        self.gc1 = GraphConvolution(nfeat, nhid)
        self.gc2 = GraphConvolution(nhid, nclass)
        self.dropout = dropout

    def forward(self, x, adj):
        x = F.relu(self.gc1(x, adj))
        x = F.dropout(x, self.dropout, training=self.training)
        x = self.gc2(x, adj)
        return F.log_softmax(x, dim=1)


class MLP_NORM(nn.Module):

    def __init__(self, nnodes, nfeat, nhid, nclass, dropout, alpha, beta, gamma, delta, norm_func_id, norm_layers, orders, orders_func_id, cuda):
        super(MLP_NORM, self).__init__()
        self.fc1 = nn.Linear(nfeat, nhid)
        self.fc2 = nn.Linear(nhid, nclass)
        self.fc3 = nn.Linear(nnodes, nhid)
        self.nclass = nclass
        self.dropout = dropout
        self.alpha = torch.tensor(alpha)
        self.beta = torch.tensor(beta)
        self.gamma = torch.tensor(gamma)
        self.delta = torch.tensor(delta)
        self.norm_layers = norm_layers
        self.orders = orders
        self.class_eye = torch.eye(nclass)
        self.nodes_eye = torch.eye(nnodes)

        if cuda:
            self.orders_weight = Parameter(
                torch.ones(orders, 1) / orders, requires_grad=True
            ).to('cuda')
            # use kaiming_normal to initialize the weight matrix in Orders3
            self.orders_weight_matrix = Parameter(
                torch.DoubleTensor(nclass, orders), requires_grad=True
            ).to('cuda')
            self.orders_weight_matrix2 = Parameter(
                torch.DoubleTensor(orders, orders), requires_grad=True
            ).to('cuda')
            # use diag matirx to initialize the second norm layer
            self.diag_weight = Parameter(
                torch.ones(nclass, 1) / nclass, requires_grad=True
            ).to('cuda')
            self.alpha = self.alpha.cuda()
            self.beta = self.beta.cuda()
            self.gamma = self.gamma.cuda()
            self.delta = self.delta.cuda()
            self.class_eye = self.class_eye.cuda()
            self.nodes_eye = self.nodes_eye.cuda()
        else:
            self.orders_weight = Parameter(
                torch.ones(orders, 1) / orders, requires_grad=True
            )
            # use kaiming_normal to initialize the weight matrix in Orders3
            self.orders_weight_matrix = Parameter(
                torch.DoubleTensor(nclass, orders), requires_grad=True
            )
            self.orders_weight_matrix2 = Parameter(
                torch.DoubleTensor(orders, orders), requires_grad=True
            )
            # use diag matirx to initialize the second norm layer
            self.diag_weight = Parameter(
                torch.ones(nclass, 1) / nclass, requires_grad=True
            )
        init.kaiming_normal_(self.orders_weight_matrix, mode='fan_out')
        init.kaiming_normal_(self.orders_weight_matrix2, mode='fan_out')
        self.elu = torch.nn.ELU()

        if norm_func_id == 1:
            self.norm = self.norm_func1
        else:
            self.norm = self.norm_func2

        if orders_func_id == 1:
            self.order_func = self.order_func1
        elif orders_func_id == 2:
            self.order_func = self.order_func2
        else:
            self.order_func = self.order_func3

    def forward(self, x, adj):
        xX = F.dropout(x, self.dropout, training=self.training)
        xX = self.fc1(x)
        xA = self.fc3(adj)
        x = F.relu(self.delta * xX + (1-self.delta) * xA)
        x = F.dropout(x, self.dropout, training=self.training)
        x = self.fc2(x)
        h0 = x
        for _ in range(self.norm_layers):
            # adj_drop = F.dropout(adj, self.dropout, training=self.training)
            x = self.norm(x, h0, adj)
        return F.log_softmax(x, dim=1)

    def norm_func1(self, x, h0, adj):
        coe = 1.0 / (self.alpha + self.beta)
        coe1 = 1 - self.gamma
        coe2 = 1.0 / coe1
        res = torch.mm(torch.transpose(x, 0, 1), x)
        inv = torch.inverse(coe2 * coe2 * self.class_eye + coe * res)
        # u = torch.cholesky(coe2 * coe2 * torch.eye(self.nclass) + coe * res)
        # inv = torch.cholesky_inverse(u)
        res = torch.mm(inv, res)
        res = coe1 * coe * x - coe1 * coe * coe * torch.mm(x, res)
        tmp = torch.mm(torch.transpose(x, 0, 1), res)
        sum_orders = self.order_func(x, res, adj)
        res = coe1 * torch.mm(x, tmp) + self.beta * sum_orders - \
            self.gamma * coe1 * torch.mm(h0, tmp) + self.gamma * h0
        return res

    def norm_func2(self, x, h0, adj):
   
        coe = 1.0 / (self.alpha + self.beta)
        coe1 = 1 - self.gamma
        coe2 = 1.0 / coe1
        res = torch.mm(torch.transpose(x, 0, 1), x)
        inv = torch.inverse(coe2 * coe2 * self.class_eye + coe * res)
        # u = torch.cholesky(coe2 * coe2 * torch.eye(self.nclass) + coe * res)
        # inv = torch.cholesky_inverse(u)
        res = torch.mm(inv, res)
        res = (coe1 * coe * x -
               coe1 * coe * coe * torch.mm(x, res)) * self.diag_weight.t()
        tmp = self.diag_weight * (torch.mm(torch.transpose(x, 0, 1), res))
        sum_orders = self.order_func(x, res, adj)
        res = coe1 * torch.mm(x, tmp) + self.beta * sum_orders - \
            self.gamma * coe1 * torch.mm(h0, tmp) + self.gamma * h0

        # calculate z
        xx = torch.mm(x, x.t())
        hx = torch.mm(h0, x.t())
        # print('adj', adj.shape)
        # print('orders_weight', self.orders_weight[0].shape)
        adj = adj.to_dense()
        adjk = adj
        a_sum = adjk * self.orders_weight[0]
        for i in range(1, self.orders):
            adjk = torch.mm(adjk, adj)
            a_sum += adjk * self.orders_weight[i]
        z = torch.mm(coe1 * xx + self.beta * a_sum - self.gamma * coe1 * hx,
                     torch.inverse(coe1 * coe1 * xx + (self.alpha + self.beta) * self.nodes_eye))
        # print(z.shape)
        # print(z)
        return res

    def order_func1(self, x, res, adj):
        # Orders1
        tmp_orders = res
        sum_orders = tmp_orders
        for _ in range(self.orders):
            tmp_orders = torch.spmm(adj, tmp_orders)
            sum_orders = sum_orders + tmp_orders
        return sum_orders

    def order_func2(self, x, res, adj):
        # Orders2
        tmp_orders = torch.spmm(adj, res)
        sum_orders = tmp_orders * self.orders_weight[0]
        for i in range(1, self.orders):
            tmp_orders = torch.spmm(adj, tmp_orders)
            sum_orders = sum_orders + tmp_orders * self.orders_weight[i]
        return sum_orders

    def order_func3(self, x, res, adj):
        # Orders3
        orders_para = torch.mm(torch.relu(torch.mm(x, self.orders_weight_matrix)),
                               self.orders_weight_matrix2)
        # orders_para = torch.mm(x, self.orders_weight_matrix)
        orders_para = torch.transpose(orders_para, 0, 1)
        tmp_orders = torch.spmm(adj, res)
        sum_orders = orders_para[0].unsqueeze(1) * tmp_orders
        for i in range(1, self.orders):
            tmp_orders = torch.spmm(adj, tmp_orders)
            sum_orders = sum_orders + orders_para[i].unsqueeze(1) * tmp_orders
        return sum_orders


def encode_onehot(labels):
    classes = set(labels)
    classes_dict = {c: np.identity(len(classes))[i, :] for i, c in
                    enumerate(classes)}
    labels_onehot = np.array(list(map(classes_dict.get, labels)),
                             dtype=np.int32)
    return labels_onehot


def normalize(mx):
    """Row-normalize sparse matrix"""
    rowsum = np.array(mx.sum(1))
    r_inv = np.power(rowsum, -1).flatten()
    r_inv[np.isinf(r_inv)] = 0.
    r_mat_inv = sp.diags(r_inv)
    mx = r_mat_inv.dot(mx)
    return mx


@torch.no_grad()
def accuracy(pr_logits, gt_labels):
    return (pr_logits.argmax(dim=-1) == gt_labels).float().mean()

@torch.no_grad()
def roc_auc(pr_logits, gt_labels):
    return roc_auc_score(gt_labels.cpu().numpy(), pr_logits[:, 1].cpu().numpy()) 


def sparse_mx_to_torch_sparse_tensor(sparse_mx):
    """Convert a scipy sparse matrix to a torch sparse tensor."""
    sparse_mx = sparse_mx.tocoo().astype(np.float32)
    indices = torch.from_numpy(
        np.vstack((sparse_mx.row, sparse_mx.col)).astype(np.int64))
    values = torch.from_numpy(sparse_mx.data)
    shape = torch.Size(sparse_mx.shape)
    return torch.sparse.DoubleTensor(indices, values, shape)


# Read split data
def parse_index_file(filename):
    """Parse index file."""
    index = []
    for line in open(filename):
        index.append(int(line.strip()))
    return index


def load_data_new(dataset_str, split):
    """
    Loads input data from gcn/data directory

    ind.dataset_str.x => the feature vectors of the training instances as scipy.sparse.csr.csr_matrix object;
    ind.dataset_str.tx => the feature vectors of the test instances as scipy.sparse.csr.csr_matrix object;
    ind.dataset_str.allx => the feature vectors of both labeled and unlabeled training instances
        (a superset of ind.dataset_str.x) as scipy.sparse.csr.csr_matrix object;
    ind.dataset_str.y => the one-hot labels of the labeled training instances as numpy.ndarray object;
    ind.dataset_str.ty => the one-hot labels of the test instances as numpy.ndarray object;
    ind.dataset_str.ally => the labels for instances in ind.dataset_str.allx as numpy.ndarray object;
    ind.dataset_str.graph => a dict in the format {index: [index_of_neighbor_nodes]} as collections.defaultdict
        object;
    ind.dataset_str.test.index => the indices of test instances in graph, for the inductive setting as list object.

    All objects above must be saved using python pickle module.

    :param dataset_str: Dataset name
    :return: All data input files loaded (as well the training/test data).
    """
 
    if dataset_str in ['chameleon', 'cornell', 'film', 'squirrel', 'texas', 'wisconsin']:
        graph_adjacency_list_file_path = os.path.join(
            'new_data', dataset_str, 'out1_graph_edges.txt')
        graph_node_features_and_labels_file_path = os.path.join('new_data', dataset_str,
                                                                f'out1_node_feature_label.txt')
        graph_dict = defaultdict(list)
        with open(graph_adjacency_list_file_path) as graph_adjacency_list_file:
            graph_adjacency_list_file.readline()
            for line in graph_adjacency_list_file:
                line = line.rstrip().split('\t')
                assert (len(line) == 2)
                graph_dict[int(line[0])].append(int(line[1]))
                graph_dict[int(line[1])].append(int(line[0]))

        graph_dict_ordered = defaultdict(list)
        for key in sorted(graph_dict):
            graph_dict_ordered[key] = graph_dict[key]
            graph_dict_ordered[key].sort()

        adj = nx.adjacency_matrix(nx.from_dict_of_lists(graph_dict_ordered))

        graph_node_features_dict = {}
        graph_labels_dict = {}

        if dataset_str == 'film':
            with open(graph_node_features_and_labels_file_path) as graph_node_features_and_labels_file:
                graph_node_features_and_labels_file.readline()
                for line in graph_node_features_and_labels_file:
                    line = line.rstrip().split('\t')
                    assert (len(line) == 3)
                    assert (int(line[0]) not in graph_node_features_dict and int(
                        line[0]) not in graph_labels_dict)
                    feature_blank = np.zeros(932, dtype=np.uint8)
                    feature_blank[np.array(
                        line[1].split(','), dtype=np.uint16)] = 1
                    graph_node_features_dict[int(line[0])] = feature_blank
                    graph_labels_dict[int(line[0])] = int(line[2])
        else:
            with open(graph_node_features_and_labels_file_path) as graph_node_features_and_labels_file:
                graph_node_features_and_labels_file.readline()
                for line in graph_node_features_and_labels_file:
                    line = line.rstrip().split('\t')
                    assert (len(line) == 3)
                    assert (int(line[0]) not in graph_node_features_dict and int(
                        line[0]) not in graph_labels_dict)
                    graph_node_features_dict[int(line[0])] = np.array(
                        line[1].split(','), dtype=np.uint8)
                    graph_labels_dict[int(line[0])] = int(line[2])

        features_list = []
        for key in sorted(graph_node_features_dict):
            features_list.append(graph_node_features_dict[key])
        features = np.vstack(features_list)
        features = sp.csr_matrix(features)

        labels_list = []
        for key in sorted(graph_labels_dict):
            labels_list.append(graph_labels_dict[key])

        label_classes = max(labels_list) + 1
        labels = np.eye(label_classes)[labels_list]

        splits_file_path = 'splits/' + dataset_str + \
            '_split_0.6_0.2_' + str(split) + '.npz'

        with np.load(splits_file_path) as splits_file:
            train_mask = splits_file['train_mask']
            val_mask = splits_file['val_mask']
            test_mask = splits_file['test_mask']

        idx_train = np.where(train_mask == 1)[0]
        idx_val = np.where(val_mask == 1)[0]
        idx_test = np.where(test_mask == 1)[0]

    elif dataset_str in DATASET_LIST:
        npz_data = np.load(f'data/{dataset_str}.npz')

        if 'directed' not in dataset_str:
            edge = np.concatenate((npz_data['edges'], npz_data['edges'][:, ::-1]), axis=0)
        else:
            edge = npz_data['edges']

        labels = npz_data['node_labels']
        features = npz_data['node_features']

        adj = nx.adj_matrix(nx.from_edgelist(edge))

        train_mask = npz_data['train_masks'][split]
        val_mask   = npz_data['val_masks'][split]
        test_mask  = npz_data['test_masks'][split]

        idx_train = np.where(train_mask == 1)[0]
        idx_val = np.where(val_mask == 1)[0]
        idx_test = np.where(test_mask == 1)[0]

    adj = normalize(adj + sp.eye(adj.shape[0]))
    adj = sparse_mx_to_torch_sparse_tensor(adj)

    features = normalize(features)
    if isinstance(features, np.ndarray):
        features = torch.DoubleTensor(features)
    else:
        features = torch.DoubleTensor(np.array(features.todense()))

    if len(labels.shape) == 1:
        labels = torch.from_numpy(labels).long()
    else:
        labels = torch.from_numpy(labels).long().argmax(dim=-1)
    
    idx_train = torch.LongTensor(idx_train)
    idx_val = torch.LongTensor(idx_val)
    idx_test = torch.LongTensor(idx_test)

    return adj, features, labels, idx_train, idx_val, idx_test


# Training settings
parser = argparse.ArgumentParser()
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='Disables CUDA training.')
parser.add_argument('--fastmode', action='store_true', default=False,
                    help='Validate during training pass.')
parser.add_argument('--seed', type=int, default=42, help='Random seed.')
parser.add_argument('--epochs', type=int, default=200,
                    help='Number of epochs to train.')
parser.add_argument('--lr', type=float, default=0.01,
                    help='Initial learning rate.')
parser.add_argument('--weight_decay', type=float, default=5e-4,
                    help='Weight decay (L2 loss on parameters).')
parser.add_argument('--hidden', type=int, default=16,
                    help='Number of hidden units.')
parser.add_argument('--dropout', type=float, default=0.5,
                    help='Dropout rate (1 - keep probability).')
parser.add_argument('--alpha', type=float, default=0.1,
                    help='Weight for frobenius norm on Z.')
parser.add_argument('--beta', type=float, default=0.1,
                    help='Weight for frobenius norm on Z-A')
parser.add_argument('--gamma', type=float, default=0.2,
                    help='Weight for MLP results kept')
parser.add_argument('--delta', type=float, default=1.0,
                    help='Weight for nodes feature kept')
parser.add_argument('--norm_layers', type=int, default=2,
                    help='Number of groupnorm layers')
parser.add_argument('--dataset', type=str, default='wisconsin',
                    help='Name of dataset')
parser.add_argument('--split', type=int, default=0,
                    help='Split part of dataset')
parser.add_argument('--early_stopping', type=int, default=40,
                    help='Early stopping')
parser.add_argument('--model', type=str, default='mlp_norm',
                    help='Model name ')
parser.add_argument('--orders', type=int, default=2,
                    help='Number of adj orders in norm layer')
parser.add_argument('--orders_func_id', type=int, default=3,
                    help='Sum function of adj orders in norm layer, ids \in [1, 2, 3]')
parser.add_argument('--norm_func_id', type=int, default=2,
                    help='Function of norm layer, ids \in [1, 2]')


args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

np.random.seed(args.seed)
torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

# Load data
adj, features, labels, idx_train, idx_val, idx_test = load_data_new(
    args.dataset, args.split)

# Change data type to float
features = features.to(torch.float64)
adj = adj.to(torch.float64)
# Model and optimizer

if args.model == 'gcn':
    model = GCN(nfeat=features.shape[1],
                nhid=args.hidden,
                nclass=labels.max().item() + 1,
                dropout=args.dropout)
elif args.model == 'mlp_norm':
    model = MLP_NORM(
        nnodes=adj.shape[0],
        nfeat=features.shape[1],
        nhid=args.hidden,
        nclass=labels.max().item() + 1,
        dropout=args.dropout,
        alpha=args.alpha,
        beta=args.beta,
        gamma=args.gamma,
        delta=args.delta,
        norm_func_id=args.norm_func_id,
        norm_layers=args.norm_layers,
        orders=args.orders,
        orders_func_id=args.orders_func_id,
        cuda=args.cuda)
optimizer = optim.Adam(model.parameters(),
                       lr=args.lr, weight_decay=args.weight_decay)

if args.cuda:
    model.cuda()
    features = features.cuda()
    adj = adj.cuda()
    labels = labels.cuda()
    idx_train = idx_train.cuda()
    idx_val = idx_val.cuda()
    idx_test = idx_test.cuda()

# Train model
t_total = time.time()

num_labels = len(torch.unique(labels))

metric = accuracy if num_labels > 2 else roc_auc

best_metric = 0
patience = 0

best_params = None

for epoch in range(args.epochs):
    t = time.time()
    model.train()
    optimizer.zero_grad()
    output = model(features, adj)
    loss_train = F.nll_loss(output[idx_train], labels[idx_train])
    metric_train = metric(output[idx_train], labels[idx_train])
    loss_train.backward()
    optimizer.step()

    if not args.fastmode:
        # Evaluate validation set performance separately,
        # deactivates dropout during validation run.
        model.eval()
        output = model(features, adj)

    loss_val = F.nll_loss(output[idx_val], labels[idx_val])
    try:
        metric_val = metric(output[idx_val], labels[idx_val])
    except:
        break
    # print(
    #     'Epoch: {:04d}'.format(epoch+1),
    #     'loss_train: {:.4f}'.format(loss_train.item()),
    #     'metric_train: {:.4f}'.format(metric_train.item()),
    #     'loss_val: {:.4f}'.format(loss_val.item()),
    #     'metric_val: {:.4f}'.format(metric_val.item()),
    #     'time: {:.4f}s'.format(time.time() - t)
    # )
    if metric_val > best_metric:
        best_metric = metric_val
        best_params = deepcopy(model.state_dict())
        patience = 0
    else:
        patience += 1

    if patience >= args.early_stopping:
        break

outfile_name = f"{args.dataset}_lr{args.lr}_do{args.dropout}_es{args.early_stopping}_" +\
    f"wd{args.weight_decay}_alpha{args.alpha}_beta{args.beta}_gamma{args.gamma}_" +\
    f"delta{args.delta}_nlid{args.norm_func_id}_nl{args.norm_layers}_" +\
    f"ordersid{args.orders_func_id}_orders{args.orders}_split{args.split}_results.txt"
# load best params
model.load_state_dict(best_params)
# Testing
model.eval()
output = model(features, adj)
loss_test = F.nll_loss(output[idx_test], labels[idx_test])
metric_test = metric(output[idx_test], labels[idx_test])
print("Test set results:",
      "loss= {:.4f}".format(loss_test.item()),
      "metric= {:.4f}".format(metric_test.item()))

test_time = time.time()
results_dict = {}
results_dict['test_cost'] = float(loss_test.item())
results_dict['test_acc'] = float(metric_test.item())
results_dict['test_duration'] = time.time()-test_time

with open(os.path.join('runs', outfile_name), 'w') as outfile:
    outfile.write(json.dumps(results_dict))
