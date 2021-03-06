import quantizedVDT

from speedrun import BaseExperiment, TensorboardMixin, InfernoMixin
from speedrun.log_anywhere import register_logger, log_image, log_scalar
from speedrun.py_utils import locate

import os
import torch
import torch.nn as nn

from speedrun.inferno import IncreaseStepCallback
from inferno.trainers.callbacks.essentials import SaveAtBestValidationScore
from neurofire.criteria.loss_wrapper import LossWrapper
from inferno.extensions.criteria.set_similarity_measures import SorensenDiceLoss
from inferno.extensions.layers.convolutional import Conv3D
from inferno.trainers.callbacks import Callback
from inferno.io.transform.base import Compose

from embeddingutils.loss import WeightedLoss, SumLoss
from segmfriends.utils.config_utils import recursive_dict_update

from shutil import copyfile
import sys

from inferno.extensions.layers.convolutional import ConvELU3D, Conv3D, BNReLUConv3D

from neurofire.criteria.loss_wrapper import LossWrapper
from neurofire.criteria.loss_transforms import ApplyAndRemoveMask
from neurofire.criteria.loss_transforms import RemoveSegmentationFromTarget
from neurofire.criteria.loss_transforms import InvertTarget

from quantizedVDT.datasets.cremi_directional import get_cremi_loader
from quantizedVDT.utils.path_utils import get_source_dir



class BaseCremiExperiment(BaseExperiment, InfernoMixin, TensorboardMixin):
    def __init__(self, experiment_directory=None, config=None):
        super(BaseCremiExperiment, self).__init__(experiment_directory)
        # Privates
        self._device = None
        self._meta_config['exclude_attrs_from_save'] = ['data_loader', '_device']
        if config is not None:
            self.read_config_file(config)


        self.DEFAULT_DISPATCH = 'train'
        self.auto_setup()

        register_logger(self, 'scalars')
        register_logger(self, 'embedding')
        register_logger(self, 'image')

        self.trainer().regist

        offsets = self.get_default_offsets()
        self.set('global/offsets', offsets)
        self.set('loaders/general/volume_config/segmentation/affinity_config/offsets', offsets)


    def get_default_offsets(self):
        return [[-1, 0, 0], [0, -1, 0], [0, 0, -1],
                [-2, 0, 0], [0, -3, 0], [0, 0, -3],
                [-3, 0, 0], [0, -9, 0], [0, 0, -9],
                [-4, 0, 0], [0, -27, 0], [0, 0, -27]]

    def build_model(self, model_config=None):
        model_config = self.get('model') if model_config is None else model_config
        model_class = list(model_config.keys())[0]
        n_channels = self.get('loaders/general/master_config/compute_directions/n_directions')
        if self.get('loaders/general/master_config/compute_directions/z_direction'):
            n_channels += 2
        model_config[model_class]['out_channels'] = 6
        self.set('model/{}/out_channels'.format(model_class), n_channels)

        self.build_final_activation(model_config)
        return super(BaseCremiExperiment, self).build_model(model_config) #parse_model(model_config)

    def build_final_activation(self, model_config=None):
        model_config = self.get('model') if model_config is None else model_config
        model_class = list(model_config.keys())[0]


        final_activation = model_config[model_class].pop('final_activation', None)
        if final_activation is None:
            return
        if isinstance(final_activation, str):
            final_activation = locate(
                    final_activation, ['torch.nn'])
            model_config[model_class]['final_activation'] = \
                final_activation()
            return
        model_config[model_class]['final_activation'] = \
            final_activation


    def inferno_build_criterion(self):
        print("Building criterion")
        loss_config = self.get('trainer/criterion/losses')

        criterion = nn.L1Loss()
        loss_train = LossWrapper(criterion=criterion,
                                 transforms=None)
        loss_val = LossWrapper(criterion=criterion,
                               transforms=None)
        self._trainer.build_criterion(loss_train)
        self._trainer.build_validation_criterion(loss_val)

    def inferno_build_metric(self):
        metric_config = self.get('trainer/metric')
        frequency = metric_config.pop('evaluate_every', (25, 'iterations'))

        self.trainer.evaluate_metric_every(frequency)
        if metric_config:
            assert len(metric_config) == 1
            for class_name, kwargs in metric_config.items():
                cls = locate(class_name)
                #kwargs['offsets'] = self.get('global/offsets')
                #kwargs['z_direction'] = self.get(
                #    'loaders/general/master_config/compute_directions/z_direction')
                print(f'Building metric of class "{cls.__name__}"')
                metric = cls(**kwargs)
                self.trainer.build_metric(metric)
        self.set('trainer/metric/evaluate_every', frequency)

    def build_train_loader(self):
        return get_cremi_loader(recursive_dict_update(self.get('loaders/train'), self.get('loaders/general')))

    def build_val_loader(self):
        return get_cremi_loader(recursive_dict_update(self.get('loaders/val'), self.get('loaders/general')))

    def run(self, *args, **kwargs):
        if self.get_arg('dispatch', None) is None and self.DEFAULT_DISPATCH is None:
            raise NotImplementedError
        else:
            # Get the method to be dispatched and call
            return self.dispatch(self.get_arg('dispatch') or self.DEFAULT_DISPATCH,
                                 *args, **kwargs)

    def resetup(self):
        for fname in dir(self):
            if fname.startswith('inferno_build_'):
                getattr(self, fname)()

        # add callback to increase step counter
        # noinspection PyUnresolvedReferences
        self._trainer.register_callback(IncreaseStepCallback(self))

        self._trainer.to(self.device)

        return self._trainer




if __name__ == '__main__':
    print(sys.argv[1])
    config_path = os.path.join(get_source_dir(), 'experiments/cremi/configs')
    experiments_path = os.path.join(get_source_dir(), './runs/cremi/speedrun')

    sys.argv[1] = os.path.join(experiments_path, sys.argv[1])
    if '--inherit' in sys.argv:
        i = sys.argv.index('--inherit') + 1
        if sys.argv[i].endswith(('.yml', '.yaml')):
            sys.argv[i] = os.path.join(config_path, sys.argv[i])
        else:
            sys.argv[i] = os.path.join(experiments_path, sys.argv[i])
    if '--update' in sys.argv:
        i = sys.argv.index('--update') + 1
        sys.argv[i] = os.path.join(config_path, sys.argv[i])
    i = 0
    while True:
        if f'--update{i}' in sys.argv:
            ind = sys.argv.index(f'--update{i}') + 1
            sys.argv[ind] = os.path.join(config_path, sys.argv[ind])
            i += 1
        else:
            break
    cls = BaseCremiExperiment()
    cls.trainer.load('/export/home/claun/PycharmProjects/quantized_vector_DT/runs/cremi/speedrun/savetest_copy_2')
    cls.resetup()
    cls.trainer.fit(max_num_iterations=10000)
    cls.register_unpickleable('_train_loader')
    cls.register_unpickleable('_val_loader')
    cls.checkpoint()

