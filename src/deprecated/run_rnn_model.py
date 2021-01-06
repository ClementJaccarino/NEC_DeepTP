import os
os.environ["CUDA_VISIBLE_DEVICES"]="-1"
import numpy as np
import tensorflow as tf
from configparser import ConfigParser
from rnn_encoder_decoder import LSTM_model
from datasets import DatasetEncoderDecoder, DatasetSample, _pad_and_flip_FP
import logging
import time
from tensorflow.python.client import device_lib
from sklearn.metrics import confusion_matrix
import pickle
import math
from utils import g

def get_available_gpus():
    """
    Returns the number of GPUs available on this system.
    """
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

def check_if_gpu_available(gpu_name):
    """
    Returns boolean of if a specific gpu_name (string) is available
    On the system
    """
    list_of_gpus = get_available_gpus()
    if gpu_name not in list_of_gpus:
        return False
    else:
        return True
class trainRNN:
    def __init__(self, 
                 conf_path,
                 sample_traj = False,
                 model_name=None
                 ):

        self.conf_path = conf_path
        self.sample_traj = sample_traj
        self.model_name = model_name
        self.load_configs()
        self.set_up_directories(self.model_name)
        # if don't have gpu, then set device to be cpu
        if not check_if_gpu_available(self.tf_device):
            self.tf_device = '/cpu:0'
        logging.info('Using device %s for main computations', self.tf_device)
        
        if not self.sample_traj:
            self.cpu_dataset = DatasetEncoderDecoder(actual_track_datapath = '../../DATA/DeepTP/processed_flight_tracks.csv',
                                                     flight_plan_datapath = '../../DATA/DeepTP/processed_flight_plans.csv',
                                                     flight_plan_utilize_datapath = '../../DATA/DeepTP/IAH_BOS_Act_Flt_Trk_20130101_1231.CSV',
                                                     feature_cubes_datapath = '../../DATA/DeepTP/feature_cubes.npz',
                                                     shuffle_or_not = True,
                                                     split = True,
                                                     batch_size = self.batch_size,
                                                     dep_lat = 29.98333333,
                                                     dep_lon = -95.33333333)
        else:
            self.std_arr_loader = np.load('../../DATA/DeepTP/standardize_arr.npz')
            self.std_arr_loader['track_mean']
            self.std_arr_loader['track_std']
            self.std_arr_loader['fp_mean']
            self.std_arr_loader['fp_std']
            self.std_arr_loader['feature_mean']
            self.std_arr_loader['feature_std']

    def load_configs(self):
        parser = ConfigParser(os.environ)
        if not os.path.exists(self.conf_path):
            raise IOError("Configuration file '%s' does not exist" % self.conf_path)
        logging.info('Loading config from %s', self.conf_path)
        parser.read(self.conf_path)

        # set which set of configs to import
        config_header = 'nn'
        logger.info('config header: %s', config_header)
        self.epochs = parser.getint(config_header, 'epochs')
        logger.debug('self.epochs = %d', self.epochs)
        # number of feature length
        self.n_input = parser.getint(config_header, 'n_input')
        self.n_channels = parser.getint('convolution', 'n_channels')
        self.n_controled_var = parser.getint('lstm', 'n_controled_var')
        self.n_encode = parser.getint(config_header, 'n_encode')
        self.state_size = parser.getint('lstm', 'n_cell_dim')
        self.n_layer = parser.getint('lstm', 'n_lstm_layers')
        # Number of contextual samples to include
        self.batch_size = parser.getint(config_header, 'batch_size')
        logger.debug('self.batch_size = %d', self.batch_size)
        self.model_dir = parser.get(config_header, 'model_dir')
        self.data_dir = parser.get(config_header, 'data_dir')
        self.n_mixture = parser.getint('lstm', 'n_mixture')
        logger.debug('self.n_mixture = %d', self.n_mixture)
        self.clipping = parser.getboolean(config_header, 'gradient_clipping')
        self.shuffle_data_after_epoch = parser.getboolean(config_header, 'shuffle_data_after_epoch')
        # set the session name
        self.session_name = '{}_{}'.format('Encoder_decoder_LSTM', time.strftime("%Y%m%d-%H%M%S"))
        sess_prefix_str = 'develop'
        if len(sess_prefix_str) > 0:
            self.session_name = '{}_{}'.format(sess_prefix_str, self.session_name)

        # How often to save the model
        self.SAVE_MODEL_EPOCH_NUM = parser.getint(config_header, 'SAVE_MODEL_EPOCH_NUM')
        self.VALIDATION_EPOCH_NUM = parser.getint(config_header, 'VALIDATION_EPOCH_NUM')
        
        # set up GPU if available
        self.tf_device = str(parser.get(config_header, 'tf_device'))
        # optimizer
        self.beta1 = parser.getfloat(config_header, 'beta1')
        self.beta2 = parser.getfloat(config_header, 'beta2')
        self.epsilon = parser.getfloat(config_header, 'epsilon')
        self.learning_rate = parser.getfloat(config_header, 'learning_rate')
        logger.debug('self.learning_rate = %.6f', self.learning_rate)

    def set_up_directories(self, model_name):
        # Set up model directory
        self.model_dir = os.path.join(os.getcwd(), self.model_dir)
        # summary will contain logs
        self.SUMMARY_DIR = os.path.join(
            self.model_dir, "summary", self.session_name)
        # session will contain models
        self.SESSION_DIR = os.path.join(
            self.model_dir, "session", self.session_name)

        if not self.sample_traj:
            if not os.path.exists(self.SESSION_DIR):
                os.makedirs(self.SESSION_DIR)
            if not os.path.exists(self.SUMMARY_DIR):
                os.makedirs(self.SUMMARY_DIR)

        # set the model name and restore if not None
        if model_name is not None:
            tmpSess = os.path.join(self.model_dir, "session")
            self.restored_model_path = os.path.join(tmpSess, model_name)
        else:
            self.restored_model_path = None

    def run_model(self, 
                  train_from_model = False,
                  test_data_start_track = None):
                # define a graph
        self.graph = tf.Graph()
        with self.graph.as_default():
            # with tf.device(self.tf_device):
            self.launchGraph()
            self.sess = tf.Session()

            if not self.sample_traj:
                self.writer = tf.summary.FileWriter(self.SUMMARY_DIR, graph=self.sess.graph)

            # Add ops to save and restore all the variables
            self.saver = tf.train.Saver(max_to_keep=50)
            section = '\n{0:=^40}\n'
            if self.restored_model_path is None:
                self.sess.run(tf.global_variables_initializer())
                self.sess.graph.finalize()
                logger.info("===============================================================")
                # logger.info("Load data into a queue ...")
                # self.sess.run(self.iterator.initializer, feed_dict={self.input_tensor: self.cpu_dataset.train_tracks,
                #                                                     self.seq_length:self.cpu_dataset.train_seq_lens,
                #                                                     self.target:self.cpu_dataset.train_tracks,
                #                                                     self.BATCH_SIZE: self.batch_size})
                self.total_samples = self.cpu_dataset.n_train_data_set
                logger.info("Total training sample size is %d", self.total_samples)
                logger.info("===============================================================")
                logger.info("Start Training ...")
                self.run_training_epoch()
            else:
                self.saver.restore(self.sess, self.restored_model_path)
                if train_from_model is True:
                    self.sess.graph.finalize()
                    logger.info("===============================================================")
                    logger.info(section.format('Run training epoch from restored model %s'%self.restored_model_path))
                    self.total_samples = self.cpu_dataset.n_train_data_set
                    logger.info("Total training sample size is %d", self.total_samples)
                    logger.info("===============================================================")
                    logger.info("Start Training ...")
                    self.run_training_epoch()
                else:
                    logger.info("===============================================================")
                    logger.info(section.format('Restore model from %s'%self.restored_model_path))
                    if self.sample_traj:
                        self.dataset_sample = DatasetSample(train_track_mean = self.std_arr_loader['track_mean'],
                                                            train_track_std = self.std_arr_loader['track_std'],
                                                            train_fp_mean = self.std_arr_loader['fp_mean'],
                                                            train_fp_std = self.std_arr_loader['fp_std'],
                                                            feature_cubes_mean =self.std_arr_loader['feature_mean'],
                                                            feature_cubes_std = self.std_arr_loader['feature_std'],
                                                            ncwf_data_rootdir = '../../DATA/NCWF/gridded_storm_hourly/',
                                                            test_track_dir = '../../DATA/DeepTP/test_flight_tracks.csv',
                                                            test_fp_dir = '../../DATA/DeepTP/test_flight_plans.csv',
                                                            flight_plan_util_dir = '../../DATA/DeepTP/test_flight_plans_util.CSV',
                                                            wind_data_rootdir = '../../DATA/filtered_weather_data/namanl_small_npz/',
                                                            grbs_common_info_dir = '/media/storage/DATA/filtered_weather_data/grbs_common_info.npz',
                                                            grbs_lvl_dict_dir = '/media/storage/DATA/filtered_weather_data/grbs_level_common_info.pkl',
                                                            grbs_smallgrid_kdtree_dir = '/media/storage/DATA/filtered_weather_data/grbs_smallgrid_kdtree.pkl',
                                                            ncwf_arr_dir = '../../DATA/NCWF/gridded_storm.npz',
                                                            ncwf_alt_dict_dir = '../../DATA/NCWF/alt_dict.pkl')
                        fp_tracks_split, tracks_split, fp_seq_length, seq_length, flight_tracks = self.dataset_sample.process_test_tracks()

                        start_tracks_feature_cubes, start_tracks_feature_grid, _ = self.dataset_sample.generate_test_track_feature_cubes(flight_tracks)
                        start_tracks_feature_cubes = self.dataset_sample.reshape_feature_cubes(start_tracks_feature_cubes,
                                                                                               track_length = seq_length)

                        self.known_flight_deptime = flight_tracks.groupby('FID')['FID', 'Elap_Time'].head(1).values

                        logger.info("=============== Start sampling ... ==============")
                        # width = 10 # 30, 300, 0.1
                        # keep_search = 10
                        search_power = 2
                        debug = True
                        weights = 0.9
                        with tf.device('/cpu:0'):
                            # predicted_tracks, \
                            #  final_top_k_idx_seq, \
                            #   buffer_total_logprob, \
                            #    mus, \
                            #     covs = self.sample_seq(start_tracks = tracks_split, 
                            #                            standard_mu = self.cpu_dataset.data_mean, 
                            #                            standard_std = self.cpu_dataset.data_std, 
                            #                            normalized_flight_plan = fp_tracks_split, 
                            #                            flight_plan_length = fp_seq_length,
                            #                            max_length = 150, 
                            #                            beam_search = True,
                            #                            width = width,
                            #                            weights = weights,
                            #                            keep_search = keep_search,
                            #                            debug = debug)

                            predicted_tracks, \
                             predicted_tracks_cov, \
                              buffer_total_logprob, \
                               buffer_pi_prob, \
                                predicted_matched_info = self.sample_seq_mu_cov(start_tracks = tracks_split, 
                                                                                start_tracks_feature_cubes = start_tracks_feature_cubes,
                                                                                normalized_flight_plan = fp_tracks_split, 
                                                                                flight_plan_length = fp_seq_length,
                                                                                max_length = 70, 
                                                                                search_power = search_power,
                                                                                weights = weights,
                                                                                debug = debug)

                                                                       # max_length = 150, 
                                                                       # beam_search = True,
                                                                       # width = 15,
                                                                       # keep_search = 100

                        predicted_tracks = self.dataset_sample.unnormalize_flight_tracks(predicted_tracks)
                        predicted_tracks_cov = predicted_tracks_cov * (self.dataset_sample.train_track_std**2)
                        # mus = mus + np.array([self.cpu_dataset.dep_lat, self.cpu_dataset.dep_lon, 0, 0])
                        # with open('../data/test/test_delta_w%d_k%d_w%d.pkl'%(width, keep_search, weights*100), 'wb') as wpkl:
                        #     pickle.dump((predicted_tracks, final_top_k_idx_seq, buffer_total_logprob, mus, covs), wpkl)
                        # print('Finished sampling. File dumped to ../data/test/test_delta_w%d_k%d_w%d.pkl'%(width, keep_search, weights*100))
                        sample_rslt_path = 'sample_results/ctrl_samp_mu_cov_test_delta_s%d_w%d.pkl'%(search_power, weights*100)
                        with open(sample_rslt_path, 'wb') as wpkl:
                            pickle.dump((predicted_tracks, predicted_tracks_cov, buffer_total_logprob, buffer_pi_prob, predicted_matched_info), wpkl)
                        print('Finished sampling. File dumped to %s'%(sample_rslt_path))
                    else:
                        pass
            # save train summaries to disk
            if not self.sample_traj:
                self.writer.flush()
            self.sess.close()

    def define_placeholder(self):
        # define placeholder
        # targetLength = self.n_controled_var
        self.input_encode_tensor = tf.placeholder(dtype = tf.float32, shape = [None, None, self.n_encode], name = 'encode_tensor')
        self.seq_len_encode = tf.placeholder(dtype = tf.int32, shape = [None], name = 'seq_length_encode')
        self.input_tensor = tf.placeholder(dtype = tf.float32, shape = [None, None, self.n_input, self.n_input, self.n_channels], name = 'decode_feature_map')
        self.input_decode_coords_tensor = tf.placeholder(dtype = tf.float32, shape = [None, None, self.n_controled_var], name = 'decode_coords')
        self.target = tf.placeholder(dtype = tf.float32, shape = [None, None, self.n_controled_var], name = 'target')
        self.target_end = tf.placeholder(dtype = tf.float32, shape = [None, None, 1], name = 'target_end')
        self.target_end_neg = tf.placeholder(dtype = tf.float32, shape = [None, None, 1], name = 'target_end_neg')
        self.seq_length = tf.placeholder(dtype = tf.int32, shape = [None], name = 'seq_length_decode')

    def launchGraph(self):
        self.define_placeholder()
        self.MODEL = LSTM_model(conf_path = self.conf_path,
                                batch_x = self.input_encode_tensor,
                                seq_length = self.seq_len_encode,
                                n_input = self.n_encode,
                                batch_x_decode = self.input_tensor,
                                batch_xcoords_decode = self.input_decode_coords_tensor,
                                seq_length_decode = self.seq_length,
                                n_input_decode = self.n_input,
                                target = self.target,
                                train = not self.sample_traj,
                                weight_summary = False)

        # if not self.sample_traj:
        #     self.loss_placeholder = tf.placeholder(dtype = tf.float32, shape = [])
        #     self.loss_summary = tf.summary.scalar("training_avg_loss", self.loss_placeholder) 
        #     self.summary_op = tf.summary.merge([self.MODEL.summary_op])
        return 
    
    def run_training_epoch(self, 
                           lr_decay_inspection_period = 30):
        train_start_time = time.time()
        train_epoch_losses = []
        lr_epoch = 0
        for epoch in range(self.epochs):
            # print(self.sess.graph.finalized)
            is_checkpoint_step, is_validation_step = self.validation_and_checkpoint_check(epoch)
            epoch_start_time = time.time()
            train_epoch_loss = self.run_batches(self.cpu_dataset, 
                                                self.total_samples, 
                                                epoch, 
                                                'train')
            epoch_elap_time = time.time() - epoch_start_time

            log = 'Epoch {}/{}, train_cost: {:.3f}, elapsed_time: {:.2f} sec \n'
            logger.info(log.format(epoch + 1, self.epochs, train_epoch_loss, epoch_elap_time))

            if lr_epoch < lr_decay_inspection_period:
                train_epoch_losses.append(train_epoch_loss)
                lr_epoch += 1
            else:
                if (train_epoch_loss + train_epoch_losses[-1])/2 >= np.percentile(train_epoch_losses, 75):
                    self.MODEL.learning_rate = self.MODEL.learning_rate * 0.5
                    logger.info('Learning rate decaying... Now is: {:.7f}'.format(self.MODEL.learning_rate))
                    lr_epoch = 0
                    train_epoch_losses = [train_epoch_loss]
                train_epoch_losses.pop(0)
                train_epoch_losses.append(train_epoch_loss)
            if (epoch + 1) % 750 == 0:
                self.MODEL.learning_rate = self.MODEL.learning_rate * 0.75

            
            # if (epoch+1) <= 1500:
            #     if (epoch+1) % 400 == 0:
            #         # print('learning rate is %f'%self.MODEL.learning_rate)
            #         self.MODEL.learning_rate = self.MODEL.learning_rate * 0.5
            #         logger.info('Learning rate decaying... Now is: {:.7f}'.format(self.MODEL.learning_rate))
            # else:
            #     if (epoch+1) % 150 == 0:
            #         # print('learning rate is %f'%self.MODEL.learning_rate)
            #         self.MODEL.learning_rate = self.MODEL.learning_rate * 0.7
            #         logger.info('Learning rate decaying... Now is: {:.7f}'.format(self.MODEL.learning_rate))

            if (epoch + 1 == self.epochs) or is_checkpoint_step:
                # summary_line = self.sess.run(self.loss_summary, feed_dict = {self.loss_placeholder: train_epoch_loss})
                # self.writer.add_summary(summary_line, epoch)
                save_path = self.saver.save(self.sess, os.path.join(self.SESSION_DIR, 'model.ckpt'), epoch)
                logger.info("Model saved to {}".format(save_path))

        train_elap_time = time.time() - train_start_time
        logger.info('Training complete, total duration: {:.2f} min'.format(train_elap_time / 60))
        return

    def validation_and_checkpoint_check(self,
                                        epoch):
        # initially set at False unless indicated to change
        is_checkpoint_step = False
        is_validation_step = False
        # Check if the current epoch is a validation or checkpoint step
        if (epoch > 0) and ((epoch + 1) != self.epochs):
            if (epoch + 1) % self.SAVE_MODEL_EPOCH_NUM == 0:
                is_checkpoint_step = True
            if (epoch + 1) % self.VALIDATION_EPOCH_NUM == 0:
                is_validation_step = True

        return is_checkpoint_step, is_validation_step

    def run_batches(self, 
                    dataset,
                    total_samples,
                    epoch,
                    train_dev_test = 'train'
                    ):
        n_batches_per_epoch = total_samples//self.batch_size + 1
        total_training_loss = 0
        for _ in range(n_batches_per_epoch):
            batch_inputs, batch_targets, _, _, batch_seq_lens, batch_inputs_FP, batch_seq_lens_FP, batch_inputs_feature_cubes = dataset.next_batch()
            feeds = {self.input_tensor: batch_inputs_feature_cubes,
                     self.input_decode_coords_tensor: batch_inputs,
                     self.target: batch_targets,
                     self.seq_length: batch_seq_lens,
                     self.input_encode_tensor: batch_inputs_FP,
                     self.seq_len_encode: batch_seq_lens_FP,
                     }

            if train_dev_test == 'train':
                # total_batch_loss, _, summary_line = self.sess.run([self.total_loss, self.optimizer, self.summary_op])
                # total_batch_loss, _, summary_line = self.sess.run([self.MODEL.total_loss, self.MODEL.optimizer, self.summary_op], feed_dict = feeds)
                # MNV_loss, p_end_loss, p_end, total_batch_loss, _ = self.sess.run([self.MODEL.MNV_loss, self.MODEL.p_end_loss, self.MODEL.p_end, self.MODEL.total_loss, self.MODEL.optimizer], feed_dict = feeds)
                # np.savez('debug_file/loss_arr.npz', p_end = p_end, MNV_loss = MNV_loss, p_end_loss = p_end_loss)
                total_batch_loss, _ = self.sess.run([self.MODEL.total_loss, self.MODEL.optimizer], feed_dict = feeds)
                # encoder_final_state, _initial_state = self.sess.run([self.MODEL.encoder_final_state, self.MODEL._initial_state], feed_dict = feeds)
                # np.savez('debug_file/encoder_decoder_state.npz', encoder_state = encoder_final_state, decoder_state = _initial_state)
                total_training_loss += total_batch_loss
                logger.debug('Total batch loss: %2.f |Total train cost so far: %.2f', total_batch_loss, total_training_loss)
        # self.writer.add_summary(summary_line, epoch)
        return total_training_loss

    def sample_seq_mu_cov(self, 
                          start_tracks, # normalized
                          start_tracks_feature_cubes, # normalized
                          normalized_flight_plan, 
                          flight_plan_length, 
                          max_length = 100, 
                          search_power = 2,
                          weights = 0.1,
                          end_thres = 0.9,
                          debug = False):
        # start_tracks should have the shape of [n_seq, n_time, n_input]
        # normalized_flight_plan should have the shape of [n_seq, n_time, n_input] (also flipped)
        # normalized_flight_plan should be (flight_plan - [dep_lat, dep_lon] - fp_mu)/fp_std; and then pad_and_flip
        # for each sample in the start_tracks, it should have the same length
        # flight_plan_length should have the shape of [n_seq]
        #################################################
        #############   data preprocessing  #############
        #################################################
        n_seq, n_time, _ = start_tracks.shape

        coords_logprob_tensor = self.MODEL.MVN_pdf.log_prob(self.MODEL.mu_layer)
        coords_cov_tensor = tf.matmul(self.MODEL.L_layer, self.MODEL.L_layer, transpose_b = True)
        
        #########################################################
        #############   initialize neural network   #############
        #########################################################
        encoder_state = self.sess.run([self.MODEL.encoder_final_state], 
                                      feed_dict = {self.input_encode_tensor: normalized_flight_plan,
                                                   self.seq_len_encode: flight_plan_length})       
        
        if debug:
            with open('debug_file/Endlayer_encoder_state.pkl', 'wb') as f:
                    pickle.dump((encoder_state), f)
        ###################################################
        #############   start the main loop   #############
        ###################################################
        # dynamic shapes
        # cur_time_len = n_time
        prob_end = []
        for i in range(search_power):
            print('current search power: %d'%i)
            if i == 0:
                feeds_update = {self.input_tensor: start_tracks_feature_cubes,
                                self.seq_length: [n_time]*n_seq,
                                self.input_decode_coords_tensor: start_tracks,
                                # self.input_encode_tensor: normalized_flight_plan,
                                # self.seq_len_encode: flight_plan_length,
                                self.MODEL._initial_state: encoder_state}
            else:
                feeds_update = {self.input_tensor: pred_feature_cubes,
                                self.seq_length: [1]*last_input_track_point.shape[0],
                                self.input_decode_coords_tensor: last_input_track_point,
                                # self.input_encode_tensor: normalized_flight_plan,
                                # self.seq_len_encode: flight_plan_length,
                                self.MODEL._initial_state: state}
            state, pi_logprob, coords_logprob, coords_mu, coords_cov = self.sess.run([self.MODEL.decode_final_state, 
                                                                                      tf.log(self.MODEL.pi_layer), 
                                                                                      coords_logprob_tensor,
                                                                                      self.MODEL.mu_layer,
                                                                                      coords_cov_tensor], 
                                                                                     feed_dict = feeds_update)
            if i == 0:
                # only select the last element (last time stamp)
                pi_logprob = pi_logprob[range(n_time - 1, n_time*n_seq, n_time), :]
                coords_logprob = coords_logprob[range(n_time - 1, n_time*n_seq, n_time), :]
                coords_mu = coords_mu[range(n_time - 1, n_time*n_seq, n_time), :, :]
                coords_cov = coords_cov[range(n_time - 1, n_time*n_seq, n_time), :, :, :]
                # p_end = p_end[range(n_time - 1, n_time*n_seq, n_time), :]
            """
            state: tuple with size n_layers, each is a LSTMtuple object; 
                state[i].c.shape = (n_seq*n_mixture^i, 256); 
                state[i].h.shape = (n_seq*n_mixture^i, 256);
            pi_logprob: np array with size (n_seq*n_mixture^i, n_mixture)
            coords_mu: np array with size (n_seq*n_mixture^i, n_mixture, n_input)
            coords_cov: np array with size (n_seq*n_mixture^i, n_mixture, n_input, n_input)
            coords_logprob: np array with size (n_seq*n_mixture^i, n_mixture)
            """
            last_input_track_point = coords_mu.reshape(coords_mu.shape[0]*coords_mu.shape[1], 1, -1) # shape of [n_seq*n_mixture^(i+1), 1, n_controled_var]
            last_input_track_point_cov = coords_cov.reshape(-1, 1, coords_cov.shape[2], coords_cov.shape[3]) # shape of [n_seq*n_mixture^(i+1), n_input, n_controled_var]
            state = tuple([tf.nn.rnn_cell.LSTMStateTuple(c = np.repeat(tmp_state.c, self.n_mixture, axis = 0), 
                                                         h = np.repeat(tmp_state.h, self.n_mixture, axis = 0)) for tmp_state in state])


            ##########################################################################################
            ##########################       Controlled Prediction        ############################
            ##########################################################################################

            # unnormalize predicted flight tracks to limit the size of next predicted point
            # with shape of [n_seq*n_mixture^(i+1), 1, n_controled_var]: [lat, lon, alt, time, spd, theta]
            unnormalized_last_track_point = self.dataset_sample.unnormalize_flight_tracks(last_input_track_point.reshape(-1, self.n_controled_var))
            if i == 0:
                prev_track_point = np.repeat(self.dataset_sample.unnormalize_flight_tracks(start_tracks[:, -1, :]), self.n_mixture, axis = 0)
            else:
                prev_track_point = np.repeat(self.dataset_sample.unnormalize_flight_tracks(final_tracks[:, -1, :]), self.n_mixture, axis = 0)

            controlled_next_point = self.calculate_next_pnt(current_lons = prev_track_point[:, 1], 
                                                            current_lats = prev_track_point[:, 0], 
                                                            controlled_azi = prev_track_point[:, 5] * 180 / np.pi, 
                                                            controlled_dist = unnormalized_last_track_point[:, 4]*1852*(120))
                                                            # controlled_dist = unnormalized_last_track_point[:, 4]*1852*(unnormalized_last_track_point[:, 3] - prev_track_point[:, 3]))
            unnormalized_last_track_point[:, 0] = controlled_next_point[1]
            unnormalized_last_track_point[:, 1] = controlled_next_point[0]
            unnormalized_last_track_point[:, 3] = prev_track_point[:, 3] + 120
            normalized_last_track_point = self.dataset_sample.normalize_flight_tracks(unnormalized_last_track_point)
            last_input_track_point[:, :, 0] = (normalized_last_track_point[:, 0, None]+last_input_track_point[:, :, 0])/2
            last_input_track_point[:, :, 1] = (normalized_last_track_point[:, 1, None]+last_input_track_point[:, :, 1])/2
            last_input_track_point[:, :, 3] = normalized_last_track_point[:, 3, None]

            ##########################################################################################
            #########################      End of Controlled Prediction        #######################
            ##########################################################################################
            
            if i == 0:
                buffer_total_logprob = (pi_logprob*weights + coords_logprob*(1-weights)) # has the shape of [n_seq, n_mixture]
                buffer_pi_prob = pi_logprob.copy()
                final_tracks = np.concatenate((np.repeat(start_tracks, self.n_mixture, axis = 0), last_input_track_point), axis = 1)
                # has the shape of [n_seq*n_mixture, n_time+1, 4]
                final_tracks_cov = last_input_track_point_cov.copy()


                pred_feature_cubes, \
                 pred_feature_grid, \
                  predicted_matched_info = self.dataset_sample.generate_predicted_pnt_feature_cube(predicted_final_track = final_tracks, 
                                                                                                   known_flight_deptime = self.known_flight_deptime,
                                                                                                   shift_xleft = 0,
                                                                                                   shift_xright = 2, 
                                                                                                   shift_yup = 1,
                                                                                                   shift_ydown = 1,
                                                                                                   nx = 20,
                                                                                                   ny = 20)
            else:
                buffer_total_logprob = buffer_total_logprob.reshape(-1, 1)
                buffer_pi_prob = buffer_pi_prob.reshape(-1, 1)
                # prob_end = np.concatenate((np.repeat(prob_end, self.n_mixture, axis = 0), p_end), axis = 1)

                buffer_total_logprob = buffer_total_logprob + (pi_logprob*weights + coords_logprob*(1-weights)) # has shape of [n_seq*n_mixture^i, n_mixture]
                buffer_pi_prob = buffer_pi_prob + pi_logprob  + i * np.log(0.95) # has shape of [n_seq*n_mixture^i, n_mixture]
                final_tracks = np.concatenate((np.repeat(final_tracks, self.n_mixture, axis = 0), last_input_track_point), axis = 1) 
                # has the shape of [n_seq*n_mixture^(i+1), ?, 4]
                final_tracks_cov = np.concatenate((np.repeat(final_tracks_cov, self.n_mixture, axis = 0), last_input_track_point_cov), axis = 1) 

                pred_feature_cubes, \
                 pred_feature_grid, \
                  predicted_matched_info = self.dataset_sample.generate_predicted_pnt_feature_cube(predicted_final_track = final_tracks, 
                                                                                                   known_flight_deptime = self.known_flight_deptime,
                                                                                                   shift_xleft = 0,
                                                                                                   shift_xright = 2, 
                                                                                                   shift_yup = 1,
                                                                                                   shift_ydown = 1,
                                                                                                   nx = 20,
                                                                                                   ny = 20)
            if (i == 0) and (debug is True):
                with open('debug_file/samp_mu_cov_inner_loop0_debug.pkl', 'wb') as f:
                    pickle.dump((state, pi_logprob, coords_logprob, coords_mu, coords_cov, pred_feature_cubes, pred_feature_grid, predicted_matched_info), f)

        # From here, feeds_update will have fixed shapes
        buffer_pi_prob_all_mix = []
        for j in range(max_length - search_power):
            print('===current predicting time stamps: %d==='%j)
            feeds_update = {self.input_tensor: pred_feature_cubes,
                            self.seq_length: [1]*last_input_track_point.shape[0],
                            self.input_decode_coords_tensor: last_input_track_point,
                            # self.input_encode_tensor: normalized_flight_plan,
                            # self.seq_len_encode: flight_plan_length,
                            self.MODEL._initial_state: state}
            state, pi_logprob, coords_logprob, coords_mu, coords_cov = self.sess.run([self.MODEL.decode_final_state, 
                                                                                             tf.log(self.MODEL.pi_layer), 
                                                                                             coords_logprob_tensor,
                                                                                             self.MODEL.mu_layer,
                                                                                             coords_cov_tensor], feed_dict = feeds_update)
            """
            state: tuple with size n_layers, each is a LSTMtuple object; state[0].c.shape = (n_seq*n_mixture^(i+1), 256); state[0].h.shape = (n_seq*n_mixture^(i+1), 256);
            pi_logprob: np array with size (n_seq*n_mixture^(i+1), n_mixture)
            coords_mu: np array with size (n_seq*n_mixture^(i+1), n_mixture, 4)
            coords_cov: np array with size (n_seq*n_mixture^(i+1), n_mixture, 4, 4)
            coords_logprob: np array with size (n_seq*n_mixture^(i+1), n_mixture)
            """
            if (j == 0) and (debug is True):
                with open('debug_file/samp_mu_cov_inner_loop1_debug.pkl', 'wb') as f:
                    pickle.dump((state, pi_logprob, coords_logprob, coords_mu, coords_cov, pred_feature_cubes, pred_feature_grid, predicted_matched_info), f)

            if j == 0:
                buffer_total_logprob = buffer_total_logprob.reshape(-1, 1)
                buffer_pi_prob = buffer_pi_prob.reshape(-1, 1)
                # prob_end = np.concatenate((np.repeat(prob_end, self.n_mixture, axis = 0), p_end), axis = 1)
            # else:
            #     prob_end = np.concatenate((prob_end, p_end), axis = 1)

            tmp_buffer_total_logprob = (pi_logprob*weights + coords_logprob*(1-weights))
            buffer_total_logprob = buffer_total_logprob + tmp_buffer_total_logprob # has shape of [n_seq*n_mixture^(i+1), n_mixture]
            # buffer_pi_prob += np.exp(pi_logprob)*(0.5**(j+search_power))
            tmp_pi_prob = pi_logprob  + (j + search_power) * np.log(0.95)
            buffer_pi_prob_all_mix.append(tmp_pi_prob)
            # buffer_pi_prob = buffer_pi_prob + pi_logprob  + (j + search_power) * np.log(0.95)

            # e.g., n_mixture = 10, search_power == 4, total 10000 trajs, then has the prob of buffer_total_logprob
            # 000| 0,1,2,...,9
            # 000| 0,1,2,...,9
            # 000| 0,1,2,...,9
            # ...
            # 099| 0,1,2,...,9
            # 100| 0,1,2,...,9
            # ...
            # 999| 0,1,2,...,9
            
            top_k_idx = np.argsort(buffer_total_logprob, axis = -1)[:, -1] # shape of (n_seq*n_mixture^(i+1), )
            # top_k_idx = np.argsort(tmp_buffer_total_logprob, axis = -1)[:, -1] # shape of (n_seq*n_mixture^(i+1), )

            # buffer_total_logprob = buffer_total_logprob + tmp_buffer_total_logprob[range(coords_mu.shape[0]), top_k_idx, None]
            buffer_total_logprob = buffer_total_logprob[range(coords_mu.shape[0]), top_k_idx, None]
            buffer_pi_prob = np.concatenate((buffer_pi_prob, tmp_pi_prob[range(coords_mu.shape[0]), top_k_idx, None]), axis = 1)
            # buffer_pi_prob = buffer_pi_prob[range(coords_mu.shape[0]), top_k_idx, None]

            last_input_track_point = coords_mu[range(coords_mu.shape[0]), top_k_idx, None] # shape of [n_seq*n_mixture^(i+1), 1, n_controled_var]
            last_input_track_point_cov = coords_cov[range(coords_cov.shape[0]), top_k_idx, :, :].reshape(-1, 1, coords_cov.shape[2], coords_cov.shape[3]) 
            # shape of [n_seq*n_mixture^(i+1), 1, 4, 4]

            ##########################################################################################
            ##########################       Controlled Prediction        ############################
            ##########################################################################################

            # unnormalize predicted flight tracks to limit the size of next predicted point
            # with shape of [n_seq*n_mixture^(i+1), 1, n_controled_var]: [lat, lon, alt, time, spd, theta]
            unnormalized_last_track_point = self.dataset_sample.unnormalize_flight_tracks(last_input_track_point.reshape(-1, self.n_controled_var))
            prev_track_point = self.dataset_sample.unnormalize_flight_tracks(final_tracks[:, -1, :])
            controlled_next_point = self.calculate_next_pnt(current_lons = prev_track_point[:, 1], 
                                                            current_lats = prev_track_point[:, 0], 
                                                            controlled_azi = prev_track_point[:, 5] * 180 / np.pi, 
                                                            controlled_dist = unnormalized_last_track_point[:, 4]*1852*(120))
                                                            # controlled_dist = unnormalized_last_track_point[:, 4]*1852*(unnormalized_last_track_point[:, 3] - prev_track_point[:, 3]))
            unnormalized_last_track_point[:, 0] = controlled_next_point[1]
            unnormalized_last_track_point[:, 1] = controlled_next_point[0]
            unnormalized_last_track_point[:, 3] = prev_track_point[:, 3] + 120
            normalized_last_track_point = self.dataset_sample.normalize_flight_tracks(unnormalized_last_track_point)
            last_input_track_point[:, :, 0] = (normalized_last_track_point[:, 0, None]+last_input_track_point[:, :, 0])/2
            last_input_track_point[:, :, 1] = (normalized_last_track_point[:, 1, None]+last_input_track_point[:, :, 1])/2
            last_input_track_point[:, :, 3] = normalized_last_track_point[:, 3, None]

            ##########################################################################################
            #########################      End of Controlled Prediction        #######################
            ##########################################################################################

            final_tracks = np.concatenate((final_tracks, last_input_track_point), axis = 1) 
            # has the shape of [n_seq*n_mixture^(i+1), ?, 4]
            final_tracks_cov = np.concatenate((final_tracks_cov, last_input_track_point_cov), axis = 1) 

            pred_feature_cubes, \
                 pred_feature_grid, \
                  predicted_matched_info = self.dataset_sample.generate_predicted_pnt_feature_cube(predicted_final_track = final_tracks, 
                                                                                                   known_flight_deptime = self.known_flight_deptime,
                                                                                                   shift_xleft = 0,
                                                                                                   shift_xright = 2, 
                                                                                                   shift_yup = 1,
                                                                                                   shift_ydown = 1,
                                                                                                   nx = 20,
                                                                                                   ny = 20)

        if debug:
            with open('debug_file/samp_mu_cov_outer_loop_debug.pkl', 'wb') as f:
                pickle.dump((state, buffer_pi_prob_all_mix, prob_end, coords_logprob, final_tracks, pred_feature_cubes, pred_feature_grid, predicted_matched_info), f)
        return final_tracks[:, :, :], final_tracks_cov, buffer_total_logprob, buffer_pi_prob, predicted_matched_info


    def arrange_top_k(self, 
                      top_k_idx_seq, 
                      keep_search):
        final_seq = []
        i = 0
        for seq in top_k_idx_seq[::-1]:
            if i == 0:
                final_seq.append(seq)
                idx = seq//keep_search
            else:
                seq = seq[idx]
                final_seq.append(seq)
                idx = seq // keep_search
            i += 1
        final_seq = np.array(final_seq)
        return final_seq

    def calculate_next_pnt(self, 
                           current_lons, 
                           current_lats, 
                           controlled_azi, 
                           controlled_dist):
        lons, lats, _ = g.fwd(lons = current_lons, 
                              lats = current_lats, 
                              az = controlled_azi, 
                              dist = controlled_dist)
        return lons, lats



