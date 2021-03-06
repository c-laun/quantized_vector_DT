import sys

sys.path += [
    '/net/hcihome/storage/claun/PycharmProjects/quantized_vector_DT/experiments/cremi',
    '/export/home/claun/PycharmProjects/quantized_vector_DT',
    '/export/home/claun/repositories/affogato',
    '/export/home/claun/PycharmProjects/segmfriends',
    '/export/home/claun/PycharmProjects/neurofire',
    '/export/home/claun/PycharmProjects/speedrun',
    '/export/home/claun/PycharmProjects/embeddingutils',
    '/export/home/claun/PycharmProjects/inferno',
    '/export/home/claun/PycharmProjects/firelight',
    '/export/home/claun/PycharmProjects/uppsala_hackathon',
]

# import quantizedVDT

from speedrun import BaseExperiment, TensorboardMixin, InfernoMixin
from speedrun.log_anywhere import register_logger, log_image, log_scalar
from speedrun.py_utils import locate

import os
import torch
# import torch.nn as nn
# from torch.nn import Softmax

from inferno.utils import python_utils as pyu
# from inferno.trainers.callbacks.essentials import SaveAtBestValidationScore
# from neurofire.criteria.loss_wrapper import LossWrapper
# from inferno.extensions.criteria.set_similarity_measures import SorensenDiceLoss
# from inferno.extensions.layers.convolutional import Conv3D
# from inferno.trainers.callbacks import Callback
# from inferno.io.transform.base import Compose

# from embeddingutils.loss import WeightedLoss, SumLoss
from quantizedVDT.utils.core import recursive_dict_update

# from shutil import copyfile

# from inferno.extensions.layers.convolutional import ConvELU3D, Conv3D, BNReLUConv3D

from neurofire.criteria.loss_wrapper import LossWrapper
# from neurofire.criteria.loss_transforms import ApplyAndRemoveMask
# from neurofire.criteria.loss_transforms import RemoveSegmentationFromTarget
# from neurofire.criteria.loss_transforms import InvertTarget

from quantizedVDT.datasets.new_cremi import get_cremi_loader, get_inference_loader
from quantizedVDT.utils.path_utils import get_source_dir

