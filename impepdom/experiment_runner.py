import os
import pickle
from itertools import permutations, product
import copy
import time

import torch
import torch.nn as nn
import torch.optim as optim

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from sklearn.metrics import roc_auc_score

import impepdom


def hyperparam_search(
    model_type, dataset, fold_idx=[0, 1, 2, 3],
    max_epochs=15, batch_sizes=[32, 64, 128], learning_rates=[5e-4, 1e-3, 5e-3],
    dropout_input_list=[0.85], dropout_hidden_list=[0.65],
    conv_flags=[True], num_conv_layers_list=[1], conv_filt_sz_list=[5], conv_stride_list=[1],
    optimizer=None, scheduler=None, sort_by='mean_auc_01'
):
    '''
    Parameters
    ----------
    model_type: string
        String of class name of neural network to be used

    dataset: impepdom.PeptideDataset
        Initialized peptide dataset for MHC I

    fold_idx: list
        List of number (from 0 to 4) to specify k-folds for cross-validation

    max_epochs: int, optional
        Epoch at which to stop training. Results of all intermediary epochs will be saved

    batch_sizes: list, optional

    learning_rates: list, optional
    criterion: nn.Loss, optional  # in the works
    scheduler: torch.optim.lr_scheduler, optional  # in the works

    sort_by: string, optional
        Sort results to present, desc_stat + metric. Examples: 'mean_acc', 'min_auc_01', 'max_ppv'

    !!! COPY PARAMETERS DESCRIPTION FROM MODELS.PY !!!

    Returns
    ----------
    best_config: dict
        Dictionary of hyperparameters that showed the best performance based on eval
    '''
    
    since = time.time()
    tot_experiments = len(batch_sizes) * len(learning_rates)
    experiment_count = 0

    padding = dataset.padding
    best_config = None

    for hyperparams in product(
        batch_sizes,  # 0
        learning_rates,  # 1
        dropout_input_list,  # 2
        dropout_hidden_list,  # 3
        conv_flags,  # 4
        num_conv_layers_list,  # 5
        conv_filt_sz_list,  # 6
        conv_stride_list  # 7
    ):
        print(impepdom.time_tracker.now() + 'running experiment {} out of {}'.format(experiment_count + 1, tot_experiments))
        experiment_count += 1
        results_store = []  # to store model names and scores

        metrics = impepdom.metrics.METRICS
        desc_stats = impepdom.metrics.DESC_STATS
        
        cross_eval = {}  # store history of validation metrics. Per metric: columns are epochs, rows are results on folds
        for metric in metrics:
            cross_eval[metric] = []
        
        model_run_time = None
        for val_fold_id in fold_idx:
            train_fold_idx = copy.copy(fold_idx)
            train_fold_idx.remove(val_fold_id)  # remove validation fold

            folder, _, config = run_experiment(
                model_type=model_type,
                dataset=dataset,
                train_fold_idx=train_fold_idx,
                val_fold_idx=[val_fold_id],
                learning_rate=hyperparams[1],
                num_epochs=max_epochs,
                batch_size=hyperparams[0],
                scheduler=scheduler,

                # in-model, non-traning hyperparams
                dropout_input=hyperparams[2],
                dropout_hidden=hyperparams[3],
                conv=hyperparams[4],
                num_conv_layers=hyperparams[5],
                conv_filt_sz=hyperparams[6],
                conv_stride=hyperparams[7],

                show_output=False,
                model_run_time=model_run_time
            )

            train_history = impepdom.load_train_history(folder)            
            for metric in metrics:
                cross_eval[metric].append(train_history['val']['metrics'][metric])  # get metric over epochs
            model_run_time = impepdom.store_manager.extract_model_run_time(folder)  # to keep in the same folder
        
        for epoch in range(max_epochs):
            res_obj = {
                'model': model_run_time,
                'padding': padding,
                'batch_size': hyperparams[0],
                'num_epochs': epoch + 1,
                'learning_rate': hyperparams[1],
                'optimizer': config['optimizer'],
                'scheduler': config['scheduler'],
                
                'dropout_input': hyperparams[2],
                'dropout_hidden': hyperparams[3],
                'conv': hyperparams[4],
                'num_conv_layers': hyperparams[5],
                'conv_filt_sz': hyperparams[6],
                'conv_stride': hyperparams[7],
            }

            for metric in metrics:
                cross_eval_metric = np.vstack(cross_eval[metric])  # make into one numpy array
                for desc_stat in desc_stats:
                    res_obj[desc_stat[0] + '_' + metric] = desc_stat[1](cross_eval_metric[:, epoch])

            results_store.append(res_obj)

        results_store.sort(key=(lambda model_res: model_res[sort_by]), reverse=True)
        impepdom.store_manager.update_hyperparams_store(results_store)

        if best_config == None or best_config[sort_by] < results_store[0][sort_by]:
            best_config = results_store[0]
        print(impepdom.time_tracker.now() + 'experiment {} results saved'.format(experiment_count))

    time_elapsed = time.time() - since
    print(impepdom.time_tracker.now() + 'evaluation completed!'.format(
            time_elapsed // 60, time_elapsed % 60))

    return best_config

def run_experiment(
    model_type, dataset, train_fold_idx, val_fold_idx=None,
    criterion=None, optimizer=None, scheduler=None,
    batch_size=64, num_epochs=25, learning_rate=1e-3, show_output=True,
    dropout_input=0.85, dropout_hidden=0.65,
    conv=False, num_conv_layers=None, conv_filt_sz=None, conv_stride=None,
    model_run_time=None
):
    '''
    Run a neural network training on specified train and validation set, with parameters.

    Parameters
    ----------
    model_type: string
        String of class name of neural network to be used

    dataset: impepdom.PeptideDataset
        Initialized peptide dataset for MHC I

    train_fold_idx: list
        List of number (from 0 to 4) to specify training folds

    val_fold_idx: list, optional
        List of number (from 0 to 4) to specify validation folds

    criterion: nn.Loss, optional
    scheduler: torch.optim.lr_scheduler, optional
    batch_size: int, optional
    num_epochs: int, optional
    learning_rate: float, optional

    !!! COPY PARAMETERS DESCRIPTION FROM MODELS.PY !!!

    show_output: bool
        Show output of training process (everything will be saved anyway)

    model_run_time: string
        Attach results to the same model if we're just doing cross-validation

    Returns
    ----------
    folder: string
        Path inside STORE_PATH to folder storing training cache

    baseline_metrics: dict
        Dictionary of baseline metrics in train (and val) datasets

    config: dict
        Dictionary of configurations used in the training process
    '''
    
    need_validation = False if val_fold_idx is None else True

    # obtain torch dataloader for batch learning
    peploader = {}
    peploader['train'] = dataset.get_peptide_dataloader(fold_idx=train_fold_idx, batch_size=batch_size)
    if need_validation:
        peploader['val'] = dataset.get_peptide_dataloader(fold_idx=val_fold_idx, batch_size=batch_size)

    # (re-)initialize model
    model = impepdom.models[model_type](
        num_hidden_layers=2,
        hidden_layer_size=100,
        dropout_input=dropout_input,
        dropout_hidden=dropout_hidden,

        conv=conv,
        num_conv_layers=num_conv_layers,
        conv_filt_sz=conv_filt_sz,
        conv_stride=conv_stride,
    ) 

    # set up optimization criterion, optimization algorithm, and learning rate decay
    if criterion == None:
        criterion = nn.BCELoss()
    if optimizer == None:
        optimizer = optim.Adam(model.parameters(), lr=learning_rate)
    if scheduler == None:
        steps = 25
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, steps)
        # lr_decay_step = 5
        # decay_factor = 0.9
        # exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=lr_decay_step, gamma=decay_factor)

    # collect baseline metrics
    baseline_metrics = get_baseline_metrics(dataset, train_fold_idx, val_fold_idx)

    # train the model, collect data
    model, train_history, state_dicts = impepdom.train_nn(
        model=model,
        peploader=peploader,
        criterion=criterion,
        optimizer=optimizer,
        scheduler=scheduler,
        num_epochs=num_epochs,
        learning_rate=learning_rate,
        validation=need_validation,
        show_output=show_output
    )

    folder = impepdom.store_manager.get_save_path(model, dataset.get_allele(), train_fold_idx, model_run_time=model_run_time)
    # save model
    state_dict_folder = os.path.join(impepdom.store_manager.STORE_PATH, folder, 'torch_models')
    os.makedirs(state_dict_folder, exist_ok=True)
    torch.save(model.state_dict(), os.path.join(impepdom.store_manager.STORE_PATH, folder, 'best_model'))  # store best model
    for i, state_dict in enumerate(state_dicts):
        torch.save(state_dict, os.path.join(state_dict_folder, 'epoch_{}'.format(i)))
    
    # save training history
    impepdom.store_manager.pickle_dump(train_history, folder, 'train_history')

    # save validation predictions
    if need_validation:
        val = {}
        data, val['target'] = dataset.get_fold(val_fold_idx)
        val['pred'] = model(torch.tensor(data).float())
        impepdom.store_manager.pickle_dump(val, folder, 'val_' + impepdom.store_manager.list_to_str(val_fold_idx))

    config = {
        # pass these parameters in a more obvious and descriptive way in the future
        'optimizer': str(optimizer)[:str(optimizer).find(' ')],
        'scheduler': 'CosineAnnealingLR'  # hard code for now
    }

    return folder, baseline_metrics, config

# !!! Maybe take this out to a separate module !!!
def plot_train_history(train_history, baseline_metrics=None, metrics=['loss', 'acc', 'auc']):
    '''
    Plot metrics to observe training (vs validation) progress.

    Parameters
    ----------
    train_history: dict
        Dictionary containing training (and validation) metric logs over epochs
    baseline_metrics: dict, optional
        Dictionary containing reference metrics (e.g., if we guess everything as non-binding)
    '''

    matplotlib.rcParams['axes.spines.right'] = False
    matplotlib.rcParams['axes.spines.top'] = False

    num_epochs = len(train_history['train']['metrics']['loss'])
    val_exists = len(train_history['val']['metrics']['loss']) > 0 

    plt.figure(figsize=(16, 5))
    for i, metric in enumerate(metrics):
        plt.subplot(1, len(metrics), i + 1)

        plt.plot(range(num_epochs), train_history['train']['metrics'][metric], color='skyblue', label='train')
        if val_exists:
            plt.plot(range(num_epochs), train_history['val']['metrics'][metric], color='darkorange', label='val')
        if baseline_metrics and metric != 'loss':
            plt.axhline(y=baseline_metrics['train'][metric] + 1e-3, color='skyblue', alpha=0.7, linestyle='--', label='train base')
            if 'acc' in baseline_metrics['val']:
                plt.axhline(y=baseline_metrics['val'][metric] - 1e-3, color='darkorange', alpha=0.7, linestyle='--', label='val base')

        plt.ylabel(metric)
        plt.xlabel('epoch')
        plt.legend()

    plt.show()

def get_baseline_metrics(dataset, train_fold_idx, val_fold_idx): 
    baseline_metrics = {
        'train': {},
        'val': {}
    }
    metrics = impepdom.metrics.METRICS

    _, train_targets = dataset.get_fold(fold_idx=train_fold_idx)
    train_zeros = np.zeros(train_targets.shape)
    if val_fold_idx:
        _, val_targets = dataset.get_fold(fold_idx=val_fold_idx)
        val_zeros = np.zeros(val_targets.shape)

    baseline_metrics['train']['acc'] = np.sum((train_targets == train_zeros)) / len(train_zeros)
    baseline_metrics['train']['auc'] = roc_auc_score(train_targets, train_zeros)
    if val_fold_idx:
        baseline_metrics['val']['acc'] = np.sum((val_targets == val_zeros)) / len(val_zeros)
        baseline_metrics['val']['auc'] = roc_auc_score(val_targets, val_zeros)

    return baseline_metrics

def list_to_str(ls):
    _str = ''.join(sorted([str(c) for c in ls]))
    return _str
