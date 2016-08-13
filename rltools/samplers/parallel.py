import logging
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import tensorflow as tf
import zerorpc
from gevent import Timeout
from zerorpc.gevent_zmq import logger as gevent_log

from rltools.samplers import Sampler, decrollout, centrollout
from rltools.trajutil import TrajBatch, Trajectory
from six.moves import cPickle

gevent_log.setLevel(logging.CRITICAL)


class ThreadedSampler(Sampler):

    def __init__(self, algo, n_timesteps, max_traj_len, timestep_rate, n_timesteps_min,
                 n_timesteps_max, adaptive=False, enable_rewnorm=True, n_workers=4):
        super(ThreadedSampler, self).__init__(algo, n_timesteps, max_traj_len, timestep_rate,
                                              n_timesteps_min, n_timesteps_max, adaptive,
                                              enable_rewnorm)

        self.n_workers = n_workers

    def sample(self, sess, itr):
        if self.adaptive and itr > 0 and self.n_timesteps < self.n_timesteps_max:
            if itr % self.timestep_rate == 0:
                self.n_timesteps *= 2

        r_func = lambda mtl: centrollout(self.algo.env, self.algo.obsfeat_fn, lambda ofeat: self.algo.policy.sample_actions(sess, ofeat), mtl, self.algo.policy.action_space)

        with ThreadPoolExecutor(self.n_workers) as self.executor:
            trajs = self.executor.map(r_func, [self.max_traj_len] * int(self.n_timesteps /
                                                                        self.max_traj_len))  # XXX

        if not isinstance(trajs, list):
            trajs = list(trajs)

        trajbatch = TrajBatch.FromTrajs(trajs)
        return (trajbatch,
                [('ret', trajbatch.r.padded(fill=0.).sum(axis=1).mean(),
                  float),  # average return for batch of traj
                 ('avglen', int(np.mean([len(traj) for traj in trajbatch])),
                  int),  # average traj length
                 ('maxlen', int(np.max([len(traj) for traj in trajbatch])), int),  # max traj length
                 ('minlen', int(np.min([len(traj) for traj in trajbatch])), int),  # min traj length
                 ('ravg', trajbatch.r.stacked.mean(),
                  int)  # avg reward encountered per time step (probably not that useful)
                ])


class ParallelSampler(Sampler):

    def __init__(self, algo, n_timesteps, max_traj_len, timestep_rate, n_timesteps_min,
                 n_timesteps_max, adaptive=False, enable_rewnorm=True, n_workers=4,
                 mode='centralized', discard_extra=False):
        super(ParallelSampler, self).__init__(algo, n_timesteps, max_traj_len, timestep_rate,
                                              n_timesteps_min, n_timesteps_max, adaptive,
                                              enable_rewnorm)
        self.n_workers = n_workers
        self.mode = mode
        self.discard_extra = discard_extra
        self.proxies = [
            RolloutProxy(self.algo.env, self.algo.policy, max_traj_len, self.mode, i)
            for i in range(self.n_workers)
        ]
        self.seed_idx = 0
        self.seed_idx2 = 0

    def sample(self, sess, itr):
        if self.adaptive and itr > 0 and self.n_timesteps < self.n_timesteps_max:
            if itr % self.timestep_rate == 0:
                self.n_timesteps *= 2

        state_str = _dumps(self.algo.policy.get_state(sess))
        get_values([proxies.client("set_state", state_str, async=True) for proxies in self.proxies])

        self.seed_idx2 = self.seed_idx
        timesteps_sofar = 0
        seed2traj = {}
        worker2job = {}

        def assign_job_to(i_worker):
            worker2job[i_worker] = (self.seed_idx2, self.proxies[i_worker].client("sample",
                                                                                  self.seed_idx2,
                                                                                  async=True))
            self.seed_idx2 += 1

        # Start jobs
        for i_worker in range(self.n_workers):
            assign_job_to(i_worker)

        while True:
            for i_worker in range(self.n_workers):
                try:
                    (seed_idx, future) = worker2job[i_worker]
                    traj_string = future.get(timeout=1e-3)  # XXX
                except Timeout:
                    pass
                else:
                    traj = _loads(traj_string)
                    seed2traj[seed_idx] = traj
                    if self.mode == 'centralized':
                        timesteps_sofar += len(traj)
                    elif self.mode == 'decentralized':
                        assert isinstance(traj, list)
                        timesteps_sofar += np.sum(map(len, traj))
                    else:
                        raise NotImplementedError()
                    if timesteps_sofar >= self.n_timesteps:
                        break
                    else:
                        assign_job_to(i_worker)
            if timesteps_sofar >= self.n_timesteps:
                break
            time.sleep(0.01)

        # Wait until all jobs finish
        for seed_idx, future in worker2job.values():
            seed2traj[seed_idx] = _loads(future.get())

        trajs = []
        for (seed, traj) in seed2traj.items():
            if self.mode == 'centralized':
                trajs.append(traj)
                timesteps_sofar += len(traj)
            elif self.mode == 'decentralized':
                trajs.extend(traj)
                timesteps_sofar += np.sum(map(len, traj))

            self.seed_idx += 1
            if self.discard_extra and timesteps_sofar >= self.n_timesteps:
                break

        trajbatch = TrajBatch.FromTrajs(trajs)
        self.n_episodes += len(trajbatch)
        return (trajbatch,
                [('ret', trajbatch.r.padded(fill=0.).sum(axis=1).mean(),
                  float),  # average return for batch of traj
                 ('batch', len(trajbatch), int),  # batch size
                 ('n_episodes', self.n_episodes, int), # total number of episodes
                 ('avglen', int(np.mean([len(traj) for traj in trajbatch])),
                  int),  # average traj length
                 ('maxlen', int(np.max([len(traj) for traj in trajbatch])), int),  # max traj length
                 ('minlen', int(np.min([len(traj) for traj in trajbatch])), int),  # min traj length
                 ('ravg', trajbatch.r.stacked.mean(),
                  int)  # avg reward encountered per time step (probably not that useful)
                ])


