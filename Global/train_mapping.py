# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import time
from collections import OrderedDict
from options.train_options import TrainOptions
from data.data_loader import CreateDataLoader
from models.mapping_model import Pix2PixHDModel_Mapping
import util.util as util
from util.visualizer import Visualizer
import os
import numpy as np
import torch
import torchvision.utils as vutils
from torch.autograd import Variable
import datetime
import random



opt = TrainOptions().parse()
visualizer = Visualizer(opt)
iter_path = os.path.join(opt.checkpoints_dir, opt.name, 'iter.txt')
if opt.continue_train:
    try:
        start_epoch, epoch_iter = np.loadtxt(iter_path , delimiter=',', dtype=int)
    except:
        start_epoch, epoch_iter = 1, 0
    visualizer.print_save('Resuming from epoch %d at iteration %d' % (start_epoch-1, epoch_iter))
else:
    start_epoch, epoch_iter = 1, 0

if opt.which_epoch != "latest":
    start_epoch=int(opt.which_epoch)
    visualizer.print_save('Notice : Resuming from epoch %d at iteration %d' % (start_epoch - 1, epoch_iter))

opt.start_epoch=start_epoch
### temp for continue train unfixed decoder

data_loader = CreateDataLoader(opt)
dataset = data_loader.load_data()
dataset_size = len(dataset) * opt.batchSize
print('#training images = %d' % dataset_size)


model = Pix2PixHDModel_Mapping()
model.initialize(opt)


path = os.path.join(opt.checkpoints_dir, opt.name, 'model.txt')
fd = open(path, 'w')

if opt.use_skip_model:
    fd.write(str(model.mapping_net))
    fd.close()
else:
    fd.write(str(model.netG_A))
    fd.write(str(model.mapping_net))
    fd.close()

if opt.isTrain and len(opt.gpu_ids) > 1:
    model = torch.nn.DataParallel(model, device_ids=opt.gpu_ids)



total_steps = (start_epoch-1) * dataset_size + epoch_iter

display_delta = total_steps % opt.display_freq
print_delta = total_steps % opt.print_freq
save_delta = total_steps % opt.save_latest_freq
### used for recovering training

for epoch in range(start_epoch, opt.niter + opt.niter_decay + 1):
    epoch_s_t=datetime.datetime.now()
    epoch_start_time = time.time()
    if epoch != start_epoch:
        epoch_iter = epoch_iter % dataset_size
    for i, data in enumerate(dataset, start=epoch_iter):
        iter_start_time = time.time()
        total_steps += opt.batchSize
        epoch_iter += opt.batchSize

        # whether to collect output images
        save_fake = total_steps % opt.display_freq == display_delta

        ############## Forward Pass ######################
        #print(pair)
        #model.to("cuda")
        parameter_label = Variable(data['label']).to("cuda")
        parameter_inst = Variable(data['inst']).to("cuda")
        parameter_image = Variable(data['image']).to("cuda")
        parameter_feat = Variable(data['feat']).to("cuda")
        losses, generated = model(parameter_label, parameter_inst,
            parameter_image, parameter_feat, infer=save_fake)

        if losses is None:
            print("="*80)
            print("Skipped iteration")
            print("=" * 80)
            continue
        
        # sum per device losses
        losses = [ torch.mean(x) if not isinstance(x, int) else x for x in losses ]
        loss_dict = dict(zip(model.loss_names, losses))

        # calculate final loss scalar
        loss_D = (loss_dict['D_fake'] + loss_dict['D_real']) * 0.5
        loss_G = loss_dict['G_GAN'] + loss_dict.get('G_GAN_Feat',0) + loss_dict.get('G_VGG',0) + loss_dict.get('G_Feat_L2', 0) +loss_dict.get('Smooth_L1', 0)+loss_dict.get('G_Feat_L2_Stage_1',0)
        #loss_G = loss_dict['G_Feat_L2'] 

        ############### Backward Pass ####################
        # update generator weights
        model.optimizer_mapping.zero_grad()
        loss_G.backward()
        model.optimizer_mapping.step()

        # update discriminator weights
        model.optimizer_D.zero_grad()
        loss_D.backward()
        model.optimizer_D.step()

        ############## Display results and errors ##########
        ### print out errors
        if i == 0 or total_steps % opt.print_freq == print_delta:
            errors = {k: v.data if not isinstance(v, int) else v for k, v in loss_dict.items()}
            t = (time.time() - iter_start_time) / opt.batchSize
            visualizer.print_current_errors(epoch, epoch_iter, errors, t,model.old_lr)
            visualizer.plot_current_errors(errors, total_steps)

        ### display output images
        if save_fake:

            if not os.path.exists(opt.outputs_dir + opt.name):
                os.makedirs(opt.outputs_dir + opt.name)

            imgs_num = 5
            if opt.NL_use_mask:
                mask=data['inst'][:imgs_num]
                mask=mask.repeat(1,3,1,1)
                imgs = torch.cat((data['label'][:imgs_num], mask,generated.data.cpu()[:imgs_num], data['image'][:imgs_num]), 0)
            else:
                imgs = torch.cat((data['label'][:imgs_num], generated.data.cpu()[:imgs_num], data['image'][:imgs_num]), 0)

            imgs=(imgs+1.)/2.0   ## de-normalize

            try:
                image_grid = vutils.save_image(imgs, opt.outputs_dir + opt.name + '/' + str(epoch) + '_' + str(total_steps) + '.png',
                        nrow=imgs_num, padding=0, normalize=True)
            except OSError as err:
                print(err)

        if epoch_iter >= dataset_size:
            break
       
    # end of epoch
    epoch_e_t=datetime.datetime.now()
    iter_end_time = time.time()
    print('End of epoch %d / %d \t Time Taken: %s' %
          (epoch, opt.niter + opt.niter_decay, str(epoch_e_t-epoch_s_t)))

    ### save model for this epoch
    if epoch % opt.save_epoch_freq == 0:
        print('saving the model at the end of epoch %d, iters %d' % (epoch, total_steps))        
        model.save('latest')
        model.save(epoch)
        np.savetxt(iter_path, (epoch+1, 0), delimiter=',', fmt='%d')

    ### instead of only training the local enhancer, train the entire network after certain iterations
    if (opt.niter_fix_global != 0) and (epoch == opt.niter_fix_global):
        model.update_fixed_params()

    ### linearly decay learning rate after certain iterations
    if epoch > opt.niter:
        model.update_learning_rate()