# to run in console
if __name__ == '__main__':
    import click
    # Use click to parse command line arguments
    @click.command()
    @click.option('--train_or_predict', type=str, default='train', help='Train the model or predict model based on input')
    @click.option('--config', default='configs/encoder_decoder_nn.ini', help='Configuration file name')
    @click.option('--name', default=None, help='Path for retored model')
    @click.option('--train_from_model', type=bool, default=False, help='train from restored model')

    # for prediction
    @click.option('--test_data', default='../../DATA/DeepTP/', help='test data path') # not useful for now

    # Train RNN model using a given configuration file
    def main(config='configs/encoder_decoder_nn.ini',
             name = None,
             train_from_model = False,
             train_or_predict = 'train',
             test_data = '../data/test_data.csv'):
        # create the Tf_train_ctc class
        if train_or_predict == 'train':
            tmpBinary = True
        elif train_or_predict == 'predict':
            tmpBinary = False
        else:
            raise ValueError('train_or_predict not valid')

        try:        
            os.mkdir('log')     
        except:     
            pass

        log_name = '{}_{}_{}'.format('log/log', train_or_predict, time.strftime("%Y%m%d-%H%M%S"))
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                            filename=log_name + '.log',
                            filemode='w')
        global logger
        logger = logging.getLogger(os.path.basename(__file__))
        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        logger.addHandler(consoleHandler)

        
        tf_train = trainRNN(conf_path=config,
                            model_name=name, 
                            sample_traj = not tmpBinary)
        if tmpBinary:
            # run the training
            tf_train.run_model(train_from_model = train_from_model)
        else:
            tf_train.run_model(train_from_model = False,
                               test_data_start_track = test_data) 
    main()

    # def sample_seq(self, 
    #                start_tracks, # normalized
    #                start_tracks_feature_cubes, # normalized
    #                normalized_flight_plan, 
    #                flight_plan_length, 
    #                max_length = 100, 
    #                beam_search = True, 
    #                width = 10, 
    #                weights = 0.1,
    #                keep_search = 50,
    #                debug = False):
    #     # start_tracks should have the shape of [n_sample, n_time, n_input]
    #     # normalized_flight_plan should have the shape of [n_sample, n_time, n_input] (also flipped)
    #     # normalized_flight_plan should be (flight_plan - [dep_lat, dep_lon] - fp_mu)/fp_std; and then pad_and_flip
    #     # for each sample in the start_tracks, it should have the same length
    #     # flight_plan_length should have the shape of [n_sample]
    #     # fp_tracks = (flight_plan - fp_mu)/fp_std
    #     n_seq, n_time, _ = start_tracks.shape
    #     if not beam_search:
    #         width = 1
    #         keep_search = 1
    #     buffer_size = width * keep_search

    #     buffer_track = np.repeat(start_tracks, buffer_size, axis = 0) # shape of [n_seq*buffer_size, n_time, 4]; duplicate of seq_1, then seq_2, ...
    #     buffer_fp = np.repeat(normalized_flight_plan, buffer_size, axis = 0) # shape of [n_seq*buffer_size, n_time, 2]; duplicate of seq_1, then seq_2, ...
    #     buffer_fp_len = np.repeat(flight_plan_length, buffer_size) # shape of [n_seq*buffer_size,]
    #     buffer_total_logprob = np.zeros(shape = (buffer_size * n_seq, 1), dtype = np.float32) # shape of [n_seq*buffer_size,1]

    #     # # # init_state = np.zeros(shape = (self.n_layer, 2, n_seq, self.state_size))
    #     # tmp_feed = {
    #     #              # self.input_tensor: buffer_track,
    #     #              # self.seq_length: [n_time]*n_seq*buffer_size,
    #     #              self.input_encode_tensor: buffer_fp,
    #     #              self.seq_len_encode: buffer_fp_len}
    #     # encoder_state0 = self.sess.run(self.MODEL.encoder_final_state, feed_dict = tmp_feed) # should have the size of (buffer_size*n_seq) tuples
        
    #     feeds = {self.input_tensor: start_tracks_feature_cubes,
    #              self.seq_length: [n_time]*n_seq*buffer_size,
    #              self.input_encode_tensor: buffer_fp,
    #              self.seq_len_encode: buffer_fp_len}

    #     state, encoder_state = self.sess.run([self.MODEL.decode_final_state, self.MODEL._initial_state], feed_dict = feeds)
    #     if debug:
    #         with open('debug_file/encoder_state_debug.pkl', 'wb') as f:
    #                 pickle.dump((state, encoder_state), f)


    #     last_input_track_point = buffer_track[:, -1, None]   # shape of [n_seq*buffersize, 1, 4]

    #     pi_sample_tensor = tf.multinomial(logits = self.MODEL.pi_layer, num_samples = 1, output_dtype = tf.int32)
    #     coords_sample_tensor = self.MODEL.MVN_pdf.sample()
    #     coords_logprob_tensor = self.MODEL.MVN_pdf.log_prob(coords_sample_tensor)

    #     # coords_mu = self.MODEL.mu_layer
    #     coords_cov_tensor = tf.matmul(self.MODEL.L_layer, self.MODEL.L_layer, transpose_b = True)
    #     # predicted_tracks = np.repeat(start_tracks, width, axis = 0)
    #     top_k_idx_seq = []
    #     all_mus = []
    #     all_covs = []
    #     for i in range(max_length):
    #         # self.MODEL._initial_state = state
    #         feeds_update = {self.input_tensor: last_input_track_point,
    #                         self.seq_length: [1]*n_seq*buffer_size,
    #                         self.input_encode_tensor: buffer_fp,
    #                         self.seq_len_encode: buffer_fp_len,
    #                         self.MODEL._initial_state: state}
    #         if (i == 0) and (debug is True):
    #             with open('debug_file/init_state_debug.pkl', 'wb') as f:
    #                 pickle.dump((state), f)
    #         state, pi_logprob, coords_sample, pi_sample, coords_logprob, coords_mu, coords_cov = self.sess.run([self.MODEL.decode_final_state, 
    #                                                                                                              tf.log(self.MODEL.pi_layer), 
    #                                                                                                              coords_sample_tensor, 
    #                                                                                                              pi_sample_tensor, 
    #                                                                                                              coords_logprob_tensor,
    #                                                                                                              self.MODEL.mu_layer,
    #                                                                                                              coords_cov_tensor], feed_dict = feeds_update)
    #         # print(pi_sample.flatten()[:5])
    #         # print(coords_sample[:5, :, :])
    #         """
    #         state: tuple with size n_layers, each is a LSTMtuple object; state[0].c.shape = (n_seq*buffersize, 256); state[0].h.shape = (n_seq*buffersize, 256);
    #         pi_logprob: np array with size (n_seq*buffersize, n_mixture)
    #         coords_sample: np array with size (n_seq*buffersize, n_mixture, 4)
    #         pi_sample: np array with size (n_seq * buffersize, 1)
    #         coords_logprob: np array with size (n_seq*buffersize, n_mixture)
    #         """
    #         if (i == 0) and (debug is True):
    #             with open('debug_file/inner_loop_debug.pkl', 'wb') as f:
    #                 pickle.dump((state, pi_logprob, coords_sample, pi_sample, coords_logprob, coords_mu, coords_cov), f)

    #         pi_logprob_sample = pi_logprob[range(buffer_size*n_seq), pi_sample.flatten(), None] # [n_seq*buffersize, 1]
    #         coords_logprob_sample = coords_logprob[range(buffer_size*n_seq), pi_sample.flatten(), None] # [n_seq*buffersize, 1]
    #         # total_log_prob = np.sum(pi_logprob, axis = 1, keepdims=True) + np.sum(coords_logprob, axis = 1, keepdims=True)
    #         # buffer_total_logprob += (pi_logprob_sample/np.sum(pi_logprob, axis = 1, keepdims=True) + coords_logprob_sample/np.sum(coords_logprob, axis = 1, keepdims=True)) # has shape of [buffer_size*n_seq, 1]
    #         buffer_total_logprob += (pi_logprob_sample*weights + coords_logprob_sample*(1-weights))/(0.95**i) # has shape of [buffer_size*n_seq, 1]
    #         # select top k sequence
    #         tmp_buffer_total_logprob = buffer_total_logprob.reshape(n_seq, buffer_size, 1)
    #         top_k_idx = np.argsort(tmp_buffer_total_logprob, axis = 1)[:, (-width):,:] + (np.repeat(range(n_seq), width)*buffer_size).reshape(n_seq, width, 1)
    #         top_k_idx = top_k_idx.reshape(-1)
    #         top_k_idx_seq.append(top_k_idx)

    #         top_k_logprob = buffer_total_logprob[top_k_idx]
    #         selected_coords = coords_sample[top_k_idx, pi_sample[top_k_idx].flatten(), None] # shape of [width*n_seq, 1, 4]
    #         selected_coords_mu = coords_mu[top_k_idx, pi_sample[top_k_idx].flatten(), None] # shape of [width*n_seq, 1, 4]
    #         selected_coords_cov = coords_cov[top_k_idx, pi_sample[top_k_idx].flatten(), :] # shape of [width*n_seq, 1, 2, 2]
            
    #         state = tuple([tf.nn.rnn_cell.LSTMStateTuple(c = np.repeat(tmp_state.c[top_k_idx], keep_search, axis = 0), 
    #                                                      h = np.repeat(tmp_state.h[top_k_idx], keep_search, axis = 0)) for tmp_state in state])
            
    #         last_input_track_point = np.repeat(selected_coords, keep_search, axis = 0) # shape of [n_seq*buffersize, 1, 4]
    #         buffer_total_logprob = np.repeat(top_k_logprob, keep_search, axis = 0)
    #         # last_input_track_point = param_sample[range(buffer_size*n_seq), pi_sample.flatten()].reshape(n_seq, 1, -1)
    #         buffer_track = np.concatenate((buffer_track, last_input_track_point), axis = 1)

    #         all_mus.append(np.repeat(selected_coords_mu, keep_search, axis = 0))
    #         all_covs.append(np.repeat(selected_coords_cov, keep_search, axis = 0))

    #     all_mus = np.array(all_mus)
    #     all_covs = np.array(all_covs)
    #     all_mus = all_mus * standard_std + standard_mu # shape of [max_length, n_seq*width, 1, 4]
    #     all_covs = all_covs * (standard_std**2) # shape of [max_length, n_seq*width, 1, 4, 4]
    
    #     top_k_idx_seq = np.array(top_k_idx_seq) # shape of [max_length, n_seq*width]
    #     final_tracks = (buffer_track * standard_std) + standard_mu # shape of [n_seq*buffersize, maxLlenth+init_len, 4]

    #     final_top_k_idx_seq = self.arrange_top_k(top_k_idx_seq, keep_search) # shape of [max_lenth, n_seq*width]
    #     final_tracks = final_tracks[final_top_k_idx_seq[::-1].T, range(n_time, max_length + n_time)]  # shape of [n_seq*width, max_length, 4]

    #     a = np.transpose(all_covs, [1, 0, 2, 3])
    #     b = np.transpose(all_mus, [1, 0, 2,3])
    #     final_covs = a[final_top_k_idx_seq[::-1].T, range(max_length)]
    #     final_mus = b[final_top_k_idx_seq[::-1].T, range(max_length)].reshape(n_seq*width, max_length, -1)

    #     if debug:
    #         with open('debug_file/outer_loop_debug.pkl', 'wb') as f:
    #             pickle.dump((state, pi_logprob, coords_sample, pi_sample, coords_logprob, final_tracks, top_k_idx_seq, buffer_total_logprob, all_mus, all_covs), f)
    #     return final_tracks, final_top_k_idx_seq, buffer_total_logprob, final_mus, final_covs



    # def sample_seq_kalman_filter_greedy(self, 
    #                                     start_tracks, 
    #                                     standard_mu, 
    #                                     standard_std, 
    #                                     normalized_flight_plan, 
    #                                     flight_plan_length, 
    #                                     max_length = 100,
    #                                     beam_search = False,
    #                                     width = 50,
    #                                     keep_search = 50
    #                                     ):
    #     # start_tracks should have the shape of [n_sample, n_time, n_input]
    #     # normalized_flight_plan should have the shape of [n_sample, n_time, n_input] (also flipped)
    #     # normalized_flight_plan should be (flight_plan - [dep_lat, dep_lon] - fp_mu)/fp_std; and then pad_and_flip
    #     # for each sample in the start_tracks, it should have the same length
    #     # flight_plan_length should have the shape of [n_sample]
    #     start_tracks = (start_tracks - standard_mu)/standard_std
    #     # fp_tracks = (flight_plan - fp_mu)/fp_std
    #     n_seq, n_time, _ = start_tracks.shape
    #     if not beam_search:
    #         width = 1
    #         keep_search = 1
    #     buffer_size = width * keep_search

    #     buffer_track = np.repeat(start_tracks, buffer_size, axis = 0) # shape of [n_seq*buffer_size, n_time, 4]; duplicate of seq_1, then seq_2, ...
    #     buffer_fp = np.repeat(normalized_flight_plan, buffer_size, axis = 0) # shape of [n_seq*buffer_size, n_time, 2]; duplicate of seq_1, then seq_2, ...
    #     buffer_fp_len = np.repeat(flight_plan_length, buffer_size) # shape of [n_seq*buffer_size,]
    #     buffer_total_logprob = np.zeros(shape = (buffer_size * n_seq, 1), dtype = np.float32) # shape of [n_seq*buffer_size,1]

    #     # # # init_state = np.zeros(shape = (self.n_layer, 2, n_seq, self.state_size))
    #     # tmp_feed = {
    #     #              # self.input_tensor: buffer_track,
    #     #              # self.seq_length: [n_time]*n_seq*buffer_size,
    #     #              self.input_encode_tensor: buffer_fp,
    #     #              self.seq_len_encode: buffer_fp_len}
    #     # encoder_state0 = self.sess.run(self.MODEL.encoder_final_state, feed_dict = tmp_feed) # should have the size of (buffer_size*n_seq) tuples
        
    #     feeds = {self.input_tensor: buffer_track,
    #              self.seq_length: [n_time]*n_seq*buffer_size,
    #              self.input_encode_tensor: buffer_fp,
    #              self.seq_len_encode: buffer_fp_len}

    #     state, encoder_state = self.sess.run([self.MODEL.decode_final_state, self.MODEL._initial_state], feed_dict = feeds)
    #     # with open('debug_file/encoder_state_debug.pkl', 'wb') as f:
    #     #         pickle.dump((state, encoder_state), f)


    #     last_input_track_point = buffer_track[:, -1, None]   # shape of [n_seq*buffersize, 1, 4]

    #     pi_sample_tensor = tf.multinomial(logits = self.MODEL.pi_layer, num_samples = 1, output_dtype = tf.int32)

    #     coords_mu = self.MODEL.mu_layer
    #     coords_cov = tf.matmul(self.MODEL.L_layer, self.MODEL.L_layer, transpose_b = True)


    #     coords_sample_tensor = self.MODEL.MVN_pdf.sample()
    #     coords_logprob_tensor = self.MODEL.MVN_pdf.log_prob(coords_sample_tensor)
    #     # predicted_tracks = np.repeat(start_tracks, width, axis = 0)
    #     top_k_idx_seq = []
    #     for i in range(max_length):
    #         # self.MODEL._initial_state = state
    #         feeds_update = {self.input_tensor: last_input_track_point,
    #                         self.seq_length: [1]*n_seq*buffer_size,
    #                         self.input_encode_tensor: buffer_fp,
    #                         self.seq_len_encode: buffer_fp_len,
    #                         self.MODEL._initial_state: state}
    #         # if i == 10:
    #         #     with open('debug_file/init_state_debug.pkl', 'wb') as f:
    #         #         pickle.dump((state), f)
    #         state, pi_logprob, coords_sample, pi_sample, coords_logprob = self.sess.run([self.MODEL.decode_final_state, 
    #                                                                                      tf.log(self.MODEL.pi_layer), 
    #                                                                                      coords_sample_tensor, 
    #                                                                                      pi_sample_tensor, 
    #                                                                                      coords_logprob_tensor], feed_dict = feeds_update)
    #         """
    #         state: tuple with size 3, each is a LSTMtuple object; state[0].c.shape = (n_seq*buffer_size, 256); state[0].h.shape = (50, 256);
    #         pi_logprob: np array with size (n_seq*buffersize, n_mixture)
    #         coords_sample: np array with size (n_seq*buffersize, n_mixture, 4)
    #         pi_sample: np array with size (n_seq * buffersize, 1)
    #         coords_logprob: np array with size (n_seq*buffersize, n_mixture)
    #         """
    #         # if i == 10:
    #         #     with open('debug_file/inner_loop_debug.pkl', 'wb') as f:
    #         #         pickle.dump((state, pi_logprob, coords_sample, pi_sample, coords_logprob, init_state), f)

    #         pi_logprob = pi_logprob[range(buffer_size*n_seq), pi_sample.flatten(), None] # [n_seq*buffersize, 1]
    #         coords_logprob = coords_logprob[range(buffer_size*n_seq), pi_sample.flatten(), None] # [n_seq*buffersize, 1]
    #         buffer_total_logprob += (pi_logprob + coords_logprob) # has shape of [buffer_size*n_seq, 1]
    #         # select top k sequence
    #         tmp_buffer_total_logprob = buffer_total_logprob.reshape(n_seq, buffer_size, 1)
    #         top_k_idx = np.argsort(tmp_buffer_total_logprob, axis = 1)[:, (-width):,:] + (np.repeat(range(n_seq), width)*buffer_size).reshape(n_seq, width, 1)
    #         top_k_idx = top_k_idx.reshape(-1)
    #         top_k_idx_seq.append(top_k_idx)

    #         top_k_logprob = buffer_total_logprob[top_k_idx]
    #         selected_coords = coords_sample[top_k_idx, pi_sample[top_k_idx].flatten(), None] # shape of [width*n_seq, 1, 4]
            
    #         state = tuple([tf.nn.rnn_cell.LSTMStateTuple(c = np.repeat(tmp_state.c[top_k_idx], keep_search, axis = 0), 
    #                                                      h = np.repeat(tmp_state.h[top_k_idx], keep_search, axis = 0)) for tmp_state in state])
            
    #         last_input_track_point = np.repeat(selected_coords, keep_search, axis = 0) # shape of [n_seq*buffersize, 1, 4]
    #         buffer_total_logprob = np.repeat(top_k_logprob, keep_search, axis = 0)
    #         # last_input_track_point = param_sample[range(buffer_size*n_seq), pi_sample.flatten()].reshape(n_seq, 1, -1)
    #         buffer_track = np.concatenate((buffer_track, last_input_track_point), axis = 1)
    #     top_k_idx_seq = np.array(top_k_idx_seq) # shape of [max_length, n_seq*width]
    #     final_tracks = (buffer_track * standard_std) + standard_mu

    #     final_top_k_idx_seq = self.arrange_top_k(top_k_idx_seq, keep_search)
    #     final_tracks = final_tracks[final_top_k_idx_seq[::-1].T, range(n_time, max_length + n_time)]
    #     # with open('debug_file/outer_loop_debug.pkl', 'wb') as f:
    #     #     pickle.dump((state, pi_logprob, coords_sample, pi_sample, coords_logprob, final_tracks, top_k_idx_seq, buffer_total_logprob), f)
    #     return final_tracks, final_top_k_idx_seq, buffer_total_logprob
