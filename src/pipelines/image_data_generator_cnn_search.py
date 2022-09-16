#!/usr/bin/env python

import os
import random
import shutil
import subprocess
import traceback
from collections import ChainMap

import atexit
import itertools
import sys
import tensorflow as tf

from src.pipelines.image_data_generator_cnn import image_data_generator_cnn
from src.settings import settings
from src.util.argparse import argparse_from_dict
from src.util.hparam import hparam_key
from src.util.hparam_search import hparam_combninations, hparam_logdir, hparam_run_name
from src.util.logs import log_model_stats


# remove logs and models for incomplete trainings
def onexit(outputs: list):
    for output in outputs:
        if os.path.exists(output):
            if os.path.isdir(output):
                shutil.rmtree(output)
                print(f'rm -rf {output}')
            else:
                os.remove(output)
                print(f'rm -f {output}')


def image_data_generator_cnn_search(
        debug=0,
        verbose=0,
        force=False,
):
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # 0, 1, 2, 3 # Disable Tensortflow Logging
    tf.keras.backend.set_floatx('float32')    # BUGFIX: Nan in summary histogram

    search = {}
    search['train'] = {
        # "optimized_scheduler": {
        #     "Adagrad_triangular":   { "learning_rate": 0.1,    "optimizer": "Adagrad",  "scheduler": "triangular"  },
        #     "Adagrad_plateau":      { "learning_rate": 0.1,    "optimizer": "Adagrad",  "scheduler": "plateau2"    },
        #     "Adam_triangular2":     { "learning_rate": 0.01,   "optimizer": "Adam",     "scheduler": "triangular2" },
        #     "Nadam_plateau":        { "learning_rate": 0.01,   "optimizer": "Nadam",    "scheduler": "plateau10"   },
        #     "Adadelta_plateau_1.0": { "learning_rate": 1.0,    "optimizer": "Adadelta", "scheduler": "plateau10"   },
        #     "Adadelta_plateau_0.1": { "learning_rate": 0.1,    "optimizer": "Adadelta", "scheduler": "plateau10"   },
        #     "SGD_triangular2":      { "learning_rate": 1.0,    "optimizer": "SGD",      "scheduler": "triangular2" },
        #     "RMSprop_constant":     { "learning_rate": 0.001,  "optimizer": "RMSprop",  "scheduler": "constant"    },
        # },
        # "optimizer":     [ "RMSprop", "Adagrad", "Adam", "Nadam", "Adadelta" ],
        # "scheduler":     "constant",
        # "learning_rate": [ 0.001, 0.01 ],
        "optimizer":       "Nadam",         # Nadam 0.01 + plateau10 converges quickly
        "scheduler":       "plateau10",
        "learning_rate":   0.01,
        # "best_only":     True,
        "batch_size":      128,  # IO bound | GPU max memory = 512 | 128 seems optimal
        "patience":        [2,4],
        "min_lr":          [1e-05, 1e-07],
        "epochs":          999,
        # "loss_weights":  False,
        # "timeout":       "6h"
    }
    search['model'] = {
        ### Searched
        # "cnns_per_maxpool":   [1,2,3,4],
        # "maxpool_layers":     [4,5],
        # "dense_layers":       [1,2,3],
        # "dense_units":        [128,256,512],
        # "regularization":     [True,False],
        # "global_maxpool":     [True,False],

        "cnns_per_maxpool":   3,
        "maxpool_layers":     5,  # increasing `maxpool_layers` prefers fewer `cnns_per_maxpool` (ideal total CNNs = 15 / 16)
        "cnn_units":         32,
        "cnn_kernel":         3,
        "cnn_strides":        1,
        "dense_layers":       1,  # `dense_layers=1` is preferred over `2` or `3`
        "dense_units":      256,
        "regularization": False,  # `regularization=True` prefers `global_maxpool=False` (but not vice versa) - worse results
        "global_maxpool": False,  # `global_maxpool=True` prefers double the number of `dense_units` and +1 `cnns_per_maxpool`
        "activation":    'relu',  # 'relu' | 'crelu' | 'leaky_relu' | 'relu6' | 'softmax' | 'tanh' | 'hard_sigmoid' | 'sigmoid'
        "dropout":         0.25,
    }
    search['transform_X'] = {
        "resize":       2,
        # "invert":    True,
        "rescale":   True,
        "denoise":   True,
        "center":    True,
        # "normalize": True,
    }
    search['transform_Y'] = {
    }
    # Source: https://www.kaggle.com/jamesmcguigan/bengali-ai-image-processing
    search['datagen'] = {
        # "rescale":          1./255,  # "normalize": True is default in Transforms
        "zoom_range":         0.2,
        "width_shift_range":  0.1,     # we already have centering
        "height_shift_range": 0.1,     # we already have centering
        "rotation_range":     45/2,
        "shear_range":        45/2,
        # "brightness_range":   0.5,   # Prebrightness normalized
        "fill_mode":         'constant',
        "cval": 0,
        # "featurewise_center": True,             # No visible effect in plt.imgshow()
        # "samplewise_center": True,              # No visible effect in plt.imgshow()
        # "featurewise_std_normalization": True,  # No visible effect in plt.imgshow() | requires .fit()
        # "samplewise_std_normalization": True,   # No visible effect in plt.imgshow() | requires .fit()
        # "zca_whitening": True,                   # Kaggle, insufficent memory
    }
    combninations_search = {
        key: hparam_combninations(value)
        for key, value in search.items()
    }
    combninations = list(itertools.product(*combninations_search.values()))
    combninations = [ dict(zip(combninations_search.keys(), combnination)) for combnination in combninations ]
    random.shuffle(combninations)
    stats_history = []

    pipeline_name  = "image_data_generator_cnn_search_"
    pipeline_name += "_patience"
    # pipeline_name += "_" + "_".join(sorted([ key for key,values in combninations_search.items() if len(values) >= 2 ]))

    print(f"--- Testing {len(combninations)} combinations")
    for key, value in search.items(): print(f"--- search: {key}", value)

    index = 0
    for hparams in combninations:
        index += 1

        hparams_searched = dict(ChainMap(*[
            { key: hparams[name][key] for key, values in sorted(options.items()) if isinstance(values, (list,dict)) }
            for name, options in search.items()
        ]))
        hparams_key = hparam_key(hparams_searched)

        run_name = hparam_run_name( hparams_searched )
        logdir   = hparam_logdir(   hparams_searched, log_dir=settings['dir']['logs'] )

        print("")
        print(f"--- Starting trial {index}/{len(combninations)}: {logdir.split('/')[-2]} | {run_name}")
        for key, value in hparams.items(): print(f"--- search: {key}", value)


        logfilename       = f"{settings['dir']['submissions']}/{pipeline_name}/{hparams_key}-submission.log"
        csv_filename      = f"{settings['dir']['submissions']}/{pipeline_name}/{hparams_key}-submission.csv"
        model_file        = f"{settings['dir']['models']}/{pipeline_name}/{hparams_key}.hdf5"
        log_dir           = f"{settings['dir']['logs']}/{pipeline_name}/{hparams_key}"

        for dirname in [ log_dir ] + list(map(os.path.dirname, [logfilename, csv_filename, model_file])):
            os.makedirs(dirname, exist_ok=True)

        if os.path.exists(model_file) and not force:
            print('Exists: skipping')
            continue
        if debug: continue

        try:
            tf.keras.backend.clear_session()
            model, model_stats, output_shape = image_data_generator_cnn(
                train_hparams    = hparams['train'],
                model_hparams    = hparams['model'],
                transform_X_args = hparams['transform_X'],
                transform_Y_args = hparams['transform_Y'],
                datagen_args     = hparams['datagen'],
                pipeline_name    = pipeline_name,
                model_file       = model_file,
                log_dir          = log_dir,
                verbose          = verbose,
                load_weights     = False,
                # fileglobs = {
                #     "train": f"{settings['dir']['data']}/train_image_data_1.parquet",
                #     "valid": f"{settings['dir']['data']}/train_image_data_0.parquet",
                #     "test":  f"{settings['dir']['data']}/test_image_data_*.parquet",
                # }
            )

            log_model_stats(model_stats, logfilename, hparams)
            # submission = submission_df_generator(model, output_shape)
            # df_to_submission_csv( submission, csv_filename )
            stats_history += model_stats
            print(model_stats)

            atexit.unregister(onexit)
            subprocess.run(['./logfiles_to_csv.sh', os.path.dirname(logfilename)], stdout=subprocess.DEVNULL, shell=False)

        except KeyboardInterrupt:
            print('Ctrl-C KeyboardInterrupt')
            onexit([logfilename, csv_filename, model_file, log_dir])
            sys.exit()
        except Exception as exception:
            print("-"*10 + "\nException")
            traceback.print_exception(type(exception), exception, exception.__traceback__)
            traceback.print_tb(exception.__traceback__)
            print("-"*10)
            onexit([logfilename, csv_filename, model_file, log_dir])

    print("")
    print("--- Stats History")
    print(stats_history)
    print("--- Finished")

    return stats_history

if __name__ == '__main__':
    argv = argparse_from_dict({ "debug": 0, "verbose": 0, "force": False })
    image_data_generator_cnn_search(**argv)
