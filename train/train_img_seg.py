from __future__ import print_function

import os
import sys
import argparse
import importlib
import numpy as np
import tensorflow as tf
slim = tf.contrib.slim
import pickle
from threading import Thread
from datetime import datetime
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'models'))
sys.path.append(os.path.join(ROOT_DIR, 'dataset'))
from data_conf import g_type2onehotclass
from rpn_dataset import Dataset
from model_util import NUM_FG_POINT
from img_seg_net import ImgSegNet, NUM_SEG_CLASSES
import train_util

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--log_dir', default='log', help='Log dir [default: log]')
parser.add_argument('--num_point', type=int, default=16384, help='Point Number [default: 16384]')
parser.add_argument('--max_epoch', type=int, default=201, help='Epoch to run [default: 201]')
parser.add_argument('--batch_size', type=int, default=16, help='Batch Size during training [default: 16]')
parser.add_argument('--learning_rate', type=float, default=0.002, help='Initial learning rate [default: 0.002]')
parser.add_argument('--momentum', type=float, default=0.9, help='Initial learning rate [default: 0.9]')
parser.add_argument('--optimizer', default='adam', help='adam or momentum [default: adam]')
parser.add_argument('--decay_step', type=int, default=200000, help='Decay step for lr decay [default: 200000]')
parser.add_argument('--decay_rate', type=float, default=0.7, help='Decay rate for lr decay [default: 0.7]')
parser.add_argument('--restore_model_path', default=None, help='Restore model path e.g. log/model.ckpt [default: None]')
FLAGS = parser.parse_args()

# Set training configurations
EPOCH_CNT = 0
BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
GPU_INDEX = FLAGS.gpu
MOMENTUM = FLAGS.momentum
OPTIMIZER = FLAGS.optimizer
DECAY_STEP = FLAGS.decay_step
DECAY_RATE = FLAGS.decay_rate

LOG_DIR = FLAGS.log_dir
if not os.path.exists(LOG_DIR): os.mkdir(LOG_DIR)
LOG_FOUT = open(os.path.join(LOG_DIR, 'log_train.txt'), 'w')
LOG_FOUT.write(str(FLAGS)+'\n')

BN_INIT_DECAY = 0.5
BN_DECAY_DECAY_RATE = 0.5
BN_DECAY_DECAY_STEP = float(DECAY_STEP)
BN_DECAY_CLIP = 0.99

def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)

def get_learning_rate(batch):
    learning_rate = tf.train.exponential_decay(
                        BASE_LEARNING_RATE,  # Base learning rate.
                        batch * BATCH_SIZE,  # Current index into the dataset.
                        DECAY_STEP,          # Decay step.
                        DECAY_RATE,          # Decay rate.
                        staircase=True)
    learing_rate = tf.maximum(learning_rate, 0.00001) # CLIP THE LEARNING RATE!
    return learning_rate

def get_bn_decay(batch):
    bn_momentum = tf.train.exponential_decay(
                      BN_INIT_DECAY,
                      batch*BATCH_SIZE,
                      BN_DECAY_DECAY_STEP,
                      BN_DECAY_DECAY_RATE,
                      staircase=True)
    bn_decay = tf.minimum(BN_DECAY_CLIP, 1 - bn_momentum)
    return bn_decay


TRAIN_DATASET = Dataset(NUM_POINT, '/data/ssd/public/jlliu/Kitti/object', 'train', is_training=True, train_img_seg=True)
# data loading threads
# FIXME: don't use data augmentation with image feature before calib matrix is adjust accordingly
train_produce_thread = Thread(target=TRAIN_DATASET.load, args=(True,))
train_produce_thread.start()

