import argparse
import sys
import time

## for drawing package
import matplotlib.pyplot as plt
import matplotlib.patches as patches

import torch.optim as optim
from torch.autograd import Variable

sys.path.insert(0,'./modules')
from sample_generator import *
from data_prov import *
from model import *
from bbreg import *
from options import *
from gen_config import *
from img_cropper import *
from roi_align.modules.roi_align import RoIAlignAvg,RoIAlignMax,RoIAlignAdaMax,RoIAlignDenseAdaMax

np.random.seed(123)
torch.manual_seed(456)
torch.cuda.manual_seed(789)


##################################################################################
############################Do not modify opts anymore.###########################
######################Becuase of synchronization of options#######################
##################################################################################

def forward_samples(model, image, samples, out_layer='conv3'):
    model.eval()
    extractor = RegionExtractor(image, samples, opts['img_size'], opts['padding'], opts['batch_test'])
    for i, regions in enumerate(extractor):
        regions = Variable(regions)
        if opts['use_gpu']:
            regions = regions.cuda()
        feat = model(regions, out_layer=out_layer)
        if i==0:
            feats = feat.data.clone()
        else:
            feats = torch.cat((feats,feat.data.clone()),0)
    return feats


def set_optimizer(model, lr_base, lr_mult=opts['lr_mult'], momentum=opts['momentum'], w_decay=opts['w_decay']):
    params = model.get_learnable_params()
    param_list = []
    for k, p in params.iteritems():
        lr = lr_base
        for l, m in lr_mult.iteritems():
            if k.startswith(l):
                lr = lr_base * m
        param_list.append({'params': [p], 'lr':lr})
    optimizer = optim.SGD(param_list, lr = lr, momentum=momentum, weight_decay=w_decay)
    # optimizer = optim.SGD(param_list, lr = 1., momentum=momentum, weight_decay=w_decay)
    return optimizer


def train(model, criterion, optimizer, pos_feats, neg_feats, maxiter, in_layer='fc4'):
    model.train()

    batch_pos = opts['batch_pos']
    batch_neg = opts['batch_neg']
    batch_test = opts['batch_test']
    batch_neg_cand = max(opts['batch_neg_cand'], batch_neg)

    pos_idx = np.random.permutation(pos_feats.size(0))
    neg_idx = np.random.permutation(neg_feats.size(0))
    while(len(pos_idx) < batch_pos*maxiter):
        pos_idx = np.concatenate([pos_idx, np.random.permutation(pos_feats.size(0))])
    while(len(neg_idx) < batch_neg_cand*maxiter):
        neg_idx = np.concatenate([neg_idx, np.random.permutation(neg_feats.size(0))])
    pos_pointer = 0
    neg_pointer = 0

    for iter in range(maxiter):

        # select pos idx
        pos_next = pos_pointer+batch_pos
        pos_cur_idx = pos_idx[pos_pointer:pos_next]
        # pos_cur_idx = pos_feats.new(pos_cur_idx).long()
        pos_cur_idx = pos_feats.new(pos_cur_idx).long()
        pos_pointer = pos_next

        # select neg idx
        neg_next = neg_pointer+batch_neg_cand
        neg_cur_idx = neg_idx[neg_pointer:neg_next]
        # neg_cur_idx = neg_feats.new(neg_cur_idx).long()
        neg_cur_idx = neg_feats.new(neg_cur_idx).long()
        neg_pointer = neg_next

        # create batch
        batch_pos_feats = Variable(pos_feats.index_select(0, pos_cur_idx))
        batch_neg_feats = Variable(neg_feats.index_select(0, neg_cur_idx))
        # batch_pos_feats = Variable(pos_feats.index_select(0, pos_cur_idx))
        # batch_neg_feats = Variable(neg_feats.index_select(0, neg_cur_idx))

        # hard negative mining
        if batch_neg_cand > batch_neg:
            model.eval() ## model transfer into evaluation mode
            for start in range(0,batch_neg_cand,batch_test):
                end = min(start+batch_test,batch_neg_cand)
                score = model(batch_neg_feats[start:end], in_layer=in_layer)
                if start==0:
                    neg_cand_score = score.data[:,1].clone()
                else:
                    neg_cand_score = torch.cat((neg_cand_score, score.data[:,1].clone()),0)

            _, top_idx = neg_cand_score.topk(batch_neg)
            batch_neg_feats = batch_neg_feats.index_select(0, Variable(top_idx))
            model.train() ## model transfer into train mode

        # forward
        pos_score = model(batch_pos_feats, in_layer=in_layer)
        neg_score = model(batch_neg_feats, in_layer=in_layer)

        # optimize
        loss = criterion(pos_score, neg_score)
        model.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(model.parameters(), opts['grad_clip'])
        optimizer.step()

        if opts['visual_log']:
            print "Iter %d, Loss %.4f" % (iter, loss.data[0])