class RolloutProxy(object):

    def __init__(self, env, policy, max_traj_len, mode, idx):
        self.f = tempfile.NamedTemporaryFile()
        args = (env, policy, max_traj_len, mode)
        self.f.write(_dumps(args))
        self.f.flush()

        pid = os.getpid()
        addr = "ipc:///tmp/{}_{}.ipc".format(pid, idx)
        oenv = os.environ.copy()
        oenv["CUDA_VISIBLE_DEVICES"] = ""
        oenv["OMP_NUM_THREADS"] = "1"
        oenv["MKL_NUM_THREADS"] = "1"

        self.popen = subprocess.Popen(
            ["python2", "-m", "rltools.samplers.parallel", self.f.name, addr], env=oenv)
        if sys.platform in ["linux2", "linux"]:
            subprocess.check_call(["taskset", "-cp", str(idx), str(self.popen.pid)])

        self.client = zerorpc.Client(heartbeat=60, timeout=1000)
        self.client.connect(addr)

    def __del__(self):
        self.popen.terminate()
        self.f.close()


class RolloutServer(object):

    def __init__(self, sess, env, policy, max_traj_len, action_space, mode='centralized'):
        self.sess = sess
        self.env = env
        self.obsfeat_fn = lambda obs: obs
        self.policy = policy
        self.max_traj_len = max_traj_len
        self.action_space = action_space
        self.mode = mode
        if self.mode == 'centralized':
            self.rollout_fn = centrollout
        elif self.mode == 'decentralized':
            self.rollout_fn = decrollout

    def sample(self, seed):
        self.env.seed(seed)
        np.random.seed(seed)
        tf.set_random_seed(seed)
        random.seed(seed)
        traj = self.rollout_fn(self.env, self.obsfeat_fn,
                               lambda ofeat: self.policy.sample_actions(self.sess, ofeat),
                               self.max_traj_len, self.action_space)

        return _dumps(traj)

    def set_state(self, state_str):
        self.policy.set_state(self.sess, _loads(state_str))


def _start_server():
    fname = sys.argv[1]
    addr = sys.argv[2]
    with open(fname, 'r') as fh:
        s = fh.read()

    tfconfig = tf.ConfigProto(inter_op_parallelism_threads=1, intra_op_parallelism_threads=1)

    with tf.Session(config=tfconfig) as sess:
        env, policy, max_traj_len, mode = _loads(s)
        sess.run(tf.initialize_all_variables())
        server = zerorpc.Server(
            RolloutServer(sess, env, policy, max_traj_len, policy.action_space, mode), heartbeat=60)
        server.bind(addr)
        server.run()


def _loads(s):
    return cPickle.loads(s)


def _dumps(o):
    return cPickle.dumps(o, protocol=-1)


def get_values(li):
    return [maybe_unpickle(el.get()) for el in li]


def maybe_unpickle(x):
    if isinstance(x, str) and x[0] == '\x80':
        return cPickle.loads(x)
    else:
        return x


if __name__ == "__main__":
    _start_server()