def train():
    ''' Main function for training and simple evaluation. '''

    with tf.Graph().as_default():
        with tf.device('/gpu:0'):
            # is_training_pl = tf.placeholder(tf.bool, shape=())

            # Note the global_step=batch parameter to minimize.
            # That tells the optimizer to increment the 'batch' parameter
            # for you every time it trains.
            batch = tf.get_variable('batch', [],
                initializer=tf.constant_initializer(0), trainable=False)
            bn_decay = get_bn_decay(batch)
            tf.summary.scalar('bn_decay', bn_decay)

            # Get model and losses
            img_seg_net = ImgSegNet(BATCH_SIZE, NUM_POINT, num_channel=4, bn_decay=bn_decay, is_training=True)
            placeholders = img_seg_net.placeholders
            end_points = img_seg_net.end_points
            loss = img_seg_net.get_loss()

            # Get training operator
            learning_rate = get_learning_rate(batch)
            tf.summary.scalar('learning_rate', learning_rate)
            if OPTIMIZER == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate,
                    momentum=MOMENTUM)
            elif OPTIMIZER == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate)

            # Note: when training, the moving_mean and moving_variance need to be updated.
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                #train_op = optimizer.minimize(loss, global_step=batch)
                train_op = slim.learning.create_train_op(
                    loss,
                    optimizer,
                    #clip_gradient_norm=1.0,
                    global_step=batch)

            # Add ops to save and restore all the variables.
            saver = tf.train.Saver()

        # Create a session
        config = tf.ConfigProto()
        #config.gpu_options.allow_growth = True
        config.gpu_options.allow_growth = False
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        # Add summary writers
        merged = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'train'), sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'test'), sess.graph)

        # Init variables
        if FLAGS.restore_model_path is None:
            init = tf.global_variables_initializer()
            sess.run(init)
        else:
            saver.restore(sess, FLAGS.restore_model_path)

        ops = {
            'loss': loss,
            'train_op': train_op,
            'step': batch,
            'merged': merged,
            'end_points': end_points}

        for epoch in range(MAX_EPOCH):
            log_string('**** EPOCH %03d ****' % (epoch))
            sys.stdout.flush()
            train_one_epoch(sess, ops, placeholders, train_writer)
            save_path = saver.save(sess, os.path.join(LOG_DIR, "model.ckpt.%03d" % epoch))
            log_string("Model saved in file: {0}".format(save_path))
            if epoch % 2 == 0:
                val_loss = eval_one_epoch(sess, ops, placeholders, test_writer)
    TRAIN_DATASET.stop_loading()
    train_produce_thread.join()


def train_one_epoch(sess, ops, pls, train_writer):
    is_training = True
    log_string(str(datetime.now()))

    # To collect statistics
    total_correct = 0
    total_seen = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    tp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    fp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    fn = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    loss_sum = 0
    total_nms = 0

    # Training with batches
    batch_idx = 0
    while(True):
        batch_data, is_last_batch = TRAIN_DATASET.get_next_batch(BATCH_SIZE)

        feed_dict = {
            pls['pointclouds']: batch_data['pointcloud'],
            pls['img_inputs']: batch_data['images'],
            pls['calib']: batch_data['calib'],
            pls['seg_labels']: batch_data['seg_label'],
            pls['is_training_pl']: is_training
        }
        summary, step, loss_val, _, logits_val = sess.run([
            ops['merged'], ops['step'], ops['loss'], ops['train_op'],
            ops['end_points']['foreground_logits']], feed_dict=feed_dict)

        train_writer.add_summary(summary, step)

        # segmentation acc
        preds_val = np.argmax(logits_val, 2)
        correct = np.sum(preds_val == batch_data['seg_label'])
        for c in ['Car', 'Pedestrian', 'Cyclist']:
            one_hot_class = g_type2onehotclass[c]
            tp[c] += np.sum(np.logical_and(preds_val == batch_data['seg_label'], batch_data['seg_label'] == one_hot_class))
            fp[c] += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] != one_hot_class))
            fn[c] += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] == one_hot_class))
        total_correct += correct
        total_tp += np.sum(np.logical_and(preds_val == batch_data['seg_label'], batch_data['seg_label'] != 0))
        total_fp += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] == 0))
        total_fn += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] != 0))
        total_seen += NUM_POINT * BATCH_SIZE
        loss_sum += loss_val

        if (batch_idx+1)%10 == 0:
            sample_num = 10 * BATCH_SIZE
            log_string(' -- %03d --' % (batch_idx+1))
            log_string('mean loss: %f' % (loss_sum / sample_num))
            if total_seen > 0:
                log_string('segmentation accuracy: %f' % \
                    (total_correct / float(total_seen)))
            if total_tp+total_fn > 0 and total_tp+total_fp > 0:
                log_string('segmentation recall: %f'% \
                    (float(total_tp)/(total_tp+total_fn)))
                log_string('segmentation precision: %f'% \
                    (float(total_tp)/(total_tp+total_fp)))
            for c in ['Car', 'Pedestrian', 'Cyclist']:
                if (tp[c]+fn[c] == 0) or (tp[c]+fp[c]) == 0:
                    continue
                log_string(c + ' segmentation recall: %f'% \
                    (float(tp[c])/(tp[c]+fn[c])))
                log_string(c + ' segmentation precision: %f'% \
                    (float(tp[c])/(tp[c]+fp[c])))
            total_correct = 0
            total_seen = 0
            total_tp = 0
            total_fp = 0
            total_fn = 0
            tp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
            fp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
            fn = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
            loss_sum = 0
            total_nms = 0
        if is_last_batch:
            break
        batch_idx += 1