import numpy as np


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
        # register_logger(self, 'embedding')
        # register_logger(self, 'image')

    #        offsets = self.get_default_offsets()
    #        self.set('global/offsets', offsets)
    #        self.set('loaders/general/volume_config/segmentation/affinity_config/offsets', offsets)

    # def get_default_offsets(self):
    #     return [[-1, 0, 0], [0, -1, 0], [0, 0, -1],
    #             [-2, 0, 0], [0, -3, 0], [0, 0, -3],
    #             [-3, 0, 0], [0, -9, 0], [0, 0, -9],
    #             [-4, 0, 0], [0, -27, 0], [0, 0, -27]]

    def build_model(self, model_config=None):
        model_config = self.get('model') if model_config is None else model_config
        model_class = list(model_config.keys())[0]
        # n_channels = self.get('loaders/general/master_config/compute_directions/n_directions')
        # if self.get('loaders/general/master_config/compute_directions/z_direction'):
        #     n_channels += 2
        n_channels = self.get(f'model/{model_class}/out_channels')
        model_config[model_class]['out_channels'] = n_channels
        self.set('model/{}/out_channels'.format(model_class), n_channels)

        self.build_final_activation(model_config)
        return super(BaseCremiExperiment, self).build_model(model_config)  # parse_model(model_config)

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


        criterion = loss_config.get('loss')
        transforms = None  # loss_config.get('transforms', None)


        loss_train = LossWrapper(criterion=criterion,
                                 transforms=transforms)
        loss_val = LossWrapper(criterion=criterion,
                               transforms=transforms)
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
                # kwargs['offsets'] = self.get('global/offsets')
                # kwargs['z_direction'] = self.get(
                #    'loaders/general/master_config/compute_directions/z_direction')
                print(f'Building metric of class "{cls.__name__}"')
                metric = cls(**kwargs)
                self.trainer.build_metric(metric)
        self.set('trainer/metric/evaluate_every', frequency)

    def build_train_loader(self):
        return get_cremi_loader(recursive_dict_update(self.get('loaders/train'), self.get('loaders/general')))

    def build_val_loader(self):
        return get_cremi_loader(recursive_dict_update(self.get('loaders/val'), self.get('loaders/general')))

    def build_infer_loader(self):
        return get_inference_loader(self.get('loaders/infer'))

    def infer_simple(self):

        self.infer_loader = self.build_infer_loader()

        print('start inference')
        self.trainer.eval_mode()
        loader_name = "validate"

        # Everytime we should loop over the full dataset (restart generators):
        # TODO: this will probably load a random batch. You maybe want to turn random=False
        loader_iter = self.infer_loader.__iter__()

        # Record the epoch we're validating in
        iteration_num = 0
        nb_tot_predictions = len(self.infer_loader.dataset)

        from quantizedVDT.transforms import Reassemble
        import h5py
        f = h5py.File(f'{workdirectory}/inference.hdf5', 'a')

        # re = Reassemble(4, 100)
        while True:
            print(f'start iteration {iteration_num}')
            try:
                batch2 = next(loader_iter)
            except StopIteration:
                self.trainer.console.info("Generator exhausted, breaking.")
                break
            try:
                batch = self.infer_loader.dataset[iteration_num]
            except:
                self.trainer.console.info("Generator exhausted, breaking.")
                break

            # Delay SIGINTs till after computation
            with pyu.delayed_keyboard_interrupt(), torch.no_grad():
                batch = self.trainer.to_device(batch)
                # batch = self.trainer.wrap_batch(batch, from_loader=loader_name, volatile=True)
                # Separate
                # inputs, target = self.trainer.split_batch(batch, from_loader=loader_name)
                # inputs = [inputs[0][None]]
                # target = target[None]
                # # Wrap
                # inputs = self.trainer.to_device(inputs)
                outputs = self.trainer.apply_model(batch[None])

                self.trainer.console.progress(
                    "{} of {} inference predictions done".format(iteration_num, nb_tot_predictions))

            # TODO: save the outputs somewhere on disk
            f.create_dataset('batch', data=batch.cpu().numpy())
            f.create_dataset('outputs', data=outputs.cpu().numpy())


            # np.save(f'{workdirectory}/output_{iteration_num}', outputs.cpu().detach().numpy())
            # np.save(f'{workdirectory}/dir_{iteration_num}', re(outputs.cpu().detach().numpy()[0]))
            # np.save(f'{workdirectory}/inp_{iteration_num}', inputs[0].cpu().detach().numpy())
            # np.save(f'{workdirectory}/tar_{iteration_num}', target.cpu().detach().numpy())

            print('success')
            # TODO: decide if you want to predict more or break
            iteration_num += 1
            # break
        f.close()


if __name__ == '__main__':
    print(sys.argv[1])
    config_path = os.path.join(get_source_dir(), 'experiments/cremi/configs')
    experiments_path = os.path.join(get_source_dir(), 'runs/cremi/speedrun')

    sys.argv[1] = os.path.join(experiments_path, sys.argv[1])
    workdirectory = os.path.join(experiments_path, sys.argv[1])
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
    if '--load' in sys.argv:
        i = sys.argv.index('--load') + 1
        if sys.argv[i] == 'True':
            sys.argv[i] = os.path.join(sys.argv[1], 'checkpoint.pytorch')
        else:
            sys.argv[i] = os.path.join(experiments_path, sys.argv[i], 'checkpoint.pytorch')
        sys.argv[i - 1] = '--config.model.embeddingutils.models.unet.UNet3D.loadfrom'

    cls = BaseCremiExperiment()
    # cls.run()
    cls.infer_simple()