def run_mdnet(img_list, init_bbox, gt=None, savefig_dir='', display=False):

    ############################################
    ############################################
    ############################################
    # Init bbox
    target_bbox = np.array(init_bbox)
    result = np.zeros((len(img_list),4))
    result_bb = np.zeros((len(img_list),4))
    result[0] = np.copy(target_bbox)
    result_bb[0] = np.copy(target_bbox)

    iou_result = np.zeros((len(img_list),1))

    # execution time array
    exec_time_result = np.zeros((len(img_list),1))

    # Init model
    model = MDNet(opts['model_path'])
    if opts['adaptive_align']:
        align_h = model.roi_align_model.aligned_height
        align_w = model.roi_align_model.aligned_width
        spatial_s = model.roi_align_model.spatial_scale
        model.roi_align_model = RoIAlignAdaMax(align_h, align_w, spatial_s)
        # model.roi_align_model = RoIAlignDenseAdaMax(align_h, align_w, spatial_s)
    if opts['use_gpu']:
        model = model.cuda()
        #torch.backends.cudnn.benchmark = True

    model.set_learnable_params(opts['ft_layers'])

    # Init image crop model
    img_crop_model = imgCropper(opts['padded_img_size'])
    if opts['use_gpu']:
        img_crop_model.gpuEnable()

    # Init criterion and optimizer
    criterion = BinaryLoss()
    init_optimizer = set_optimizer(model, opts['lr_init'])
    update_optimizer = set_optimizer(model, opts['lr_update'])

    tic = time.time()
    # Load first image
    cur_image = Image.open(img_list[0]).convert('RGB')
    cur_image = np.asarray(cur_image)

    # Draw pos/neg samples
    ishape = cur_image.shape
    pos_examples = gen_samples(SampleGenerator('gaussian', (ishape[1],ishape[0]), 0.1, 1.2),
                               target_bbox, opts['n_pos_init'], opts['overlap_pos_init'])
    neg_examples = gen_samples(SampleGenerator('uniform', (ishape[1],ishape[0]), 1, 2, 1.1),
                                target_bbox, opts['n_neg_init'], opts['overlap_neg_init'])
    neg_examples = np.random.permutation(neg_examples)



    # fig1,ax1 = plt.subplots(1)
    # ax1.imshow(cur_image)
    # for i in range(0,pos_examples.shape[0]):
    #     rect = patches.Rectangle((pos_examples[i,0:2]), pos_examples[i,2] , pos_examples[i,3], linewidth=1, edgecolor='r',
    #                          facecolor='none')
    #     ax1.add_patch(rect)
    # plt.show()

    # compute padded sample
    padded_x1 = (neg_examples[:,0]-neg_examples[:,2]*(opts['padding']-1.)/2.).min()
    padded_y1 = (neg_examples[:,1]-neg_examples[:,3]*(opts['padding']-1.)/2.).min()
    padded_x2 = (neg_examples[:,0]+neg_examples[:,2]*(opts['padding']+1.)/2.).max()
    padded_y2 = (neg_examples[:,1]+neg_examples[:,3]*(opts['padding']+1.)/2.).max()
    padded_scene_box = np.reshape(np.asarray((padded_x1,padded_y1,padded_x2-padded_x1,padded_y2-padded_y1)),(1,4))

    scene_boxes = np.reshape(np.copy(padded_scene_box), (1,4))
    if opts['jitter']:
        ## horizontal shift
        jittered_scene_box_horizon = np.copy(padded_scene_box)
        # jittered_scene_box_horizon[0,0] -= 4.
        jitter_scale_horizon = 1.05**(-1)
        # jitter_scale_horizon = 1.
        ## vertical shift
        jittered_scene_box_vertical = np.copy(padded_scene_box)
        # jittered_scene_box_vertical[0,1] -= 4.
        jitter_scale_vertical = 1.05**(1)
        # jitter_scale_vertical = 1.
        ## scale reduction
        jittered_scene_box_reduce = np.copy(padded_scene_box)
        jitter_scale_reduce = 1.05**(-2)
        ## scale enlarge
        jittered_scene_box_enlarge = np.copy(padded_scene_box)
        jitter_scale_enlarge = 1.05 ** (2)

        scene_boxes = np.concatenate([scene_boxes, jittered_scene_box_horizon, jittered_scene_box_vertical,jittered_scene_box_reduce,jittered_scene_box_enlarge],axis=0)
        jitter_scale = [1.,jitter_scale_horizon,jitter_scale_vertical,jitter_scale_reduce,jitter_scale_enlarge]
    else:
        jitter_scale = [1.]

    model.eval()
    for bidx in range(0,scene_boxes.shape[0]):
        crop_img_size = (scene_boxes[bidx,2:4] * ((opts['img_size'],opts['img_size'])/target_bbox[2:4])).astype('int64')*jitter_scale[bidx]
        cropped_image, cur_image_var = img_crop_model.crop_image(cur_image, np.reshape(scene_boxes[bidx],(1,4)), crop_img_size)
        cropped_image = cropped_image - 128.

        feat_map = model(cropped_image, out_layer='conv3')

        rel_target_bbox = np.copy(target_bbox)
        rel_target_bbox[0:2] -= scene_boxes[bidx,0:2]

        batch_num = np.zeros((pos_examples.shape[0], 1))
        cur_pos_rois = np.copy(pos_examples)
        cur_pos_rois[:,0:2] -= np.repeat(np.reshape(scene_boxes[bidx,0:2],(1,2)),cur_pos_rois.shape[0],axis=0)
        scaled_obj_size = float(opts['img_size'])*jitter_scale[bidx]
        cur_pos_rois = samples2maskroi(cur_pos_rois, model.receptive_field,(scaled_obj_size,scaled_obj_size), target_bbox[2:4], opts['padding'])
        cur_pos_rois = np.concatenate((batch_num, cur_pos_rois), axis=1)
        cur_pos_rois = Variable(torch.from_numpy(cur_pos_rois.astype('float32'))).cuda()
        cur_pos_feats = model.roi_align_model(feat_map, cur_pos_rois)
        cur_pos_feats = cur_pos_feats.view(cur_pos_feats.size(0), -1).data.clone()

        batch_num = np.zeros((neg_examples.shape[0], 1))
        cur_neg_rois = np.copy(neg_examples)
        cur_neg_rois[:,0:2] -= np.repeat(np.reshape(scene_boxes[bidx,0:2],(1,2)),cur_neg_rois.shape[0],axis=0)
        cur_neg_rois = samples2maskroi(cur_neg_rois, model.receptive_field, (scaled_obj_size,scaled_obj_size), target_bbox[2:4], opts['padding'])
        cur_neg_rois = np.concatenate((batch_num, cur_neg_rois), axis=1)
        cur_neg_rois = Variable(torch.from_numpy(cur_neg_rois.astype('float32'))).cuda()
        cur_neg_feats = model.roi_align_model(feat_map, cur_neg_rois)
        cur_neg_feats = cur_neg_feats.view(cur_neg_feats.size(0), -1).data.clone()

        feat_dim = cur_pos_feats.size(-1)

        if bidx==0:
            pos_feats = cur_pos_feats
            neg_feats = cur_neg_feats
        else:
            pos_feats = torch.cat((pos_feats,cur_pos_feats),dim=0)
            neg_feats = torch.cat((neg_feats,cur_neg_feats),dim=0)

    # Train bbox regressor
    '''
    bbreg_examples = gen_samples(SampleGenerator('uniform', cur_image.size, 0.3, 1.5, 1.1),
                                 target_bbox, opts['n_bbreg'], opts['overlap_bbreg'], opts['scale_bbreg'])
    bbreg_feats = forward_samples(model, image, bbreg_examples)
    bbreg = BBRegressor(image.size)
    bbreg.train(bbreg_feats, bbreg_examples, target_bbox)
    '''
    #
    # # Draw pos/neg samples
    # pos_examples = gen_samples(SampleGenerator('gaussian', padded_scene_box[2:4], 0.1, 1.2),
    #                            rel_target_bbox, opts['n_pos_init'], opts['overlap_pos_init'])
    # neg_examples = np.concatenate([
    #                 gen_samples(SampleGenerator('uniform', padded_scene_box[2:4], 1, 2, 1.1),
    #                             rel_target_bbox, opts['n_neg_init']//2, opts['overlap_neg_init']),
    #                 gen_samples(SampleGenerator('whole', padded_scene_box[2:4], 0, 1.2, 1.1),
    #                             rel_target_bbox, opts['n_neg_init']//2, opts['overlap_neg_init'])])
    # neg_examples = np.random.permutation(neg_examples)

    # Extract pos/neg features

    # batch_num = np.zeros((pos_examples.shape[0],1))
    # pos_rois = samples2maskroi(pos_examples, model.receptive_field, cshape[0:2],padded_scene_size,opts['padding'])
    # pos_rois = np.concatenate((batch_num,pos_rois),axis=1)
    # pos_rois=Variable(torch.from_numpy(pos_rois.astype('float32'))).cuda()
    # pos_feats = model.roi_align_model(feat_map,pos_rois)
    # pos_feats = pos_feats.view(pos_feats.size(0), -1).data.clone()
    #
    # batch_num = np.zeros((neg_examples.shape[0],1))
    # neg_rois = samples2maskroi(neg_examples,model.receptive_field,cshape[0:2],padded_scene_size,opts['padding'])
    # neg_rois = np.concatenate((batch_num,neg_rois),axis=1)
    # neg_rois=Variable(torch.from_numpy(neg_rois.astype('float32'))).cuda()
    # neg_feats = model.roi_align_model(feat_map, neg_rois)
    # neg_feats = neg_feats.view(neg_feats.size(0), -1).data.clone()
    #
    # feat_dim = pos_feats.size(-1)

    '''
    pos_feats = forward_samples(model, image, pos_examples)
    neg_feats = forward_samples(model, image, neg_examples)
    '''

    # fig1,ax1 = plt.subplots(1)
    # ax1.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8')+128))
    # for i in range(0,pos_examples.shape[0]):
    #     rect = patches.Rectangle((pos_rois.data[i,1:3]), pos_rois.data[i,3] - pos_rois.data[i,1]+model.receptive_field, pos_rois.data[i,4] - pos_rois.data[i,2]+model.receptive_field, linewidth=1, edgecolor='r',
    #                          facecolor='none')
    #     ax1.add_patch(rect)
    # plt.show()
    # fig2,ax2 = plt.subplots(1)
    # ax2.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8')+128))
    # for i in range(0,neg_examples.shape[0]):
    #     rect = patches.Rectangle((neg_rois.data[i,1:3]), neg_rois.data[i,3] - neg_rois.data[i,1], neg_rois.data[i,4] - neg_rois.data[i,2], linewidth=1, edgecolor='b',
    #                          facecolor='none')
    #     ax2.add_patch(rect)
    # plt.show()

    # Initial training
    train(model, criterion, init_optimizer, pos_feats, neg_feats, opts['maxiter_init'])

    # Init sample generators
    # sample_generator = SampleGenerator('gaussian', (cur_image.shape[1],cur_image.shape[0]), opts['trans_f'], opts['scale_f'], valid=True)
    # pos_generator = SampleGenerator('gaussian', (cur_image.shape[1],cur_image.shape[0]), 0.1, 1.2)
    # neg_generator = SampleGenerator('uniform', (cur_image.shape[1],cur_image.shape[0]), 1.5, 1.2)

    # Init pos/neg features for update
    pos_feats_all = [pos_feats[:opts['n_pos_update']]]
    neg_feats_all = [neg_feats[:opts['n_neg_update']]]

    spf_total = time.time()-tic

    # Display
    savefig = savefig_dir != ''
    if display or savefig:
        dpi = 80.0
        figsize = (cur_image.shape[1]/dpi, cur_image.shape[0]/dpi)

        fig = plt.figure(frameon=False, figsize=figsize, dpi=dpi)
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        ax.set_axis_off()
        fig.add_axes(ax)
        im = ax.imshow(cur_image, aspect='normal')

        if gt is not None:
            gt_rect = plt.Rectangle(tuple(gt[0,:2]),gt[0,2],gt[0,3],
                    linewidth=3, edgecolor="#00ff00", zorder=1, fill=False)
            ax.add_patch(gt_rect)

        rect = plt.Rectangle(tuple(result_bb[0,:2]),result_bb[0,2],result_bb[0,3],
                linewidth=3, edgecolor="#ff0000", zorder=1, fill=False)
        ax.add_patch(rect)

        if display:
            plt.pause(.01)
            plt.draw()
        if savefig:
            fig.savefig(os.path.join(savefig_dir,'0000.jpg'),dpi=dpi)

    # Main loop
    trans_f = opts['trans_f']
    for i in range(1,len(img_list)):

        tic = time.time()
        # Load image
        cur_image = Image.open(img_list[i]).convert('RGB')
        cur_image = np.asarray(cur_image)

        # Estimate target bbox
        ishape = cur_image.shape
        samples = gen_samples(SampleGenerator('gaussian', (ishape[1], ishape[0]), trans_f, opts['scale_f'],valid=True), target_bbox, opts['n_samples'])

        padded_x1 = (samples[:, 0] - samples[:, 2]*(opts['padding']-1.)/2.).min()
        padded_y1 = (samples[:, 1] - samples[:,3]*(opts['padding']-1.)/2.).min()
        padded_x2 = (samples[:, 0] + samples[:, 2]*(opts['padding']+1.)/2.).max()
        padded_y2 = (samples[:, 1] + samples[:, 3]*(opts['padding']+1.)/2.).max()
        padded_scene_box = np.asarray((padded_x1, padded_y1, padded_x2 - padded_x1, padded_y2 - padded_y1))


        if padded_scene_box[0] > cur_image.shape[1]:
            padded_scene_box[0] = cur_image.shape[1]-1
        if padded_scene_box[1] > cur_image.shape[0]:
            padded_scene_box[1] = cur_image.shape[0]-1
        if padded_scene_box[0] + padded_scene_box[2] < 0:
            padded_scene_box[2] = -padded_scene_box[0]+1
        if padded_scene_box[1] + padded_scene_box[3] < 0:
            padded_scene_box[3] = -padded_scene_box[1]+1


        crop_img_size = (padded_scene_box[2:4] * ((opts['img_size'], opts['img_size']) / target_bbox[2:4])).astype(
            'int64')
        cropped_image,cur_image_var = img_crop_model.crop_image(cur_image, np.reshape(padded_scene_box,(1,4)),crop_img_size)
        # cropped_image = crop_image(cur_image, padded_scene_box, crop_img_size, 0, False)
        cropped_image = cropped_image - 128.


        # plt.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8') + 128))
        # plt.show()

        model.eval()
        feat_map = model(cropped_image, out_layer='conv3')

        # relative target bbox with padded_scene_box
        rel_target_bbox = np.copy(target_bbox)
        rel_target_bbox[0:2] -= padded_scene_box[0:2]


        # Extract sample features and get target location
        batch_num = np.zeros((samples.shape[0], 1))
        sample_rois = np.copy(samples)
        sample_rois[:, 0:2] -= np.repeat(np.reshape(padded_scene_box[0:2], (1, 2)), sample_rois.shape[0], axis=0)
        sample_rois = samples2maskroi(sample_rois,model.receptive_field, (opts['img_size'],opts['img_size']), target_bbox[2:4],opts['padding'])
        sample_rois = np.concatenate((batch_num, sample_rois), axis=1)
        sample_rois = Variable(torch.from_numpy(sample_rois.astype('float32'))).cuda()
        sample_feats = model.roi_align_model(feat_map, sample_rois)
        sample_feats = sample_feats.view(sample_feats.size(0), -1).clone()
        sample_scores = model(sample_feats, in_layer='fc4')
        # sample_scores = forward_samples(model, image, samples, out_layer='fc6')
        top_scores, top_idx = sample_scores[:,1].topk(5)
        top_idx = top_idx.data.cpu().numpy()
        target_score = top_scores.data.mean()
        target_bbox = samples[top_idx].mean(axis=0)

        success = target_score > opts['success_thr']

        if (success>0) and opts['multi_scale_infer']:
            samples = gen_samples(SampleGenerator('gaussian', (ishape[1], ishape[0]), 0.2, opts['scale_f'], valid=True), target_bbox,32)

            padded_x1 = (samples[:, 0] - samples[:, 2] * (opts['padding'] - 1.) / 2.).min()
            padded_y1 = (samples[:, 1] - samples[:, 3] * (opts['padding'] - 1.) / 2.).min()
            padded_x2 = (samples[:, 0] + samples[:, 2] * (opts['padding'] + 1.) / 2.).max()
            padded_y2 = (samples[:, 1] + samples[:, 3] * (opts['padding'] + 1.) / 2.).max()
            padded_scene_box = np.asarray((padded_x1, padded_y1, padded_x2 - padded_x1, padded_y2 - padded_y1))

            if padded_scene_box[0] > cur_image.shape[1]:
                padded_scene_box[0] = cur_image.shape[1] - 1
            if padded_scene_box[1] > cur_image.shape[0]:
                padded_scene_box[1] = cur_image.shape[0] - 1
            if padded_scene_box[0] + padded_scene_box[2] < 0:
                padded_scene_box[2] = -padded_scene_box[0] + 1
            if padded_scene_box[1] + padded_scene_box[3] < 0:
                padded_scene_box[3] = -padded_scene_box[1] + 1

            multi_scales = [1.05**(-2), 1.05**(-1), 1.05**1,1.05**2]
            coarse_target_bbox = np.copy(target_bbox)
            for midx in range(len(multi_scales)):
                crop_img_size = multi_scales[midx]*(padded_scene_box[2:4] * ((opts['img_size'], opts['img_size']) / coarse_target_bbox[2:4])).astype('int64')
                cropped_image, cur_image_var = img_crop_model.crop_image(cur_image, np.reshape(padded_scene_box, (1, 4)),crop_img_size)
                cropped_image = cropped_image - 128.

                # plt.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8') + 128))
                # plt.show()

                model.eval()
                feat_map = model(cropped_image, out_layer='conv3')

                # Extract sample features and get target location
                batch_num = np.zeros((samples.shape[0], 1))
                sample_rois = np.copy(samples)
                sample_rois[:, 0:2] -= np.repeat(np.reshape(padded_scene_box[0:2], (1, 2)), sample_rois.shape[0], axis=0)
                scaled_obj_size = multi_scales[midx] * opts['img_size']
                sample_rois = samples2maskroi(sample_rois, model.receptive_field, (scaled_obj_size, scaled_obj_size),coarse_target_bbox[2:4], opts['padding'])
                sample_rois = np.concatenate((batch_num, sample_rois), axis=1)
                sample_rois = Variable(torch.from_numpy(sample_rois.astype('float32'))).cuda()
                sample_feats = model.roi_align_model(feat_map, sample_rois)
                sample_feats = sample_feats.view(sample_feats.size(0), -1).clone()
                sample_scores = model(sample_feats, in_layer='fc4')
                # sample_scores = forward_samples(model, image, samples, out_layer='fc6')
                top_scores, top_idx = sample_scores[:, 1].topk(5)
                top_idx = top_idx.data.cpu().numpy()
                cur_target_score = top_scores.data.mean()
                if cur_target_score > target_score:
                    target_score = cur_target_score
                    target_bbox = samples[top_idx].mean(axis=0)

                # fig1,ax1 = plt.subplots(1)
                # ax1.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8')+128))
                # for sidx in range(0,samples.shape[0]-1):
                #     rect = patches.Rectangle((sample_rois.data[sidx,1:3]), sample_rois.data[sidx,3] - sample_rois.data[sidx,1]+model.receptive_field, sample_rois.data[sidx,4] - sample_rois.data[sidx,2]+model.receptive_field, linewidth=1, edgecolor='r',
                #                              facecolor='none')
                #     ax1.add_patch(rect)
                # plt.draw()
                # plt.show()
                #
                # print('debug')

            success = target_score > opts['success_thr']

        # # Expand search area at failure
        if success:
             # sample_generator.set_trans_f(opts['trans_f'])
            trans_f = opts['trans_f']
        else:
            trans_f = opts['trans_f_expand']
        #     # sample_generator.set_trans_f(opts['trans_f_expand'])
        #
        # fig1,ax1 = plt.subplots(1)
        # ax1.imshow(np.squeeze(cropped_image.data.cpu().numpy().transpose(0, 2, 3, 1).astype('uint8')+128))
        # for sidx in range(0,samples.shape[0]-1):
        #     rect = patches.Rectangle((sample_rois.data[sidx,1:3]), sample_rois.data[sidx,3] - sample_rois.data[sidx,1]+model.receptive_field, sample_rois.data[sidx,4] - sample_rois.data[sidx,2]+model.receptive_field, linewidth=1, edgecolor='r',
        #                          facecolor='none')
        #     ax1.add_patch(rect)
        # rect = patches.Rectangle((rel_target_bbox[0:2]*((opts['img_size'],opts['img_size'])/rel_target_bbox[2:4])),opts['img_size'],opts['img_size'],linewidth=3, edgecolor='b',facecolor='none')
        # ax1.add_patch(rect)
        #
        # plt.draw()
        # plt.show()

        # Bbox regression
        # if success:
        #     bbreg_samples = samples[top_idx]
        #     bbreg_feats = forward_samples(model, image, bbreg_samples)
        #     bbreg_samples = bbreg.predict(bbreg_feats, bbreg_samples)
        #     bbreg_bbox = bbreg_samples.mean(axis=0)
        # else:
        #     bbreg_bbox = target_bbox

        bbreg_bbox = np.copy(target_bbox)

        # Copy previous result at failure
        # (ilchae) this is different from original MD-Net
        # if not success:
        #     target_bbox = result[i-1]
        #     bbreg_bbox = result_bb[i-1]

        # Save result
        result[i] = target_bbox
        result_bb[i] = bbreg_bbox
        iou_result[i] = 1.

        # Data collect
        if success:

            # Draw pos/neg samples
            pos_examples = gen_samples(
                SampleGenerator('gaussian', (ishape[1], ishape[0]), 0.1, 1.2), target_bbox,
                opts['n_pos_update'],
                opts['overlap_pos_update'])
            neg_examples = gen_samples(
                SampleGenerator('uniform', (ishape[1], ishape[0]), 1.5, 1.2), target_bbox,
                opts['n_neg_update'],
                opts['overlap_neg_update'])

            padded_x1 = (neg_examples[:, 0] - neg_examples[:, 2] * (opts['padding'] - 1.) / 2.).min()
            padded_y1 = (neg_examples[:, 1] - neg_examples[:, 3] * (opts['padding'] - 1.) / 2.).min()
            padded_x2 = (neg_examples[:, 0] + neg_examples[:, 2] * (opts['padding'] + 1.) / 2.).max()
            padded_y2 = (neg_examples[:, 1] + neg_examples[:, 3] * (opts['padding'] + 1.) / 2.).max()
            padded_scene_box = np.reshape(np.asarray((padded_x1, padded_y1, padded_x2 - padded_x1, padded_y2 - padded_y1)),(1,4))

            scene_boxes = np.reshape(np.copy(padded_scene_box), (1, 4))
            if opts['online_jitter']:
                ## horizontal shift
                jittered_scene_box_horizon = np.copy(padded_scene_box)
                jittered_scene_box_horizon[0, 0] -= 4.
                jitter_scale_horizon = 1.
                ## vertical shift
                jittered_scene_box_vertical = np.copy(padded_scene_box)
                jittered_scene_box_vertical[0, 1] -= 4.
                jitter_scale_vertical = 1.
                ## scale reduction
                jittered_scene_box_reduce = np.copy(padded_scene_box)
                jitter_scale_reduce = 1.05 ** (-1)
                ## scale enlarge
                jittered_scene_box_enlarge = np.copy(padded_scene_box)
                jitter_scale_enlarge = 1.05 ** (1)

                scene_boxes = np.concatenate([scene_boxes, jittered_scene_box_horizon, jittered_scene_box_vertical, jittered_scene_box_reduce,jittered_scene_box_enlarge], axis=0)
                jitter_scale = [1., jitter_scale_horizon, jitter_scale_vertical, jitter_scale_reduce,jitter_scale_enlarge]
            else:
                jitter_scale = [1.]

            for bidx in range(0, scene_boxes.shape[0]):
                crop_img_size = (scene_boxes[bidx, 2:4] * ((opts['img_size'], opts['img_size']) / target_bbox[2:4])).astype('int64') * jitter_scale[bidx]
                cropped_image, cur_image_var = img_crop_model.crop_image(cur_image,np.reshape(scene_boxes[bidx], (1, 4)),crop_img_size)
                cropped_image = cropped_image - 128.

                feat_map = model(cropped_image, out_layer='conv3')

                rel_target_bbox = np.copy(target_bbox)
                rel_target_bbox[0:2] -= scene_boxes[bidx, 0:2]

                batch_num = np.zeros((pos_examples.shape[0], 1))
                cur_pos_rois = np.copy(pos_examples)
                cur_pos_rois[:, 0:2] -= np.repeat(np.reshape(scene_boxes[bidx, 0:2], (1, 2)), cur_pos_rois.shape[0],axis=0)
                scaled_obj_size = float(opts['img_size']) * jitter_scale[bidx]
                cur_pos_rois = samples2maskroi(cur_pos_rois, model.receptive_field, (scaled_obj_size, scaled_obj_size),target_bbox[2:4], opts['padding'])
                cur_pos_rois = np.concatenate((batch_num, cur_pos_rois), axis=1)
                cur_pos_rois = Variable(torch.from_numpy(cur_pos_rois.astype('float32'))).cuda()
                cur_pos_feats = model.roi_align_model(feat_map, cur_pos_rois)
                cur_pos_feats = cur_pos_feats.view(cur_pos_feats.size(0), -1).data.clone()

                batch_num = np.zeros((neg_examples.shape[0], 1))
                cur_neg_rois = np.copy(neg_examples)
                cur_neg_rois[:, 0:2] -= np.repeat(np.reshape(scene_boxes[bidx, 0:2], (1, 2)), cur_neg_rois.shape[0],
                                                  axis=0)
                cur_neg_rois = samples2maskroi(cur_neg_rois, model.receptive_field, (scaled_obj_size, scaled_obj_size),
                                               target_bbox[2:4], opts['padding'])
                cur_neg_rois = np.concatenate((batch_num, cur_neg_rois), axis=1)
                cur_neg_rois = Variable(torch.from_numpy(cur_neg_rois.astype('float32'))).cuda()
                cur_neg_feats = model.roi_align_model(feat_map, cur_neg_rois)
                cur_neg_feats = cur_neg_feats.view(cur_neg_feats.size(0), -1).data.clone()

                feat_dim = cur_pos_feats.size(-1)

                if bidx == 0:
                    pos_feats = cur_pos_feats ##index select
                    neg_feats = cur_neg_feats
                else:
                    pos_feats = torch.cat((pos_feats, cur_pos_feats), dim=0)
                    neg_feats = torch.cat((neg_feats, cur_neg_feats), dim=0)

            if pos_feats.size(0) > opts['n_pos_update']:
                pos_idx = np.asarray(range(pos_feats.size(0)))
                np.random.shuffle(pos_idx)
                pos_feats = pos_feats.index_select(0, torch.from_numpy(pos_idx[0:opts['n_pos_update']]).cuda())
            if neg_feats.size(0) > opts['n_neg_update']:
                neg_idx = np.asarray(range(neg_feats.size(0)))
                np.random.shuffle(neg_idx)
                neg_feats = neg_feats.index_select(0,torch.from_numpy(neg_idx[0:opts['n_neg_update']]).cuda())

            pos_feats_all.append(pos_feats)
            neg_feats_all.append(neg_feats)

            if len(pos_feats_all) > opts['n_frames_long']:
                del pos_feats_all[0]
            if len(neg_feats_all) > opts['n_frames_short']:
                del neg_feats_all[0]

        # Short term update
        if not success:
            if opts['short_update_enable']:
                nframes = min(opts['n_frames_short'],len(pos_feats_all))
                pos_data = torch.stack(pos_feats_all[-nframes:],0).view(-1,feat_dim)
                neg_data = torch.stack(neg_feats_all,0).view(-1,feat_dim)
                train(model, criterion, update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        # Long term update
        elif i % opts['long_interval'] == 0:
            pos_data = torch.stack(pos_feats_all,0).view(-1,feat_dim)
            neg_data = torch.stack(neg_feats_all,0).view(-1,feat_dim)
            train(model, criterion, update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        spf = time.time()-tic
        spf_total += spf

        # Display
        if display or savefig:
            im.set_data(cur_image)

            if gt is not None:
                gt_rect.set_xy(gt[i,:2])
                gt_rect.set_width(gt[i,2])
                gt_rect.set_height(gt[i,3])

            rect.set_xy(result_bb[i,:2])
            rect.set_width(result_bb[i,2])
            rect.set_height(result_bb[i,3])

            if display:
                plt.pause(.01)
                plt.draw()
            if savefig:
                fig.savefig(os.path.join(savefig_dir,'%04d.jpg'%(i)),dpi=dpi)

        if opts['visual_log']:
            if gt is None:
                print "Frame %d/%d, Score %.3f, Time %.3f" % \
                    (i, len(img_list), target_score, spf)
            else:
                print "Frame %d/%d, Overlap %.3f, Score %.3f, Time %.3f" % \
                    (i, len(img_list), overlap_ratio(gt[i],result_bb[i])[0], target_score, spf)
        iou_result[i]= overlap_ratio(gt[i],result_bb[i])[0]

    fps = len(img_list) / spf_total
    return iou_result, result_bb, fps


'''
if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--seq', default='', help='input seq')
    parser.add_argument('-j', '--json', default='', help='input json')
    parser.add_argument('-f', '--savefig', action='store_true')
    parser.add_argument('-d', '--display', action='store_true')

    args = parser.parse_args()
    assert(args.seq != '' or args.json != '')

    # Generate sequence config
    img_list, init_bbox, gt, savefig_dir, display, result_path = gen_config(args)

    # Run tracker
    result, result_bb, fps = run_mdnet(img_list, init_bbox, gt=gt, savefig_dir=savefig_dir, display=display)

    # Save result
    res = {}
    res['res'] = result_bb.round().tolist()
    res['type'] = 'rect'
    res['fps'] = fps
    json.dump(res, open(result_path, 'w'), indent=2)
'''