def eval_one_epoch(sess, ops, pls, test_writer):
    TEST_DATASET = Dataset(NUM_POINT, '/data/ssd/public/jlliu/Kitti/object', 'val', is_training=True, train_img_seg=True)
    test_produce_thread = Thread(target=TEST_DATASET.load, args=(False,))
    test_produce_thread.start()

    global EPOCH_CNT
    is_training = False
    #is_training = True
    log_string(str(datetime.now()))
    log_string('---- EPOCH %03d EVALUATION ----'%(EPOCH_CNT))

    # To collect statistics
    total_correct = 0
    total_seen = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    tp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    fp = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    fn = {'Car': 0, 'Pedestrian': 0, 'Cyclist': 0}
    loss_sum = 0
    num_samples = 0

    while(True):
        batch_data, is_last_batch = TEST_DATASET.get_next_batch(BATCH_SIZE)

        feed_dict = {
            pls['pointclouds']: batch_data['pointcloud'],
            pls['img_inputs']: batch_data['images'],
            pls['calib']: batch_data['calib'],
            pls['seg_labels']: batch_data['seg_label'],
            pls['is_training_pl']: is_training
        }

        summary, step, loss_val, logits_val = sess.run([
            ops['merged'], ops['step'], ops['loss'],
            ops['end_points']['foreground_logits']], feed_dict=feed_dict)
        test_writer.add_summary(summary, step)

        # segmentation acc
        preds_val = np.argmax(logits_val, 2)
        correct = np.sum(preds_val == batch_data['seg_label'])
        for c in ['Car', 'Pedestrian', 'Cyclist']:
            one_hot_class = g_type2onehotclass[c]
            tp[c] += np.sum(np.logical_and(preds_val == batch_data['seg_label'], batch_data['seg_label'] == one_hot_class))
            fp[c] += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] != one_hot_class))
            fn[c] += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] == one_hot_class))
        total_tp += np.sum(np.logical_and(preds_val == batch_data['seg_label'], batch_data['seg_label'] != 0))
        total_fp += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] == 0))
        total_fn += np.sum(np.logical_and(preds_val != batch_data['seg_label'], batch_data['seg_label'] != 0))
        total_correct += correct
        total_seen += NUM_POINT * BATCH_SIZE
        loss_sum += loss_val
        num_samples += BATCH_SIZE
        if is_last_batch:
            break

    log_string('eval mean loss: %f' % (loss_sum / float(num_samples)))
    log_string('eval segmentation accuracy: %f'% \
        (total_correct / float(total_seen)))
    if total_tp+total_fn > 0 and total_tp+total_fp > 0:
        log_string('eval segmentation recall: %f'% \
            (float(total_tp)/(total_tp+total_fn)))
        log_string('eval segmentation precision: %f'% \
            (float(total_tp)/(total_tp+total_fp)))
    for c in ['Car', 'Pedestrian', 'Cyclist']:
        if (tp[c]+fn[c] == 0) or (tp[c]+fp[c]) == 0:
            continue
        log_string(c + 'eval segmentation recall: %f'% \
            (float(tp[c])/(tp[c]+fn[c])))
        log_string(c + 'eval segmentation precision: %f'% \
            (float(tp[c])/(tp[c]+fp[c])))
    EPOCH_CNT += 1

    TEST_DATASET.stop_loading()
    test_produce_thread.join()

    return loss_sum / float(num_samples)

if __name__ == "__main__":
    log_string('pid: %s'%(str(os.getpid())))
    train()
    LOG_FOUT.close()
