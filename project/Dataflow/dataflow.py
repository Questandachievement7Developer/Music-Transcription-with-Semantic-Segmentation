
import os
import h5py
import pickle
import numpy as np

from project.configuration import get_MusicNet_label_num_mapping, get_instruments_num, MusicNet_Instruments

from keras.utils import Sequence

class BaseDataflow(Sequence):
    
    # Dictionary of dataset structure 
    # Should contain the fields point to the data directory: 
    #   "train", "train_label", "val", "val_label", "test", "test_label"
    structure = None 

    def __init__(self, dataset_path, phase, train_val_split=0.8, use_ram=False, 
                 timesteps=128, b_sz=32, channels=[1, 3], feature_num=384, 
                 mpe_only=True, **kwargs):

        self.dataset_path    = dataset_path
        self.use_ram         = use_ram
        self.train_val_split = train_val_split
        self.timesteps       = timesteps
        self.b_sz            = b_sz
        self.channels        = channels
        self.feature_num     = feature_num
        self.mpe_only        = mpe_only
        self.phase           = phase


        self.features, self.labels = self.load_data(phase, use_ram=use_ram)

        self.init_index()
        
        self.post_init(**kwargs)

    
    def post_init(self, **kwargs):
        # For child classes that need additional initialization.
        pass
    
    def init_index(self):
        num_piece = len(self.features)
        self.idxs = []
        
        for i, pi in enumerate(self.features):
            iid = [(i, a) for a in range(0, len(pi), self.timesteps)]
            self.idxs += iid
        
        if not self.phase == "test":
            np.random.shuffle(self.idxs)

        b_itv = len(self.idxs) // self.b_sz
        self.b_start_idx = [a for a in range(0, len(self.idxs), b_itv)]
        self.b_idx_len = np.array([b_itv for _ in range(self.b_sz)])
        
        diff = self.b_sz - len(self.b_start_idx)
        self.b_idx_len[:diff] += 1
        for i in range(diff):
            self.b_start_idx[i+1:] += 1

        self.b_offset = self.b_start_idx.copy()
        self.b_idx_len[-1] = len(self.idxs) - self.b_start_idx[-1]

        self.d_buffer = np.zeros((self.b_sz, self.timesteps, self.feature_num, len(self.channels)))
        self.l_buffer = np.zeros((self.b_sz, self.timesteps, self.feature_num, 3 if self.mpe_only else 12))
        self.batch = 0

    def __getitem__(self, i):
        if self.phase=="test":
            # Warning!
            # We suggest to use EvalFlow class rather than original Dataflow class for test phase
            pid, tid = self.idxs[i]
            x, y = self.get_feature(pid, tid)
        else:
            ii = i % self.b_sz
            bi = self.b_start_idx[ii]
            b_off = self.b_offset[ii]
            self.b_offset[ii] = (b_off+1) % self.b_idx_len[ii]
            iid = (bi+b_off) % self.b_idx_len[ii]

            pid, tid = self.idxs[iid]
            x, y = self.get_feature(pid, tid)
        
        self.d_buffer[self.batch] = x
        self.l_buffer[self.batch] = y
        self.batch += 1

        if self.batch == self.b_sz:
            self.batch = 0
            return self.d_buffer, self.l_buffer


    def __len__(self):
        # Total available steps
        if self.phase == "test":
            return len(self.idxs)
        return np.ceil(len(self.idxs)/self.b_sz).astype('int')

    def get_feature(self, pid, tid):
        h_ref = self.features[pid]
        cc = BaseDataflow
        x = cc.pad_hdf(h_ref, tid, self.channels, timesteps=self.timesteps, feature_num=self.feature_num)

        l_conv = lambda **arg: cc.label_conversion(self.labels[pid], tid,
                                                   timesteps=self.timesteps,
                                                   feature_num=self.feature_num,
                                                   mpe=self.mpe_only,
                                                   **arg)
        y = l_conv()
        onsets = l_conv(onsets=True)[:,:,1]
        offsets = l_conv(offsets=True)[:,:,1]
        
        """ onset, offset, duration
        tmp = y[:,:,1]-onsets-offsets
        tmp[tmp>0] = 0
        offsets += tmp
        y[:,:,1] = y[:,:,1]-onsets-offsets

        y = np.dstack([y, onsets, offsets])
        """

        # onset, duration
        y[:,:,1] -= onsets
        y = np.dstack([y, onsets])
        y[:,:,0] = 1-np.sum(y[:,:,1:], axis=2)
        
        total_elem = y.shape[0]*y.shape[1]
        assert(total_elem==np.sum(y)), "Label error"

        return x, y

    def load_data(self, phase, use_ram=False):
        raise NotImplementedError

    def parse_files(self, path, ext):
        paths = []
        for root, dirs, files in os.walk(path):
            for ff in files:
                if ff.endswith(ext):
                    paths.append(os.path.join(root, ff))
        return paths

    def parse_hdf(self, paths, use_ram=False):
        hdf_insts = []
        
        for pp in paths:
            f = h5py.File(pp, "r")
            for i in range(len(f)):
                inst = f[str(i)]
                hdf_insts.append(inst[:] if use_ram else inst)

        return hdf_insts

    def parse_pickle(self, paths):
        pkls = []
        
        for pp in paths:
            pkl = pickle.load(open(pp, "rb"))
            for value in pkl:
                pkls.append(value)
        
        return pkls

    def pad_label(self, labels):
        # Padding data to the special data structure of label for this project
        # labels should contain both training and validation label

        new_x = [{} for _ in range(self.timesteps)]
        for p in labels:
            new_x.append(p)
        for _ in range(self.timesteps):
            new_x.append({})
        
        return new_x
    
    @staticmethod
    def pad_hdf(piece, tid, channels, timesteps=128, feature_num=384):
        feature = np.zeros((timesteps, feature_num, len(channels)))

        assert(feature_num >= piece.shape[1])
        h = piece.shape[1]
        p_b = (feature_num - h) // 2
        insert_range = range(p_b, p_b+h)
        t_len = min(timesteps, len(piece)-tid)

        feature[:t_len, insert_range] = piece[tid:(tid+timesteps), :, channels]

        return feature
    

    @staticmethod
    def label_conversion(label, tid, 
                         timesteps=128, 
                         ori_feature_size=352, 
                         feature_num=384, 
                         base=88, 
                         mpe=False, 
                         onsets=False,
                         offsets=False):

        '''
        This function is designed for MusicNet.
        Labels formatted with information of different instruments.
        
        See MusicNet/code/ProcessFeature.py for details about the format of labels stored in pickle files.
        '''

        labels = label[tid:(tid+timesteps)]
        if len(labels) < timesteps:
            for _ in range(timesteps-len(labels)):
                labels.append({})
        
        assert(ori_feature_size % base == 0)
        scale = feature_num // base
        spec_inst = MusicNet_Instruments
        inst_num  = get_instruments_num(spec_inst)
        mapping   = get_MusicNet_label_num_mapping(1, spec_inst)
        new_l     = np.zeros((len(labels), ori_feature_size, inst_num+1))
        new_l[:,:,0] = 1

        for t, label in enumerate(labels):
            if len(label.items()) == 0:
                continue

            for pitch, insts in label.items():
                for it in insts:
                    if it not in mapping:
                        continue
                        
                    ii = mapping[it]
                    pr = range(pitch*scale, (pitch+1)*scale)
                    #new_l[t, pr, 0] = 0
                    if onsets:
                        new_l[t, pr, ii] = insts[it][0]
                    elif offsets:
                        new_l[t, pr, ii] = insts[it][1]
                    else:
                        new_l[t, pr, ii] = 1
        new_l[:,:,0] = 1 - new_l[:,:,1]

        h = new_l.shape[1]
        p_b = (feature_num-h) // 2
        p_t = feature_num - p_b - new_l.shape[1]
        
        b_shape = (new_l.shape[0], p_b)
        t_shape = (new_l.shape[0], p_t)
        if len(new_l.shape) == 3:
            b_shape += (new_l.shape[2],)
            t_shape += (new_l.shape[2],)
        bottom = np.zeros(b_shape)
        top = np.zeros(t_shape)
        new_l  = np.concatenate([bottom, new_l, top], axis=1)
        
        # This is for single channel output, by merging all the labels into one channel
        if mpe:
            mpe_l = np.nanmax(new_l[:,:,1:], axis=2)
            mpe_l = np.dstack((new_l[:,:,0], mpe_l)) 
            new_l = mpe_l
            
        return new_l

    # Deprecated, use pad_hdf instead
    def pad_feature(self, x,
                    feature_num=384,
                    dimension=False):
                    
        extended_chorale = np.array(x)

        # Pad vertically
        if (((feature_num - x.shape[1]) % 2) == 0):
            p_t = (feature_num - x.shape[1]) // 2
            p_b = p_t
        else:
            p_t = (feature_num - x.shape[1]) // 2
            p_b = p_t + 1
        
        top_shape = (extended_chorale.shape[0], p_t)
        bot_shape = (extended_chorale.shape[0], p_b)
        if len(extended_chorale.shape) == 3:
            top_shape += (extended_chorale.shape[2],)
            bot_shape += (extended_chorale.shape[2],)
        top = np.zeros(top_shape)
        bottom = np.zeros(bot_shape)

        extended_chorale = np.concatenate([top, extended_chorale, bottom], axis=1)

        # Pad horizontally
        padding_dimensions = (timesteps,) + extended_chorale.shape[1:] 
        padding_start = np.zeros(padding_dimensions)
        padding_end = np.zeros(padding_dimensions)
        #padding_start[:, :p_t] = 1
        #padding_end[:, -p_b:] = 1

        extended_chorale = np.concatenate([padding_start, extended_chorale,padding_end], axis=0)

        if (dimension):
            return extended_chorale, p_t, p_b
        else:
            return extended_chorale

